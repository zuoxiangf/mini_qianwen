# Mini Qwen —— 带鉴权与 RAG 知识库的极简千问聊天应用

> 本文档基于**当前实际代码**（`backend/` 五个模块 + `frontend/` + `docker-compose.yml` + `deploy/`）整理，
> 说明项目现状、架构设计、配置项与生产部署流程。

---

## 1. 项目简介

Mini Qwen 是一个"前端聊天页面 + FastAPI 后端 + 云端大模型"的 AI 应用。相比最初的纯 Demo 版本，
当前已经完成的能力有：

1. **JWT 用户鉴权**——注册/登录颁发 Bearer Token，所有业务接口受保护，会话按用户隔离；
2. **会话历史持久化 MySQL**——重启不丢、多实例可共享，AI 回复流结束后完整回写（多轮记忆闭环）；
3. **单轮问答接入 RAG**——问答前先检索 Qdrant 向量库，命中资料注入系统提示词，来源透出到前端；
4. **知识库多格式入库**——支持 TXT / PDF / DOCX / HTML 文件上传与网页 URL 抓取，一条流水线：
   抽取纯文本 → 编码嗅探 → 切块 → DashScope 向量化 → 幂等写入 Qdrant；
5. **生产部署体系**——Docker Compose（MySQL + Qdrant + 后端）+ 宿主机 Nginx（静态站点 + API 反代 + SSE 支持）。

### 1.1 能力总览

| 能力 | 说明 | 对应代码 |
|---|---|---|
| 注册 / 登录 | bcrypt 哈希密码，签发 HS256 JWT；登录防用户名探测 | `auth.py` + `main.py /api/auth/*` |
| 多轮闲聊 | 按 `session_id` 读写 **MySQL** 会话历史，流式返回；会话绑定归属用户 | `main.py /api/chat` + `db.py` |
| 单轮问答 + RAG | 先检索 **Qdrant** 命中资料，注入 system 提示，`sources` 事件先于正文下发 | `main.py /api/ask` + `vector.py retrieve()` |
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
    B -->|消息 / 会话 / 用户读写| D[(MySQL<br/>messages / sessions / users)]
    B -->|文件 / URL| EX[extract.py<br/>PDF·DOCX·HTML·编码嗅探]
    B -->|retrieve / add_docs| E[vector.py]
    E -->|TextEmbedding 原生 SDK| F[DashScope Embedding]
    E -->|upsert / query_points| G[(Qdrant 向量库<br/>server 模式)]
```

数据流要点：

- **鉴权无状态**：业务接口经 `Depends(get_current_user)` 验 JWT 签名（不查库），拦截发生在
  DB / 模型调用之前；会话数据靠 `sessions.uid` 归属实现用户间隔离；
- **聊天模型**走 OpenAI 兼容协议（`AsyncOpenAI`），换服务商只需改 `.env` 里的 `BASE_URL` 和模型名；
- **Embedding 模型**走 DashScope **原生 SDK**（`dashscope.TextEmbedding.call`），不兼容 OpenAI 协议，
  单独封装在 `vector.py`，且 `DASHSCOPE_EMB_URL` 必须在代码里显式赋值（环境变量不会自动生效）；
- **同步阻塞调用**（DashScope、Qdrant client、bcrypt、文件解析）全部用 `asyncio.to_thread` 包裹，
  避免卡住事件循环；
- **SSE 流式**经 Nginx 反代时必须关缓冲（`proxy_buffering off`），否则流式回复会被攒包卡住。

---

## 3. 目录结构

```
mini_qianwen/
├── backend/
│   ├── main.py        # FastAPI 接口层：鉴权 / chat / ask / kb 上传·网页·状态 / 会话管理
│   ├── auth.py        # 鉴权基础设施：bcrypt 哈希、JWT 签发/校验、get_current_user 依赖
│   ├── db.py          # MySQL 存储层：消息、会话（含归属 uid）、用户表读写
│   ├── vector.py      # 向量层：Qdrant 客户端管理 + DashScope Embedding + add_docs / retrieve
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
| POST | `/api/chat` | 多轮闲聊（有状态，历史存 MySQL） | `{message, session_id?}` | SSE 流 + 响应头 `X-Session-Id` |
| POST | `/api/ask` | 单轮问答（RAG，无状态） | `{question, system_prompt?}` | SSE 流（正文前先下发 `{sources}` 事件） |
| POST | `/api/kb/upload` | 知识库入库（multipart，支持 txt/pdf/docx/html） | `file` | `{ok, source, chars, chunks}` |
| POST | `/api/kb/web` | 网页入库（抓取 → 抽正文 → 同一流水线） | `{url}` | `{ok, source, chars, chunks}` |
| GET | `/api/kb/stats` | 知识库状态（维度/点数/按来源聚合块数） | 无 | JSON |
| GET | `/api/sessions` | 当前用户会话列表（limit/offset 分页） | 无 | `{items, limit, offset}` |
| DELETE | `/api/session/{sid}` | 删除会话（先校验归属，越权 403） | 无 | `{ok}` |

**`/api/chat` 流程**：

1. `sid = session_id or uuid4()`；前端未带 ID 时后端惰性生成，通过 `X-Session-Id` 响应头返回
   （CORS 里 `expose_headers=["X-Session-Id"]`，否则跨域 JS 读不到该头）；
2. **先落库本轮用户消息**（`append_message`，会话首建时绑定当前用户 `uid`，已存在只刷新活跃时间）；
3. `load_history(sid, MAX_HISTORY)` 取最近 N 轮（`ORDER BY id DESC LIMIT 2N` 再翻正），裁剪在 DB 层完成；
4. 拼 `SYSTEM_CHAT + history` 调模型 `stream=True`（temperature=0.7, max_tokens=1024）；
5. `_stream_to_browser(..., on_complete=...)` 转发 SSE，**流结束后把完整助手回复回写 MySQL**——
   多轮记忆闭环的关键。

**`/api/ask` 流程**（RAG）：

1. `retrieve(question, k=4, score_threshold=KB_SCORE_THRESHOLD)` 先从 Qdrant 检索 Top-4 相似块，
   低于阈值的结果被过滤（阈值留空则不过滤）；
2. 命中则用 `_make_context()` 把资料拼成 `【资料1｜来源：xxx】...` 注入 system 提示，
   并要求"仅依据资料作答、句末标注出处"；未命中则退回 `SYSTEM_QA`（或前端自定义人设）；
3. 通过 `_stream_to_browser(pre_events=[{"sources": [...]}])` 把命中来源（`文件名#块号`）**先于正文**
   下发给前端渲染"📄 参考"条。全程不读写会话历史。

**知识库入库流水线 `_ingest_text()`**：切块（`_chunk`，500 字窗口 + 80 字重叠）→
`add_docs` 向量化写 Qdrant。**抽取不到文本时显式抛 422**（而非返回 `ok=True`），
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

对外只暴露两个异步函数：`add_docs(docs, ids)` 入库、`retrieve(query, k, score_threshold)` 检索。

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

### 4.5 `extract.py` —— 文本抽取层

`extract_text(raw, filename)` 按扩展名分流：

| 格式 | 方式 | 说明 |
|---|---|---|
| pdf | pypdf 逐页抽取 | 扫描版/图片型会抽出空文本（记 warning），由入库流水线显式 422 拒绝 |
| docx | python-docx 按段落抽取 | 旧版二进制 `.doc` 不支持，报错提示转存 |
| html/htm | BeautifulSoup 去 script/style 抽正文 | 网页入库复用此路径 |
| 其他/未知后缀 | 编码嗅探：UTF-8 → GBK → charset-normalizer 自动探测 | 尽量宽容不误伤；全部失败按 UTF-8 宽松解码 |

解析库**软导入**：缺哪个库只影响对应格式，其余格式仍可用。抽取文本超 `KB_MAX_TEXT_CHARS` 截断。

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
| `KB_SCORE_THRESHOLD` | RAG 相似度下限 | 留空不过滤；建议 0.2~0.35，结合检索得分日志调 |

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
4. **问答**：`POST /api/ask` 问一个**库内**问题 → 前端气泡顶部出现"📄 参考：文件名#块号"，
   回答依据资料；问**库外**问题 → 明确说不知道、不受资料污染；
5. **多轮记忆**：`POST /api/chat` 连发两轮（"我叫小明" → "我叫什么"）→ 第二次能答对，
   MySQL `messages` 表 user / assistant 两类记录都已落库；
6. **用户隔离**：换账号查 `/api/sessions` 看不到他人会话；删除他人会话 → 403。

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
4. 检索仍是单路向量：可加关键词混合检索与重排序（rerank）；`.doc` 与扫描版 OCR 未支持；
5. 知识库未按用户隔离：全库共享，`/api/kb/stats` 对任何登录用户可见全部内容，
   如需私有知识库应在 payload 加 uid 过滤或分集合。

**数据层与工程化**

6. `append_message` 回写失败静默：流结束回调若抛异常只影响日志，可加重试或死信记录；
7. 前端 `reasoning` 未展示：后端已下发思考过程增量，可加可折叠"思考中"面板；
8. 依赖未固定版本：`requirements.txt` 只列包名，建议 `pip freeze` 固化或加约束文件；
9. HTTPS 未启用：JWT 走明文 HTTP 有被截获风险，公网部署建议上 TLS。
