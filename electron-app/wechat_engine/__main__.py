"""Engine CLI entry point.

    python -m wechat_engine --rpc                     # JSON-RPC daemon (used by Electron)
    python -m wechat_engine --call engine_status     # one-shot method call, JSON on stdout
    python -m wechat_engine --headless               # legacy watcher, no UI
"""

from __future__ import annotations

import argparse
import sys


def build_parser():
    parser = argparse.ArgumentParser(
        prog="wechat-engine",
        description="WeChat AI AutoReply engine (Win32 + RapidOCR backend)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--rpc", action="store_true", help="run the JSON-RPC stdio server")
    mode.add_argument("--call", metavar="METHOD", help="call one RPC method and exit")
    mode.add_argument("--headless", action="store_true", help="watch and reply without a UI")
    mode.add_argument("--selftest", action="store_true", help="print an environment report")
    parser.add_argument("params", nargs="?", default="{}", help="JSON params for --call")
    parser.add_argument("--version", action="store_true", help="print the engine version")
    return parser


def run_selftest():
    import json

    from . import logutil, paths, service, win32

    logutil.configure(level="warn", stream=sys.stderr)
    app = service.Service()
    try:
        report = {
            "ok": True,
            "hello": app.hello(),
            "diagnostics": app.diagnostics(),
            "win32": {
                "isWindows": win32.IS_WINDOWS,
                "dpiAwareness": win32.enable_dpi_awareness(),
                "dpiScale": win32.get_dpi_scale(),
                "wechat": win32.describe_wechat(),
            },
            "dataDir": str(paths.data_dir()),
        }
    finally:
        app.close()
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0


def force_utf8_streams():
    """Make stdout/stderr UTF-8 regardless of the console codepage.

    A Chinese Windows console defaults to cp936, and both the selftest JSON and the engine
    log lines contain emoji. Without this, print() raises UnicodeEncodeError and the
    process dies while reporting its own status; the 1.x scripts patched this call site by
    call site, which is why one missed site still crashed the daemon on startup.
    """
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _split_call(argv):
    """Pull the method name out of a --call invocation.

    argparse cannot express "the name after --call plus free-form JSON", and treating
    --call as a normal option silently bound the *option string itself* as the method,
    so `--call ping` dispatched a method literally named "--call" and fell through to
    daemon mode. Parsing it explicitly removes that whole class of confusion.
    """
    index = argv.index("--call")
    remainder = argv[index + 1:]
    if not remainder:
        return None, [], "缺少方法名：用法为 --call METHOD [JSON]"
    method = remainder[0]
    rest = remainder[1:]
    params = "{}"
    passthrough = []
    for item in rest:
        if item.startswith("--"):
            passthrough.append(item)
        elif params == "{}":
            params = item
        else:
            passthrough.append(item)
    return method, [method, params] + passthrough, None


def main(argv=None):
    force_utf8_streams()
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()

    if "--call" in argv:
        method, call_argv, error = _split_call(argv)
        if error:
            sys.stderr.write(error + "\n")
            return 2
        from . import runner

        return runner.run_call(call_argv)

    known, tail = parser.parse_known_args(argv)

    from . import __version__

    if known.version:
        print(__version__)
        return 0

    from . import runner

    if known.selftest:
        return run_selftest()
    if known.headless:
        return runner.run_headless(tail)
    return runner.run_rpc(tail)


if __name__ == "__main__":
    sys.exit(main())
