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
    // three.js + Cornerstone are both large; splitting them keeps the initial
    // route fast and stops a viewer change from invalidating the app bundle.
    // Written as a function because Rollup's object form is awkward to type
    // against the package ids these two pull in transitively.
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (id.includes('node_modules')) {
            if (id.includes('/three/') || id.includes('@react-three')) return 'three'
            if (id.includes('@cornerstonejs')) return 'cornerstone'
          }
          return undefined
        },
      },
    },
    chunkSizeWarningLimit: 1500,
  },
})
