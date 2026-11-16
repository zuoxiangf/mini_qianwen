# backend/tools.py
# 本模块职责：Function Calling 工具层——向大模型声明工具 schema，并提供具体实现。
# 目前内置四个工具：
#   1. get_weather         查天气（和风天气每日预报，需 QWEATHER 凭据，内部先调 GeoAPI 定位坐标）
#   2. get_user_info       查用户资料（MySQL users 表，只回非敏感字段）
#   3. search_chat_history 检索当前用户的历史对话（MySQL messages 表关键词搜索）
#   4. city_lookup         城市搜索（和风天气 GeoAPI，与 get_weather 共用凭据）
# 约定：execute_tool 永远返回 JSON 字符串（失败也包成 {"error": ...}），
# 绝不向外抛异常——工具报错应回喂给模型让它向用户解释，而不是掐断 SSE 流。
import json
import logging
import os
import re
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

from db import get_user_profile, search_messages

load_dotenv(Path(__file__).resolve().parent / ".env")   # ★ 读取本目录 .env，与运行目录/导入顺序无关（同 db.py 约定）

log = logging.getLogger(__name__)

# ---------- 和风天气（get_weather / city_lookup 共用：API Host + JWT / API Key 二选一认证）----------
# 认证方式二选一，绝不同时附加两个请求头（和风文档警告多认证共存会导致认证失败）：
#   优先 X-QW-Api-Key（本项目已实测可用）；API Key 未配置但配了 QWEATHER_JWT 时改用 Authorization: Bearer
# 另外：和风 API 均返回 gzip 压缩内容、参数含中文需 URL 编码，httpx 会自动解压/编码，无需手动处理
QWEATHER_API_KEY = os.getenv("QWEATHER_API_KEY", "")
QWEATHER_JWT = os.getenv("QWEATHER_JWT", "")             # 可选：控制台生成的 JWT，留空则用 API Key 方式
QWEATHER_GEO_HOST = os.getenv("QWEATHER_GEO_HOST", "")   # 仅域名，不带 https:// 前缀
HTTP_TIMEOUT = 15  # 秒

# ---------- 临时：固定城市坐标（跳过 GeoAPI 城市搜索，绕过 GeoAPI 403）----------
# 设为 (纬度, 经度) 则 _get_weather 直接用此坐标，不再调 _geo_lookup；恢复 GeoAPI 时改回 None
# FIXED_LATLON: tuple[float, float] | None = (31.23, 121.47)  # 上海
FIXED_LATLON: tuple[float, float] | None = None  # None=已恢复正常查询（走坐标输入 / GeoAPI 定位）
FIXED_CITY_NAME = "上海"  # 配合上面坐标显示的城市名

# "经度,纬度"坐标输入（和风文档顺序：lon 在前，如 116.41,39.92）；命中则 get_weather 跳过城市搜索
LATLON_RE = re.compile(r"^(-?\d{1,3}(?:\.\d+)?),\s*(-?\d{1,2}(?:\.\d+)?)$")


def _qweather_url(path: str) -> str:
    return f"https://{QWEATHER_GEO_HOST}{path}"


def _qweather_headers() -> dict:
    """认证头二选一：优先 API Key；仅配了 JWT 时用 Bearer。两者都不会同时出现。"""
    if QWEATHER_API_KEY:
        return {"X-QW-Api-Key": QWEATHER_API_KEY}
    return {"Authorization": f"Bearer {QWEATHER_JWT}"}


def _qweather_ready() -> bool:
    """凭据是否可用：Host 必填，JWT / API Key 至少有一个。"""
    return bool(QWEATHER_GEO_HOST) and bool(QWEATHER_API_KEY or QWEATHER_JWT)


async def _qx_get(cx: httpx.AsyncClient, path: str, params: dict) -> httpx.Response:
    """和风 GET 请求统一包装：请求前记录接口+参数，请求后记录状态码+耗时+响应体，便于定位联调问题。

    安全：只记路径与查询参数，绝不打印认证请求头（防 Key 泄露进日志）。
    非 2xx 时额外记录完整 URL 和响应 body（和风错误信息在 body 中），再由 raise_for_status 上抛；
    网络层异常额外记 FAILED 后重抛，最终由调用方/execute_tool 兜底转 error JSON。
    """
    t0 = time.monotonic()
    url = _qweather_url(path)
    log.info("和风请求开始 | %s | 参数=%s | 完整URL=%s", path, params, url)
    try:
        resp = await cx.get(url, params=params, headers=_qweather_headers())
    except httpx.HTTPError as e:
        log.warning("和风请求失败 | %s | 耗时=%dms | %s", path, (time.monotonic() - t0) * 1000, e)
        raise
    elapsed = int((time.monotonic() - t0) * 1000)
    # 非 2xx 时记录响应体（和风 403 等错误信息在 body 中），便于排查订阅/权限问题
    if resp.status_code >= 400:
        body_text = resp.text[:2000]  # 截断防止超长日志
        log.warning("和风请求异常 | %s | HTTP %s | 耗时=%dms | 实际请求URL=%s | 响应体=%s",
                    path, resp.status_code, elapsed, resp.request.url, body_text)
    else:
        log.info("和风请求结束 | %s | HTTP %s | 耗时=%dms", path, resp.status_code, elapsed)
    resp.raise_for_status()
    return resp


def _http_error_hint(e: httpx.HTTPStatusError) -> str:
    """把和风常见 HTTP 错误码翻译成可操作的中文提示，便于模型向用户解释、运维快速定位。"""
    code = e.response.status_code
    hints = {
        401: "和风天气身份认证失败：凭据无效或认证方式冲突（JWT 与 API Key 只能二选一，优先检查是否同时配置了两者）",
        403: "和风天气拒绝访问：凭据有效但未订阅该 API 产品，请在控制台「项目管理」中订阅对应服务（如 城市搜索 GeoAPI）",
        402: "和风天气余额/配额问题：请检查开发者账户订阅计划与用量",
        429: "和风天气请求过频：请稍后重试",
    }
    return f"{hints.get(code, '外部服务访问失败')}（HTTP {code}）"


# ---------- 遗留：Open-Meteo 免 Key 接口（切和风后暂不删除，如需回退可恢复旧 _get_weather 实现）----------
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# 遗留：WMO 天气代码 → 中文描述表（Open-Meteo 时代使用；和风 condition.text 直接返回中文描述）
WMO_CODES = {
    0: "晴", 1: "基本晴", 2: "局部多云", 3: "阴",
    45: "雾", 48: "雾凇",
    51: "毛毛雨(弱)", 53: "毛毛雨(中)", 55: "毛毛雨(强)",
    56: "冻毛毛雨", 57: "强冻毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨",
    66: "冻雨(弱)", 67: "冻雨(强)",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
    80: "阵雨(弱)", 81: "阵雨(中)", 82: "阵雨(强)",
    85: "阵雪(弱)", 86: "阵雪(强)",
    95: "雷暴", 96: "雷暴伴小冰雹", 99: "雷暴伴大冰雹",
}

# 面向模型的工具声明（OpenAI chat completions tools 格式）
TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查询指定城市的实时天气（当前温度/体感/湿度/气压）与未来几天每日天气预报（白天/晚间天气现象、最高最低温、降水概率、紫外线等）。用户问天气、温度、是否下雨时使用。重名城市先用 city_lookup 确认位置。",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "城市名，中文或英文均可，如：北京、上海、Tokyo；也接受“经度,纬度”坐标（如 116.41,39.92）"},
                    "days": {"type": "integer", "description": "预报天数 1~10，默认 3"},
                },
                "required": ["city"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_user_info",
            "description": "查询用户账号资料（uid、用户名、注册时间）。不传 username 时查询当前登录用户自己。",
            "parameters": {
                "type": "object",
                "properties": {
                    "username": {"type": "string", "description": "要查询的用户名；留空=查当前登录用户"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_chat_history",
            "description": "按关键词检索当前用户过往的对话记录（跨会话）。用户问“我之前说过/问过什么”时使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "搜索关键词"},
                    "days": {"type": "integer", "description": "往前检索的天数 1~365，默认 30"},
                    "limit": {"type": "integer", "description": "最多返回条数 1~20，默认 10"},
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "city_lookup",
            "description": "根据地名、LocationID 或“经度,纬度”坐标搜索城市/地点，返回 LocationID、经纬度、时区、上级行政区划等信息。用户询问地点位置，或需要先定位城市再查天气时使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {"type": "string", "description": "要查询的地区名称、LocationID 或“经度,纬度”坐标，如：北京、101010100、116.41,39.92"},
                    "adm": {"type": "string", "description": "上级行政区划（如 beijing），用于排除重名城市；可选"},
                    "range": {"type": "string", "description": "国家/地区 ISO 3166 代码（如 cn），限定搜索范围；不设置则搜索所有城市；可选"},
                    "number": {"type": "integer", "description": "返回结果数量 1~20，默认 10；可选"},
                    "lang": {"type": "string", "description": "返回语言（如 zh、en）；可选"},
                },
                "required": ["location"],
            },
        },
    },
]


# ---------- 各工具实现 ----------

async def _geo_lookup(cx: httpx.AsyncClient, location: str, number: int = 1,
                      lang: str = "zh", **extra) -> list[dict]:
    """调和风 GeoAPI 按地名/LocationID/“经度,纬度”搜索城市，返回 location 数组。

    非 200 或无结果统一返回空列表，由调用方决定如何向模型解释；HTTP 层异常上抛给 execute_tool 兜底。
    """
    params = {"location": location, "number": number, "lang": lang}
    params.update({k: v for k, v in extra.items() if v})   # 可选参数（adm/range 等）为空则不传
    resp = await _qx_get(cx, "/geo/v2/city/lookup", params)
    data = resp.json()
    if str(data.get("code")) != "200":
        log.warning("和风 GeoAPI 返回异常 | code=%s | location=%s", data.get("code"), location)
        return []
    cities = data.get("location") or []
    log.info("城市搜索完成 | location=%s | 命中=%d", location, len(cities))
    return cities


def _fmt_current(now: dict) -> dict:
    """把 /weather/v1/current 的实时天气抽成模型友好的中文字典（字段结构已实测确认）。"""
    wind = now.get("wind") or {}
    return {
        "天气": (now.get("condition") or {}).get("text", "未知"),
        "温度℃": (now.get("temperature") or {}).get("value"),
        "体感℃": (now.get("feelsLike") or {}).get("value"),
        "湿度%": round((now.get("humidity") or 0) * 100),
        "风": f"{(wind.get('direction') or {}).get('compass', '?')} {(wind.get('speed') or {}).get('value', '?')}m/s({wind.get('scale', '?')}级)",
        "气压hPa": (now.get("pressure") or {}).get("value"),
        "能见度km": round(((now.get("visibility") or {}).get("value") or 0) / 1000, 1),
        "紫外线指数": now.get("uvIndex"),
        "发布时间": now.get("validTime"),
    }


async def _get_weather(city: str, days: int = 3) -> str:
    """和风天气查询：先定位城市（GeoAPI 拿经纬度）→ 再查实时天气 + 每日预报（1~10 天）。

    优化点：
    - 入参直接给“经度,纬度”时跳过城市搜索，省一次 GeoAPI 调用（免耗免费额度）；
    - 城市搜索取 Top3，存在重名/相近地点时随结果回喂候选，让模型引导用户消歧，
      避免“查朝阳区天气”默默命中错误的城市；
    - 额外并行请求 /weather/v1/current 返回实时天气；该请求失败不影响预报主链路（容错降级）；
    - 接口要求坐标最多两位小数（约 1km 分辨率），四舍五入即可；
    - 和风 condition.text 直接返回中文描述（lang=zh），无需再查 WMO 码表。
    """
    if not city or not city.strip():
        return json.dumps({"error": "城市名不能为空"}, ensure_ascii=False)
    if not _qweather_ready():
        return json.dumps({"error": "未配置和风天气凭据（QWEATHER_GEO_HOST 必填，QWEATHER_API_KEY / QWEATHER_JWT 至少其一）"}, ensure_ascii=False)
    days = max(1, min(int(days or 3), 10))

    # --- 阶段 1：确定经纬度 ---
    # ★ 临时：FIXED_LATLON 不为 None 时直接用固定坐标，跳过 GeoAPI 城市搜索
    if FIXED_LATLON:
        lat, lon = FIXED_LATLON
        loc = {"name": FIXED_CITY_NAME, "adm2": "", "adm1": ""}
        others = []
        log.info("天气查询使用固定坐标 | %s (%s, %s)", FIXED_CITY_NAME, lat, lon)
    else:
        q = city.strip()
        m = LATLON_RE.match(q)
        if m:
            lon_f, lat_f = float(m.group(1)), float(m.group(2))
            if not (-90 <= lat_f <= 90 and -180 <= lon_f <= 180):
                return json.dumps({"error": f"经纬度超出合法范围：{q}"}, ensure_ascii=False)
            loc = {"name": f"坐标({lat_f},{lon_f})", "adm2": "", "adm1": ""}
            lat, lon = round(lat_f, 2), round(lon_f, 2)
            others = []
        else:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as cx:
                locs = await _geo_lookup(cx, q, number=3)
            if not locs:
                return json.dumps({"error": f"找不到城市「{city}」，请换标准城市名"}, ensure_ascii=False)
            loc = locs[0]
            lat, lon = round(float(loc["lat"]), 2), round(float(loc["lon"]), 2)
            others = [f"{c.get('name')}（{c.get('adm2', '')}{c.get('adm1', '')}）" for c in locs[1:]]

    # --- 阶段 2：用经纬度查天气（预报 + 实时） ---
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as cx:
        fc = await _qx_get(cx, f"/weather/v1/daily/{lat}/{lon}",
                           {"days": days, "lang": "zh", "localTime": "true"})
        data = fc.json()
        now = None
        try:
            cc = await _qx_get(cx, f"/weather/v1/current/{lat}/{lon}",
                               {"lang": "zh", "localTime": "true"})
            now = _fmt_current(cc.json())
        except Exception as e:  # noqa: BLE001  降级路径：实时接口失败不中断预报
            log.info("实时天气获取失败（不影响预报） | %s", e)

    def _span(part: dict) -> dict:
        """抽取白天/晚间段位关键字段：天气、温度区间、降水概率、风、湿度；缺结构返回空字典。"""
        if not part:
            return {}
        wind = part.get("wind") or {}
        speed = (wind.get("speed") or {}).get("value")
        prec = part.get("precipitation") or {}
        return {
            "天气": (part.get("condition") or {}).get("text", "未知"),
            "温度℃": f"{(part.get('temperatureMin') or {}).get('value', '?')}~{(part.get('temperatureMax') or {}).get('value', '?')}",
            "风": f"{wind.get('direction', {}).get('compass', '?')} {speed}m/s({wind.get('scale', '?')}级)" if speed is not None else "",
            "降水概率%": round((prec.get("probability") or 0) * 100),
            "湿度%": round((part.get("humidity") or 0) * 100),
        }

    days_list = data.get("days") or []
    forecast = [
        {
            "日期": (d.get("forecastStartTime") or "")[:10],
            "最高温℃": (d.get("temperatureMax") or {}).get("value"),
            "最低温℃": (d.get("temperatureMin") or {}).get("value"),
            "紫外线指数": d.get("uvIndexMax"),
            "白天": _span(d.get("daytime")),
            "晚间": _span(d.get("nighttime")),
        }
        for d in days_list
    ]
    astro = (days_list[0].get("astro") or {}) if days_list else {}
    region = f"{loc.get('adm2', '')}{loc.get('adm1', '')}"
    result = {
        "城市": f"{loc.get('name', city)}（{region}）" if region else loc.get("name", city),
        "今日日出": astro.get("sunrise"), "今日日落": astro.get("sunset"),
    }
    if now:
        result["实时"] = now
    result["预报"] = forecast
    if others:
        result["提示"] = "存在重名/相近地点：" + "、".join(others) + "；若不符合用户意图，请引导用户补充上级行政区后用 city_lookup 精确查询"
    log.info("天气查询完成 | 入参=%s | 定位=%s(%s,%s) | 实时=%s | 预报天数=%d | 重名候选=%d",
             city, loc.get("name"), lat, lon, "有" if now else "无", len(forecast), len(others))
    return json.dumps(result, ensure_ascii=False)


async def _get_user_info(ctx: dict, username: str | None = None) -> str:
    """查用户资料：留空查自己；查他人时同样只回非敏感三字段（资料本就无更多可回）。"""
    if username and username.strip() and username.strip() != ctx.get("username"):
        row = await get_user_profile(username=username.strip())
    else:
        row = await get_user_profile(uid=ctx["uid"])
    if not row:
        return json.dumps({"error": f"用户「{username}」不存在"}, ensure_ascii=False)
    return json.dumps(row, ensure_ascii=False, default=str)


async def _search_history(ctx: dict, keyword: str, days: int = 30, limit: int = 10) -> str:
    """在当前用户的全部会话消息里按关键词检索（严格限定 uid，绝不可搜到他人对话）。"""
    if not keyword or not keyword.strip():
        return json.dumps({"error": "搜索关键词不能为空"}, ensure_ascii=False)
    days = max(1, min(int(days or 30), 365))
    limit = max(1, min(int(limit or 10), 20))
    rows = await search_messages(ctx["uid"], keyword.strip(), days, limit)
    if not rows:
        return json.dumps({"命中": 0, "说明": f"最近 {days} 天内没有找到包含「{keyword}」的对话"}, ensure_ascii=False)
    return json.dumps({"命中": len(rows), "记录": rows}, ensure_ascii=False, default=str)


async def _city_lookup(location: str, adm: str | None = None, region: str | None = None,
                       number: int = 10, lang: str | None = None) -> str:
    """和风 GeoAPI 城市搜索：返回 LocationID、经纬度、时区、区划等；无结果/异常均转 JSON 不抛出。

    注意 region 参数对外的查询 key 仍叫 range（range 是 Python 内置名，函数内避免遮蔽）。
    """
    if not location or not location.strip():
        return json.dumps({"error": "查询地点（名称/LocationID/经度,纬度）不能为空"}, ensure_ascii=False)
    if not _qweather_ready():
        return json.dumps({"error": "未配置和风天气凭据（QWEATHER_GEO_HOST 必填，QWEATHER_API_KEY / QWEATHER_JWT 至少其一）"}, ensure_ascii=False)
    number = max(1, min(int(number or 10), 20))
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as cx:
        cities = await _geo_lookup(cx, location.strip(), number=number,
                                   lang=lang or "zh", adm=adm, range=region)
    if not cities:
        return json.dumps({"命中": 0, "说明": f"未找到与「{location}」匹配的地点"}, ensure_ascii=False)
    items = [
        {
            "名称": c.get("name"), "LocationID": c.get("id"),
            "纬度": c.get("lat"), "经度": c.get("lon"),
            "所属市": c.get("adm2"), "所属省": c.get("adm1"), "国家": c.get("country"),
            "时区": c.get("tz"), "UTC偏移": c.get("utcOffset"),
            "夏令时": "是" if str(c.get("isDst")) == "1" else "否",
            "Rank": c.get("rank"), "天气网页": c.get("fxLink"),
        }
        for c in cities
    ]
    return json.dumps({"命中": len(items), "地点": items}, ensure_ascii=False)


# ---------- 统一分发入口 ----------

async def execute_tool(name: str, arguments: str | None, ctx: dict) -> str:
    """按模型给出的工具名分发执行。arguments 是模型生成的 JSON 字符串（可能非法）。

    ctx 为调用上下文：{"uid": 当前用户id, "username": 当前用户名, "sid": 会话id}，
    由后端注入而非模型提供，防止越权指定别人的 uid。
    任何异常都在此处兜底转成 error JSON，保证 SSE 流不中断。
    """
    try:
        args = json.loads(arguments) if arguments else {}
        if not isinstance(args, dict):
            args = {}
    except json.JSONDecodeError:
        return json.dumps({"error": "工具参数不是合法 JSON，请重新组织调用"}, ensure_ascii=False)
    try:
        if name == "get_weather":
            return await _get_weather(args.get("city", ""), args.get("days", 3))
        if name == "get_user_info":
            return await _get_user_info(ctx, args.get("username"))
        if name == "search_chat_history":
            return await _search_history(ctx, args.get("keyword", ""), args.get("days", 30), args.get("limit", 10))
        if name == "city_lookup":
            return await _city_lookup(args.get("location", ""), args.get("adm"),
                                      args.get("range"), args.get("number", 10), args.get("lang"))
        log.warning("模型请求了未注册的工具 | name=%s", name)
        return json.dumps({"error": f"未知工具：{name}"}, ensure_ascii=False)
    except httpx.HTTPStatusError as e:
        log.warning("工具调用外部接口返回错误状态 | name=%s | %s", name, e)
        return json.dumps({"error": f"{_http_error_hint(e)} | 接口={e.request.url.path}"}, ensure_ascii=False)
    except httpx.HTTPError as e:
        log.warning("工具调用外部接口失败 | name=%s | %s", name, e)
        return json.dumps({"error": f"外部服务访问失败：{e}"}, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001  工具失败回喂给模型，不掐断用户会话
        log.exception("工具执行异常 | name=%s", name)
        return json.dumps({"error": f"工具执行失败：{e}"}, ensure_ascii=False)
