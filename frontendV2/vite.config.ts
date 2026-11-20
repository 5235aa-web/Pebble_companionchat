import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'

// Stamped into the bundle so the boot probe can prove which build is running.
// Without it a cached index.html silently serves the previous bundle and every
// fix looks like it did nothing.
const buildId = Date.now().toString(36)

export default defineConfig({
  define: { __BUILD_ID__: JSON.stringify(buildId) },
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5176,
    strictPort: false,
    proxy: {
      '/api': {
        target: 'http://localhost:8002',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
    },
  },
})
