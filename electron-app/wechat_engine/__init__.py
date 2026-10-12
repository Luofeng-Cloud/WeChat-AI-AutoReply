"""WeChat AI AutoReply - Python engine package.

The engine owns every OS-level capability (Win32 message routing, DWM/PrintWindow
capture, RapidOCR inference) and every decision that must not be duplicated in the
Electron layer. The Electron main process only supervises it and talks to it over a
line-delimited JSON protocol (see :mod:wechat_engine.protocol).
"""

__version__ = "2.0.0"
__all__ = ["__version__"]
