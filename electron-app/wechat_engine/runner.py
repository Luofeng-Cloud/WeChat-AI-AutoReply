"""Process entry points: RPC daemon, one-shot CLI, headless watcher.

Three consumers, one implementation:

* the Electron main process spawns --rpc and keeps it for the app lifetime;
* --call METHOD [JSON] answers a single question and exits, which is how the app shell
  queries state before the window exists and how scripts and CI poke the engine;
* --headless reproduces the original "python wechat_ai_bot_dev.py" behaviour, so the 1.x
  workflow keeps working for anyone who never opens the GUI.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time


def _redirect_stdout_for_rpc():
    """Keep stdout clean: the RPC transport owns it.

    A library that prints a banner (onnxruntime, PIL) would otherwise inject text into
    the protocol stream and desynchronise the client. Everything is funnelled to stderr.
    """
    try:
        sys.stdout = sys.stderr
    except Exception:
        pass


def _write_pid(path):
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))
    except Exception:
        pass


def _clear_pid(path):
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def run_rpc(argv=None):
    """Line-delimited JSON-RPC over stdin/stdout. Returns a process exit code."""
    from . import config as config_mod, logutil, paths, service as service_mod, win32

    parser = argparse.ArgumentParser(prog="wechat-engine --rpc", add_help=False)
    parser.add_argument("--log-level", default=None)
    parser.add_argument("--log-file", default=None)
    parser.add_argument("--data-dir", default=None)
    known, _unknown = parser.parse_known_args(argv)

    if known.data_dir:
        os.environ["WECHAT_ENGINE_DATA_DIR"] = known.data_dir

    if win32.IS_WINDOWS:
        win32.enable_dpi_awareness()
        win32.ensure_default_desktop()

    store = config_mod.ConfigStore(path=paths.config_file())
    log_path = known.log_file or str(paths.log_file())
    bus = logutil.configure(
        path=log_path,
        level=known.log_level or store.get("log_level") or "info",
        stream=sys.stderr,
    )
    _redirect_stdout_for_rpc()
    _write_pid(paths.pid_file())

    logutil.log("[engine] rpc daemon starting (pid=%s, python=%s)" % (os.getpid(), sys.version.split()[0]))
    logutil.log("[engine] data dir: %s" % paths.data_dir())

    app = service_mod.Service(store=store)
    dispatcher = service_mod.build_dispatcher(app, stream_in=sys.stdin, stream_out=sys.__stdout__)

    def _handle_signal(_signum, _frame):
        logutil.log("[engine] shutdown signal received")
        dispatcher.request_stop()

    for name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, name):
            try:
                signal.signal(getattr(signal, name), _handle_signal)
            except Exception:
                pass

    dispatcher.notify("engine.ready", {"pid": os.getpid(), "config": store.masked()})
    try:
        dispatcher.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.close()
        dispatcher.shutdown()
        bus.close()
        _clear_pid(paths.pid_file())
        logutil.log("[engine] rpc daemon exited")
    return 0


def run_call(argv):
    """Execute one RPC method and print its JSON result on stdout."""
    from . import config as config_mod, logutil, paths, service as service_mod

    parser = argparse.ArgumentParser(prog="wechat-engine --call", add_help=False)
    parser.add_argument("method")
    parser.add_argument("params", nargs="?", default="{}")
    parser.add_argument("--log-level", default="warn")
    known, _unknown = parser.parse_known_args(argv)

    try:
        params = json.loads(known.params) if known.params else {}
    except json.JSONDecodeError as exc:
        print(json.dumps({"ok": False, "error": "invalid params JSON: %s" % exc}, ensure_ascii=False))
        return 2
    if not isinstance(params, dict):
        print(json.dumps({"ok": False, "error": "params must be an object"}, ensure_ascii=False))
        return 2

    logutil.configure(level=known.log_level, stream=sys.stderr)
    store = config_mod.ConfigStore(path=paths.config_file())
    # emit_events=False keeps stdout reserved for the single JSON result line.
    app = service_mod.Service(store=store, emit_events=False)
    try:
        handler = getattr(app, known.method, None)
        if handler is None or known.method.startswith("_"):
            print(json.dumps({"ok": False, "error": "unknown method: %s" % known.method}, ensure_ascii=False))
            return 3
        result = handler(**params)
        print(json.dumps(result, ensure_ascii=False, default=str))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc), "type": type(exc).__name__}, ensure_ascii=False))
        return 1
    finally:
        app.close()


def run_headless(argv=None):
    """The original CLI daemon: watch WeChat and reply, no UI."""
    from . import config as config_mod, logutil, paths, service as service_mod, win32

    parser = argparse.ArgumentParser(prog="wechat-engine --headless", add_help=False)
    parser.add_argument("--log-level", default=None)
    parser.add_argument("--duration", type=float, default=0.0, help="exit after N seconds (testing)")
    known, _unknown = parser.parse_known_args(argv)

    if not win32.IS_WINDOWS:
        sys.stderr.write("headless mode requires Windows\n")
        return 1

    win32.enable_dpi_awareness()
    win32.ensure_default_desktop()

    store = config_mod.ConfigStore(path=paths.config_file())
    logutil.configure(level=known.log_level or store.get("log_level") or "info")
    _write_pid(paths.pid_file())

    app = service_mod.Service(store=store, headless=True)
    app.engine.start()
    logutil.log("[engine] headless mode started; press Ctrl+C to stop")

    deadline = time.time() + known.duration if known.duration > 0 else None
    try:
        while True:
            time.sleep(0.5)
            if deadline and time.time() >= deadline:
                break
    except KeyboardInterrupt:
        pass
    finally:
        app.close()
        _clear_pid(paths.pid_file())
        logutil.log("[engine] headless mode exited")
    return 0


def run_legacy_loop(argv=None):
    """Compatibility shim for code that still calls the 1.x main_loop()."""
    return run_headless(argv)
