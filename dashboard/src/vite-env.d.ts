/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Order API origin only. Unset → same-origin `/snapshot` via the Vite proxy. */
  readonly VITE_API_BASE_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
