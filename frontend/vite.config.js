import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// base: './' 让构建产物可被 Nginx 从任意子路径托管（当前部署在 /var/www/mini_qianwen 根路径）
export default defineConfig({
  plugins: [vue()],
  base: './',
  build: {
    // Element Plus 全量引入体积较大，拆成独立 vendor chunk 提升缓存命中率
    rollupOptions: {
      output: {
        manualChunks: {
          'element-plus': ['element-plus', '@element-plus/icons-vue'],
          vue: ['vue'],
        },
      },
    },
  },
  server: {
    port: 5173,
    // 本地开发：/api 请求代理到后端 8000，与线上 Nginx 反代行为一致，前端代码统一用相对路径
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
})
