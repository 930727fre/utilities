import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    allowedHosts: true,
    proxy: {
      '/api':    { target: process.env.BACKEND_URL || 'http://localhost:8000', changeOrigin: true },
      '/cache':  { target: process.env.BACKEND_URL || 'http://localhost:8000', changeOrigin: true },
      '/audio':  { target: process.env.BACKEND_URL || 'http://localhost:8000', changeOrigin: true },
      '/health': { target: process.env.BACKEND_URL || 'http://localhost:8000', changeOrigin: true },
    },
  },
})
