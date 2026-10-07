/// <reference types="vite/client" />

/**
 * Build-time configuration.
 *
 * Declared explicitly rather than relying on the index signature `vite/client`
 * provides, which types every variable as `any` -- so `VITE_API_BSE` would
 * compile and then silently be `undefined` at runtime, falling back to a
 * relative path and 404ing every request on a deployment that is not
 * same-origin. The whole point of naming it here is that a typo is a build error.
 */
interface ImportMetaEnv {
  /**
   * Where the API lives, e.g. `https://api.example.com`.
   *
   * Unset means the app's own origin behind `/api`, which is what the Vite dev
   * proxy serves and what a same-origin deployment wants.
   */
  readonly VITE_API_BASE?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
