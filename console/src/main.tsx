import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { initTheme } from './lib/theme'
import { captureSetupLink } from './lib/setupLink'

// First: a setup link's token leaves the address bar before anything renders
// or fetches.
captureSetupLink(import.meta.env.BASE_URL)
initTheme()

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
