<!-- 登录/注册面板：未登录时全屏展示，成功后向父组件回传 token 与用户名 -->
<script setup>
import { reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { API, extractError } from '../api.js'

const emit = defineEmits(['success'])

const tab = ref('login')          // 当前页签：login | register
const formRef = ref()
const form = reactive({ username: '', password: '' })
const loading = ref(false)

// 校验规则与后端 Pydantic Field(min_length=...) 保持一致（用户名 3~32，密码 ≥6）
const rules = {
  username: [
    { required: true, message: '请输入用户名', trigger: 'blur' },
    { min: 3, max: 32, message: '用户名需为 3~32 个字符', trigger: 'blur' },
  ],
  password: [
    { required: true, message: '请输入密码', trigger: 'blur' },
    { min: 6, message: '密码至少 6 位', trigger: 'blur' },
  ],
}

/** 切换登录/注册页签：清空密码与旧校验提示 */
function onTabChange() {
  form.password = ''
  formRef.value?.clearValidate()
}

/** 提交登录/注册：先走 ElForm 前置校验，再请求后端；后端 422/400 错误经 extractError 透出 */
async function submit() {
  const valid = await formRef.value.validate().catch(() => false)
  if (!valid) return
  loading.value = true
  try {
    const res = await fetch(`${API}/api/auth/${tab.value}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: form.username.trim(), password: form.password }),
    })
    const data = await res.json().catch(() => ({}))
    if (!res.ok) {
      ElMessage.error(extractError(data, res.status))
      return
    }
    emit('success', { token: data.token, username: data.username })
  } catch (err) {
    ElMessage.error('网络错误：' + err.message)
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="auth-wrap">
    <el-card class="auth-card" shadow="always">
      <div class="auth-title">Mini Qwen</div>
      <el-tabs v-model="tab" stretch class="auth-tabs" @tab-change="onTabChange">
        <el-tab-pane label="登录" name="login" />
        <el-tab-pane label="注册" name="register" />
      </el-tabs>
      <el-form ref="formRef" :model="form" :rules="rules" label-position="top" @submit.prevent="submit">
        <el-form-item prop="username">
          <el-input
            v-model="form.username"
            placeholder="用户名"
            :prefix-icon="'User'"
            autocomplete="username"
            maxlength="32"
            clearable
          />
        </el-form-item>
        <el-form-item prop="password">
          <el-input
            v-model="form.password"
            type="password"
            placeholder="密码（至少 6 位）"
            :prefix-icon="'Lock'"
            show-password
            :autocomplete="tab === 'login' ? 'current-password' : 'new-password'"
            @keyup.enter="submit"
          />
        </el-form-item>
        <el-button type="primary" class="auth-submit" :loading="loading" native-type="submit">
          {{ tab === 'login' ? '登录' : '注册' }}
        </el-button>
      </el-form>
    </el-card>
  </div>
</template>

<style scoped>
.auth-wrap {
  height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(160deg, var(--el-color-primary-light-9), var(--el-fill-color-light));
}
.auth-card {
  width: 380px;
  border-radius: 12px;
}
.auth-title {
  text-align: center;
  font-size: 24px;
  font-weight: 600;
  margin-bottom: 8px;
  color: var(--el-color-primary);
}
.auth-tabs {
  margin-bottom: 8px;
}
.auth-submit {
  width: 100%;
  margin-top: 4px;
}
</style>
