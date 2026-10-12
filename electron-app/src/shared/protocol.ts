/**
 * Wire contract between the Electron main process and the Python engine.
 *
 * The engine speaks line-delimited JSON-RPC 2.0 on stdio (see wechat_engine/protocol.py).
 * This file is the single TypeScript source of truth for that surface; the corresponding
 * Python method list is pinned by tests/test_protocol.py so the two cannot drift silently.
 */

export interface RpcRequest {
  jsonrpc: "2.0";
  id: number;
  method: string;
  params?: Record<string, unknown>;
}

export interface RpcSuccess<T = unknown> {
  jsonrpc: "2.0";
  id: number;
  result: T;
}

export interface RpcFailure {
  jsonrpc: "2.0";
  id: number | null;
  error: { code: number; message: string; data?: unknown };
}

export interface RpcNotification<T = unknown> {
  jsonrpc: "2.0";
  method: string;
  params?: T;
}

export type RpcMessage<T = unknown> = RpcSuccess<T> | RpcFailure | RpcNotification<T>;

export const RPC_ERROR = {
  PARSE_ERROR: -32700,
  INVALID_REQUEST: -32600,
  METHOD_NOT_FOUND: -32601,
  INVALID_PARAMS: -32602,
  INTERNAL_ERROR: -32603,
} as const;

// ---------------------------------------------------------------------------
// Domain shapes
// ---------------------------------------------------------------------------

export interface OpenAiConfig {
  api_key: string;
  api_base: string;
  model: string;
  temperature: number;
  timeout_seconds: number;
  max_retries: number;
}

export interface UiConfig {
  theme: string;
  compact: boolean;
  log_tail_lines: number;
}

export type InputInjectionMode = "auto" | "foreground" | "background";

export interface EngineConfig {
  config_version: number;
  ai_engine: string;
  openai_api: OpenAiConfig;
  system_prompt: string;
  prompt_preset: string;
  custom_prompt: string;
  reply_prefix: string;
  include_prefix: boolean;
  whitelist_mode: boolean;
  whitelist: string[];
  blacklist: string[];
  friend_personas: Record<string, string>;
  reply_delay_seconds: number;
  reply_delay_jitter_seconds: number;
  check_interval_seconds: number;
  settle_delay_seconds: number;
  auto_focus_wechat: boolean;
  ignored_keywords: string[];
  saved_window_rect: number[];
  offscreen: boolean;
  input_injection_mode: InputInjectionMode;
  max_history_turns: number;
  log_level: string;
  ui: UiConfig;
}

export interface WeChatStatus {
  found: boolean;
  hwnd?: number;
  version?: number;
  title?: string;
  visible?: boolean;
  minimized?: boolean;
  offscreen?: boolean;
  rect?: [number, number, number, number] | null;
  restoreRect?: number[] | null;
  savedRect?: number[];
  suggestedRestoreRect?: number[];
  screen?: { width: number; height: number };
  recordedOffscreen?: boolean;
  dpiScale?: number;
}

export interface DetectedRow {
  name: string;
  canonical: string;
  y: number;
  unread: boolean;
  active: boolean;
  tier: string;
}

export interface Detection {
  width: number;
  height: number;
  title: string;
  target: string | null;
  source: string;
  pendingCount: number;
  pendingText: string;
  bubbleCount: number;
  rows: DetectedRow[];
  scale: number;
  at: number;
}

export interface OcrStats {
  ready: boolean;
  error: string | null;
  inferences: number;
  averageSeconds: number;
  modelsDir: string;
  bundledModels: boolean;
}

export interface EngineActivity {
  running: boolean;
  phase: string;
  paused: boolean;
  dryRun: boolean;
  cycles: number;
  replies: number;
  lastError: string | null;
  lastScanAt: number | null;
  lastReplyAt: number | null;
  lastReplyTarget: string | null;
  lastReplyText: string | null;
  startedAt: number | null;
  ocrStats: OcrStats;
  aiStats: { requests: number; failures: number; retries: number; totalSeconds: number };
  memoryContacts: number;
}

export interface EngineStatus extends EngineActivity {
  wechat: WeChatStatus;
  configDegraded: boolean;
  /** Last perception result, mirrored from the engine.detection event. */
  detection: Detection | null;
}

export interface PromptPresets {
  order: string[];
  prompts: Record<string, string>;
  default: string;
  customKey: string;
  blacklistDefaults: string[];
  ignoredKeywordsDefaults: string[];
}

export interface LogLine { level: string; line: string; at: number; }
export interface LogTail { lines: LogLine[]; offset: number; path: string; }

export interface SandboxResult {
  ok: boolean;
  reply?: string;
  raw?: string;
  elapsed?: number;
  model?: string;
  usage?: Record<string, unknown>;
  attempts?: number;
  systemPrompt?: string;
  reason?: string;
  message?: string;
  configRequired?: boolean;
}

export interface NamingCheck {
  contact: string;
  matched: boolean;
  canonical: string;
  tier: string;
  isGroup: boolean;
  isSystem: boolean;
  normalized: string;
}

export interface HostDiagnostics {
  python: string;
  executable: string;
  frozen: boolean;
  platform: string;
  pid: number;
  dataDir: string;
  installDir: string;
  configFile: string;
  logFile: string;
  modelsDir: string;
  dpiScale: number;
  dpiAwareness: string;
  isWindows: boolean;
}

export type Diagnostics = {
  version: string;
  uptimeSeconds: number;
  host: HostDiagnostics;
  ocr: OcrStats;
  ai: Record<string, number>;
  logFile: string;
  logLevel: number;
  configDegraded: boolean;
  sitePackages: Record<string, string>;
} & Record<string, unknown>;

// ---------------------------------------------------------------------------
// Engine method map: name -> [params, result]
// ---------------------------------------------------------------------------

export interface EngineMethods {
  ping: [Record<string, never>, { pong: boolean; at: number }];
  hello: [Record<string, never>, Record<string, unknown>];
  diagnostics: [Record<string, never>, Diagnostics];
  config_get: [Record<string, never>, EngineConfig];
  config_masked: [Record<string, never>, EngineConfig];
  config_update: [{ patch: Partial<EngineConfig> }, EngineConfig];
  config_reload: [Record<string, never>, EngineConfig];
  prompt_presets: [Record<string, never>, PromptPresets];
  apply_preset: [{ key: string }, EngineConfig];
  save_custom_prompt: [{ text: string }, EngineConfig];
  engine_status: [Record<string, never>, EngineStatus];
  engine_start: [Record<string, never>, EngineStatus];
  engine_stop: [Record<string, never>, EngineStatus];
  engine_pause: [Record<string, never>, EngineStatus];
  engine_resume: [Record<string, never>, EngineStatus];
  engine_restart: [Record<string, never>, EngineStatus];
  engine_set_dry_run: [{ enabled?: boolean }, EngineStatus];
  engine_tick: [{ force?: boolean }, Record<string, unknown>];
  engine_preview: [Record<string, never>, { ok: boolean; detection?: Detection; reason?: string }];
  engine_reset_memory: [{ contact?: string }, { ok: boolean; cleared: string }];
  wechat_status: [Record<string, never>, WeChatStatus];
  wechat_offscreen: [{ hidden?: boolean | null }, Record<string, unknown>];
  wechat_toggle_offscreen: [Record<string, never>, Record<string, unknown>];
  wechat_show: [Record<string, never>, Record<string, unknown>];
  wechat_minimize: [Record<string, never>, Record<string, unknown>];
  wechat_open: [Record<string, never>, Record<string, unknown>];
  capture_screenshot: [
    { max_width?: number; include_data_url?: boolean; save_path?: string },
    { ok: boolean; dataUrl?: string; width?: number; height?: number; savedPath?: string | null; reason?: string },
  ];
  capture_data_url: [{ max_width?: number }, { ok: boolean; dataUrl?: string; width?: number; height?: number; reason?: string }];
  log_tail: [{ lines?: number }, LogTail];
  log_read: [{ offset?: number }, { text: string; offset: number }];
  log_clear: [Record<string, never>, { ok: boolean }];
  log_open: [Record<string, never>, { ok: boolean; path?: string; reason?: string }];
  sandbox_test: [{ sender?: string; message: string; use_history?: boolean }, SandboxResult];
  sandbox_preview_prompt: [{ sender?: string }, { systemPrompt: string; persona: string; hasPersona: boolean }];
  test_api: [Record<string, never>, { ok: boolean; endpoint?: string; model?: string; keyPreview?: string; problems?: string[]; reason?: string }];
  naming_check: [{ contact: string; whitelist?: string[] }, NamingCheck];
  ocr_warmup: [Record<string, never>, { ok: boolean; reason?: string; stats?: OcrStats }];
  shutdown: [Record<string, never>, { ok: boolean }];
}

export type EngineMethodName = keyof EngineMethods;

/**
 * Methods the renderer may call, grouped by what they can do. Anything that can send a
 * message or move a window is gated by an explicit confirmation in the UI; the grouping
 * exists so a future IPC allow-list has something authoritative to read.
 */
export const MUTATING_METHODS: ReadonlySet<string> = new Set([
  "config_update",
  "config_reload",
  "apply_preset",
  "save_custom_prompt",
  "engine_start",
  "engine_stop",
  "engine_pause",
  "engine_resume",
  "engine_restart",
  "engine_set_dry_run",
  "engine_tick",
  "engine_reset_memory",
  "wechat_offscreen",
  "wechat_toggle_offscreen",
  "wechat_show",
  "wechat_minimize",
  "wechat_open",
  "capture_screenshot",
  "log_clear",
  "log_open",
  "sandbox_test",
  "ocr_warmup",
  "shutdown",
]);

/** Engine lifecycle events pushed as JSON-RPC notifications. */
export const ENGINE_EVENTS = [
  "engine.ready",
  "engine.started",
  "engine.stopped",
  "engine.paused",
  "engine.resumed",
  "engine.log",
  "engine.detection",
  "engine.replied",
  "engine.send_failed",
  "engine.ai_failed",
  "engine.error",
] as const;

export type EngineEventName = (typeof ENGINE_EVENTS)[number];
