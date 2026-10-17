import { defineConfig, type Connect, type Plugin } from 'vite'
import react from '@vitejs/plugin-react'

// The bare base (`/ui`, `/ui?token=...` — a setup link's short form) into the
// app with its query, as the console's nginx does. Vite only serves `/ui/`.
const bareBaseRedirect: Connect.NextHandleFunction = (req, res, next) => {
  const url = req.url ?? ''
  if (url === '/ui' || url.startsWith('/ui?')) {
    res.statusCode = 302
    res.setHeader('Location', `/ui/${url.slice(3)}`)
    res.end()
    return
  }
  next()
}

const bareBase: Plugin = {
  name: 'sinas-bare-base-redirect',
  configureServer: (server) => { server.middlewares.use(bareBaseRedirect) },
  configurePreviewServer: (server) => { server.middlewares.use(bareBaseRedirect) },
}

// https://vite.dev/config/
export default defineConfig({
  // The console is served under /ui so the ingress can route by path with no
  // per-endpoint allowlist: /ui -> console, everything else -> backend.
  base: '/ui/',
  plugins: [react(), bareBase],
  resolve: {
    dedupe: ['react', 'react-dom'],
  },
  server: {
    port: 51245,
  },
})
