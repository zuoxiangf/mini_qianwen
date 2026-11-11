# Mini Qwen —— 带鉴权与 RAG 知识库的极简千问聊天应用

> 本文档基于**当前实际代码**（`backend/` 七个模块 + `frontend/` + `docker-compose.yml` + `deploy/`）整理，
> 说明项目现状、架构设计、配置项与生产部署流程。

---

## 1. 项目简介

Mini Qwen 是一个"前端聊天页面 + FastAPI 后端 + 云端大模型"的 AI 应用。相比最初的纯 Demo 版本，
当前已经完成的能力有：

1. **JWT 用户鉴权**——注册/登录颁发 Bearer Token，所有业务接口受保护，会话按用户隔离；
2. **会话历史持久化 MySQL**——重启不丢、多实例可共享，AI 回复流结束后完整回写（多轮记忆闭环）；
3. **单轮问答接入 RAG（混合检索）**——问答前先做**向量(Qdrant) + 关键字(BM25/jieba) 混合检索**并 RRF 融合，
   命中资料注入系统提示词，来源透出到前端；
4. **知识库多格式入库**——支持 TXT / PDF / DOCX / HTML 文件上传与网页 URL 抓取，一条流水线：
   抽取纯文本 → 编码嗅探 → 切块 → DashScope 向量化 → 幂等写入 Qdrant；
5. **闲聊工具调用（Function Calling）**——`/api/chat` 升级为 Agent 循环，模型按需调用
   内置工具（**查天气 / 查用户资料 / 检索历史对话**），后端解析 `tool_calls`、执行、回喂结果后继续生成；
6. **生产部署体系**——Docker Compose（MySQL + Qdrant + 后端）+ 宿主机 Nginx（静态站点 + API 反代 + SSE 支持）。

### 1.1 能力总览

| 能力 | 说明 | 对应代码 |
|---|---|---|
| 注册 / 登录 | bcrypt 哈希密码，签发 HS256 JWT；登录防用户名探测 | `auth.py` + `main.py /api/auth/*` |
| 多轮闲聊 | 按 `session_id` 读写 **MySQL** 会话历史，流式返回；会话绑定归属用户 | `main.py /api/chat` + `db.py` |
| 工具调用（Function Calling） | 闲聊时模型按需调用外部工具并回喂结果，形成 Agent 循环；`ENABLE_TOOLS` 一键开关 | `main.py /api/chat` + `tools.py` |
| ├ 查天气 | Open-Meteo 免费接口（免 Key、支持中文城市名），返回实况 + 逐日预报 | `tools.py get_weather` |
| ├ 查用户 | 查 users 表资料（留空查自己），只回非敏感字段 | `tools.py get_user_info` + `db.py get_user_profile()` |
| └ 查历史对话 | 按关键词 JOIN sessions 过滤当前用户，LIKE 检索过往消息 | `tools.py search_chat_history` + `db.py search_messages()` |
| 单轮问答 + RAG | 检索命中资料注入 system 提示，`sources` 事件先于正文下发 | `main.py /api/ask` + `vector.py retrieve()/hybrid_retrieve()` |
| 混合检索 | 向量(Qdrant) + 关键字(BM25/jieba) 双路，RRF 融合排序，`RETRIEVAL_MODE` 切换 | `vector.py hybrid_retrieve()` + `bm25.py` |
| 知识库入库 | 多格式文件 → 抽取文本 → 切块 → 向量化 → 幂等写 Qdrant；空文本显式 422 拒绝 | `main.py /api/kb/upload` + `extract.py` + `vector.py add_docs()` |
| 网页入库 | 抓取 URL → 去标签抽正文 → 同一入库流水线（来源前缀 `web_<哈希>`） | `main.py /api/kb/web` + `extract.py _html_to_text()` |
| 知识库状态查询 | 集合/维度/点数 + **按来源聚合块数**，专治"到底入库没有" | `main.py /api/kb/stats` |
| 会话管理 | 分页查当前用户会话列表；删除前校验归属（越权 403） | `main.py /api/sessions`、`DELETE /api/session/{sid}` |
| 流式输出 | 模型流 → SSE 逐条推送，兼容 `reasoning_content` 思考流 | `main.py _stream_to_browser()` |
| 登录门前端 | 未登录全屏遮罩；token 存 localStorage，任意接口 401 自动登出 | `frontend/index.html` + `app.js` |

---

## 2. 整体架构

```mermaid
graph TB
    A[浏览器前端<br/>登录门 + 聊天页] -->|Nginx :8080 静态托管| N[宿主机 Nginx]
    A -->|"fetch + Authorization: Bearer"| N
    N -->|反代 /api/ 透传鉴权头| B[FastAPI 后端<br/>main.py]
    B -->|"Depends(get_current_user)"| AU[auth.py<br/>JWT / bcrypt]
    B -->|OpenAI 兼容协议 stream=True| C[云端聊天模型<br/>BASE_URL 网关]
    B -->|解析 tool_calls / 回喂结果| TL[tools.py<br/>天气·查用户·查历史]
    TL -->|Open-Meteo HTTP| WX[天气接口]
    TL -->|get_user_profile / search_messages| D
    B -->|消息 / 会话 / 用户读写| D[(MySQL<br/>messages / sessions / users)]
    B -->|文件 / URL| EX[extract.py<br/>PDF·DOCX·HTML·编码嗅探]
    B -->|retrieve / hybrid_retrieve / add_docs| E[vector.py]
    B -->|search / load_from_qdrant| BM[bm25.py<br/>jieba 分词 + BM25 内存索引]
    E -->|TextEmbedding 原生 SDK| F[DashScope Embedding]
    E -->|upsert / query / scroll| G[(Qdrant 向量库<br/>server 模式)]
    BM -->|复用同一客户端 scroll payload| G
```

数据流要点：

- **鉴权无状态**：业务接口经 `Depends(get_current_user)` 验 JWT 签名（不查库），拦截发生在
  DB / 模型调用之前；会话数据靠 `sessions.uid` 归属实现用户间隔离；
- **聊天模型**走 OpenAI 兼容协议（`AsyncOpenAI`），换服务商只需改 `.env` 里的 `BASE_URL` 和模型名；
- **Embedding 模型**走 DashScope **原生 SDK**（`dashscope.TextEmbedding.call`），不兼容 OpenAI 协议，
  单独封装在 `vector.py`，且 `DASHSCOPE_EMB_URL` 必须在代码里显式赋值（环境变量不会自动生效）；
- **混合检索**：`/api/ask` 默认并发跑向量（Qdrant）+ 关键字（`bm25.py` jieba+BM25 内存索引）两路，
  各取 `k×2` 后用 **RRF（Reciprocal Rank Fusion）** 融合取 Top-k；由 `RETRIEVAL_MODE` 切换 hybrid/vector/bm25；
  BM25 索引与 Qdrant 数据同源（启动 `load_from_qdrant` 全量重建、入库 `add_chunks` 增量），复用同一客户端避免文件锁；
- **同步阻塞调用**（DashScope、Qdrant client、bcrypt、文件解析）全部用 `asyncio.to_thread` 包裹，
  避免卡住事件循环；
- **SSE 流式**经 Nginx 反代时必须关缓冲（`proxy_buffering off`），否则流式回复会被攒包卡住。

---

## 3. 目录结构

```
mini_qianwen/
├── backend/
│   ├── main.py        # FastAPI 接口层：鉴权 / chat（含工具循环）/ ask / kb 上传·网页·状态 / 会话管理
│   ├── auth.py        # 鉴权基础设施：bcrypt 哈希、JWT 签发/校验、get_current_user 依赖
│   ├── tools.py       # Function Calling 工具层：天气 / 查用户 / 检索历史对话的 schema 与实现
│   ├── db.py          # MySQL 存储层：消息、会话（含归属 uid）、用户表读写 + 工具专用查询
│   ├── vector.py      # 向量层：Qdrant 客户端管理 + DashScope Embedding + add_docs / retrieve / hybrid_retrieve
│   ├── bm25.py        # 关键字层：jieba 分词 + 进程内 BM25 索引（复用 vector 客户端，供混合检索）
│   ├── extract.py     # 文本抽取层：PDF / DOCX / HTML / 纯文本编码嗅探
│   ├── test.py        # 聊天模型连通性自检脚本
│   ├── test_embed.py  # Embedding 模型可用性与向量维度实测脚本
│   ├── Dockerfile     # 后端镜像（python:3.12-slim + requirements）
│   ├── .env           # 本地配置与密钥（不进 git）
│   └── qdrant_data/   # Qdrant 本地嵌入模式的默认数据目录
├── frontend/
│   ├── index.html     # 登录/注册门 + 聊天页结构
│   ├── style.css      # 布局、气泡、登录门样式
│   └── app.js         # token 管理 + fetch/ReadableStream 手动解析 SSE
├── deploy/
│   ├── init.sql       # MySQL 建表脚本（users / messages / sessions），容器首启自动执行
│   └── nginx-site.conf# Nginx 站点：静态托管 + /api/ 反代 + SSE + Authorization 透传
├── docker-compose.yml # 生产编排：mysql + qdrant + backend
├── .env.prod.example  # 生产环境变量模板（复制为 .env.prod 后填写）
└── README.md
```

---

## 4. 后端模块说明

### 4.1 `main.py` —— 接口层

**配置加载（重要约定）**：模块顶部先 `load_dotenv(Path(__file__).resolve().parent / ".env")`
锚定**文件所在目录**加载配置，再读取环境变量、再 `import db / vector / auth`。顺序不能调换——
这些模块在 import 期就会读环境变量，晚加载会直接 `KeyError`。
（`db.py`、`vector.py`、`auth.py`、`extract.py` 各自也带同样的自加载兜底，与运行目录、导入顺序解耦。）

**接口一览**（除标注"公开"外均需 `Authorization: Bearer <token>`）：

| 方法 | 路径 | 功能 | 请求体 | 响应 |
|---|---|---|---|---|
| POST | `/api/auth/register` | 公开·注册（成功直接发 token，免二次登录） | `{username, password}` | 201 `{token, username}` |
| POST | `/api/auth/login` | 公开·登录 | `{username, password}` | `{token, username}` |
| GET | `/api/auth/me` | 校验 token 并回显当前用户（前端启动时调用） | 无 | `{uid, username}` |
| POST | `/api/chat` | 多轮闲聊（有状态，历史存 MySQL；支持 Function Calling 工具循环） | `{message, session_id?}` | SSE 流（工具执行前先下发 `{tool}` 事件）+ 响应头 `X-Session-Id` |
| POST | `/api/ask` | 单轮问答（RAG，无状态） | `{question, system_prompt?}` | SSE 流（正文前先下发 `{sources}` 事件） |
| POST | `/api/kb/upload` | 知识库入库（multipart，支持 txt/pdf/docx/html） | `file` | `{ok, source, chars, chunks}` |
| POST | `/api/kb/web` | 网页入库（抓取 → 抽正文 → 同一流水线） | `{url}` | `{ok, source, chars, chunks}` |
| GET | `/api/kb/stats` | 知识库状态（维度/点数/按来源聚合块数） | 无 | JSON |
| GET | `/api/sessions` | 当前用户会话列表（limit/offset 分页） | 无 | `{items, limit, offset}` |
| DELETE | `/api/session/{sid}` | 删除会话（先校验归属，越权 403） | 无 | `{ok}` |

**`/api/chat` 流程**（`ENABLE_TOOLS=1` 时带工具循环）：

1. `sid = session_id or uuid4()`；前端未带 ID 时后端惰性生成，通过 `X-Session-Id` 响应头返回
   （CORS 里 `expose_headers=["X-Session-Id"]`，否则跨域 JS 读不到该头）；
2. **先落库本轮用户消息**（`append_message`，会话首建时绑定当前用户 `uid`，已存在只刷新活跃时间）；
3. `load_history(sid, MAX_HISTORY)` 取最近 N 轮（`ORDER BY id DESC LIMIT 2N` 再翻正），裁剪在 DB 层完成；
4. 拼 `SYSTEM_CHAT + history` 调模型 `stream=True`（temperature=0.7, max_tokens=1024），
   `ENABLE_TOOLS=1` 时附带 `tools=TOOL_SCHEMAS`；
5. **工具循环**（`_chat_sse`）：逐 chunk 把 `content` 增量直接下发浏览器，同时按 `index` 拼接
   `tool_calls` 分片（`name`/`arguments` 会分散在多个 chunk）；一轮结束若存在工具调用——先下发
   `{"tool": {name, args}}` 事件供前端显示提示，`execute_tool` 执行后按 OpenAI 协议把
   `assistant(tool_calls)` 与 `tool` 结果回填 `msgs`，再发起下一轮；达 `MAX_TOOL_ROUNDS` 末轮去掉
   `tools` 强制模型文字收尾防死循环（详见 4.7）；
6. `_chat_sse(..., on_complete=...)` **流结束后把完整助手回复回写 MySQL**——多轮记忆闭环的关键。
   工具调用过程（assistant/tool 消息）仅存在于本次请求内存，**不落库**，历史不会因工具膨胀。

**`/api/ask` 流程**（RAG）：

1. 按 `RETRIEVAL_MODE` 检索 Top-4：默认 `hybrid_retrieve` 并发跑向量（Qdrant，低于 `KB_SCORE_THRESHOLD` 过滤）
   与关键字（`bm25.search`）两路，各取 k×2 再 RRF 融合取 4；也可切 `vector` / `bm25` 单路；
2. 命中则用 `_make_context()` 把资料拼成 `【资料1｜来源：xxx】...` 注入 system 提示，
   并要求"仅依据资料作答、句末标注出处"；未命中则退回 `SYSTEM_QA`（或前端自定义人设）；
3. 通过 `_stream_to_browser(pre_events=[{"sources": [...]}])` 把命中来源（`文件名#块号`）**先于正文**
   下发给前端渲染"📄 参考"条。全程不读写会话历史。

**知识库入库流水线 `_ingest_text()`**：切块（`_chunk`，500 字窗口 + 80 字重叠）→
`add_docs` 向量化写 Qdrant → `bm25.add_chunks` 增量更新关键字索引。**抽取不到文本时显式抛 422**（而非返回 `ok=True`），
杜绝扫描版/图片型 PDF 呈现为"入库成功但库里没有"的假阳性。

**异常处理**：`APIStatusError` → 透传状态码 + 错误体；`APITimeoutError`（90s）→ 504；
上传超限 → 413；解析失败按用户侧 400 / 服务端 500 分流；向量化/入库失败 → 502。
前端统一从 `detail` 字段拿真实原因，形成完整的错误传导链。

### 4.2 `auth.py` —— 鉴权基础设施

- **密码哈希**：直接用 `bcrypt` 库（规避 passlib 与 bcrypt 4.x 的兼容坑），自带随机盐；
  计算量百毫秒级，所有调用点都用 `asyncio.to_thread` 包裹避免阻塞事件循环；
- **JWT**：HS256，payload 携带 `sub`（uid）与 `username`，有效期 `JWT_EXPIRE_MINUTES`（默认 24h）。
  未配置 `JWT_SECRET` 时生成临时随机密钥并告警——本地零配置可跑，生产必须在 `.env` 写死；
- **`get_current_user` 依赖**：`HTTPBearer(auto_error=False)` 手动接管，缺失/非法/过期统一 401
  （过期文案"登录已过期"，前端据此清 token 回登录门）；
- **防探测**：登录对不存在的用户也跑一次 bcrypt 校验（假哈希），拉平响应时间；
  失败统一返回"用户名或密码错误"，不区分用户是否存在。

### 4.3 `db.py` —— MySQL 存储层

- 引擎：`SQLAlchemy` 异步 + `aiomysql` 驱动，`create_async_engine(DB_URL, pool_pre_ping=True, pool_size=5)`。
  ⚠️ requirements 中必须写 `sqlalchemy[asyncio]`（隐式依赖 greenlet，裸写曾导致容器启动崩溃 502）；
- `append_message(sid, role, content, uid)`：同一事务里插 `messages` +
  `INSERT ... ON DUPLICATE KEY UPDATE last_active = NOW()` 维护 `sessions`
  （首建绑定归属 uid，已存在**不覆写**归属，防会话劫持）；
- `load_history(sid, max_turns)`：按 `sid` 取最近 `max_turns * 2` 条，倒序取、翻正回；
- `list_sessions(uid, ...)` / `get_session_owner(sid)`：会话列表按用户过滤、删除前校验归属；
- `create_user(username, pw_hash)` / `get_user_by_username(username)`：users 表读写，
  用户名重复由 UNIQUE 约束抛 `IntegrityError`，由接口层转 409。

表 DDL 见 `deploy/init.sql`（三张表核心约束）：

- `users.username` UNIQUE，`pw_hash` 存 bcrypt 串（绝不明文）；
- `messages.content` 用 **MEDIUMTEXT**（TEXT 64KB 上限会截断长回答），`idx_sid_id` 覆盖历史查询；
- `sessions.sid` **必须为主键**（`ON DUPLICATE KEY` 依赖它），`uid` 列 + `idx_uid_active` 支撑
  "按用户列会话 + 活跃时间倒序"。已有库升级需手动执行 init.sql 尾部注释里的 `ALTER` 语句。

### 4.4 `vector.py` —— 向量层（Qdrant + DashScope Embedding）

对外暴露三个异步函数：`add_docs(docs, ids)` 入库、`retrieve(query, k, score_threshold)` 向量检索、
`hybrid_retrieve(query, k, score_threshold)` 向量+关键字混合检索（内部调 `bm25.search` 并用 `_rrf_merge` 融合）。

| 设计 | 做法 | 原因 |
|---|---|---|
| 连接形态自适应 | `_make_client()`：`QDRANT_URL` 有值 → server/云模式；否则本地嵌入 `QDRANT_PATH` | Windows 本地免服务；上生产切 Docker 不改代码 |
| 懒加载单例 | `_get_client()` 首次调用才创建客户端 | 避免 import 期就抢占本地模式的**文件锁**（同目录只允许单进程访问） |
| 稳定点 ID | `_pid()` 用 `uuid5(文件名#块号)` 映射成 UUID | Qdrant 只收 int/UUID；同一块重复上传**幂等覆盖**，不堆积 |
| 集合自创建 | `_ensure_collection()` 按 `EMBED_DIM` + COSINE 建集合，已存在时校对维度并打 error 日志 | 换 Embedding 模型必须同步改 `EMBED_DIM` **并删除旧集合重建**，否则维度不匹配写入报错 |
| 原生 SDK | `dashscope.TextEmbedding.call`，包在 `asyncio.to_thread` 里 | 该网关不提供 OpenAI 兼容的 embedding 入口；同步 SDK 不能裸调 |
| 分批调用 | `batch=10` 分批发送 | 规避单次 input 条数上限 |
| 响应结构兼容 | `_get(obj, key)` 兼容 dict / 对象两种返回形态 | 新版 SDK 返回 AttributeDict，旧写法会 KeyError |
| 检索降级 | 集合不存在时 `retrieve` 返回空列表 | 灌库前 `/api/ask` 依然可用（退回纯模型问答） |
| 得分日志 | 检索后打印每条命中的相似度分 | 据此选定 `KB_SCORE_THRESHOLD` |
| 混合融合 | `hybrid_retrieve` 双路各取 k×2，RRF(Σ 1/(c+rank)，c=60) 融合取 k | 只看排名不看分数，规避 cosine(0~1) 与 BM25(无上界) 量纲差异 |

### 4.5 `extract.py` —— 文本抽取层

`extract_text(raw, filename)` 按扩展名分流：

| 格式 | 方式 | 说明 |
|---|---|---|
| pdf | pypdf 逐页抽取 | 扫描版/图片型会抽出空文本（记 warning），由入库流水线显式 422 拒绝 |
| docx | python-docx 按段落抽取 | 旧版二进制 `.doc` 不支持，报错提示转存 |
| html/htm | BeautifulSoup 去 script/style 抽正文 | 网页入库复用此路径 |
| 其他/未知后缀 | 编码嗅探：UTF-8 → GBK → charset-normalizer 自动探测 | 尽量宽容不误伤；全部失败按 UTF-8 宽松解码 |

解析库**软导入**：缺哪个库只影响对应格式，其余格式仍可用。抽取文本超 `KB_MAX_TEXT_CHARS` 截断。

### 4.6 `bm25.py` —— 关键字检索层（jieba + BM25）

与 `vector.py` 的向量检索并联，为 `hybrid_retrieve` 提供关键字通路：

| 设计 | 做法 | 原因 |
|---|---|---|
| 数据同源 | `from vector import _get_client, COLLECTION` 复用进程内客户端单例；启动 `load_from_qdrant()` scroll payload 全量重建，入库 `add_chunks()` 增量追加 | 不新建 `QdrantClient`，本地嵌入模式避免抢占文件锁；关键字与向量始终指向同一批文本 |
| 中文分词 | `rank_bm25.BM25Okapi` + `jieba.lcut` | BM25 本身不懂中文，分词质量直接决定关键字检索效果 |
| 单进程约束 | 索引活在进程内存，须保持 uvicorn `workers=1` | 本地模式本就是单进程；多 worker 时各进程独立加载，数据一致但内存翻倍 |
| 失败自愈 | 启动/增量同步失败只记日志、不阻断请求 | 向量已入库不受影响，下次 `load_from_qdrant()` 会重建 |

对外暴露：`load_from_qdrant()` 启动加载、`add_chunks(ids, texts)` 增量、`search(query, k)` 检索
（返回 `{id, text, score}`，结构与 `vector.retrieve` 对齐，得分为 0 表示无命中直接丢弃）。

### 4.7 `tools.py` —— Function Calling 工具层

为 `/api/chat` 提供可被模型调用的外部工具。模块导出三样东西：

- **`TOOL_SCHEMAS`**：OpenAI `chat.completions.tools` 格式的工具声明（name / description / parameters JSON Schema），
  直接传给模型；模型据此判断“要不要调用、调用哪个、传什么参数”；
- **`execute_tool(name, arguments, ctx)`**：统一分发入口，根据模型给出的工具名路由到具体实现；
- 三个工具实现：

| 工具 | 作用 | 数据源 | 关键约定 |
|---|---|---|---|
| `get_weather(city, days?)` | 查当前天气 + 逐日预报 | Open-Meteo（地理编码 + 预报两个 HTTP 接口，免 Key） | 城市名先解析成坐标；WMO 天气码经 `WMO_CODES` 字典翻译成中文 |
| `get_user_info(username?)` | 查账号资料（uid/用户名/注册时间） | `db.get_user_profile()` | 留空查自己；**只回非敏感三字段，`pw_hash` 绝不出 DB 层** |
| `search_chat_history(keyword, days?, limit?)` | 跨会话检索历史对话 | `db.search_messages()` | 按关键词 LIKE；`days/limit` 夹到安全区间 |

**安全与健壮性设计**（工具体现在哪都不抛异常、不抢权限）：

- **上下文由后端注入**：`execute_tool` 的 `ctx={uid, username, sid}` 来自 JWT 校验后的当前用户，
  **不由模型提供**——模型无法伪造 uid 越权查他人数据；`search_messages` 在 SQL 层 JOIN sessions 按 uid 过滤，
  绝不全表 LIKE，查不到别人的对话；
- **失败不掉流**：`execute_tool` **永远返回 JSON 字符串**，非法参数/未知工具/外部接口异常/DB 异常全部
  兜底成 `{"error": ...}` 回喂给模型，让它向用户解释，而不是掐断 SSE 流；
- **同步不阻塞**：天气走 `httpx.AsyncClient` 异步请求；DB 查询复用 `db.py` 已有的异步引擎，无需 `to_thread`。

> ⚠️ **前提**：工具能否触发取决于 `CHAT_MODEL` 与 MaaS 网关是否支持 OpenAI `tools` 参数。若上线后
> 发现模型不调工具或报 400，将 `.env` 的 `ENABLE_TOOLS=0` 即可无缝回退到原有纯闲聊逻辑。

---

## 5. 配置说明（`backend/.env`，生产用 `.env.prod`）

| 变量 | 用途 | 备注 |
|---|---|---|
| `API_KEY` | 大模型 / Embedding 密钥 | 仅存在于后端，绝不下发前端；`.env` 不进 git |
| `BASE_URL` | 聊天模型兼容网关 | 换服务商只改这里 |
| `CHAT_MODEL` / `QA_MODEL` | 闲聊 / 问答模型名 | 需与网关提供的名称严格一致 |
| `MAX_HISTORY` | 历史窗口轮数 | 默认 10 |
| `DB_URL` | MySQL 连接串 | 格式 `mysql+aiomysql://user:pwd@host:3306/db`；compose 内主机名即服务名 `mysql` |
| `QDRANT_PATH` / `QDRANT_URL` / `QDRANT_API_KEY` | 向量库连接 | 二者选一：本地嵌入 vs server/云；生产填 `http://qdrant:6333` 并留空 PATH |
| `QDRANT_COLLECTION` | 集合名 | 默认 `kb` |
| `EMBED_MODEL` | Embedding 模型名 | DashScope 原生口径，如 `qwen3.7-text-embedding-flash` |
| `DASHSCOPE_EMB_URL` | 原生 Embedding 地址 | **必须在代码里显式赋值**，SDK 不会自动读环境变量 |
| `EMBED_DIM` | 向量维度 | 当前模型为 1024；换模型必须实测（`test_embed.py`）并重建集合 |
| `JWT_SECRET` | JWT 签名密钥 | **生产必填**（`openssl rand -hex 32`）；泄露=可伪造任意用户 |
| `JWT_EXPIRE_MINUTES` | token 有效期（分钟） | 默认 1440 = 24 小时 |
| `FRONTEND_ORIGINS` | CORS 白名单 | 逗号分隔；留空回退 `*`（仅限本地开发） |
| `KB_MAX_UPLOAD_MB` | 上传大小上限（MB） | 默认 20，超限 413 |
| `KB_MAX_TEXT_CHARS` | 抽取文本上限（字符） | 默认 200 万，超限截断 |
| `KB_SCORE_THRESHOLD` | RAG 相似度下限 | 留空不过滤；建议 0.2~0.35，结合检索得分日志调；混合模式下**仅作用于向量路** |
| `RETRIEVAL_MODE` | 检索通路开关 | `hybrid`（默认，向量+BM25 RRF 融合）/ `vector` / `bm25`；改后需重启后端生效 |
| `ENABLE_TOOLS` | 闲聊工具调用（Function Calling）开关 | 默认 `1`；置 `0` 一键回退纯闲聊（网关/模型不支持 tools 时用） |
| `MAX_TOOL_ROUNDS` | 单次对话最多允许几轮工具调用 | 默认 4；末轮去掉 tools 强制文字收尾防死循环 |

完整模板见 `.env.prod.example`（复制为 `.env.prod` 填写，**绝不进 git**）。

---

## 6. 生产部署（Docker Compose + 宿主机 Nginx）

### 6.1 编排拓扑

`docker-compose.yml` 编排三个服务；Nginx 用宿主机已有的，不在编排内：

| 服务 | 镜像 | 端口 | 说明 |
|---|---|---|---|
| mysql | mysql:8.0 | 内部 | `deploy/init.sql` 首启自动建表；数据存 volume `mysql_data` |
| qdrant | qdrant/qdrant:latest | 内部 | server 模式，数据存 volume `qdrant_data`，容器重建不丢 |
| backend | 本地构建（`backend/Dockerfile`） | **仅 127.0.0.1:8000** | 公网流量必须经宿主机 Nginx 反代进来 |

### 6.2 部署步骤

```bash
# 1. 服务器上准备环境变量
cp .env.prod.example .env.prod   # 填真实值，JWT_SECRET 必填

# 2. 起三个容器（首次会自动 build 后端镜像）
docker compose --env-file .env.prod up -d --build

# 3. Nginx：静态页 + 站点配置
sudo mkdir -p /var/www/mini_qianwen
sudo cp -r frontend/* /var/www/mini_qianwen/
sudo cp deploy/nginx-site.conf /etc/nginx/sites-available/mini_qianwen
sudo ln -s /etc/nginx/sites-available/mini_qianwen /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx

# 4. 验证：浏览器访问 http://<服务器IP>:8080，出现登录门即成功
```

要点：

- **端口 8080**：宿主机 80 已被既有项目占用，本站点独立监听 8080，注意与 `FRONTEND_ORIGINS` 一致；
- **Nginx 三件套**：`proxy_set_header Authorization $http_authorization`（JWT 透传，缺了全员 401）、
  `proxy_buffering off` + `proxy_cache off`（SSE 必需）、`proxy_read_timeout 300s`（大于后端 90s 模型超时）；
- **改了 `requirements.txt` 后**必须 `docker compose up -d --build --force-recreate backend`
  强制重建镜像，否则容器内还是旧依赖；
- **已有数据库升级**（init.sql 只对新装生效）：手动执行 init.sql 尾部注释里的
  `ALTER TABLE sessions ADD COLUMN uid ...` 等语句。

### 6.3 服务器日常操作命令速查

以下命令均在**服务器**的项目根目录（即 `docker-compose.yml` 所在目录）执行。

**① 改了后端代码（含 `requirements.txt`）后的更新流程：**

```bash
git pull                                        # 拉取本地 push 的新代码
docker compose --env-file .env.prod up -d --build --force-recreate backend
#   ▲ --build 必带：镜像不重建，容器里还是旧代码/旧依赖
#   ▲ 只动 backend 一个服务，mysql / qdrant 容器不受影响，数据不丢

docker compose logs --tail 50 backend           # 确认无 ImportError，出现“服务启动 | CHAT=...”日志
docker compose ps                               # 三个服务都应为 Up / healthy
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/docs   # 应返回 200
```

**② 改了前端代码后的更新流程（前端是静态文件，不涉及容器）：**

```bash
git pull
sudo cp -r frontend/* /var/www/mini_qianwen/    # 同步静态文件到 Nginx 目录
# 无需 reload nginx，浏览器强制刷新（Ctrl+F5）即可看到新版本
```

**③ 改了 `deploy/nginx-site.conf` 后的更新流程：**

```bash
git pull
sudo cp deploy/nginx-site.conf /etc/nginx/sites-available/mini_qianwen
sudo nginx -t                                   # ★ 先测配置，语法错不能 reload
sudo systemctl reload nginx                     # 平滑生效，不断开已有连接
```

**④ 改了 `.env.prod`（如换模型、改 JWT_SECRET）后的更新流程：**

```bash
vim .env.prod                                   # 此文件不在 git 里，直接在服务器上改
docker compose --env-file .env.prod up -d --force-recreate backend
#   ▲ 只重建容器不重建镜像（代码没变时可省 --build），环境变量会重新注入
docker compose logs --tail 20 backend           # 确认新配置生效（看“服务启动”日志行）
```

**⑤ 改了 `deploy/init.sql` 后的注意**：它对**已存在**的 mysql 数据卷不生效，
必须手动进容器执行 SQL：

```bash
docker compose exec mysql mysql -uroot -p bp_local    # 建表/ALTER 语句手动粘贴执行
```

**⑥ 日常查看状态与日志：**

```bash
docker compose ps                               # 服务状态（Restarting = 启动即崩，查 ⑦）
docker compose logs -f backend                  # 实时跟踪后端日志（Ctrl+C 退出）
docker compose logs --tail 100 mysql            # 数据库日志
```

**⑦ 故障恢复：**

```bash
# 接口 502：分层排查 —— 先看容器是否崩了，再看首行报错
docker compose ps && docker compose logs --tail 50 backend
# 修复代码后回到 ① 的流程

# 彻底重启全部服务（数据在 volume 里，不会丢）
docker compose --env-file .env.prod down
docker compose --env-file .env.prod up -d

# ⚠️ 切勿随意使用 down -v：会删除 mysql_data / qdrant_data 数据卷，
#    会话历史和知识库向量全部丢失，仅在重装且已 mysqldump 备份后才可用
```

**完整更新速记（前后端都改了）：**

```bash
git pull \
&& docker compose --env-file .env.prod up -d --build --force-recreate backend \
&& sudo cp -r frontend/* /var/www/mini_qianwen/
```

---

## 7. 本地开发启动

```powershell
cd E:\ZUO\mini_qianwen\backend

# 1. 安装依赖
pip install -r requirements.txt

# 2. 准备 MySQL：建库并执行 deploy/init.sql；确认 .env 中 DB_URL 可用
#    （Qdrant 本地嵌入模式免安装，首次入库自动创建 ./qdrant_data）

# 3. 连通性自检（先跑这两个脚本，失败则问题在 Key/网络，不在业务代码）
python test.py         # 聊天模型是否可达
python test_embed.py   # Embedding 模型是否可用、实测维度与 EMBED_DIM 是否一致

# 4. 启动后端
python main.py         # 日志出现"服务启动 | CHAT=... QA=..."即成功
```

前端：浏览器直接打开 `frontend/index.html`，或 `python -m http.server 8080` 后访问
`http://localhost:8080`。首次使用在登录门**注册**一个账号即可（本地无需任何额外鉴权配置，
`JWT_SECRET` 未设置会自动生成临时密钥，仅限开发——重启后旧 token 失效属正常现象）。

> ⚠️ 本地开发别开 `uvicorn reload=True`，也别在运行后端的同时用脚本直连 `qdrant_data` 目录：
> Qdrant 本地嵌入模式是单进程文件锁，两者都会触发 `already accessed` 错误。

---

## 8. 验证清单

1. **鉴权**：不带 token 请求 `/api/chat` → 401；注册 → 登录 → `/api/auth/me` 回显用户名；
2. **RAG 入库**：`POST /api/kb/upload` 传一个 UTF-8 文本或 PDF → 响应 `{ok: true, chunks: N}`；
   扫描版 PDF 应返回 422 而非假阳性成功；
3. **入库核查**：`GET /api/kb/stats` → `exists=true`、`dim` 与 `EMBED_DIM` 一致、
   `sources` 里能看到刚上传的文件名及其块数（同名文件重复上传是幂等覆盖，总点数不涨属正常）；
4. **问答（RAG / 混合检索）**：`POST /api/ask` 问一个**库内**问题 → 前端气泡顶部出现"📄 参考：文件名#块号"，
   回答依据资料；问**库外**问题 → 明确说不知道、不受资料污染。再问一个只含**罕见专有名词/错误码**的问题，
   日志应出现 `混合检索 | 向量命中=x | 关键字命中=y`；`RETRIEVAL_MODE=vector` 常漏检、`hybrid` 靠 BM25 捞回；
5. **多轮记忆**：`POST /api/chat` 连发两轮（"我叫小明" → "我叫什么"）→ 第二次能答对，
   MySQL `messages` 表 user / assistant 两类记录都已落库；
6. **用户隔离**：换账号查 `/api/sessions` 看不到他人会话；删除他人会话 → 403。
7. **工具调用（Function Calling）本地验证**：

   ```powershell
   # 1) 确认 .env 中 ENABLE_TOOLS=1，启动后端
   cd E:\ZUO\mini_qianwen\backend
   python main.py            # 日志应出现“服务启动 ... | 检索模式=...”，无 ImportError
   ```

   登录前端后切到**闲聊模式**（`/api/chat`，非问答模式），逐句验证：

   - **查天气**：问“北京今天天气怎么样？” → AI 气泡顶部先出现“🌤️ 正在查天气 · 北京”提示行，
     随后流式输出实况温度与预报（能拿到真实数据即说明 Open-Meteo 通路打通，需容器/本机可访问外网）；
   - **查用户**：问“我的账号是什么时候注册的？” → 出现“👤 正在查用户信息”，回答含注册时间（取自 users 表）；
   - **查历史**：先聊几句植入一个关键词（如“我叫小明”），新开会话再问“我之前说过什么名字？”
     → 出现“🔍 正在检索历史对话”并命中旧消息；
   - **看日志**：后端应打印 `工具调用 | sid=... | 轮=n | get_weather(...)` 一行，可据此确认模型真的发了 `tool_calls`；
   - **回退验证**：若模型不调工具或网关报 400，把 `ENABLE_TOOLS=0` 重启，确认 `/api/chat` 回到正常纯闲聊。

   > 可用 `backend/test.py` 快速自检聊天模型连通性；但 `test.py` 不带 `tools` 参数，工具链路需按上面走真实 `/api/chat` 验证。

---

## 9. 常见问题速查

| 现象 | 最可能原因 | 处置 |
|---|---|---|
| 启动即 `KeyError: 'DB_URL'` | `.env` 缺失或加载顺序被改动 | 确认各模块顶部的锚定 `load_dotenv` 还在最前 |
| 部署后登录成功但接口全 401 | Nginx 未透传 Authorization 头 | 加 `proxy_set_header Authorization $http_authorization` 后 reload |
| 容器反复 Restarting / Nginx 502 | 依赖缺失（典型：`sqlalchemy` 没写 `[asyncio]`） | `docker compose logs backend` 看首行报错，修复后 `--build --force-recreate` |
| 前端登录门不消失 / 主界面不显示 | `hidden` 属性被 `display` 样式覆盖 | 保留 style.css 顶部的 `[hidden]{display:none!important}` |
| embedding 返回 404 Model not exist | 模型名与网关文档不一致 | 以网关文档为准改 `EMBED_MODEL`，跑 `test_embed.py` |
| `expected dim: 1536, got 1024` | 换模型后旧集合维度不匹配 | 改 `EMBED_DIM` → **删除旧集合** → 重新上传 |
| 上传 PDF 提示 422 未入库 | 扫描版/图片型 PDF 抽不出文本 | 属预期拦截；需先 OCR 转成文字版再上传 |
| `Storage folder ... already accessed` | 后端与脚本同时锁本地 Qdrant | 先停后端再跑脚本；或切 server 模式 |
| 闲聊不记事 | 前端没读到 `X-Session-Id` | 检查 CORS `expose_headers`（见 4.1） |
| SSE 回复一段段憋出来 | Nginx 开了代理缓冲 | `proxy_buffering off; proxy_cache off;` |

---

## 10. 待优化方向

**鉴权**

1. 无 refresh token 轮换：token 一发到底，到期强制重登；更严格可做双 token；
2. 注册/登录无限流：公网可被暴力尝试密码，应加频率限制或验证码。

**知识库与 RAG 质量**

3. 切块策略原始：固定 500 字符窗口不分语言、不认标题/段落边界。可演进为按 Markdown 标题、
   句子边界或语义切块，并让 overlap 随块大小可调；
4. 已实现向量 + BM25 混合检索（RRF 融合）；下一步可加**重排序（rerank）模型**对融合结果精排、
   jieba 领域词典定制以提升专业术语切分；`.doc` 与扫描版 OCR 仍未支持；
5. 知识库未按用户隔离：全库共享，`/api/kb/stats` 对任何登录用户可见全部内容，
   如需私有知识库应在 payload 加 uid 过滤或分集合。

**数据层与工程化**

6. `append_message` 回写失败静默：流结束回调若抛异常只影响日志，可加重试或死信记录；
7. 前端 `reasoning` 未展示：后端已下发思考过程增量，可加可折叠"思考中"面板；
8. 依赖未固定版本：`requirements.txt` 只列包名，建议 `pip freeze` 固化或加约束文件；
9. HTTPS 未启用：JWT 走明文 HTTP 有被截获风险，公网部署建议上 TLS。

**AI 应用开发常见方向**

10. Workflow 工作流编排：复杂任务（多步检索 → 汇总 → 生成报告）靠单次提示词难以稳定完成，
    可引入工作流引擎（如自建 DAG 或 LangGraph 类框架），详见下文「Agent 能力演进」第 19 条；
11. Prompt 管理：系统提示词目前散落在代码里，应抽成模板集中管理，支持版本化、
    变量渲染与 A/B 切换，避免改提示词要重新发版；
12. 上下文窗口控制：多轮对话历史无限拼接会撑爆 token 上限，需要滑动窗口、
    历史摘要压缩或"记忆库"机制，同时注意 RAG 引用块与对话历史的预算分配；
13. 效果评测（Evals）：换模型、改切块、调提示词后好坏全凭手感，应建立评测集
    （问答对 + 期望来源），对检索命中率、答案相关性做自动化回归评分；
14. 成本与限流：按 token 计费无用量统计，建议记录每请求的 prompt/completion token，
    并按用户做配额与并发限流，防止单一用户打爆额度；
15. 幻觉与兜底：RAG 未命中时应降低模型"自由发挥"权限（约束提示词、阈值过滤），
    并对高风险场景（如引用来源）做事实性校验与降级话术。

**Agent 能力演进（MCP / Skill / Harness / Workflow）**

> 演进主线：**Function Calling（已完成）→ Harness 加固 → Skill 知识化 → MCP 生态化 → Workflow 编排化**。
> 前三条改造的是现有 `tools.py` / `_chat_sse` 循环，都不需要推翻现有架构；Workflow 是新增能力，放最后。

16. **Harness 加固**（模型外围脚手架：提示词组装、工具协议、执行循环、状态管理、错误恢复）——
    当前 `_chat_sse` 已是最小 Harness（工具循环 + `MAX_TOOL_ROUNDS` 防死循环 + 失败兜底 + 流结束回写），
    下一步按优先级补：
    - **可观测性**：引入 run_id（一次问答=一个 run），贯穿日志、SSE 事件与工具轨迹；把
      `assistant(tool_calls)` / `tool` 消息单独落一张 `traces` 表（不进 messages 防历史膨胀），
      支持事后回放"模型这轮到底调了哪些工具、拿到什么结果"；
    - **护栏（Guardrails）**：输入侧提示词注入检测（工具结果回喂前也要检，防间接注入）；
      输出侧敏感信息过滤（工具已保证 `pw_hash` 不出 DB，但模型复述仍需拦截）；
    - **循环策略升级**：从"单轮调工具→收尾"演进为 plan → execute → reflect 循环
      （模型先列计划、逐项执行、自检补漏），仍受 `MAX_TOOL_ROUNDS` 类预算约束；
    - **抽象解耦**：把 `_chat_sse` / `_stream_to_browser` 里"消息组装 + 循环控制 + 下发格式"抽成
      独立的 AgentRunner 类，`/api/chat` 与未来的 Workflow / Skill 共用同一 Harness，
      避免每个场景各写一套循环。
17. **Skill（领域能力包）**：把"某类任务该怎么做"的专家流程（提示词模板 + 步骤说明 +
    可选脚本/资料）沉淀成 Markdown 技能文件，与工具区分——工具是"可执行动作"，Skill 是"做事方法论"。
    - 落地方式：新建 `skills/` 目录（每个技能一个文件夹，`SKILL.md` 含 frontmatter：name/description），
      启动时只把 name+description 注入 system 提示（**渐进式加载**，防上下文爆炸），
      模型判断命中哪个技能后再通过新工具 `load_skill(name)` 读取全文；
    - 与现有能力复用：技能意图匹配可直接走 `hybrid_retrieve`（技能库向量化后与知识库同管道）；
      下发"⚡ 正在使用技能：xx"事件复用 `/api/chat` 现有的 `{tool}` 事件机制，前端几乎不改；
    - 配合第 11 条 Prompt 管理一起做：技能本质上是版本化的提示词资产。
18. **MCP（Model Context Protocol）**：把工具从"代码里写死"变成"协议化插拔"，双向接入：
    - **对内：backend 兼作 MCP Server**——用 FastMCP SDK 把 `tools.py` 三个工具
      （get_weather / get_user_info / search_chat_history）暴露为标准 MCP 服务，Claude Desktop、
      Qoder 等任意 MCP 客户端可复用同一套能力，与 HTTP 接口并存互不影响；
    - **对外：backend 作 MCP Client**——新增 `mcp_registry.py`，启动时连接 `.env` 配置的若干
      MCP Server（`mcp_servers.json`），把远端 `tools/list` 动态转成 `TOOL_SCHEMAS` 附加进模型请求，
      `execute_tool` 遇到 `mcp__<server>__<tool>` 前缀时经 MCP 会话转发执行——接入社区工具
      （搜索、数据库、浏览器等）零代码改动；
    - **安全约定（延续现有 ctx 原则）**：uid 等身份信息绝不能经模型或 MCP 参数传递，
      由后端在发起 MCP 调用时经 header/参数注入；外部 MCP 工具须白名单启用，防工具数量
      爆炸污染上下文与供应链风险；返回内容回喂模型前按不可信输入处理（同护栏）；
    - **传输选型**：本机 Docker 内用 streamable-http，不建议 stdio（与 uvicorn 单进程模型混管子进程
      生命周期复杂度高）。
19. **AI Workflow（工作流编排）**：面向"多步骤、可复放、需断点续跑"的复杂任务
    （如：多文档检索 → 分节摘要 → 汇总成报告；数据抓取 → 清洗 → 入库知识库）：
    - **节点类型**：LLM 调用、工具调用（复用 Harness 的 execute_tool）、条件分支、循环、
      人工审核（human-in-the-loop，暂停等前端确认后续跑）；
    - **引擎选型**：建议**先自建轻量状态机**（数据 = 工作流定义 JSON + 实例状态表），
      与本项目体量匹配、无重框架依赖；确实复杂到需要分支并发/时间旅行调试时再评估 LangGraph，
      且仅用其图执行内核、不引入其部署模型，避免与现有 FastAPI + SSE 架构冲突；
    - **执行模型**：长任务必须异步化——`POST /api/workflow/{name}/run` 立即返回 run_id，
      后台 `asyncio` 任务（量大再上任务队列）推进状态机，前端经 SSE 轮询/订阅节点进度；
      每节点输出落库（`workflow_runs` / `workflow_steps` 表），失败从最后成功节点**断点续跑**
      而非整体重来；
    - **依赖关系**：Workflow 建立在 Harness 之上（第 16 条的 AgentRunner 即 Workflow 的
      "LLM+工具"复合节点实现），故排在其后；Skill 可作为流程节点内注入的方法论。

**建议实施顺序**：16（Harness 加固，改现有代码收益最快）→ 17（Skill，复用 RAG + 提示词基建）→
18（MCP，能力外溢/外引）→ 19（Workflow，独立新模块）。
