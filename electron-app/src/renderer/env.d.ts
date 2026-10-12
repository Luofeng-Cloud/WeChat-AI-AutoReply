import type { RendererApi } from "../preload/index";

declare global {
  interface Window {
    /** Injected by preload via contextBridge; the only renderer capability surface. */
    engine: RendererApi;
  }
}

export {};
