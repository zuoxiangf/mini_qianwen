// 后端 API 访问层：统一路径前缀、鉴权头、错误信息提取
// 开发环境经 Vite proxy、生产环境经 Nginx 反代，/api 均为同源相对路径，无需按域名切换地址
export const API = ''

/** 所有需鉴权的请求统一使用；带 Content-Type 与 Authorization 头 */
export function authHeaders(token, extra) {
  return Object.assign(
    { 'Content-Type': 'application/json' },
    token ? { Authorization: 'Bearer ' + token } : {},
    extra || {},
  )
}

/**
 * 统一提取后端错误信息：兼容字符串 detail 与 Pydantic 422 校验错误的数组 detail，
 * 否则数组被直接渲染会显示成 [object Object]
 */
export function extractError(data, status) {
  const d = data && data.detail
  if (typeof d === 'string') return d
  if (Array.isArray(d) && d.length) {
    const first = d[0]
    const field = (first.loc || []).slice(-1)[0] // loc 末位是字段名，如 "username"
    const msg = first.msg || '参数不合法'
    if (first.type === 'string_too_short' && /at least (\d+)/.test(msg)) {
      return `${field || '字段'}太短，至少需要 ${RegExp.$1} 个字符`
    }
    return (field ? field + ': ' : '') + msg
  }
  return `请求失败 (${status})`
}

// 工具名 → 用户可读的中文提示（后端 SSE 下发 {"tool": {name, args}} 事件时使用）
export const TOOL_LABELS = {
  get_weather: '🌤️ 正在查天气',
  get_user_info: '👤 正在查用户信息',
  search_chat_history: '🔍 正在检索历史对话',
}
