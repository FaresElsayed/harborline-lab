import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import '@fontsource/hanken-grotesk/400.css';
import '@fontsource/hanken-grotesk/500.css';
import '@fontsource/hanken-grotesk/600.css';
import '@fontsource/hanken-grotesk/700.css';
import '@fontsource/archivo-narrow/500.css';
import '@fontsource/archivo-narrow/600.css';
import '@fontsource/jetbrains-mono/400.css';
import '@fontsource/jetbrains-mono/500.css';
import './styles/brand-fallback.css';
import 'virtual:brand-tokens.css';
import './styles/app.css';
import { App } from './App';

async function start() {
  if (import.meta.env.VITE_MOCK === '1') {
    const { installMocks } = await import('./mock');
    installMocks();
  }
  createRoot(document.getElementById('root')!).render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
}

void start();
