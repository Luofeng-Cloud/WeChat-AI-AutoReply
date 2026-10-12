"""Log reading: byte offsets, truncation recovery and encoding boundaries."""

from __future__ import annotations

import io

from wechat_engine import logutil


def test_incremental_read_returns_only_new_bytes(tmp_path):
    path = tmp_path / "a.log"
    path.write_bytes(b"first\n")
    text, offset = logutil.read_new(path, 0)
    assert text == "first\n"
    path.write_bytes(b"first\nsecond\n")
    text2, offset2 = logutil.read_new(path, offset)
    assert text2 == "second\n"
    assert offset2 > offset


def test_reading_past_the_end_returns_nothing(tmp_path):
    path = tmp_path / "a.log"
    path.write_bytes(b"x\n")
    text, _offset = logutil.read_new(path, 999)
    assert text == ""


def test_truncation_self_corrects_without_duplicating(tmp_path):
    """After a truncation the reader must not replay the file, and must not get stuck."""
    path = tmp_path / "a.log"
    path.write_bytes(b"old content\n")
    _text, offset = logutil.read_new(path, 0)
    assert offset == len(b"old content\n")

    path.write_bytes(b"new\n")
    text, new_offset = logutil.read_new(path, offset)
    assert text == ""
    assert new_offset == len(b"new\n")

    # The corrected offset now tracks appends normally.
    path.write_bytes(b"new\nmore\n")
    text2, _offset2 = logutil.read_new(path, new_offset)
    assert text2 == "more\n"


def test_multibyte_boundary_is_not_split(tmp_path):
    """A byte offset must never land mid-character, which produced mojibake in 1.x."""
    path = tmp_path / "a.log"
    path.write_bytes("中文日志一行\n".encode("utf-8"))
    _text, offset = logutil.read_new(path, 0)
    assert offset == len("中文日志一行\n".encode("utf-8"))


def test_partial_trailing_character_is_not_consumed(tmp_path):
    path = tmp_path / "a.log"
    path.write_bytes("好的".encode("utf-8")[:-1])
    text, offset = logutil.read_new(path, 0)
    assert text == "好"
    assert offset == len("好".encode("utf-8"))


def test_missing_file_is_tolerated(tmp_path):
    text, offset = logutil.read_new(tmp_path / "nope.log", 0)
    assert text == "" and offset == 0


def test_bus_writes_utf8_even_on_a_cp936_console(tmp_path, monkeypatch):
    """Emoji used to raise UnicodeEncodeError and kill the daemon on startup."""
    class Cp936Stream(io.TextIOBase):
        def write(self, text):
            text.encode("gbk")  # raises for characters outside cp936
            return len(text)

    bus = logutil.LogBus(path=tmp_path / "b.log", level="info", stream=Cp936Stream())
    try:
        bus.info("🚀 启动成功")
    finally:
        bus.close()
    assert "启动成功" in (tmp_path / "b.log").read_text(encoding="utf-8")


def test_bus_level_filtering(tmp_path):
    bus = logutil.LogBus(path=tmp_path / "c.log", level="warn", stream=io.StringIO())
    try:
        bus.info("hidden")
        bus.warn("shown")
    finally:
        bus.close()
    content = (tmp_path / "c.log").read_text(encoding="utf-8")
    assert "hidden" not in content
    assert "shown" in content


def test_subscribers_receive_lines(tmp_path):
    bus = logutil.LogBus(path=tmp_path / "d.log", level="info", stream=io.StringIO())
    seen = []
    unsubscribe = bus.subscribe(lambda level, line: seen.append((level, line)))
    try:
        bus.info("payload")
    finally:
        unsubscribe()
        bus.close()
    assert seen and "payload" in seen[0][1]
