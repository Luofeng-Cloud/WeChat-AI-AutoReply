"""Logging with byte-offset continuity.

The version 1.x daemon appended lines to wechat_bot_dev.log while the GUI followed the
same file with a saved offset. When the user pressed "clear logs" the file was
truncated, the offset became larger than the file, and the GUI reset it to 0 only after
that first poll had already returned nothing. Worse, the offset was tracked in
characters rather than bytes, so any non-ASCII line silently desynchronised the reader
and produced mojibake in the log pane.

This module fixes both: it writes UTF-8 with a strictly append-only contract, ignores
the console encoding entirely (legacy cp936 consoles used to raise UnicodeEncodeError on
emoji), and exposes the helpers a reader needs to stay in sync.
"""

from __future__ import annotations

import io
import os
import sys
import threading
import time
from pathlib import Path

from . import paths

_LEVELS = {"debug": 10, "info": 20, "success": 25, "warn": 30, "error": 40}


class LogBus:
    """Thread-safe append-only log sink with optional in-process subscribers."""

    def __init__(self, path=None, level="info", stream=None):
        self._lock = threading.RLock()
        self.path = Path(path) if path is not None else paths.log_file()
        self.level = _LEVELS.get(level, 20)
        self._subscribers = []
        # The RPC transport owns stdout, so the daemon mirrors to stderr instead.
        self._stdout = stream if stream is not None else sys.stdout
        self._file = None
        self._open()

    def _open(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # newline="" keeps a newline as a single byte so offsets stay predictable.
            self._file = open(self.path, "a", encoding="utf-8", errors="replace", newline="")
        except Exception:
            self._file = None

    def close(self):
        with self._lock:
            if self._file is not None:
                try:
                    self._file.flush()
                    self._file.close()
                except Exception:
                    pass
                self._file = None

    def reopen(self):
        with self._lock:
            self.close()
            self._open()

    def subscribe(self, fn):
        with self._lock:
            self._subscribers.append(fn)

        def _unsubscribe():
            with self._lock:
                if fn in self._subscribers:
                    self._subscribers.remove(fn)

        return _unsubscribe

    def emit(self, level, message):
        if _LEVELS.get(level, 20) < self.level:
            return
        line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), message)
        with self._lock:
            if self._file is not None:
                try:
                    self._file.write(line + "\n")
                    self._file.flush()
                except Exception:
                    # Disk full or file locked: keep going, console and subscribers
                    # are still useful and the daemon must not die over logging.
                    self._open()
            for fn in list(self._subscribers):
                try:
                    fn(level, line)
                except Exception:
                    pass
        self._console(line)

    def _console(self, line):
        stream = self._stdout
        if stream is None:
            return
        try:
            stream.write(line + "\n")
            stream.flush()
        except Exception:
            try:
                stream.write(line.encode("utf-8", "replace").decode("utf-8", "replace") + "\n")
                stream.flush()
            except Exception:
                pass

    def debug(self, message):
        self.emit("debug", message)

    def info(self, message):
        self.emit("info", message)

    def success(self, message):
        self.emit("success", message)

    def warn(self, message):
        self.emit("warn", message)

    def error(self, message):
        self.emit("error", message)

    def banner(self, title, lines=()):
        self.emit("info", "=" * 60)
        self.emit("info", title)
        for line in lines:
            self.emit("info", line)
        self.emit("info", "=" * 60)


_BUS = None
_BUS_LOCK = threading.Lock()


def get_bus():
    global _BUS
    with _BUS_LOCK:
        if _BUS is None:
            _BUS = LogBus()
        return _BUS


def configure(path=None, level="info", stream=None):
    """(Re)create the process-wide bus. Call once, early, from an entry point."""
    global _BUS
    with _BUS_LOCK:
        if _BUS is not None:
            _BUS.close()
        _BUS = LogBus(path=path, level=level, stream=stream)
        return _BUS


def debug(message):
    get_bus().debug(message)


def info(message):
    get_bus().info(message)


def warn(message):
    get_bus().warn(message)


def error(message):
    get_bus().error(message)


def success(message):
    get_bus().success(message)


def log(message):
    """Version 1.x compatible single-argument logger."""
    get_bus().info(message)


def _safe_cut(chunk, limit):
    """Largest prefix of chunk that ends on a UTF-8 character boundary."""
    cut = min(len(chunk), limit)
    # Drop trailing continuation bytes.
    while cut > 0 and (chunk[cut - 1] & 0xC0) == 0x80:
        cut -= 1
    # Drop a trailing truncated multi-byte head (110xxxxx / 1110xxxx / 11110xxx).
    if cut > 0 and (chunk[cut - 1] & 0xC0) == 0xC0:
        cut -= 1
    return cut


def read_new(path, offset, limit=256 * 1024):
    """Return (new_text, new_offset) for an append-only UTF-8 log file.

    The offset is a byte offset. Decoding uses errors="replace" so a torn multi-byte
    character at the tail degrades to U+FFFD instead of raising.
    """
    path = Path(path)
    try:
        size = os.path.getsize(path)
    except OSError:
        return "", 0
    if offset >= size:
        # At the end, or holding an offset that no longer exists because the file shrank
        # ("clear logs", rotation, or a stale offset across restarts). Reporting EOF and
        # handing back the true size is self-correcting: the next poll sees a real offset
        # and picks up anything appended after the truncation.
        return "", size
    try:
        with open(path, "rb") as handle:
            handle.seek(offset)
            chunk = handle.read(max(1, min(size - offset, limit)))
    except OSError:
        return "", offset
    cut = _safe_cut(chunk, len(chunk))
    return chunk[:cut].decode("utf-8", "replace"), offset + cut


def truncate(path):
    """Clear the log file without breaking the append handle held by the bus."""
    bus = get_bus()
    path = Path(path)
    with bus._lock:
        try:
            if bus._file is not None:
                bus._file.close()
        except Exception:
            pass
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("", encoding="utf-8")
        except Exception:
            pass
        bus._open()
