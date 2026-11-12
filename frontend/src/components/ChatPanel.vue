<!-- 主聊天界面：模式切换（闲聊/单轮问答）、消息列表、SSE 流式接收与停止生成 -->
<script setup>
import { computed, nextTick, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { API, authHeaders, extractError, TOOL_LABELS } from '../api.js'

const props = defineProps({
  username: { type: String, required: true },
  token: { type: String, default: '' },
})
const emit = defineEmits(['logout'])

const mode = ref('chat')        // chat（闲聊，带上下文）| ask（单轮问答，无历史）
const input = ref('')
const streaming = ref(false)    // 是否正在流式接收 AI 回复
const sessionId = ref(null)     // 闲聊会话 ID，由后端通过 X-Session-Id 响应头返回
const messages = reactive([])   // 消息数组：{ role: user|ai|system, content, sources[], tools[] }
const scrollbarRef = ref()
const inputRef = ref()
let aborter = null              // 当前请求的 AbortController，用于停止生成

const hint = computed(() =>
  mode.value === 'chat' ? '闲聊模式：会记住上下文' : '单轮问答：不记历史，每次独立作答')

/** 滚动消息区到底部（流式更新与新消息插入时调用） */
function scrollToBottom() {
  nextTick(() => {
    const sb = scrollbarRef.value
    if (sb) sb.setScrollTop(999999)
  })
}

/** 切换模式：生成中禁止切换避免上下文错乱；进入单轮问答时插入系统提示 */
function switchMode(m) {
  if (m === mode.value) return
  if (streaming.value) { ElMessage.info('请等待当前回复生成完毕'); return }
  mode.value = m
  if (m === 'ask') {
    messages.push({ role: 'system', content: '—— 已进入单轮问答模式，上下文已清空 ——' })
    scrollToBottom()
  }
}

/** 新建会话：重置会话 ID 并清空消息列表，下一轮闲聊从空白上下文开始 */
function newChat() {
  if (streaming.value) { ElMessage.info('请等待当前回复生成完毕'); return }
  sessionId.value = null
  messages.splice(0)
}

async function logout() {
  try {
    await ElMessageBox.confirm('确定退出登录吗？', '提示', { type: 'warning', confirmButtonText: '退出', cancelButtonText: '取消' })
  } catch (_) { return }   // 用户取消
  emit('logout')
}

/** 工具事件 → 可读中文提示行：优先查表，再从参数里挑最有信息量的字段展示 */
function toolBrief(tool) {
  let label = TOOL_LABELS[tool.name] || ('🔧 正在调用工具：' + tool.name)
  try {
    const a = JSON.parse(tool.args || '{}')
    const brief = a.city || a.keyword || a.username
    if (brief) label += ' · ' + brief
  } catch (_) {}
  return label
}

/**
 * 发送用户消息并流式接收 AI 回复（核心交互函数）
 * 流程：chat 模式请求 /api/chat（携带 session_id），ask 模式请求 /api/ask（仅问题）；
 * 通过 SSE 逐段解析 data: 行实时更新气泡；error/sources/tool/content 四类事件分别处理；
 * 结束（完成 / 出错 / 停止）后恢复输入状态
 */
async function send() {
  const text = input.value.trim()
  if (!text || streaming.value) return
  input.value = ''
  streaming.value = true

  messages.push({ role: 'user', content: text, sources: [], tools: [] })
  const aiMsg = reactive({ role: 'ai', content: '', sources: [], tools: [] })
  messages.push(aiMsg)
  scrollToBottom()

  const controller = new AbortController()
  aborter = controller

  const url = mode.value === 'chat' ? `${API}/api/chat` : `${API}/api/ask`
  const body = mode.value === 'chat'
    ? JSON.stringify({ message: text, session_id: sessionId.value })
    : JSON.stringify({ question: text })

  try {
    const res = await fetch(url, {
      method: 'POST',
      headers: authHeaders(props.token),
      body,
      signal: controller.signal,
    })
    // 后端可能在新建会话后返回会话 ID，需记录以便后续请求携带
    sessionId.value = res.headers.get('X-Session-Id') || sessionId.value
    // token 过期：清除凭证回到登录门
    if (res.status === 401) {
      ElMessage.warning('登录已过期，请重新登录')
      emit('logout')
      return
    }
    if (!res.ok) {
      const err = await res.json().catch(() => ({}))
      aiMsg.content = extractError(err, res.status)
      return
    }

    // 流式读取 SSE 响应：按行拆分，data: 行解析为 JSON 后分发到对应渲染字段
    const reader = res.body.getReader()
    const decoder = new TextDecoder()
    let buf = ''
    let content = ''

    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buf += decoder.decode(value, { stream: true })
      // 末行可能不完整，保留到缓冲区等待下一个数据块拼接
      const lines = buf.split('\n')
      buf = lines.pop()
      for (const line of lines) {
        const t = line.trim()
        if (!t.startsWith('data: ')) continue   // 跳过空行及非数据行（如 event: 行）
        const data = t.slice(6)
        if (data === '[DONE]') continue         // 流结束标记，无需处理
        try {
          const obj = JSON.parse(data)
          if (obj.error) { content += `\n[错误] ${obj.error}`; aiMsg.content = content; continue }
          if (obj.sources) { aiMsg.sources.push(...obj.sources); continue }   // RAG 来源列表，先于正文下发
          if (obj.tool) { aiMsg.tools.push(toolBrief(obj.tool)); continue }   // 工具调用提示（Function Calling）
          if (obj.content) {
            content += obj.content
            aiMsg.content = content
            scrollToBottom()
          }
          // reasoning 字段可留到 v1.1 再展示
        } catch (_) {}
      }
    }
  } catch (e) {
    // 区分用户主动停止（AbortError，保留已生成内容）与真正的网络异常
    if (e.name !== 'AbortError') aiMsg.content = '网络错误：' + e.message
    else aiMsg.content += '\n[已停止]'
  } finally {
    streaming.value = false
    aborter = null
    nextTick(() => inputRef.value?.focus())
  }
}

/** 点击停止按钮：中断当前流式请求，已生成内容保留在气泡中 */
function stop() {
  aborter?.abort()
}

onMounted(() => inputRef.value?.focus())
</script>

<template>
  <el-container class="chat-app">
    <el-header class="app-header" height="56px">
      <div class="header-left">
        <el-icon :size="22" color="var(--el-color-primary)"><ChatDotRound /></el-icon>
        <span class="brand">Mini Qwen</span>
        <el-radio-group :model-value="mode" size="small" @update:model-value="switchMode">
          <el-radio-button value="chat">💬 闲聊</el-radio-button>
          <el-radio-button value="ask">❓ 单轮问答</el-radio-button>
        </el-radio-group>
      </div>
      <div class="header-right">
        <el-button :icon="'Plus'" plain size="small" :disabled="streaming" @click="newChat">新对话</el-button>
        <el-dropdown trigger="click">
          <span class="user-entry">
            <el-avatar :size="28">{{ props.username[0]?.toUpperCase() }}</el-avatar>
            <el-icon><ArrowDown /></el-icon>
          </span>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item disabled>👤 {{ props.username }}</el-dropdown-item>
              <el-dropdown-item :icon="'SwitchButton'" divided @click="logout">退出登录</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </div>
    </el-header>

    <el-main class="msg-main">
      <el-scrollbar ref="scrollbarRef" class="msg-scroll">
        <div class="msg-list">
          <template v-for="(m, i) in messages" :key="i">
            <!-- 系统提示：居中灰字 -->
            <div v-if="m.role === 'system'" class="msg-system">
              <el-text type="info" size="small">{{ m.content }}</el-text>
            </div>
            <!-- 用户 / AI 消息气泡 -->
            <div v-else class="msg-row" :class="m.role">
              <el-avatar v-if="m.role === 'ai'" :size="32" class="avatar ai-avatar" :icon="'Cpu'" />
              <div class="bubble" :class="m.role">
                <el-tag
                  v-for="(t, ti) in m.tools" :key="'t' + ti"
                  type="info" size="small" class="tag-line" effect="plain"
                >{{ t }}</el-tag>
                <el-tag
                  v-for="(s, si) in m.sources" :key="'s' + si"
                  type="success" size="small" class="tag-line" effect="light"
                >📄 参考：{{ s }}</el-tag>
                <div v-if="m.content" class="content">{{ m.content }}</div>
                <div v-else-if="m.role === 'ai' && streaming" class="content pending">
                  <el-icon class="is-loading"><Loading /></el-icon> 正在生成…
                </div>
              </div>
              <el-avatar v-if="m.role === 'user'" :size="32" class="avatar user-avatar" :icon="'User'" />
            </div>
          </template>
          <el-empty v-if="!messages.length" description="发一条消息开始对话吧" :image-size="80" />
        </div>
      </el-scrollbar>
    </el-main>

    <el-footer class="app-footer" height="auto">
      <div class="composer">
        <el-input
          ref="inputRef"
          v-model="input"
          type="textarea"
          :autosize="{ minRows: 2, maxRows: 6 }"
          resize="none"
          placeholder="输入消息…（Enter 发送，Shift+Enter 换行）"
          :disabled="streaming"
          @keydown.enter.exact.prevent="send"
        />
        <div class="actions">
          <el-text type="info" size="small">{{ hint }}</el-text>
          <el-button v-if="!streaming" type="primary" :icon="'Promotion'" :disabled="!input.trim()" @click="send">发送</el-button>
          <el-button v-else type="danger" :icon="'CircleStop'" plain @click="stop">停止</el-button>
        </div>
      </div>
    </el-footer>
  </el-container>
</template>

<style scoped>
.chat-app {
  height: 100vh;
  max-width: 960px;
  margin: 0 auto;
  background: var(--el-bg-color);
  box-shadow: 0 0 12px var(--el-box-shadow-light);
}
.app-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  border-bottom: 1px solid var(--el-border-color-light);
}
.header-left,
.header-right {
  display: flex;
  align-items: center;
  gap: 12px;
}
.brand {
  font-size: 18px;
  font-weight: 600;
  color: var(--el-color-primary);
}
.user-entry {
  display: flex;
  align-items: center;
  gap: 4px;
  cursor: pointer;
  outline: none;
  color: var(--el-text-color-regular);
}
.msg-main {
  padding: 0;
  overflow: hidden;
}
.msg-scroll {
  height: 100%;
}
.msg-list {
  padding: 16px 20px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.msg-system {
  text-align: center;
}
.msg-row {
  display: flex;
  align-items: flex-start;
  gap: 10px;
}
.msg-row.user {
  justify-content: flex-end;
}
.avatar {
  flex-shrink: 0;
}
.ai-avatar {
  background: var(--el-color-primary-light-7);
  color: var(--el-color-primary);
}
.user-avatar {
  background: var(--el-color-success-light-7);
  color: var(--el-color-success);
}
.bubble {
  max-width: 72%;
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px 14px;
  border-radius: 12px;
  line-height: 1.6;
  word-break: break-word;
}
.bubble.ai {
  background: var(--el-fill-color-light);
  border-top-left-radius: 4px;
}
.bubble.user {
  background: var(--el-color-primary);
  color: #fff;
  border-top-right-radius: 4px;
}
.bubble.user .content {
  white-space: pre-wrap;
}
.tag-line {
  align-self: flex-start;
  max-width: 100%;
  white-space: normal;
  height: auto;
  line-height: 1.5;
  padding-top: 2px;
  padding-bottom: 2px;
}
.content {
  white-space: pre-wrap;
  margin: 0;
}
.content.pending {
  display: flex;
  align-items: center;
  gap: 6px;
  color: var(--el-text-color-secondary);
}
.app-footer {
  border-top: 1px solid var(--el-border-color-light);
  padding: 12px 20px 16px;
}
.actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 12px;
  margin-top: 8px;
}
</style>
