// 后端 API 服务地址：本地开发（file:// 或 localhost/127.0.0.1）直连 8000；线上按 IP/域名访问，与页面同域由 Nginx 反代 /api/
const API = location.protocol === "file:" || ["localhost", "127.0.0.1"].includes(location.hostname)
  ? "http://localhost:8000"
  : "";

let mode = "chat";          // 当前模式：chat（闲聊，带上下文）| ask（单轮问答，无历史）
let sessionId = null;       // 闲聊会话 ID，由后端通过 X-Session-Id 响应头返回
let aborter = null;         // 当前请求的 AbortController，用于停止生成

// 页面核心 DOM 元素引用

const $list  = document.getElementById("list");
const $input = document.getElementById("input");
const $send  = document.getElementById("btnSend");
const $stop  = document.getElementById("btnStop");
const $hint  = document.getElementById("hint");
const $newBtn = document.getElementById("btnNew");

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
 * 使用场景：发送消息时插入用户气泡，收到响应前创建空的 AI 气泡供流式填充
 */
function append(role, text) {
  const div = document.createElement("div");
  div.className = "msg " + role;
  if (role !== "system") div.textContent = text;
  else div.textContent = text;
  $list.appendChild(div);
  $list.scrollTop = $list.scrollHeight;
  return div;
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
      headers: { "Content-Type": "application/json" },
      body,
      signal: controller.signal,
    });
    // 后端可能在新建会话后返回会话 ID，需记录以便后续请求携带
    console.log("Session ID before:", res)
    sessionId = res.headers.get("X-Session-Id") || sessionId;
    console.log("Session ID:", sessionId)
    // 非 2xx 响应：展示错误状态码与详情，终止本次流程
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      aiBox.textContent = `请求失败 (${res.status})：${err.detail || res.statusText}`;
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
          if (obj.error) { aiBox.textContent += `\n[错误] ${obj.error}`; continue; }
          if (obj.content) { content += obj.content; aiBox.textContent = content; $list.scrollTop = $list.scrollHeight; }
          // reasoning 字段可留到 v1.1 再展示
        } catch (_) {}
      }
    }
  } catch (e) {
    // 区分用户主动停止（AbortError，保留已生成内容）与真正的网络异常
    if (e.name !== "AbortError") aiBox.textContent = "网络错误：" + e.message;
    else aiBox.textContent += "\n[已停止]";
  } finally {
    // 无论成功、失败或中断，均恢复输入与按钮状态，并将焦点还给输入框
    $input.disabled = false;
    $send.hidden = false;
    $stop.hidden = true;
    aborter = null;
    $input.focus();
  }
}