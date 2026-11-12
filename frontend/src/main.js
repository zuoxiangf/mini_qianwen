// 应用入口：注册 Element Plus（中文语言包）与图标，挂载根组件
import { createApp } from 'vue'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import 'element-plus/dist/index.css'
import * as ElementPlusIconsVue from '@element-plus/icons-vue'
import App from './App.vue'
import './style.css'

const app = createApp(App)

// 全局注册所有图标组件（模板里可直接 <el-icon><ChatDotRound /></el-icon> 使用）
for (const [name, comp] of Object.entries(ElementPlusIconsVue)) {
  app.component(name, comp)
}

app.use(ElementPlus, { locale: zhCn })
app.mount('#app')
