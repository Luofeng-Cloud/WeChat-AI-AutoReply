import { spawn, spawnSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { app } from "electron";

/**
 * Locating a usable Python interpreter for the engine.
 *
 * The engine needs numpy, Pillow, requests, psutil, pyperclip and rapidocr_onnxruntime.
 * A packaged Electron build cannot assume any of them.
 *
 * The hard part is not finding Python, it is finding a *launchable* one. A GUI process
 * started from Explorer receives the machine PATH, not the user PATH that the installer
 * and most "add to PATH" instructions modify -- so a `python` that works in the user
 * terminal is invisible to the app. Reconstructing the user PATH from the registry and
 * scanning the standard install roots is what makes this work on a normal machine.
 */

export interface PythonCandidate {
  command: string;
  args: string[];
  source: string;
}

export interface PythonProbe {
  ok: boolean;
  command: string;
  args: string[];
  source: string;
  version?: string;
  missingModules: string[];
  error?: string;
}

export const REQUIRED_MODULES = [
  "numpy",
  "PIL",
  "requests",
  "psutil",
  "pyperclip",
  "rapidocr_onnxruntime",
] as const;

const PROBE_SNIPPET = [
  "import json,sys",
  "missing=[]",
  "for name in " + JSON.stringify(REQUIRED_MODULES) + ":",
  "    try:",
  "        __import__(name)",
  "    except Exception:",
  "        missing.append(name)",
  "print(json.dumps({'version': sys.version.split()[0], 'missing': missing}))",
].join("\n");

/**
 * Directory that contains the wechat_engine package and requirements.txt.
 *
 * Normally the Electron app path or the packaged resources directory. The env
 * override exists because app.getAppPath() is only meaningful for a real app entry:
 * the smoke harness runs a generated script, where it points at .smoke/ instead.
 */
export function appRoot(): string {
  const override = process.env.WECHAT_ENGINE_APP_DIR;
  if (override?.trim()) return override.trim();
  return app.isPackaged ? process.resourcesPath : app.getAppPath();
}

const PREFS_FILE = () => join(app.getPath("userData"), "engine-python.json");

function readPreferred(): PythonCandidate | null {
  try {
    const parsed = JSON.parse(readFileSync(PREFS_FILE(), "utf8")) as PythonCandidate;
    if (parsed?.command) return { command: parsed.command, args: parsed.args ?? [], source: "saved" };
  } catch {
    /* no saved preference yet */
  }
  return null;
}

export function savePreferred(candidate: PythonCandidate): void {
  try {
    mkdirSync(app.getPath("userData"), { recursive: true });
    writeFileSync(PREFS_FILE(), JSON.stringify(candidate, null, 2), "utf8");
  } catch {
    /* a read-only userData directory only means the probe re-runs next launch */
  }
}

/**
 * The PATH a user terminal would have.
 *
 * Explorer-launched processes get the machine PATH only, which is why `python` resolves
 * in the terminal but not in the app. Registry values are expanded here, and the result
 * is used for every child process the app spawns.
 */
let cachedPath: string | null = null;

export function effectivePath(): string {
  if (cachedPath !== null) return cachedPath;
  const current = process.env.PATH ?? "";
  const parts = current.split(";").filter(Boolean);
  if (process.platform !== "win32") {
    cachedPath = current;
    return cachedPath;
  }
  for (const hive of ["HKCU", "HKLM"]) {
    for (const sub of [
      "Environment",
      "SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Environment",
    ]) {
      const result = spawnSync("reg", ["query", `${hive}\\${sub}`, "/v", "Path"], {
        encoding: "utf8", timeout: 5000, windowsHide: true,
      });
      if (result.status !== 0) continue;
      const match = /Path\s+REG_(?:EXPAND_)?SZ\s+(.+)$/m.exec(result.stdout ?? "");
      if (!match) continue;
      const expanded = expandEnvironment(match[1].trim());
      for (const entry of expanded.split(";")) {
        const trimmed = entry.trim().replace(/\\+$/, "");
        if (trimmed && !parts.some((item) => item.toLowerCase() === trimmed.toLowerCase())) {
          parts.push(trimmed);
        }
      }
    }
  }
  cachedPath = parts.join(";");
  return cachedPath;
}

function expandEnvironment(value: string): string {
  return value.replace(/%([^%]+)%/g, (_match, name: string) => {
    const key = Object.keys(process.env).find((item) => item.toLowerCase() === name.toLowerCase());
    return (key && process.env[key]) || process.env[name.toUpperCase()] || "";
  });
}

/** Install roots worth scanning, newest Python first. */
function pythonInstallRoots(): string[] {
  const roots: string[] = [];
  const localAppData = process.env.LOCALAPPDATA;
  if (localAppData) roots.push(join(localAppData, "Programs", "Python"));
  if (process.env.ProgramFiles) roots.push(process.env.ProgramFiles);
  if (process.env["ProgramFiles(x86)"]) roots.push(process.env["ProgramFiles(x86)"]);
  const programData = process.env.ProgramData;
  if (programData) roots.push(join(programData, "Anaconda3"), join(programData, "miniconda3"));
  if (process.env.USERPROFILE) {
    roots.push(join(process.env.USERPROFILE, "anaconda3"), join(process.env.USERPROFILE, "miniconda3"));
  }
  return roots;
}

function executablesIn(directory: string): string[] {
  if (!existsSync(directory)) return [];
  try {
    return readdirSync(directory)
      .filter((name) => /^python(\d+(\.\d+)?)?\.exe$/i.test(name))
      .sort()
      .reverse()
      .map((name) => join(directory, name));
  } catch {
    return [];
  }
}

/**
 * Ordered candidates, most specific and cheapest first.
 *
 *  1. a frozen engine binary shipped with the app (no Python at all);
 *  2. an explicit override for conda environments and unusual setups;
 *  3. the interpreter that worked last time;
 *  4. a project virtual environment next to the sources;
 *  5. `py` (the Windows launcher, which knows every registered install);
 *  6. every python*.exe under the standard install roots;
 *  7. `python` from the reconstructed PATH.
 */
export function candidateInterpreters(): PythonCandidate[] {
  const isWindows = process.platform === "win32";
  const candidates: PythonCandidate[] = [];
  const seen = new Set<string>();
  const push = (candidate: PythonCandidate) => {
    const key = `${candidate.command}|${candidate.args.join(" ")}`.toLowerCase();
    if (seen.has(key)) return;
    seen.add(key);
    candidates.push(candidate);
  };

  if (isWindows) {
    const frozen = join(process.resourcesPath ?? "", "engine", "wechat-engine.exe");
    if (existsSync(frozen)) push({ command: frozen, args: [], source: "bundled-engine" });
  }

  const override = process.env.WECHAT_ENGINE_PYTHON;
  if (override?.trim()) push({ command: override.trim(), args: [], source: "env-override" });

  const saved = readPreferred();
  if (saved) push(saved);

  const projectRoot = appRoot();
  for (const relative of [".venv/Scripts/python.exe", ".venv/bin/python", "venv/Scripts/python.exe"]) {
    const local = join(projectRoot, relative);
    if (existsSync(local)) push({ command: local, args: [], source: "project-venv" });
  }

  if (isWindows) push({ command: "py", args: ["-3"], source: "py-launcher" });

  for (const root of pythonInstallRoots()) {
    for (const executable of executablesIn(root)) {
      push({ command: executable, args: [], source: "install-root" });
    }
    for (const sub of ["Python311", "Python310", "Python312", "Python39", "Python313", "Python38"]) {
      for (const executable of executablesIn(join(root, sub))) {
        push({ command: executable, args: [], source: "install-root" });
      }
    }
  }

  push({ command: "python3", args: [], source: "path" });
  push({ command: "python", args: [], source: "path" });
  return candidates;
}

function childEnv(): NodeJS.ProcessEnv {
  return { ...process.env, PATH: effectivePath(), PYTHONIOENCODING: "utf-8" };
}

export { childEnv };

function probeCandidate(candidate: PythonCandidate, timeoutMs = 20000): PythonProbe {
  const result = spawnSync(candidate.command, [...candidate.args, "-c", PROBE_SNIPPET], {
    encoding: "utf8",
    timeout: timeoutMs,
    windowsHide: true,
    env: childEnv(),
  });

  if (result.error) {
    return { ...candidate, ok: false, missingModules: [], error: String((result.error as Error).message ?? result.error) };
  }
  if (result.status !== 0) {
    const detail = (result.stderr || result.stdout || "").trim().split("\n").slice(-3).join(" ");
    return { ...candidate, ok: false, missingModules: [], error: detail || `exit ${result.status}` };
  }

  const line = (result.stdout || "").trim().split("\n").pop() ?? "";
  try {
    const parsed = JSON.parse(line) as { version: string; missing: string[] };
    return {
      ...candidate,
      ok: parsed.missing.length === 0,
      version: parsed.version,
      missingModules: parsed.missing,
      error: parsed.missing.length ? `缺少依赖: ${parsed.missing.join(", ")}` : undefined,
    };
  } catch {
    return { ...candidate, ok: false, missingModules: [], error: "无法解析探测输出" };
  }
}

export interface DiscoveryResult {
  interpreter: PythonProbe | null;
  /** Best candidate that exists but is missing packages, so the UI can offer install. */
  incomplete: PythonProbe | null;
  attempts: PythonProbe[];
}

/**
 * Probe every candidate and return the first fully usable interpreter.
 *
 * `incomplete` is returned separately because "Python found but a package is missing"
 * needs a different remedy (pip install) from "no Python at all" (install Python).
 */
export function discoverPython(timeoutMs = 20000): DiscoveryResult {
  const attempts: PythonProbe[] = [];
  let incomplete: PythonProbe | null = null;
  let firstRunnable: PythonProbe | null = null;

  for (const candidate of candidateInterpreters()) {
    const probe = probeCandidate(candidate, timeoutMs);
    attempts.push(probe);
    if (probe.ok) {
      savePreferred({ command: probe.command, args: probe.args, source: probe.source });
      return { interpreter: probe, incomplete: incomplete ?? firstRunnable, attempts };
    }
    if (!incomplete && probe.missingModules.length > 0) incomplete = probe;
    if (!firstRunnable && !probe.error?.startsWith("spawn")) firstRunnable = probe;
  }
  return { interpreter: null, incomplete, attempts };
}

/** Install the engine requirements into an interpreter. Streams pip output line by line. */
export function installRequirements(
  candidate: PythonCandidate,
  requirementsPath: string,
  onLine: (line: string) => void,
): Promise<{ code: number | null; error?: string }> {
  return new Promise((resolve) => {
    const child = spawn(
      candidate.command,
      [...candidate.args, "-m", "pip", "install", "--disable-pip-version-check", "-r", requirementsPath],
      { windowsHide: true, env: childEnv() },
    );

    const forward = (chunk: Buffer) => {
      for (const line of chunk.toString("utf8").split(/\r?\n/)) {
        if (line.trim()) onLine(line.trimEnd());
      }
    };
    child.stdout?.on("data", forward);
    child.stderr?.on("data", forward);
    child.on("error", (error) => resolve({ code: null, error: String(error.message) }));
    child.on("close", (code) => resolve({ code }));
  });
}
