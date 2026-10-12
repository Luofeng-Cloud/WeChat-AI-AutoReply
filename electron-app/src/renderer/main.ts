import "./styles.css";

import type {
  Detection,
  EngineConfig,
  EngineMethodName,
  EngineMethods,
  EngineStatus,
  PromptPresets,
} from "../shared/protocol";
import type { AppContext, PageDefinition } from "./context";
import { enginePages } from "./pages/index";
import { button, clear, el } from "./ui";

/**
 * Renderer bootstrap: routing, global state, and the status bar.
 *
 * State refresh strategy: the engine pushes log lines and events as notifications, but
 * status is polled on a slow interval (and immediately after any mutation) because the
 * scan loop already runs on its own schedule. Polling is paused while the window is
 * hidden so a minimised control centre costs nothing.
 */

const rail = document.getElementById("rail") as HTMLElement;
const stage = document.getElementById("stage") as HTMLElement;
const statusbar = document.getElementById("statusbar") as HTMLElement;
const toasts = document.getElementById("toasts") as HTMLElement;
const modalRoot = document.getElementById("modal-root") as HTMLElement;

const timers = new Map<string, number>();
let currentPageId = "";
let currentPage: PageDefinition | null = null;
const logBuffer: string[] = [];
const MAX_BUFFERED_LOG_LINES = 500;

const ctx: AppContext = {
  config: null,
  status: null,
  presets: null,
  wechat: null,
  appInfo: null,
  engineState: { state: "starting" },

  async invoke<K extends EngineMethodName>(method: K, params?: EngineMethods[K][0]) {
    return window.engine.invoke(method, params);
  },

  async refresh(force = false) {
    const jobs: Array<Promise<unknown>> = [];
    if (force || !ctx.config) jobs.push(ctx.invoke("config_get").then((value) => { ctx.config = value as EngineConfig; }));
    if (force || !ctx.presets) jobs.push(ctx.invoke("prompt_presets").then((value) => { ctx.presets = value as PromptPresets; }));
    if (force || !ctx.appInfo) jobs.push(window.engine.appInfo().then((value) => { ctx.appInfo = value as Record<string, unknown>; }));
    jobs.push(
      ctx.invoke("engine_status").then((value) => {
        ctx.status = value as EngineStatus;
        ctx.wechat = (value as EngineStatus).wechat;
      }),
    );
    // A failing refresh must not blank the UI; the status bar shows the error instead.
    await Promise.allSettled(jobs);
    renderStatusBar();
    renderRail();
  },

  async invokeConfig(patch: Partial<EngineConfig>) {
    const updated = await ctx.invoke("config_update", { patch });
    ctx.config = updated as EngineConfig;
    renderRail();
  },

  toast(kind, title, body) {
    const node = el("div", { class: `toast toast--${kind === "ok" ? "ok" : kind === "warn" ? "warn" : kind === "error" ? "error" : "info"}` }, [
      el("strong", { text: title }),
      body ? el("span", { text: body }) : null,
    ]);
    toasts.append(node);
    setTimeout(() => node.remove(), kind === "error" ? 9000 : 4200);
  },

  confirm(options) {
    return new Promise<boolean>((resolve) => {
      const close = (value: boolean) => { modalRoot.replaceChildren(); resolve(value); };
      const modal = el("div", { class: "modal" }, [
        el("div", { class: "modal__head", text: options.title }),
        el("div", { class: "modal__body", text: options.body }),
        el("div", { class: "modal__foot" }, [
          button("取消", () => close(false), "ghost"),
          button(options.confirmLabel ?? "确定", () => close(true), options.danger ? "danger" : "primary"),
        ]),
      ]);
      modalRoot.replaceChildren(modal);
    });
  },

  async modal(options) {
    return new Promise<void>((resolve) => {
      const close = () => { modalRoot.replaceChildren(); resolve(); };
      const body = el("div", { class: "modal__body" });
      if (options.content) body.append(options.content);
      else body.textContent = options.body;
      const modal = el("div", { class: "modal" }, [
        el("div", { class: "modal__head", text: options.title }),
        body,
        el("div", { class: "modal__foot" }, [button(options.confirmLabel ?? "关闭", close, "primary")]),
      ]);
      modalRoot.replaceChildren(modal);
    });
  },

  async navigate(page) {
    const target = enginePages.find((item) => item.id === page) ?? enginePages[0];
    if (target.id === currentPageId) return;
    currentPage?.dispose?.(ctx);
    ctx.clearAllTimers();
    currentPageId = target.id;
    currentPage = target;
    renderRail();
    stage.scrollTop = 0;
    stage.replaceChildren(el("div", { class: "empty", text: "正在加载…" }));
    try {
      const view = await target.render(ctx);
      // A slow render must not overwrite a page the user already navigated away from.
      if (currentPageId !== target.id) return;
      stage.replaceChildren(view);
    } catch (error) {
      stage.replaceChildren(el("div", { class: "empty" }, [
        el("h3", { text: "页面加载失败" }),
        el("p", { text: String(error) }),
      ]));
    }
  },

  registerTimer(name, callback, intervalMs) {
    ctx.clearTimer(name);
    const id = window.setInterval(() => {
      if (document.hidden) return;
      try {
        callback();
      } catch (error) {
        console.error("[timer]", name, error);
      }
    }, intervalMs);
    timers.set(name, id);
  },

  clearTimer(name) {
    const id = timers.get(name);
    if (id !== undefined) {
      window.clearInterval(id);
      timers.delete(name);
    }
  },

  clearAllTimers() {
    for (const id of timers.values()) window.clearInterval(id);
    timers.clear();
  },

  rerender() {
    void ctx.navigate(currentPageId || enginePages[0].id);
  },
};

// ---------------------------------------------------------------------------
// Chrome
// ---------------------------------------------------------------------------

function renderRail(): void {
  clear(rail);
  const groups = new Map<string, PageDefinition[]>();
  for (const page of enginePages) {
    const list = groups.get(page.group) ?? [];
    list.push(page);
    groups.set(page.group, list);
  }

  for (const [groupName, pages] of groups) {
    rail.append(el("div", { class: "rail__group", text: groupName }));
    for (const page of pages) {
      let badge: { text: string; kind?: "live" | "warn" } | null = null;
      try {
        badge = page.badge?.(ctx) ?? null;
      } catch {
        badge = null;
      }
      const item = el("button", {
        class: "rail__item",
        type: "button",
        "aria-current": page.id === currentPageId ? "page" : false,
        onclick: () => void ctx.navigate(page.id),
      }, [
        el("span", { class: "glyph", text: page.glyph }),
        el("span", { class: "label", text: page.label }),
        badge
          ? el("span", {
              class: `rail__badge${badge.kind ? ` rail__badge--${badge.kind}` : ""}`,
              text: badge.text,
            })
          : null,
      ]);
      rail.append(item);
    }
  }
}

function statusDot(kind: "ok" | "warn" | "bad" | "busy" | "idle"): HTMLElement {
  return el("span", { class: `dot${kind === "idle" ? "" : ` dot--${kind}`}` });
}

function renderStatusBar(): void {
  const status = ctx.status;
  const wechat = ctx.wechat;
  clear(statusbar);

  const wechatFound = Boolean(wechat?.found);
  statusbar.append(el("div", { class: "seg" }, [
    statusDot(wechatFound ? (wechat?.offscreen ? "warn" : "ok") : "bad"),
    el("span", { text: wechatFound
      ? `微信 ${wechat?.version ?? "?"}.x${wechat?.offscreen ? " · 离屏" : wechat?.minimized ? " · 最小化" : ""}`
      : "未检测到微信" }),
  ]));

  const phase = status?.phase ?? "idle";
  const running = Boolean(status?.running);
  const phaseLabel: Record<string, string> = {
    idle: "未启动", starting: "启动中", watching: "监听中", generating: "生成中",
    sending: "发送中", stopped: "已停止", paused: "已暂停",
  };
  statusbar.append(el("div", { class: "seg" }, [
    statusDot(!running ? "idle" : status?.paused ? "warn" : phase === "watching" ? "ok" : "busy"),
    el("span", { text: `守护: ${phaseLabel[phase] ?? phase}` }),
  ]));

  if (status?.dryRun) statusbar.append(el("div", { class: "seg" }, [statusDot("warn"), el("span", { text: "演练模式" })]));

  statusbar.append(el("div", { class: "seg" }, [
    el("span", { text: `轮询 ${status?.cycles ?? 0}` }),
    el("span", { text: `回复 ${status?.replies ?? 0}` }),
  ]));

  const ocr = status?.ocrStats;
  statusbar.append(el("div", { class: "seg" }, [
    statusDot(ocr?.ready ? "ok" : "idle"),
    el("span", { text: ocr?.ready
      ? `OCR ${Math.round((ocr.averageSeconds ?? 0) * 1000)}ms`
      : ocr?.error ? "OCR 未就绪" : "OCR 待加载" }),
  ]));

  if (wechat?.dpiScale) statusbar.append(el("div", { class: "seg" }, [el("span", { text: `DPI ${wechat.dpiScale.toFixed(2)}x` })]));

  const state = ctx.engineState;
  const stateLabel: Record<string, string> = { starting: "引擎启动中", ready: "引擎就绪", stopped: "引擎已停止", crashed: "引擎异常" };
  statusbar.append(el("div", { class: "seg push" }, [
    statusDot(state.state === "ready" ? "ok" : state.state === "crashed" ? "bad" : "busy"),
    el("span", { text: stateLabel[state.state] ?? state.state }),
  ]));
  if (ctx.appInfo?.version) {
    statusbar.append(el("div", { class: "seg" }, [el("span", { text: `v${ctx.appInfo.version}` })]));
  }
}

// ---------------------------------------------------------------------------
// Wiring
// ---------------------------------------------------------------------------

for (const action of ["minimize", "maximize", "close", "toggle-devtools"] as const) {
  document.querySelector(`[data-action="${action}"]`)?.addEventListener("click", () => {
    void window.engine.window(action);
  });
}

window.engine.onEngineState((state, detail) => {
  // Dev smoke hook: the main process asks for a specific page before capturing it.
  if (state === "navigate" && detail) {
    void ctx.navigate(detail);
    return;
  }
  ctx.engineState = { state, detail };
  renderStatusBar();
  if (state === "crashed" && detail) {
    ctx.toast("error", "引擎进程异常", detail);
  }
});

window.engine.onEngineEvent((method, params) => {
  if (method === "engine.log") {
    const payload = params as { line?: string } | undefined;
    if (payload?.line) {
      logBuffer.push(payload.line);
      if (logBuffer.length > MAX_BUFFERED_LOG_LINES) logBuffer.shift();
    }
    return;
  }
  if (method === "engine.detection") {
    const payload = params as Detection | undefined;
    if (ctx.status && payload) ctx.status.detection = payload;
    return;
  }
  if (method === "engine.replied") {
    const payload = params as { target?: string; text?: string; dryRun?: boolean } | undefined;
    if (payload?.target) {
      ctx.toast(payload.dryRun ? "warn" : "ok",
        payload.dryRun ? `演练：${payload.target}` : `已回复 ${payload.target}`,
        payload.text);
    }
    void ctx.refresh();
    return;
  }
  if (method === "engine.ai_failed" || method === "engine.send_failed") {
    const payload = params as { target?: string; message?: string; detail?: string } | undefined;
    ctx.toast("error", `发送/生成失败${payload?.target ? ` · ${payload.target}` : ""}`,
      payload?.message ?? payload?.detail);
    return;
  }
});

export function getLogBuffer(): string[] {
  return logBuffer;
}

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------

async function boot(): Promise<void> {
  renderRail();
  renderStatusBar();
  await ctx.refresh(true);
  await ctx.navigate(enginePages[0].id);

  // Slow poll: the engine pushes events, this only reconciles missed state.
  ctx.registerTimer("global-status", () => void ctx.refresh(), 2500);

  const wechat = ctx.wechat;
  if (wechat && !wechat.found) {
    ctx.toast("warn", "未检测到微信窗口", "请先登录并打开 PC 微信，守护进程会在检测到窗口后自动开始工作。");
  }
}

// Dev-only navigation hook used by scripts/smoke.mjs. Non-enumerable so it never
// shows up in normal inspection of the renderer global scope.
Object.defineProperty(window, "__smokeNavigate", {
  value: (page: string) => ctx.navigate(page).then(() => "ok").catch((error: unknown) => `ERR: ${error}`),
  writable: false,
  enumerable: false,
});

void boot();
