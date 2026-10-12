import type { EngineConfig, EngineMethodName, EngineMethods, EngineStatus, PromptPresets, WeChatStatus } from "../shared/protocol";
import { el } from "./ui";

/**
 * Shared application context.
 *
 * Deliberately small: the engine remains the source of truth for configuration, and the
 * renderer keeps only what it needs to draw. Every mutation goes through
 * `invokeConfig`, which writes to the engine and refreshes the local copy from the
 * reply, so the UI can never drift from the file on disk.
 */

export interface AppContext {
  invoke<K extends EngineMethodName>(method: K, params?: EngineMethods[K][0]): Promise<EngineMethods[K][1]>;
  refresh(force?: boolean): Promise<void>;
  invokeConfig(patch: Partial<EngineConfig>): Promise<void>;
  toast(kind: "info" | "ok" | "warn" | "error", title: string, body?: string): void;
  confirm(options: { title: string; body: string; confirmLabel?: string; danger?: boolean }): Promise<boolean>;
  modal(options: { title: string; body: string; content?: HTMLElement; confirmLabel?: string }): Promise<void>;
  navigate(page: string): Promise<void>;
  /** Poll only while the page is on screen. */
  registerTimer(name: string, callback: () => void, intervalMs: number): void;
  clearTimer(name: string): void;
  clearAllTimers(): void;
  rerender(): void;
  config: EngineConfig | null;
  status: EngineStatus | null;
  presets: PromptPresets | null;
  wechat: WeChatStatus | null;
  appInfo: Record<string, unknown> | null;
  engineState: { state: string; detail?: string };
}

export interface PageDefinition {
  id: string;
  label: string;
  glyph: string;
  group: string;
  /** Small mutable value shown next to the rail entry. */
  badge?(ctx: AppContext): { text: string; kind?: "live" | "warn" } | null;
  render(ctx: AppContext): HTMLElement | Promise<HTMLElement>;
  /** Called before the next page replaces this one. */
  dispose?(ctx: AppContext): void;
}

export function pageHead(title: string, subtitle: string): HTMLElement {
  return el("div", { class: "page__head" }, [el("h1", { text: title }), el("p", { text: subtitle })]);
}
