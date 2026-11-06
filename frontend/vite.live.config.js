// WAREXX Live (frontend/live): the signed-in app on the real backend, built for
// /live/. In development, /live/api and /live/pos are proxied to live-api
// (LIVE_API_URL, default http://127.0.0.1:8010); see live-api/README.md.
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const here = (p) => path.resolve(path.dirname(fileURLToPath(import.meta.url)), p)
const target = process.env.LIVE_API_URL || 'http://127.0.0.1:8010'

export default defineConfig({
  plugins: [react()],
  base: '/live/',
  root: here('./live'),
  envDir: here('..'),
  publicDir: false,
  build: { outDir: process.env.LIVE_OUT || here('./dist/live'), emptyOutDir: true },
  server: {
    port: 8004,
    strictPort: true,
    fs: { allow: [here('.')] },
    proxy: { '/live/api': { target, changeOrigin: false }, '/live/pos': { target, changeOrigin: false } },
  },
})
