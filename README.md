# Mini Qwen —— 极简千问聊天应用

> 本文档基于**当前后端实际代码**（`backend/main.py`、`backend/db.py`、`backend/vector.py`）整理，
> 说明项目现状、本次迭代新增的能力，以及已识别出的待优化项。

---

## 1. 项目简介

Mini Qwen 是一个"前端聊天页面 + FastAPI 后端 + 云端大模型"的最小 AI 应用。相比最初纯 Demo 版本，
当前后端已经完成了三件事：

1. **会话历史从内存字典迁移到 MySQL**——重启不丢、多实例可共享；
2. **单轮问答接口接入 RAG**——问答前先检索 Qdrant 向量库，命中的资料注入系统提示词；
3. **知识库链路打通**——提供文件上传 → 切块 → DashScope 向量化 → 写入 Qdrant 的完整入口，并配了调试用的状态查询接口。

### 1.1 能力总览

| 能力 | 说明 | 对应代码 |
|---|---|---|
| 多轮闲聊 | 按 `session_id` 读写 **MySQL** 会话历史，流式返回 | `main.py /api/chat` + `db.py` |
| 单轮问答 + RAG | 先检索 **Qdrant** 命中资料，再注入 system 提示 | `main.py /api/ask` + `vector.py retrieve()` |
| 知识库入库 | 上传 UTF-8 文本 → 切块 → **DashScope** 向量化 → 写 Qdrant | `main.py /api/kb/upload` + `vector.py add_docs()` |
| 知识库状态查询 | 集合是否存在、向量维度、点数与抽样文本 | `main.py /api/kb/stats` |
| 流式输出 | 模型流 → SSE 逐条推送，支持 `reasoning_content` 思考流 | `main.py _stream_to_browser()` |
| AI 回复回写 | 流结束后把完整助手回复持久化进 MySQL，多轮记忆闭环 | `/api/chat` 的 `on_complete` 回调 |
| 历史窗口裁剪 | 只取最近 N 轮（默认 10），在 DB 层用 `LIMIT` 完成 | `db.py load_history()` |
| OpenAI 兼容协议 | 聊天模型走兼容网关，换服务商只改 `.env` | `main.py AsyncOpenAI` |

---

## 2. 整体架构

```mermaid
graph TB
    A[浏览器前端<br/>frontend/app.js] -->|"POST /api/chat"| B[FastAPI 后端<br/>main.py]
    A -->|"POST /api/ask"| B
    A -->|"POST /api/kb/upload"| B
    B -->|"OpenAI 兼容协议 stream=True"| C[云端聊天模型<br/>BASE_URL 网关]
    B -->|"load_history / append_message"| D[(MySQL<br/>messages / sessions 表)]
    B -->|"retrieve / add_docs"| E[vector.py]
    E -->|"TextEmbedding 原生 SDK"| F[DashScope Embedding<br/>qwen3.7-text-embedding-flash]
    E -->|"upsert / query_points"| G[(Qdrant 向量库<br/>collection: kb)]
```

数据流要点：

- **聊天模型**走 OpenAI 兼容协议（`AsyncOpenAI`），换服务商只需改 `.env` 里的 `BASE_URL` 和模型名；
- **Embedding 模型**走 DashScope **原生 SDK**（`dashscope.TextEmbedding.call`），不兼容 OpenAI 协议，
  所以单独封装在 `vector.py`，且 `DASHSCOPE_EMB_URL` 必须在代码里显式赋值（环境变量不会自动生效）；
- **同步阻塞的 SDK 调用**（DashScope、Qdrant client）全部用 `asyncio.to_thread` 包裹，避免卡住事件循环。

---

## 3. 目录结构

```
mini_qianwen/
├── backend/
│   ├── main.py        # FastAPI 接口层：chat / ask / kb 上传与状态 / 遗留会话接口
│   ├── db.py          # MySQL 会话存储层：append_message / load_history / clear_session
│   ├── vector.py      # 向量层：Qdrant 客户端管理 + DashScope Embedding + add_docs / retrieve
│   ├── test.py        # 聊天模型连通性自检脚本
│   ├── test_embed.py  # Embedding 模型可用性与向量维度实测脚本
│   ├── .env           # 配置与密钥（不进 git）
│   └── qdrant_data/   # Qdrant 本地嵌入模式的默认数据目录
├── frontend/
│   ├── index.html     # 页面结构（Tab 切换、消息列表、输入区）
│   ├── style.css      # flex 三段式布局与气泡样式
│   └── app.js         # fetch + ReadableStream 手动解析 SSE
└── README.md
```

---

## 4. 后端模块说明

### 4.1 `main.py` —— 接口层

**配置加载（重要约定）**：模块顶部先 `load_dotenv(Path(__file__).resolve().parent / ".env")`
锚定**文件所在目录**加载配置，再读取环境变量、再 `import db / vector`。顺序不能调换——
`db.py` 在 import 期就会读 `os.environ["DB_URL"]`，晚加载会直接 `KeyError`。
（`db.py`、`vector.py` 各自也带同样的自加载兜底，与运行目录、导入顺序解耦。）

**接口一览**：

| 方法 | 路径 | 功能 | 请求体 | 响应 |
|---|---|---|---|---|
| POST | `/api/chat` | 多轮闲聊（RAG 无关，有状态） | `{message, session_id?}` | SSE 流 + 响应头 `X-Session-Id` |
| POST | `/api/ask` | 单轮问答（RAG，无状态） | `{question, system_prompt?}` | SSE 流 |
| POST | `/api/kb/upload` | 知识库入库（multipart 文件） | `file`（UTF-8 文本） | `{ok, chunks}` |
| GET | `/api/kb/stats` | 知识库状态（维度/点数/抽样） | 无 | JSON |
| GET | `/api/sessions` | ⚠️ 遗留内存版会话列表 | 无 | `{ids}` |
| DELETE | `/api/session/{sid}` | ⚠️ 遗留内存版清空会话 | 无 | `{ok}` |

**`/api/chat` 流程**（历史已迁 MySQL）：

1. `sid = session_id or uuid4()`；前端未带 ID 时后端惰性生成，通过 `X-Session-Id` 响应头返回
  （CORS 里 `expose_headers=["X-Session-Id"]`，否则跨域 JS 读不到该头）；
2. **先落库本轮用户消息**（`append_message`，同时 update `sessions` 表的活跃时间）；
3. `load_history(sid, MAX_HISTORY)` 取最近 N 轮（`ORDER BY id DESC LIMIT 2N` 再翻正），裁剪在 DB 层完成；
4. 拼 `SYSTEM_CHAT + history` 调模型 `stream=True`（temperature=0.7, max_tokens=1024）；
5. `_stream_to_browser(..., on_complete=...)` 转发 SSE，**流结束后把完整助手回复回写 MySQL**——
   这是多轮记忆闭环的关键：模型记不住自己说过什么的老 Bug 已修复。

**`/api/ask` 流程**（RAG）：

1. `retrieve(question, k=4)` 先从 Qdrant 检索 Top-4 相似文本块；
2. 命中则用 `_make_context()` 把资料拼成 `【资料1】...【资料2】...` 注入 system 提示，
   并要求"仅依据资料作答，资料里没有的明确说不知道"；未命中则退回 `SYSTEM_QA`（或前端自定义人设）；
3. 调模型（temperature=0.3 求稳定，max_tokens=2048）→ SSE 返回。全程不读写会话历史。

> ⚠️ 历史上这里曾有"检索结果被后续旧代码覆盖"的死代码 Bug，现已修复：`system/messages` 只构建一次。

**工具函数**：

- `_chunk(text, size=500, overlap=80)`：固定字符窗口切分 + 80 字重叠，避免答案恰好横跨切缝被截断；
- `_stream_to_browser(stream, on_complete)`：协议翻译（OpenAI chunk 流 → SSE `data:` 行），
  兼容 `reasoning_content`（`getattr` 取值防 AttributeError），出错时下发 `{"error": ...}` 而非掐断连接，
  `finally` 保证必发 `data: [DONE]`；`on_complete` 回调用于流结束后的持久化。

**异常处理**：`APIStatusError` → 透传状态码 + 错误体；`APITimeoutError`（90s）→ 504。
转成 `HTTPException` 后前端能从 `detail` 字段拿到真实原因，形成完整的错误传导链。

### 4.2 `db.py` —— MySQL 会话存储层

- 引擎：`SQLAlchemy` 异步 + `aiomysql` 驱动，`create_async_engine(DB_URL, pool_pre_ping=True, pool_size=5)`；
- `append_message(sid, role, content)`：一条事务里做两件事——往 `messages` 表插消息，
  并 `INSERT ... ON DUPLICATE KEY UPDATE last_active = NOW()` 维护 `sessions` 表的活跃时间；
- `load_history(sid, max_turns)`：按 `sid` 取最近 `max_turns * 2` 条（一轮 = user + assistant 两条），倒序取、翻正回；
- `clear_session(sid)`：`DELETE FROM messages WHERE sid=:sid`。**注意：函数已写好但 main.py 尚未接入**（见第 7 节）。

依赖的两张表 DDL（`sid` 必须为 `sessions` 主键，否则 `ON DUPLICATE KEY` 不生效）：

```sql
CREATE TABLE messages (
  id       BIGINT AUTO_INCREMENT PRIMARY KEY,
  sid      VARCHAR(36) NOT NULL,
  role     VARCHAR(16) NOT NULL,
  content  TEXT        NOT NULL,
  created  DATETIME    DEFAULT CURRENT_TIMESTAMP,
  KEY idx_sid_id (sid, id)          -- 覆盖 WHERE sid=? ORDER BY id DESC LIMIT n
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE sessions (
  sid         VARCHAR(36) PRIMARY KEY,
  last_active DATETIME,
  KEY idx_last_active (last_active)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

### 4.3 `vector.py` —— 向量层（Qdrant + DashScope Embedding）

对外只暴露两个异步函数：`add_docs(docs, ids)` 入库、`retrieve(query, k)` 检索。内部要点：

| 设计 | 做法 | 原因 |
|---|---|---|
| 连接形态自适应 | `_make_client()`：`QDRANT_URL` 有值 → server/云模式；否则本地嵌入 `QDRANT_PATH` | Windows 本地免服务；上生产切 Docker 不改代码 |
| 懒加载单例 | `_get_client()` 首次调用才创建客户端 | 避免 import 期就抢占本地模式的**文件锁**（同目录只允许单进程访问） |
| 稳定点 ID | `_pid()` 用 `uuid5(文件名#块号)` 映射成 UUID | Qdrant 只收 int/UUID；同一块重复上传**幂等覆盖**，不堆积 |
| 集合自创建 | `_ensure_collection()` 按 `EMBED_DIM` + COSINE 建集合 | 换 Embedding 模型必须同步改 `EMBED_DIM` **并删除旧集合重建**，否则维度不匹配写入报错 |
| 原生 SDK | `dashscope.TextEmbedding.call`，包在 `asyncio.to_thread` 里 | 该网关不提供 OpenAI 兼容的 embedding 入口；同步 SDK 不能裸调以免阻塞事件循环 |
| 分批调用 | `batch=10` 分批发送 | 规避单次 input 条数上限 |
| 响应结构兼容 | `_get(obj, key)` 兼容 dict / 对象两种返回形态，且不再按 `text_index` 排序 | 新版 SDK 返回 AttributeDict，旧写法会 KeyError；SDK 已保证顺序 |
| 检索降级 | 集合不存在时 `retrieve` 返回空列表 | 灌库前 `/api/ask` 依然可用（退回纯模型问答） |

---

## 5. 配置说明（`backend/.env`）

| 变量 | 用途 | 备注 |
|---|---|---|
| `API_KEY` | 大模型 / Embedding 密钥 | 仅存在于后端，绝不下发前端；`.env` 不进 git |
| `BASE_URL` | 聊天模型兼容网关 | 换服务商只改这里 |
| `CHAT_MODEL` / `QA_MODEL` | 闲聊 / 问答模型名 | 需与网关提供的名称严格一致 |
| `MAX_HISTORY` | 历史窗口轮数 | 默认 10 |
| `DB_URL` | MySQL 连接串 | 格式 `mysql+aiomysql://user:pwd@host:3306/db` |
| `QDRANT_PATH` / `QDRANT_URL` / `QDRANT_API_KEY` | 向量库连接 | 二者选一：本地嵌入 vs server/云 |
| `QDRANT_COLLECTION` | 集合名 | 默认 `kb` |
| `EMBED_MODEL` | Embedding 模型名 | DashScope 原生口径，如 `qwen3.7-text-embedding-flash` |
| `DASHSCOPE_EMB_URL` | 原生 Embedding 地址 | **必须在代码里显式赋值**，SDK 不会自动读环境变量 |
| `EMBED_DIM` | 向量维度 | 当前模型为 1024；换模型必须实测（`test_embed.py`）并重建集合 |

---

## 6. 本次迭代新增内容（对比旧版 README）

旧版文档描述的是"无数据库、后端单文件、内存字典存会话"的纯 Demo 阶段。本次实际新增：

### 6.1 会话持久化（新文件 `db.py`）

- 闲聊历史由 `sessions` 内存字典改为 **MySQL `messages` / `sessions` 两张表**；
- 修复了旧版最大缺口：**AI 回复不回写导致模型记不住自己说过什么**——现在流结束后经
  `on_complete` 回调完整回写；
- 历史裁剪下沉到 DB 层（`LIMIT` + 翻序），`_trim()` 保留作内存模式备用；
- 进程重启会话不丢，为多实例部署扫清了内存字典的天然障碍。

### 6.2 RAG 链路（新文件 `vector.py` + 两个新接口）

- `/api/ask` 从"裸问答"升级为 **检索增强问答**：`retrieve(k=4)` → `_make_context()` 注入资料；
- 新增 `/api/kb/upload`：UTF-8 文本文件 → `_chunk` 切块 → DashScope 向量化 → Qdrant 幂等入库；
- 新增 `/api/kb/stats`：查集合存在性、**向量维度**、点数与抽样，专治"文本到底写没写进库"的排查场景；
- 向量库经 Milvus → Chroma → **Qdrant** 的选型迭代，最终因 Windows 本地嵌入模式免服务而敲定。

### 6.3 Embedding 接入方式重构

- 从 OpenAI 兼容接口迁移到 **DashScope 原生 SDK**（旧网关 404 `Model not exist` 的修复结果）；
- 落地三条硬规矩：模型名以网关文档为准、`EMBED_DIM` 必须实测回填、`base_http_api_url` 在代码中显式设置；
- 增加 `_get()` 响应结构兼容层与分批调用。

### 6.4 配置加载与稳定性修复

- 三个模块统一采用 `load_dotenv(Path(__file__).resolve().parent / ".env")` 锚定文件位置，
  彻底解决"换工作目录 / 换导入顺序就 `KeyError: 'DB_URL'`"的隐患；
- Qdrant 客户端改为**懒加载单例**，规避本地模式文件锁在 import 期被抢占；
- 全链路关键节点补充结构化日志（请求 sid、历史条数、向量化维度校验、入库点数、检索命中数等）。

---

## 7. 待优化方向

### 7.1 遗留代码清理（本次迁移的"半截工程"，优先级最高）

1. **`DELETE /api/session/{sid}` 仍操作内存字典**：删的是早已不再使用的 `sessions[sid]`，
   MySQL 里的历史纹丝不动。`db.clear_session()` 已写好却没有被调用——应接入并将接口改 `async`；
2. **`GET /api/sessions` 同理失真**：应改为查 `sessions` 表（按 `last_active` 排序、支持分页），
   顺带可作为"我的历史会话列表"功能的地基；
3. **内存 `sessions` 字典与 `_trim()`** 已无实际消费者，确认无引用后删除，避免误导后来者。

### 7.2 知识库与 RAG 质量

4. **切块策略原始**：固定 500 字符窗口不分语言、不认标题/段落边界。可演进为按 Markdown 标题、
   句子边界或语义切块，并让 overlap 随块大小可调；
5. **文件类型受限**：仅收 UTF-8 纯文本。可扩 PDF / DOCX / 网页抓取，并对非文本先做编码嗅探；
6. **检索无相似度阈值**：`retrieve` 支持 `score_threshold` 参数但 `/api/ask` 未传。
   不传时"库里再不相干的内容也会塞给模型 4 段"，容易污染回答——建议加阈值并记录命中率；
   更进一步可做重排序（rerank）与混合检索（关键词 + 向量）；
7. **来源不可追溯**：命中资料未把 `payload["id"]`（文件名#块号）透出给前端，用户看不到"答案依据哪份资料"。

### 7.3 数据层

8. **`messages.content` 为 TEXT（64KB 上限）**：长回答有截断风险，建议升级 `MEDIUMTEXT`；
9. **会话无归属、无软删除**：`sessions` 表可补 `user_id`、`created_at`、`title` 字段，为多用户做准备；
10. **回写失败静默**：`append_message` 在流结束回调中若抛异常，只影响日志。可加重试或死信记录。

### 7.4 部署与工程化

11. **Qdrant 本地模式是单进程文件锁**：后端运行时任何旁路脚本（删集合、查点数）都会
    `Storage folder already accessed`。上生产应切 `QDRANT_URL`（Docker server / 云）支持并发；
    代码已预留切换，只改 `.env` 即可；
12. **缺 `requirements.txt`**：当前依赖（fastapi / uvicorn / openai / dashscope / qdrant-client /
    sqlalchemy / aiomysql / python-dotenv）应固化版本，保证环境可复现；
13. **CORS 过宽**：`allow_origins=["*"]` 学习可用，生产应收敛到具体前端域名；
14. **接口无鉴权**：一旦公网部署，任何人可白嫖你的模型配额，至少要加 Token / JWT；
15. **配置了 `reload=False` 的启动方式**：开发期可临时开 `reload=True`，但注意它会与
    Qdrant 本地模式冲突（双进程抢文件锁），两者不能同时用；
16. **前端 `reasoning` 未展示**：后端已下发思考过程增量，前端可加可折叠"思考中"面板。

---

## 8. 启动与验证

### 8.1 启动步骤

```powershell
cd E:\ZUO\mini_qianwen\backend

# 1. 安装依赖（临时列表，正式应固化进 requirements.txt）
pip install fastapi uvicorn openai dashscope qdrant-client sqlalchemy aiomysql python-dotenv

# 2. 准备 MySQL：库与两张表见 4.2 节 DDL；确认 .env 中 DB_URL 可用

# 3. 连通性自检（先跑这两个脚本，失败则问题在 Key/网络，不在业务代码）
python test.py         # 聊天模型是否可达
python test_embed.py   # Embedding 模型是否可用、实测维度与 EMBED_DIM 是否一致

# 4. 启动后端
python main.py         # 日志出现"服务启动 | CHAT=... QA=..."即成功
```

前端：浏览器直接打开 `frontend/index.html`，或 `python -m http.server 8080` 后访问 `http://localhost:8080`。

### 8.2 RAG 链路验证顺序

1. 准备一个 UTF-8 文本文件，`POST /api/kb/upload`（`/docs` 页面可直接试）→ 响应 `{"ok": true, "chunks": N}`；
2. `GET /api/kb/stats` → 确认 `exists=true`、`dim` 与 `EMBED_DIM` 一致、`points_count > 0`、`samples` 有文本；
3. `POST /api/ask` 问一个**库内**问题 → 后端日志应显示"命中资料 4 段"，回答依据资料；
4. 问一个**库外**问题 → 回答应明确"不知道"或不受资料污染；
5. `POST /api/chat` 连发两轮（"我叫小明" → "我叫什么"）→ 第二次能答对，且 MySQL `messages` 表里
   user / assistant 两类记录都已落库（记忆闭环验证）。

### 8.3 常见问题速查

| 现象 | 最可能原因 | 处置 |
|---|---|---|
| 启动即 `KeyError: 'DB_URL'` | `.env` 缺失或加载顺序被改动 | 确认三个模块顶部的锚定 `load_dotenv` 还在最前 |
| embedding 返回 404 Model not exist | 模型名与网关文档不一致 | 以网关文档为准改 `EMBED_MODEL`，跑 `test_embed.py` |
| `expected dim: 1536, got 1024` | 换模型后旧集合维度不匹配 | 改 `EMBED_DIM` → **删除旧集合** → 重新上传 |
| `Storage folder ... already accessed` | 后端与脚本同时锁本地 Qdrant | 先停后端再跑脚本；或切 server 模式 |
| DashScope 响应取不到 embeddings | SDK 版本返回对象而非 dict | 走 `vector.py` 的 `_get()` 兼容层 |
| 闲聊不记事 | 前端没读到 `X-Session-Id` | 检查 CORS `expose_headers`（见 4.1） |
| 点"清空会话"没效果 | 该接口还在操作内存字典 | 已知遗留问题，见 7.1 第 1 条 |
