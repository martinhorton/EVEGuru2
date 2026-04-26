import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': {
        target: 'https://192.168.135.168:8443',
        secure: false,       // allow self-signed cert
        changeOrigin: true
      }
    }
  }
})
