// 后端 API 服务地址：本地开发（file:// 或 localhost/127.0.0.1）直连 8000；线上按 IP/域名访问，与页面同域由 Nginx 反代 /api/
const API = location.protocol === "file:" || ["localhost", "127.0.0.1"].includes(location.hostname)
  ? "http://localhost:8000"
  : "";

let mode = "chat";          // 当前模式：chat（闲聊，带上下文）| ask（单轮问答，无历史）
let sessionId = null;       // 闲聊会话 ID，由后端通过 X-Session-Id 响应头返回
let aborter = null;         // 当前请求的 AbortController，用于停止生成

// ---------- 鉴权状态：JWT 存在 localStorage，请求携 Authorization: Bearer ----------
let token = localStorage.getItem("token") || null;   // 登录凭证
let authMode = "login";                               // 登录门当前页签：login | register

// 页面核心 DOM 元素引用

const $list  = document.getElementById("list");
const $input = document.getElementById("input");
const $send  = document.getElementById("btnSend");
const $stop  = document.getElementById("btnStop");
const $hint  = document.getElementById("hint");
const $newBtn = document.getElementById("btnNew");
const $app = document.getElementById("app");
const $gate = document.getElementById("authGate");
const $curUser = document.getElementById("curUser");
const $authUser = document.getElementById("authUser");
const $authPass = document.getElementById("authPass");
const $authMsg = document.getElementById("authMsg");
const $authSubmit = document.getElementById("authSubmit");

// ---------- 鉴权辅助 ----------
/** 所有需鉴权的请求统一使用；带 Content-Type 与 Authorization 头 */
function authHeaders(extra) {
  return Object.assign(
    { "Content-Type": "application/json" },
    token ? { "Authorization": "Bearer " + token } : {},
    extra || {},
  );
}

/** 切换登录门 / 主界面显示 */
function showGate() {
  $gate.hidden = false;
  $app.hidden = true;
  $authPass.value = "";
}
function showApp(username) {
  $gate.hidden = true;
  $app.hidden = false;
  $curUser.textContent = username ? ("👤 " + username) : "";
  $input.focus();
}

/** 统一提取后端错误信息：兼容字符串 detail 与 Pydantic 422 校验错误的数组 detail，
    否则数组被直接赋给 textContent 会显示成 [object Object] */
function extractError(data, status) {
  const d = data && data.detail;
  if (typeof d === "string") return d;
  if (Array.isArray(d) && d.length) {
    const first = d[0];
    const field = (first.loc || []).slice(-1)[0];          // loc 末位是字段名，如 "username"
    const msg = first.msg || "参数不合法";
    if (first.type === "string_too_short" && /at least (\d+)/.test(msg)) {
      return `${field || "字段"}太短，至少需要 ${RegExp.$1} 个字符`;
    }
    return (field ? field + ": " : "") + msg;
  }
  return `请求失败 (${status})`;
}

/** 登出：清除本地 token 并回到登录门 */
function logout() {
  token = null;
  localStorage.removeItem("token");
  sessionId = null;
  $list.innerHTML = "";
  showGate();
}
document.getElementById("btnLogout").addEventListener("click", logout);

// 登录 / 注册页签切换
document.querySelectorAll(".auth-tab").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelector(".auth-tab.active").classList.remove("active");
    btn.classList.add("active");
    authMode = btn.dataset.auth;
    $authSubmit.textContent = authMode === "login" ? "登录" : "注册";
    $authPass.autocomplete = authMode === "login" ? "current-password" : "new-password";
    $authMsg.textContent = "";
  });
});

// 提交登录/注册
document.getElementById("authForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const username = $authUser.value.trim();
  const password = $authPass.value;
  if (!username || !password) { $authMsg.textContent = "请填写用户名和密码"; return; }
  if (username.length < 3) { $authMsg.textContent = "用户名至少 3 个字符"; return; }
  if (password.length < 6) { $authMsg.textContent = "密码至少 6 位"; return; }
  $authSubmit.disabled = true;
  $authMsg.textContent = "";
  try {
    const res = await fetch(`${API}/api/auth/${authMode}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      $authMsg.textContent = extractError(data, res.status);
      return;
    }
    token = data.token;
    localStorage.setItem("token", token);
    showApp(data.username);
  } catch (err) {
    $authMsg.textContent = "网络错误：" + err.message;
  } finally {
    $authSubmit.disabled = false;
  }
});

// 启动：持 token 则校验有效性（/api/auth/me），否则弹登录门
(async function init() {
  if (!token) { showGate(); return; }
  try {
    const res = await fetch(`${API}/api/auth/me`, { headers: authHeaders() });
    if (res.ok) {
      const me = await res.json();
      showApp(me.username);
    } else {
      if (res.status === 401) localStorage.removeItem("token");
      token = null;
      showGate();
    }
  } catch (_) {
    showGate();   // 网络异常时也先展示登录门
  }
})();

// Tab 切换：在闲聊模式与单轮问答模式之间切换，切换时更新提示文案并清空上下文
// 使用场景：用户点击页面顶部的模式切换按钮时触发
document.querySelectorAll(".tab").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelector(".tab.active").classList.remove("active");
    btn.classList.add("active");
    mode = btn.dataset.mode;
    $hint.textContent = mode === "chat"
      ? "闲聊模式：会记住上下文"
      : "单轮问答：不记历史，每次独立作答";
    if (mode === "ask") append("system", "—— 已进入单轮问答模式，上下文已清空 ——");
  });
});

// 新建会话：重置会话 ID 并清空消息列表，使下一轮闲聊从空白上下文开始
$newBtn.addEventListener("click", () => {
  sessionId = null;
  $list.innerHTML = "";
});

// 绑定发送按钮点击、Enter 快捷键发送（Shift+Enter 换行不发送）、停止按钮中断请求
$send.addEventListener("click", send);
$input.addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } });
$stop.addEventListener("click", () => aborter?.abort());

/**
 * 向消息列表追加一条消息气泡
 * @param {string} role - 消息角色："user"（用户）| "ai"（助手）| "system"（系统提示）
 * @param {string} text - 消息文本内容
 * @returns {HTMLElement} 新创建的消息元素，便于后续流式更新其文本内容
 * 注：ai 气泡的正文放在内部 <span> 里，来源条（.sources）作为兄弟节点共存，
 *     避免流式更新 textContent 时误删已插入的来源 DOM。
 * 使用场景：发送消息时插入用户气泡，收到响应前创建空的 AI 气泡供流式填充
 */
function append(role, text) {
  const div = document.createElement("div");
  div.className = "msg " + role;
  if (role === "system") div.textContent = text;
  else {
    const span = document.createElement("span");
    span.textContent = text;
    div.appendChild(span);
  }
  $list.appendChild(div);
  $list.scrollTop = $list.scrollHeight;
  return div;
}

/** 取气泡里存放正文的 span（ai 气泡专用） */
function bodyOf(box) { return box.querySelector("span"); }

/**
 * 在 AI 气泡顶部渲染 RAG 参考来源列表
 * @param {HTMLElement} box - 当前 AI 消息气泡
 * @param {string[]} sources - 来源标识数组（文件名#块号）
 * 使用场景：单轮问答命中知识库资料时，后端在正文前下发 sources 事件
 */
function renderSources(box, sources) {
  const ref = document.createElement("div");
  ref.className = "sources";
  ref.textContent = "📄 参考：" + sources.join("、");
  box.insertBefore(ref, box.firstChild);
}

/**
 * 发送用户消息并流式接收 AI 回复（核心交互函数）
 * 处理流程：
 *  1. 读取输入框内容，校验非空后禁用输入、切换为"停止"按钮状态
 *  2. 根据当前模式选择接口：chat 模式请求 /api/chat（携带 session_id），
 *     ask 模式请求 /api/ask（仅问题，不带历史）
 *  3. 通过 SSE（Server-Sent Events）流式读取响应，逐段解析 data: 行并实时更新 AI 气泡
 *  4. 结束（正常完成 / 出错 / 用户停止）后恢复输入框与按钮状态
 * 使用场景：点击发送按钮或按 Enter 键时触发
 */
async function send() {
  const text = $input.value.trim();
  if (!text) return;   // 输入为空则忽略
  $input.value = "";
  $input.disabled = true;
  $send.hidden = true;
  $stop.hidden = false;

  append("user", text);
  const aiBox = append("ai", "");

  // 创建中断控制器，用户点击"停止"时可中止正在进行的请求
  const controller = new AbortController();
  aborter = controller;

  // 按模式拼装请求地址与请求体：chat 带会话 ID 以维持上下文，ask 为独立单轮问答
  const url = mode === "chat" ? `${API}/api/chat` : `${API}/api/ask`;
  const body = mode === "chat"
    ? JSON.stringify({ message: text, session_id: sessionId })
    : JSON.stringify({ question: text });

  try {
    const res = await fetch(url, {
      method: "POST",
      headers: authHeaders(),
      body,
      signal: controller.signal,
    });
    // 后端可能在新建会话后返回会话 ID，需记录以便后续请求携带
    sessionId = res.headers.get("X-Session-Id") || sessionId;
    // 会话已过期/失效：清除本地凭证并回到登录门
    if (res.status === 401) { logout(); return; }
    // 其余非 2xx 响应：展示错误状态码与详情，终止本次流程
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      bodyOf(aiBox).textContent = extractError(err, res.status);
      return;
    }

    // 流式读取 SSE 响应：按行拆分，data: 开头的行解析为 JSON 后累加到 AI 气泡
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "", content = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      // 末行可能不完整，保留到缓冲区等待下一个数据块拼接
      const lines = buf.split("\n");
      buf = lines.pop();
      for (const line of lines) {
        const t = line.trim();
        if (!t.startsWith("data: ")) continue;   // 跳过空行及非数据行（如 event: 行）
        const data = t.slice(6);
        if (data === "[DONE]") continue;         // 流结束标记，无需处理
        try {
          const obj = JSON.parse(data);
          if (obj.error) { bodyOf(aiBox).textContent += `\n[错误] ${obj.error}`; continue; }
          if (obj.sources) { renderSources(aiBox, obj.sources); continue; }   // RAG 来源列表，先于正文下发
          if (obj.content) { content += obj.content; bodyOf(aiBox).textContent = content; $list.scrollTop = $list.scrollHeight; }
          // reasoning 字段可留到 v1.1 再展示
        } catch (_) {}
      }
    }
  } catch (e) {
    // 区分用户主动停止（AbortError，保留已生成内容）与真正的网络异常
    if (e.name !== "AbortError") bodyOf(aiBox).textContent = "网络错误：" + e.message;
    else bodyOf(aiBox).textContent += "\n[已停止]";
  } finally {
    // 无论成功、失败或中断，均恢复输入与按钮状态，并将焦点还给输入框
    $input.disabled = false;
    $send.hidden = false;
    $stop.hidden = true;
    aborter = null;
    $input.focus();
  }
}