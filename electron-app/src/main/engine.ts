import { ChildProcessWithoutNullStreams, spawn, spawnSync } from "node:child_process";
import { EventEmitter } from "node:events";
import { existsSync } from "node:fs";
import { join } from "node:path";
import { app } from "electron";

import type { EngineMethodName, EngineMethods, RpcMessage } from "../shared/protocol";
import { appRoot, childEnv, discoverPython, PythonProbe, savePreferred } from "./python-env";

/**
 * Supervises the Python engine process and exposes it as a typed async client.
 *
 * Two transports share one implementation:
 *
 *  - `call()` spawns a one-shot process. Used before the GUI exists (window creation,
 *    first-run checks) and by the engine-setup screen. It costs an interpreter start per
 *    call, so it is never used for anything on a timer.
 *  - `invoke()` writes to the long-lived RPC daemon. This is the normal path.
 *
 * A crashed daemon is restarted with backoff, and every in-flight request is rejected so
 * the UI shows an error instead of hanging forever.
 */

interface PendingRequest {
  method: string;
  resolve: (value: unknown) => void;
  reject: (error: Error) => void;
  timer: NodeJS.Timeout;
}

export interface EngineEvents {
  notification: [string, unknown];
  log: [string, string];
  state: ["starting" | "ready" | "stopped" | "crashed", string?];
}

const DEFAULT_TIMEOUT_MS = 30000;
const LONG_METHODS = new Set(["sandbox_test", "engine_tick", "engine_preview", "capture_screenshot"]);
const DAEMON_TIMEOUT_MS = 120000;

export class EngineError extends Error {
  constructor(message: string, readonly code?: number, readonly data?: unknown) {
    super(message);
    this.name = "EngineError";
  }
}

export class EngineManager extends EventEmitter {
  private child: ChildProcessWithoutNullStreams | null = null;
  private buffer = "";
  private nextId = 1;
  private pending = new Map<number, PendingRequest>();
  private interpreter: PythonProbe | null = null;
  private restartAttempts = 0;
  private stopping = false;
  private readyWaiters: Array<{ resolve: () => void; reject: (error: Error) => void }> = [];
  private stderrTail: string[] = [];

  // -- interpreter ---------------------------------------------------------

  get python(): PythonProbe | null {
    return this.interpreter;
  }

  setInterpreter(probe: PythonProbe): void {
    this.interpreter = probe;
    savePreferred({ command: probe.command, args: probe.args, source: probe.source });
  }

  async ensureInterpreter(): Promise<PythonProbe> {
    if (this.interpreter?.ok) return this.interpreter;
    const discovery = discoverPython();
    if (discovery.interpreter) {
      this.interpreter = discovery.interpreter;
      return discovery.interpreter;
    }
    const hint = discovery.incomplete
      ? `${discovery.incomplete.command} 缺少依赖: ${discovery.incomplete.missingModules.join(", ")}`
      : "未找到可用的 Python 解释器";
    throw new EngineError(hint);
  }

  /** Absolute path to the directory that contains the wechat_engine package. */
  private engineRoot(): string {
    // Packaged builds keep the engine under resources/engine; a source checkout keeps
    // it next to the Electron app. appRoot() also honours WECHAT_ENGINE_APP_DIR.
    return app.isPackaged ? join(process.resourcesPath, "engine") : appRoot();
  }

  private engineArgs(mode: string[]): string[] {
    // Must be `-m wechat_engine`, not a path to the package directory: running a package
    // directory as a script leaves it with no parent package, so its relative imports
    // (from . import win32) fail with "attempted relative import with no known parent".
    // cwd is set to engineRoot(), which is what puts the package on sys.path.
    return [...(this.interpreter?.args ?? []), "-m", "wechat_engine", ...mode];
  }

  // -- one-shot calls ------------------------------------------------------

  /**
   * Spawn the engine, run one RPC method, exit. Returns the parsed result.
   *
   * Used for the pre-window checks and by the setup screen, where a long-lived daemon
   * would be wasteful or would not exist yet.
   */
  async call<K extends EngineMethodName>(
    method: K,
    params?: EngineMethods[K][0],
    options: { timeoutMs?: number } = {},
  ): Promise<EngineMethods[K][1]> {
    const probe = await this.ensureInterpreter();
    const args = this.engineArgs(["--call", method, JSON.stringify(params ?? {})]);
    const timeout = options.timeoutMs ?? 60000;

    const result = spawnSync(probe.command, args, {
      encoding: "utf8",
      timeout,
      windowsHide: true,
      cwd: this.engineRoot(),
      env: { ...childEnv(), ...this.dataDirEnv() },
    });

    if (result.error) throw new EngineError(`引擎启动失败: ${result.error.message}`);
    // The engine prints the JSON payload last; anything before it came from a library.
    const stdout = (result.stdout || "").trim();
    const line = stdout.split("\n").filter((item) => item.trim().startsWith("{")).pop();
    if (!line) {
      const detail = (result.stderr || stdout || "").split("\n").slice(-6).join("\n");
      throw new EngineError(`引擎未返回结果 (exit ${result.status})\n${detail}`);
    }
    try {
      const parsed = JSON.parse(line) as Record<string, unknown> & { ok?: boolean; error?: string };
      if (parsed.error && parsed.ok === false) throw new EngineError(String(parsed.error));
      return parsed as EngineMethods[K][1];
    } catch (error) {
      if (error instanceof EngineError) throw error;
      throw new EngineError(`无法解析引擎输出: ${line.slice(0, 200)}`);
    }
  }

  private dataDirEnv(): Record<string, string> {
    // Keep engine state under the Electron userData so uninstalling the app removes it,
    // and pin the app root so the engine resolves its package regardless of cwd.
    return {
      WECHAT_ENGINE_DATA_DIR:
        process.env.WECHAT_ENGINE_DATA_DIR ?? join(app.getPath("userData"), "engine-data"),
      WECHAT_ENGINE_APP_DIR: this.engineRoot(),
    };
  }

  // -- daemon --------------------------------------------------------------

  async start(): Promise<void> {
    if (this.child) return;
    const probe = await this.ensureInterpreter();
    this.stopping = false;
    this.emit("state", "starting");

    const child = spawn(probe.command, this.engineArgs(["--rpc"]), {
      cwd: this.engineRoot(),
      windowsHide: true,
      stdio: ["pipe", "pipe", "pipe"],
      env: { ...childEnv(), ...this.dataDirEnv() },
    });
    this.child = child;
    this.buffer = "";
    this.stderrTail = [];

    child.stdout.setEncoding("utf8");
    child.stdout.on("data", (chunk: string) => this.onStdout(chunk));

    child.stderr.setEncoding("utf8");
    child.stderr.on("data", (chunk: string) => {
      // stderr carries the engine log. Keep a tail for crash diagnostics and forward
      // anything that looks like an engine log line to the UI.
      for (const line of chunk.split(/\r?\n/)) {
        if (!line.trim()) continue;
        this.stderrTail.push(line);
        if (this.stderrTail.length > 200) this.stderrTail.shift();
        const match = /^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s?(.*)$/.exec(line);
        if (match) this.emit("log", "info", match[2]);
      }
    });

    child.on("error", (error) => {
      this.failAll(new EngineError(`引擎进程错误: ${error.message}`));
      this.emit("state", "crashed", error.message);
    });

    child.on("close", (code, signal) => {
      this.child = null;
      const reason = signal ? `signal ${signal}` : `exit ${code}`;
      this.failAll(new EngineError(`引擎进程已退出 (${reason})`));
      if (this.stopping) {
        this.emit("state", "stopped");
        return;
      }
      this.emit("state", "crashed", this.describeCrash(reason));
      this.scheduleRestart();
    });

    await this.waitForReady();
    this.restartAttempts = 0;
    this.emit("state", "ready");
  }

  private describeCrash(reason: string): string {
    const tail = this.stderrTail.slice(-8).join("\n");
    return tail ? `${reason}\n${tail}` : reason;
  }

  private scheduleRestart(): void {
    if (this.stopping) return;
    // Back off so a permanently broken interpreter does not spin a process per second.
    this.restartAttempts += 1;
    if (this.restartAttempts > 5) return;
    const delay = Math.min(30000, 1000 * 2 ** (this.restartAttempts - 1));
    setTimeout(() => {
      if (!this.stopping && !this.child) {
        void this.start().catch((error) => this.emit("state", "crashed", String(error)));
      }
    }, delay);
  }

  private waitForReady(timeoutMs = 90000): Promise<void> {
    if (this.isReady) return Promise.resolve();
    return new Promise<void>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.readyWaiters = this.readyWaiters.filter((waiter) => waiter.resolve !== wrapped.resolve);
        reject(new EngineError("引擎启动超时"));
      }, timeoutMs);
      const wrapped = {
        resolve: () => { clearTimeout(timer); resolve(); },
        reject: (error: Error) => { clearTimeout(timer); reject(error); },
      };
      this.readyWaiters.push(wrapped);
    });
  }

  private isReady = false;

  private markReady(): void {
    this.isReady = true;
    for (const waiter of this.readyWaiters.splice(0)) waiter.resolve();
  }

  private onStdout(chunk: string): void {
    this.buffer += chunk;
    let index = this.buffer.indexOf("\n");
    while (index >= 0) {
      const line = this.buffer.slice(0, index).trim();
      this.buffer = this.buffer.slice(index + 1);
      if (line) this.handleMessage(line);
      index = this.buffer.indexOf("\n");
    }
    // Guard against a wedged writer that never emits a newline.
    if (this.buffer.length > 8 * 1024 * 1024) this.buffer = "";
  }

  private handleMessage(line: string): void {
    let message: RpcMessage;
    try {
      message = JSON.parse(line) as RpcMessage;
    } catch {
      // Not protocol traffic. Surface it rather than dropping it silently.
      this.emit("log", "warn", line);
      return;
    }

    if ("method" in message && message.method) {
      const params = message.params as Record<string, unknown> | undefined;
      if (message.method === "engine.ready") this.markReady();
      if (message.method === "engine.log") {
        this.emit("log", String((params?.level as string) ?? "info"), String((params?.line as string) ?? ""));
      }
      this.emit("notification", message.method, message.params);
      return;
    }

    const id = (message as { id?: number }).id;
    if (typeof id !== "number") return;
    const pendingRequest = this.pending.get(id);
    if (!pendingRequest) return;
    this.pending.delete(id);
    clearTimeout(pendingRequest.timer);

    if ("error" in message && message.error) {
      pendingRequest.reject(new EngineError(message.error.message, message.error.code, message.error.data));
    } else if ("result" in message) {
      pendingRequest.resolve(message.result);
    } else {
      pendingRequest.reject(new EngineError("格式错误的响应"));
    }
  }

  private failAll(error: Error): void {
    this.isReady = false;
    for (const [id, request] of this.pending) {
      clearTimeout(request.timer);
      request.reject(error);
      this.pending.delete(id);
    }
    for (const waiter of this.readyWaiters.splice(0)) waiter.reject(error);
  }

  /** Send an RPC request to the running daemon. */
  async invoke<K extends EngineMethodName>(
    method: K,
    params?: EngineMethods[K][0],
    options: { timeoutMs?: number } = {},
  ): Promise<EngineMethods[K][1]> {
    if (!this.child) await this.start();
    const child = this.child;
    if (!child) throw new EngineError("引擎未运行");
    await this.waitForReady();

    const id = this.nextId++;
    const timeoutMs = options.timeoutMs ?? (LONG_METHODS.has(method) ? DAEMON_TIMEOUT_MS : DEFAULT_TIMEOUT_MS);
    const payload = JSON.stringify({ jsonrpc: "2.0", id, method, params: params ?? {} });

    return new Promise<EngineMethods[K][1]>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new EngineError(`调用 ${method} 超时 (${timeoutMs}ms)`));
      }, timeoutMs);
      this.pending.set(id, {
        method,
        resolve: resolve as (value: unknown) => void,
        reject,
        timer,
      });
      child.stdin.write(payload + "\n", (error) => {
        if (error) {
          this.pending.delete(id);
          clearTimeout(timer);
          reject(new EngineError(`写入引擎失败: ${error.message}`));
        }
      });
    });
  }

  async stop(timeoutMs = 8000): Promise<void> {
    this.stopping = true;
    const child = this.child;
    if (!child) return;
    try {
      await this.invoke("shutdown", {}, { timeoutMs: 3000 });
    } catch {
      /* the process may already be gone */
    }
    await new Promise<void>((resolve) => {
      const timer = setTimeout(() => {
        child.kill();
        resolve();
      }, timeoutMs);
      child.once("close", () => { clearTimeout(timer); resolve(); });
    });
    this.child = null;
  }

  get running(): boolean {
    return this.child !== null && this.isReady;
  }

  /** Blocking interpreter check used by the setup screen before the window opens. */
  static probeSync(): ReturnType<typeof spawnSync> | null {
    if (!existsSync(join(app.getAppPath()))) return null;
    return null;
  }
}
