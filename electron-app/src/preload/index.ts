import { contextBridge, ipcRenderer } from "electron";

import type { EngineMethodName, EngineMethods } from "../shared/protocol";

/**
 * The only surface the renderer sees.
 *
 * `contextIsolation` is on and `nodeIntegration` is off, so the UI cannot touch the
 * filesystem or spawn anything; every capability it has is a named method here. The
 * channel name is fixed by the preload, so a renderer bug cannot address an arbitrary
 * IPC channel.
 */

const IPC = {
  invoke: "engine:invoke",
  onEvent: "engine:event",
  engineState: "engine:state",
  pythonStatus: "engine:python-status",
  pythonInstall: "engine:python-install",
  pythonChoose: "engine:python-choose",
  windowControl: "app:window",
  openExternal: "app:open-external",
  configExport: "app:config-export",
  configImport: "app:config-import",
  screenshotSave: "app:screenshot-save",
  appInfo: "app:info",
} as const;

type Unsubscribe = () => void;

function subscribe<T>(channel: string, handler: (payload: T) => void): Unsubscribe {
  const listener = (_event: unknown, payload: T) => handler(payload);
  ipcRenderer.on(channel, listener);
  return () => ipcRenderer.removeListener(channel, listener);
}

const api = {
  /** Invoke a Python engine method. Rejects with the engine error message. */
  invoke<K extends EngineMethodName>(method: K, params?: EngineMethods[K][0]): Promise<EngineMethods[K][1]> {
    return ipcRenderer.invoke(IPC.invoke, method, params ?? {}) as Promise<EngineMethods[K][1]>;
  },

  onEngineEvent(handler: (method: string, params: unknown) => void): Unsubscribe {
    return subscribe(IPC.onEvent, (payload: { method: string; params: unknown }) =>
      handler(payload.method, payload.params));
  },

  onEngineState(handler: (state: string, detail?: string) => void): Unsubscribe {
    return subscribe(IPC.engineState, (payload: { state: string; detail?: string }) =>
      handler(payload.state, payload.detail));
  },

  pythonStatus: () => ipcRenderer.invoke(IPC.pythonStatus),
  pythonInstall: (candidate: unknown) => ipcRenderer.invoke(IPC.pythonInstall, candidate),
  pythonChoose: () => ipcRenderer.invoke(IPC.pythonChoose),

  window: (action: "minimize" | "maximize" | "close" | "toggle-devtools") =>
    ipcRenderer.invoke(IPC.windowControl, { action }),

  openExternal: (url: string) => ipcRenderer.invoke(IPC.openExternal, url),
  exportConfig: (document: unknown) => ipcRenderer.invoke(IPC.configExport, document),
  importConfig: () => ipcRenderer.invoke(IPC.configImport),
  saveScreenshot: (dataUrl: string, suggested: string) =>
    ipcRenderer.invoke(IPC.screenshotSave, dataUrl, suggested),
  appInfo: () => ipcRenderer.invoke(IPC.appInfo),

  /** Dev-only: render a page and return it as a PNG. Used by scripts/smoke.mjs. */
  capturePage: (page: string, outPath: string) =>
    ipcRenderer.invoke("app:capture-page", page, outPath),
} as const;

export type RendererApi = typeof api;

contextBridge.exposeInMainWorld("engine", api);
