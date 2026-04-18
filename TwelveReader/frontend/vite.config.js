import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const backendUrl = process.env.BACKEND_URL || 'http://localhost:8000'
const proxy = {
  '/api':    { target: backendUrl, changeOrigin: true },
  '/cache':  { target: backendUrl, changeOrigin: true },
  '/audio':  { target: backendUrl, changeOrigin: true },
  '/health': { target: backendUrl, changeOrigin: true },
}

export default defineConfig({
  plugins: [react()],
  server: {
    allowedHosts: true,
    proxy,
  },
  preview: {
    allowedHosts: true,
    proxy,
  },
})
