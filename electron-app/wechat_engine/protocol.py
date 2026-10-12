"""JSON-RPC 2.0 subset over line-delimited stdio.

The Electron main process spawns the engine and speaks this protocol. Design notes:

* **stdout is the transport.** Everything that wants to print must use the log bus,
  which the daemon configures onto stderr. A stray print() would otherwise corrupt the
  stream, so the daemon also redirects sys.stdout to stderr defensively.
* **Notifications carry no id and expect no reply.** Log lines and engine events are
  pushed as notifications so the UI updates without polling.
* **One request per line, UTF-8, no embedded newlines.** json.dumps never emits a raw
  newline, so the framing is safe.
* **Requests are handled on a thread pool**, so a slow AI call cannot block the log
  stream or a status query.
"""

from __future__ import annotations

import json
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, Optional

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


class Dispatcher:
    """Registry of method handlers plus the stdio transport."""

    def __init__(self, stream_in=None, stream_out=None, max_workers: int = 8):
        self._methods: Dict[str, Callable] = {}
        self._stream_in = stream_in if stream_in is not None else sys.stdin
        self._stream_out = stream_out if stream_out is not None else sys.stdout
        self._write_lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="rpc")
        self._pending = set()
        self._pending_lock = threading.Lock()
        self._stop = threading.Event()
        self._counter = 0
        self.closed = False

    # -- registration -----------------------------------------------------
    def register(self, name: str, handler: Callable):
        self._methods[name] = handler
        return self

    def method(self, name: str):
        def decorator(fn):
            self.register(name, fn)
            return fn

        return decorator

    def methods(self):
        return sorted(self._methods)

    # -- writing ----------------------------------------------------------
    def send(self, message: dict) -> None:
        if self.closed:
            return
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        with self._write_lock:
            try:
                self._stream_out.write(payload + "\n")
                self._stream_out.flush()
            except Exception:
                self.closed = True

    def notify(self, method: str, params: Any = None) -> None:
        """Push an event to the client. Never expects a reply."""
        self.send({"jsonrpc": "2.0", "method": method, "params": params})

    # -- reading ----------------------------------------------------------
    def serve_forever(self) -> None:
        for line in self._iter_lines():
            if self._stop.is_set():
                break
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                self.send({"jsonrpc": "2.0", "id": None, "error": {"code": PARSE_ERROR, "message": "invalid JSON"}})
                continue
            self._submit(message)
        self.drain()
        self.shutdown()

    def drain(self, timeout: float = 30.0) -> None:
        """Wait for in-flight requests to answer before closing.

        Handlers run on a thread pool, so returning from the read loop used to cancel
        every queued request and the client saw an EOF with no reply -- a config save
        could be silently discarded. Draining makes request/response ordering irrelevant
        to correctness.
        """
        deadline = time.time() + timeout
        while True:
            with self._pending_lock:
                pending = [future for future in self._pending if not future.done()]
            if not pending or time.time() >= deadline:
                return
            for future in pending:
                remaining = max(0.05, deadline - time.time())
                try:
                    future.result(timeout=min(0.5, remaining))
                except Exception:
                    pass

    def _iter_lines(self):
        stream = self._stream_in
        while not self._stop.is_set():
            try:
                line = stream.readline()
            except Exception:
                break
            if not line:
                break  # EOF: the parent closed the pipe
            yield line

    def _submit(self, message: Any) -> None:
        if not isinstance(message, dict):
            self.send({"jsonrpc": "2.0", "id": None, "error": {"code": INVALID_REQUEST, "message": "request must be an object"}})
            return
        request_id = message.get("id")
        method = message.get("method")
        params = message.get("params") or {}
        if not isinstance(method, str):
            self.send({"jsonrpc": "2.0", "id": request_id, "error": {"code": INVALID_REQUEST, "message": "method must be a string"}})
            return

        handler = self._methods.get(method)
        if handler is None:
            if request_id is not None:
                self.send({"jsonrpc": "2.0", "id": request_id, "error": {"code": METHOD_NOT_FOUND, "message": "unknown method: %s" % method}})
            return

        if request_id is None:
            # Notification: run inline so ordering is preserved for fire-and-forget calls.
            self._invoke(handler, params, None, method)
            return

        future = self._pool.submit(self._invoke, handler, params, request_id, method)
        with self._pending_lock:
            self._pending.add(future)
            future.add_done_callback(self._pending.discard)

    def _invoke(self, handler: Callable, params: Any, request_id, method: str) -> None:
        try:
            if not isinstance(params, dict):
                raise RpcError(INVALID_PARAMS, "params must be an object")
            result = handler(**params)
            if request_id is not None:
                self.send({"jsonrpc": "2.0", "id": request_id, "result": result})
        except RpcError as exc:
            if request_id is not None:
                self.send({"jsonrpc": "2.0", "id": request_id, "error": {"code": exc.code, "message": exc.message, "data": exc.data}})
        except TypeError as exc:
            if request_id is not None:
                self.send({"jsonrpc": "2.0", "id": request_id, "error": {"code": INVALID_PARAMS, "message": str(exc)}})
        except Exception as exc:
            tb = traceback.format_exc()
            if request_id is not None:
                self.send({"jsonrpc": "2.0", "id": request_id, "error": {"code": INTERNAL_ERROR, "message": str(exc), "data": tb}})
            self.notify("engine.error", {"method": method, "message": str(exc), "traceback": tb})

    # -- shutdown ---------------------------------------------------------
    def request_stop(self) -> None:
        self._stop.set()

    def shutdown(self) -> None:
        self.request_stop()
        with self._pending_lock:
            pending = list(self._pending)
        for future in pending:
            future.cancel()
        self._pool.shutdown(wait=False)
        self.closed = True
