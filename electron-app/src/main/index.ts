import { BrowserWindow, Menu, app, ipcMain, shell } from "electron";
import { join } from "node:path";

import { EngineManager } from "./engine";
import { IPC, registerIpc } from "./ipc";

/**
 * Application shell.
 *
 * Responsibilities kept here, deliberately small:
 *  - enforce a single instance, because two engines scanning one WeChat window would
 *    double-reply to friends;
 *  - create the window with a locked-down webPreferences;
 *  - own the engine lifecycle so the daemon never outlives the app;
 *  - re-surface renderer crashes instead of showing a blank window.
 */

const engine = new EngineManager();
let mainWindow: BrowserWindow | null = null;

const isDev = !app.isPackaged;
const DEV_URL = process.env.ELECTRON_RENDERER_URL;

// Two instances would run two engines against the same WeChat window.
const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.focus();
    }
  });
}

function createWindow(): BrowserWindow {
  const window = new BrowserWindow({
    width: 1280,
    height: 860,
    minWidth: 1020,
    minHeight: 700,
    show: false,
    backgroundColor: "#0e1117",
    autoHideMenuBar: true,
    title: "微信AI自动回复 · 控制中心",
    frame: false,
    titleBarStyle: "hidden",
    titleBarOverlay: false,
    webPreferences: {
      preload: join(__dirname, "../preload/index.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
      spellcheck: false,
      webSecurity: true,
    },
  });

  window.once("ready-to-show", () => window.show());

  // External links belong in the user browser, never in an app window.
  window.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:\/\//i.test(url)) void shell.openExternal(url);
    return { action: "deny" };
  });
  window.webContents.on("will-navigate", (event, url) => {
    const current = window.webContents.getURL();
    if (url !== current) {
      event.preventDefault();
      if (/^https?:\/\//i.test(url)) void shell.openExternal(url);
    }
  });

  // A renderer crash must be visible; otherwise the user sees an empty frame.
  window.webContents.on("render-process-gone", (_event, details) => {
    window.webContents.send(IPC.engineState, {
      state: "crashed",
      detail: `界面进程异常退出 (${details.reason})，请重启应用`,
    });
  });

  if (isDev && DEV_URL) {
    void window.loadURL(DEV_URL);
  } else {
    void window.loadFile(join(__dirname, "../renderer/index.html"));
  }
  return window;
}

function forwardEngineEvents(window: BrowserWindow): void {
  engine.on("notification", (method: string, params: unknown) => {
    if (!window.isDestroyed()) window.webContents.send(IPC.onEvent, { method, params });
  });
  engine.on("log", (level: string, line: string) => {
    if (!window.isDestroyed()) window.webContents.send(IPC.onEvent, { method: "engine.log", params: { level, line } });
  });
  engine.on("state", (state: string, detail?: string) => {
    if (!window.isDestroyed()) window.webContents.send(IPC.engineState, { state, detail });
  });
}

function registerScreenshotHandler(): void {
  ipcMain.handle("app:capture-page", async (_event, page: string, outPath: string) => {
    if (!mainWindow || mainWindow.isDestroyed()) return { ok: false, reason: "no-window" };
    if (page) {
      mainWindow.webContents.send(IPC.engineState, { state: "navigate", detail: page });
      await new Promise((resolve) => setTimeout(resolve, 900));
    }
    const image = await mainWindow.webContents.capturePage();
    const { writeFileSync } = await import("node:fs");
    writeFileSync(outPath, image.toPNG());
    return { ok: true, path: outPath };
  });
}

function applyMenu(): void {
  // The window is frameless, so the menu exists only for keyboard shortcuts.
  const template: Electron.MenuItemConstructorOptions[] = [
    {
      label: "文件",
      submenu: [
        { role: "reload", label: "重新加载界面" },
        { role: "toggleDevTools", label: "开发者工具" },
        { type: "separator" },
        { role: "quit", label: "退出" },
      ],
    },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

app.whenReady().then(async () => {
  applyMenu();
  registerScreenshotHandler();
  mainWindow = createWindow();
  forwardEngineEvents(mainWindow);
  registerIpc(engine, () => mainWindow);

  // Start the engine once the UI can display a failure. A missing interpreter is a
  // normal first-run state, not a crash: the setup screen handles it.
  try {
    await engine.start();
  } catch (error) {
    mainWindow?.webContents.send(IPC.engineState, { state: "crashed", detail: String(error) });
  }

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      mainWindow = createWindow();
      forwardEngineEvents(mainWindow);
    }
  });
});

app.on("window-all-closed", () => {
  app.quit();
});

let shuttingDown = false;
app.on("before-quit", async (event) => {
  if (shuttingDown) return;
  event.preventDefault();
  shuttingDown = true;
  try {
    await engine.stop();
  } finally {
    // A fatal error means the process is in an undefined state; a non-zero exit code
    // surfaces that to whatever launched the app instead of hiding it.
    app.exit(fatalError ? 1 : 0);
  }
});

/**
 * Crash containment.
 *
 * Electron installs its own uncaughtException listener that shows a *modal* dialog. When
 * the failure is an EPIPE from replying to a renderer that already closed its IPC pipe,
 * the main process is what died -- so the dialog it just opened has no message loop and
 * cannot be dismissed by clicking. That is a zombie window the user cannot get rid of
 * without killing the process.
 *
 * The default listener is therefore removed and replaced. The raw listener is captured
 * first so removing it is safe on Electron versions that also wire up native crash
 * reporting: if removal ever stops working, behaviour falls back to today rather than
 * losing the reporter.
 */
const electronDefaultListeners = process.listeners("uncaughtException");
process.removeAllListeners("uncaughtException");

let fatalError: Error | null = null;

let exiting = false;

function reportFatal(kind: string, error: unknown): void {
  const detail = error instanceof Error ? (error.stack ?? error.message) : String(error);
  console.error(`[main] ${kind}:`, detail);
  if (!fatalError) fatalError = error instanceof Error ? error : new Error(String(error));

  // Suppressing the dialog is only half the job: a process that logs and keeps running is
  // a zombie with no message loop, which is just as unkillable for the user. Report, give
  // the window a moment to show the reason, then exit non-zero.
  if (exiting) return;
  exiting = true;

  try {
    mainWindow?.webContents.send(IPC.engineState, {
      state: "crashed",
      detail: `主进程发生未捕获异常（${kind}）：${detail.split("\n")[0]}`,
    });
  } catch {
    /* the window may already be gone; exiting still matters */
  }

  // Hard deadline: if the graceful path stalls, the process must still die.
  const fallback = setTimeout(() => app.exit(1), 5000);
  fallback.unref?.();

  setTimeout(() => {
    void engine
      .stop(3000)
      .catch(() => undefined)
      .finally(() => {
        clearTimeout(fallback);
        app.exit(1);
      });
  }, 1500);
}

process.on("uncaughtException", (error) => reportFatal("uncaughtException", error));
process.on("unhandledRejection", (reason) => reportFatal("unhandledRejection", reason));

/** Re-install the captured Electron listeners; used only by tests and diagnostics. */
export function restoreElectronDefaultListeners(): void {
  for (const listener of electronDefaultListeners) process.on("uncaughtException", listener);
}
