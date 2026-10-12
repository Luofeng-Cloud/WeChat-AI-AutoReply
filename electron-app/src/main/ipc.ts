import { BrowserWindow, app, dialog, ipcMain, shell } from "electron";
import { readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import type { EngineMethodName } from "../shared/protocol";
import { EngineError, EngineManager } from "./engine";
import { appRoot, discoverPython, installRequirements, PythonProbe } from "./python-env";

/** Channels the renderer may use. Anything not listed here is unreachable. */
export const IPC = {
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

export interface WindowControlRequest { action: "minimize" | "maximize" | "close" | "toggle-devtools"; }

export function registerIpc(engine: EngineManager, getWindow: () => BrowserWindow | null): void {
  // -- engine invocation ----------------------------------------------------
  ipcMain.handle(IPC.invoke, async (_event, method: string, params: unknown) => {
    if (typeof method !== "string") throw new Error("method must be a string");
    return engine.invoke(method as EngineMethodName, params as never);
  });

  // -- interpreter management ----------------------------------------------
  ipcMain.handle(IPC.pythonStatus, async () => {
    if (engine.python?.ok) return { ok: true, interpreter: engine.python };
    const discovery = discoverPython();
    if (discovery.interpreter) {
      engine.setInterpreter(discovery.interpreter);
      return { ok: true, interpreter: discovery.interpreter };
    }
    return {
      ok: false,
      interpreter: null,
      incomplete: discovery.incomplete,
      attempts: discovery.attempts,
    };
  });

  ipcMain.handle(IPC.pythonInstall, async (_event, candidate: PythonProbe | null) => {
    const probe = candidate ?? engine.python;
    if (!probe) throw new Error("未指定 Python 解释器");
    const requirements = join(appRoot(), "requirements.txt");
    const lines: string[] = [];
    const result = await installRequirements(
      { command: probe.command, args: probe.args ?? [], source: probe.source ?? "manual" },
      requirements,
      (line) => {
        lines.push(line);
        getWindow()?.webContents.send(IPC.onEvent, "engine.install-log", { line });
      },
    );
    if (result.code !== 0) {
      return { ok: false, code: result.code, error: result.error ?? lines.slice(-3).join("\n") };
    }
    // Re-probe so the caller learns the new state instead of assuming success.
    const discovery = discoverPython();
    if (discovery.interpreter) engine.setInterpreter(discovery.interpreter);
    return { ok: Boolean(discovery.interpreter), interpreter: discovery.interpreter, log: lines.slice(-40) };
  });

  ipcMain.handle(IPC.pythonChoose, async () => {
    const window = getWindow();
    const picked = await dialog.showOpenDialog(window ?? undefined!, {
      title: "选择 Python 解释器",
      properties: ["openFile"],
      filters: process.platform === "win32" ? [{ name: "Python", extensions: ["exe"] }] : [],
    });
    if (picked.canceled || picked.filePaths.length === 0) return { ok: false, canceled: true };
    const chosen = picked.filePaths[0];
    // Validate before accepting: an arbitrary executable is not a Python interpreter.
    const { spawnSync } = await import("node:child_process");
    const check = spawnSync(chosen, ["-c", "import sys;print(sys.version.split()[0])"], {
      encoding: "utf8", timeout: 15000, windowsHide: true,
    });
    if (check.status !== 0) {
      return { ok: false, error: "该文件不是可用的 Python 解释器" };
    }
    const probe: PythonProbe = {
      ok: true, command: chosen, args: [], source: "manual",
      version: (check.stdout || "").trim(), missingModules: [],
    };
    engine.setInterpreter(probe);
    const discovery = discoverPython();
    if (discovery.interpreter) engine.setInterpreter(discovery.interpreter);
    return { ok: true, interpreter: discovery.interpreter ?? probe, missing: discovery.attempts };
  });

  // -- window controls ------------------------------------------------------
  ipcMain.handle(IPC.windowControl, (_event, request: WindowControlRequest) => {
    const window = getWindow();
    if (!window) return { ok: false };
    switch (request?.action) {
      case "minimize":
        window.minimize();
        break;
      case "maximize":
        if (window.isMaximized()) window.unmaximize();
        else window.maximize();
        break;
      case "close":
        window.close();
        break;
      case "toggle-devtools":
        window.webContents.toggleDevTools();
        break;
      default:
        return { ok: false, reason: "unknown action" };
    }
    return { ok: true, maximized: window.isMaximized() };
  });

  ipcMain.handle(IPC.openExternal, async (_event, url: string) => {
    // Only https links are opened; a config file must not be able to launch a local path.
    if (typeof url !== "string" || !/^https?:\/\//i.test(url)) return { ok: false };
    await shell.openExternal(url);
    return { ok: true };
  });

  // -- config import / export ----------------------------------------------
  ipcMain.handle(IPC.configExport, async (_event, document: Record<string, unknown>) => {
    const window = getWindow();
    const picked = await dialog.showSaveDialog(window ?? undefined!, {
      title: "导出配置",
      defaultPath: "wechat_config_backup.json",
      filters: [{ name: "JSON", extensions: ["json"] }],
    });
    if (picked.canceled || !picked.filePath) return { ok: false, canceled: true };
    try {
      writeFileSync(picked.filePath, JSON.stringify(document, null, 4), "utf8");
      return { ok: true, path: picked.filePath };
    } catch (error) {
      return { ok: false, error: String(error) };
    }
  });

  ipcMain.handle(IPC.configImport, async () => {
    const window = getWindow();
    const picked = await dialog.showOpenDialog(window ?? undefined!, {
      title: "导入配置",
      properties: ["openFile"],
      filters: [{ name: "JSON", extensions: ["json"] }],
    });
    if (picked.canceled || picked.filePaths.length === 0) return { ok: false, canceled: true };
    try {
      const parsed = JSON.parse(readFileSync(picked.filePaths[0], "utf8"));
      return { ok: true, document: parsed, path: picked.filePaths[0] };
    } catch (error) {
      return { ok: false, error: `配置文件无法解析: ${String(error)}` };
    }
  });

  ipcMain.handle(IPC.screenshotSave, async (_event, dataUrl: string, suggested: string) => {
    const window = getWindow();
    const picked = await dialog.showSaveDialog(window ?? undefined!, {
      title: "保存微信截图",
      defaultPath: suggested || "wechat-screenshot.png",
      filters: [{ name: "PNG", extensions: ["png"] }],
    });
    if (picked.canceled || !picked.filePath) return { ok: false, canceled: true };
    try {
      const base64 = String(dataUrl).replace(/^data:image\/png;base64,/, "");
      writeFileSync(picked.filePath, Buffer.from(base64, "base64"));
      return { ok: true, path: picked.filePath };
    } catch (error) {
      return { ok: false, error: String(error) };
    }
  });

  ipcMain.handle(IPC.appInfo, () => ({
    version: app.getVersion(),
    name: app.getName(),
    electron: process.versions.electron,
    chrome: process.versions.chrome,
    node: process.versions.node,
    platform: process.platform,
    arch: process.arch,
    packaged: app.isPackaged,
    userData: app.getPath("userData"),
    resources: process.resourcesPath,
    appPath: app.getAppPath(),
    logFile: join(app.getPath("userData"), "engine-data", "wechat_bot_dev.log"),
  }));
}

export { EngineError };
