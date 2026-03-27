import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')

  return {
    base: env.VITE_PUBLIC_BASE || '/',
    plugins: [vue()],
    test: {
      globals: true,
      environment: 'jsdom',
      setupFiles: []
    }
  }
})
