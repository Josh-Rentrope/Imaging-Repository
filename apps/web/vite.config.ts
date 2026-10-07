import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const API_TARGET = process.env.VITE_API_TARGET ?? 'http://127.0.0.1:8787'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5273,
    proxy: {
      // Keeps the browser same-origin in dev, so no CORS preflight and the
      // production topology (single origin, API behind a path) is what you
      // develop against.
      '/api': {
        target: API_TARGET,
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
    },
  },
  build: {
    // vtk.js is ~1.5 MB and is only needed once there is something to render, so
    // it gets its own chunk and is loaded on demand by the viewport.
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (id.includes('node_modules') && id.includes('@kitware/vtk.js')) return 'vtk'
          return undefined
        },
      },
    },
    chunkSizeWarningLimit: 1500,
  },
})
