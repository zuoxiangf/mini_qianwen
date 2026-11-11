# backend/tools.py
# 本模块职责：Function Calling 工具层——向大模型声明工具 schema，并提供具体实现。
# 目前内置三个工具：
#   1. get_weather         查天气（Open-Meteo 免费接口，无需 API Key）
#   2. get_user_info       查用户资料（MySQL users 表，只回非敏感字段）
#   3. search_chat_history 检索当前用户的历史对话（MySQL messages 表关键词搜索）
# 约定：execute_tool 永远返回 JSON 字符串（失败也包成 {"error": ...}），
# 绝不向外抛异常——工具报错应回喂给模型让它向用户解释，而不是掐断 SSE 流。
import json
import logging

import httpx

from db import get_user_profile, search_messages

log = logging.getLogger(__name__)

# ---------- 外部天气接口（Open-Meteo：免费、免 Key、支持中文城市名）----------
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
HTTP_TIMEOUT = 15  # 秒

# WMO 天气代码 → 中文描述（Open-Meteo 返回 code，需自行翻译）
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
            "description": "查询指定城市当前天气和未来几天预报。用户问天气、温度、是否下雨时使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "城市名，中文或英文均可，如：北京、上海、Tokyo"},
                    "days": {"type": "integer", "description": "预报天数 1~7，默认 3；只问当前天气可传 1"},
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
]


# ---------- 各工具实现 ----------

async def _get_weather(city: str, days: int = 3) -> str:
    """Open-Meteo 两步查询：城市名 → 坐标 → 当前天气 + 逐日预报。"""
    days = max(1, min(int(days or 3), 7))
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as cx:
        # 1) 地理编码：把城市名解析成坐标（language=zh 优先中文城市库）
        geo = await cx.get(GEOCODE_URL, params={"name": city, "count": 1, "language": "zh", "format": "json"})
        geo.raise_for_status()
        results = (geo.json() or {}).get("results") or []
        if not results:
            return json.dumps({"error": f"找不到城市「{city}」，请换标准城市名"}, ensure_ascii=False)
        loc = results[0]
        # 2) 天气预报：当前实况 + 逐日最高/最低温、降水概率
        fc = await cx.get(FORECAST_URL, params={
            "latitude": loc["latitude"], "longitude": loc["longitude"],
            "current": "temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            "timezone": "auto", "forecast_days": days,
        })
        fc.raise_for_status()
        data = fc.json()
    cur, daily = data.get("current", {}), data.get("daily", {})
    forecast = [
        {
            "date": d,
            "天气": WMO_CODES.get(code, f"未知(code={code})"),
            "最高温": hi, "最低温": lo,
            "降水概率%": rain,
        }
        for d, code, hi, lo, rain in zip(
            daily.get("time", []), daily.get("weather_code", []),
            daily.get("temperature_2m_max", []), daily.get("temperature_2m_min", []),
            daily.get("precipitation_probability_max", []),
        )
    ]
    return json.dumps({
        "城市": f"{loc.get('name', city)}（{loc.get('admin1', '')}{loc.get('country', '')}）",
        "当前": {
            "温度℃": cur.get("temperature_2m"),
            "体感℃": cur.get("apparent_temperature"),
            "湿度%": cur.get("relative_humidity_2m"),
            "风速km/h": cur.get("wind_speed_10m"),
            "天气": WMO_CODES.get(cur.get("weather_code"), "未知"),
        },
        "预报": forecast,
    }, ensure_ascii=False)


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
        log.warning("模型请求了未注册的工具 | name=%s", name)
        return json.dumps({"error": f"未知工具：{name}"}, ensure_ascii=False)
    except httpx.HTTPError as e:
        log.warning("工具调用外部接口失败 | name=%s | %s", name, e)
        return json.dumps({"error": f"外部服务访问失败：{e}"}, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001  工具失败回喂给模型，不掐断用户会话
        log.exception("工具执行异常 | name=%s", name)
        return json.dumps({"error": f"工具执行失败：{e}"}, ensure_ascii=False)
