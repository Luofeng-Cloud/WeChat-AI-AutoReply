/**
 * Development smoke test.
 *
 * Boots the real Electron main process, brings up the Python engine over the real RPC
 * transport, exercises a representative set of methods, renders each UI page, and writes
 * screenshots for visual review. This is the check that catches breakage the unit tests
 * cannot see: a wrong interpreter path, a protocol mismatch, a page that throws while
 * rendering, or a preload API that never reaches the renderer.
 *
 *   npm run smoke            # all pages, screenshots into .smoke/
 *   npm run smoke -- dashboard prompts
 *
 * Exits non-zero on the first hard failure so it is usable in CI.
 */

import { spawn } from "node:child_process";
import { existsSync, mkdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const projectRoot = dirname(here);
const outDir = join(projectRoot, ".smoke");
mkdirSync(outDir, { recursive: true });

const ELECTRON = join(projectRoot, "node_modules", "electron", "dist", "electron.exe");
const MAIN = join(projectRoot, "dist", "main", "index.js");
const PRELOAD = join(projectRoot, "dist", "preload", "index.js");
const RENDERER = join(projectRoot, "dist", "renderer", "index.html");

for (const [label, path] of [["electron", ELECTRON], ["main bundle", MAIN], ["preload bundle", PRELOAD], ["renderer bundle", RENDERER]]) {
  if (!existsSync(path)) {
    console.error(`missing ${label}: ${path}`);
    console.error("run `npm run build` first");
    process.exit(2);
  }
}

const script = join(outDir, "smoke-main.cjs");
writeFileSync(script, String.raw`
const { app, BrowserWindow, ipcMain } = require("electron");
const { join } = require("node:path");
const { writeFileSync, mkdirSync } = require("node:fs");

const PROJECT = process.env.SMOKE_PROJECT;
const OUT = process.env.SMOKE_OUT;
const PAGES = JSON.parse(process.env.SMOKE_PAGES || "[]");

/** Page id -> the h1 that page must render, so a screenshot cannot drift off-page. */
const PAGE_TITLES = {
  dashboard: "总览",
  logs: "运行日志",
  "ai-engine": "AI 引擎",
  prompts: "人设与 Prompt",
  firewall: "白名单与防扰",
  personas: "好友人设",
  sandbox: "回复沙盒",
  settings: "设置与诊断",
};

const results = [];
function record(name, ok, detail) {
  results.push({ name, ok, detail: detail === undefined ? "" : String(detail).slice(0, 400) });
  console.log((ok ? "PASS  " : "FAIL  ") + name + (detail ? "  ::  " + String(detail).slice(0, 200) : ""));
}

async function main() {
  const engine = require(join(PROJECT, "dist", "main", "index.js"));
  void engine;
  await app.whenReady();

  const window = new BrowserWindow({
    width: 1360, height: 900, show: false,
    webPreferences: {
      preload: join(PROJECT, "dist", "preload", "index.js"),
      contextIsolation: true, nodeIntegration: false, sandbox: false,
    },
  });

  const errors = [];
  window.webContents.on("console-message", (_event, level, message) => {
    if (level >= 2) errors.push(message);
  });
  window.webContents.on("render-process-gone", (_e, details) => errors.push("renderer gone: " + details.reason));

  await window.loadFile(join(PROJECT, "dist", "renderer", "index.html"));
  await new Promise((r) => setTimeout(r, 2500));

  // 1. The preload bridge must be present and callable from the renderer.
  const bridge = await window.webContents.executeJavaScript(
    "typeof window.engine === \"object\" && Object.keys(window.engine).length",
  );
  record("preload bridge exposed", typeof bridge === "number" && bridge >= 10, String(bridge) + " methods");

  // 2. The renderer must have drawn the rail and the status bar.
  const nav = await window.webContents.executeJavaScript(
    "JSON.stringify({rail: document.querySelectorAll(\".rail__item\").length, status: document.querySelectorAll(\".statusbar .seg\").length, title: document.title})",
  );
  const navInfo = JSON.parse(nav);
  record("navigation rail rendered", navInfo.rail === 8, navInfo.rail + " pages");
  record("status bar rendered", navInfo.status >= 3, navInfo.status + " segments");

  // 3. The engine round trip through IPC.
  const hello = await window.webContents.executeJavaScript(
    "window.engine.invoke(\"hello\").then(r => JSON.stringify({v: r.version, py: r.platform})).catch(e => \"ERR: \" + e.message)",
  );
  record("engine hello over IPC", String(hello).startsWith("{"), hello);

  const status = await window.webContents.executeJavaScript(
    "window.engine.invoke(\"engine_status\").then(r => JSON.stringify({running: r.running, ocr: r.ocrStats.ready, wechat: !!(r.wechat && r.wechat.found)})).catch(e => \"ERR: \" + e.message)",
  );
  record("engine status over IPC", String(status).startsWith("{"), status);

  // 4. Every page renders without throwing and looks like a page.
  for (const page of PAGES) {
    const outcome = await window.webContents.executeJavaScript(
      "window.__smokeNavigate ? window.__smokeNavigate(" + JSON.stringify(page) + ") : Promise.resolve(\"no-hook\")",
    );
    await new Promise((r) => setTimeout(r, 800));
    const shape = await window.webContents.executeJavaScript(
      "JSON.stringify({head: document.querySelectorAll(\".page__head h1\").length, title: (document.querySelector(\".page__head h1\")||{}).textContent || \"\", cards: document.querySelectorAll(\".card\").length, empty: document.querySelectorAll(\".empty\").length})",
    );
    const info = JSON.parse(shape);
    const expected = PAGE_TITLES[page];
    const correctPage = !expected || info.title === expected;
    const ok = info.head === 1 && (info.cards > 0 || info.empty > 0) && correctPage
      && !(outcome || "").startsWith("ERR");
    record(
      "page " + page,
      ok,
      (correctPage ? "" : "TITLE MISMATCH (got " + info.title + ") ") +
        info.cards + " cards, " + info.empty + " empty states",
    );

    const image = await window.webContents.capturePage();
    mkdirSync(OUT, { recursive: true });
    writeFileSync(join(OUT, page + ".png"), image.toPNG());
  }

  // 5. Renderer console errors are failures.
  record("no renderer errors", errors.length === 0, errors.slice(0, 3).join(" | "));

  writeFileSync(join(OUT, "report.json"), JSON.stringify({ results, errors }, null, 2), "utf8");
  const failed = results.filter((item) => !item.ok).length;
  console.log("\n" + (results.length - failed) + "/" + results.length + " checks passed");
  app.exit(failed === 0 ? 0 : 1);
}

app.whenReady().then(() =>
  main().catch((error) => {
    console.error("smoke crashed:", error);
    app.exit(3);
  }),
);
`);

const pages = process.argv.slice(2);
const defaultPages = ["dashboard", "logs", "ai-engine", "prompts", "firewall", "personas", "sandbox", "settings"];

// Some shells (and Electron-based tooling) export ELECTRON_RUN_AS_NODE, which makes the
// binary behave as plain Node and leaves `require("electron")` returning the module path
// instead of the API. Clear it so the app actually boots as Electron.
const childEnv = { ...process.env };
delete childEnv.ELECTRON_RUN_AS_NODE;
childEnv.SMOKE_PROJECT = projectRoot;
// The generated entry lives in .smoke/, so app.getAppPath() is wrong there.
childEnv.WECHAT_ENGINE_APP_DIR = projectRoot;
childEnv.SMOKE_OUT = outDir;
childEnv.SMOKE_PAGES = JSON.stringify(pages.length ? pages : defaultPages);

const child = spawn(ELECTRON, [script], { cwd: projectRoot, stdio: "inherit", env: childEnv });
child.on("exit", (code) => process.exit(code ?? 1));
