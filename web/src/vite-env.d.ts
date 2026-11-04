/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** "1" serves /api/* from the in-browser mock (src/mock). */
  readonly VITE_MOCK?: string;
  /** "1" in the public demo build (.env.demo): hides what needs the backend and shows the demo notice. */
  readonly VITE_DEMO?: string;
  /** Where the demo notice links to. */
  readonly VITE_REPO_URL?: string;
}
interface ImportMeta {
  readonly env: ImportMetaEnv;
}

declare module 'virtual:brand-tokens.css';
