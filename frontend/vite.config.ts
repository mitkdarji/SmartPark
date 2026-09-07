import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Point the dev proxy elsewhere with VITE_API_TARGET when port 8000 is taken:
//   VITE_API_TARGET=http://127.0.0.1:8021 npm run dev
const API_TARGET = process.env.VITE_API_TARGET ?? 'http://127.0.0.1:8000'
const WS_TARGET = API_TARGET.replace(/^http/, 'ws')

export default defineConfig({
  plugins: [react()],
  server: {
    port: Number(process.env.PORT ?? 5173),
    proxy: {
      // Proxy the API and WebSockets in development so the browser sees a
      // single origin and CORS never enters the picture.
      '/api': { target: API_TARGET, changeOrigin: true },
      '/ws': { target: WS_TARGET, ws: true },
      '/media': { target: API_TARGET, changeOrigin: true },
      '/health': { target: API_TARGET, changeOrigin: true },
      '/docs': { target: API_TARGET, changeOrigin: true },
      '/openapi.json': { target: API_TARGET, changeOrigin: true },
    },
  },
  build: { outDir: 'dist', sourcemap: false, chunkSizeWarningLimit: 900 },
})
