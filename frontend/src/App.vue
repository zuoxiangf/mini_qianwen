<!-- 根组件：启动时校验本地 token（/api/auth/me），按登录态切换登录门 / 主界面 -->
<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import AuthPanel from './components/AuthPanel.vue'
import ChatPanel from './components/ChatPanel.vue'
import { API, authHeaders } from './api.js'

const token = ref(localStorage.getItem('token') || '')
const username = ref('')
// 持有 token 时先显示"校验中"遮罩，避免登录门一闪而过
const checking = ref(!!token.value)

onMounted(async () => {
  if (!token.value) return
  try {
    const res = await fetch(`${API}/api/auth/me`, { headers: authHeaders(token.value) })
    if (res.ok) {
      username.value = (await res.json()).username
    } else {
      // token 过期/无效：清除本地凭证回登录门；网络异常也先展示登录门
      if (res.status === 401) {
        localStorage.removeItem('token')
        token.value = ''
      }
    }
  } catch (_) {
    /* 网络异常：静默回登录门 */
  } finally {
    checking.value = false
  }
})

/** 登录/注册成功：存 token 并进入主界面 */
function onLoginSuccess({ token: t, username: u }) {
  token.value = t
  username.value = u
  localStorage.setItem('token', t)
  ElMessage.success(u ? `欢迎，${u}` : '登录成功')
}

/** 登出（含流式请求收到 401 的"登录已过期"场景）：清除凭证回到登录门 */
function onLogout() {
  localStorage.removeItem('token')
  token.value = ''
  username.value = ''
}
</script>

<template>
  <div v-if="checking" class="boot-mask">
    <el-icon class="is-loading" :size="28"><Loading /></el-icon>
    <span>正在校验登录状态…</span>
  </div>
  <AuthPanel v-else-if="!username" @success="onLoginSuccess" />
  <ChatPanel v-else :username="username" :token="token" @logout="onLogout" />
</template>

<style scoped>
.boot-mask {
  height: 100vh;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 12px;
  color: var(--el-text-color-secondary);
}
</style>
