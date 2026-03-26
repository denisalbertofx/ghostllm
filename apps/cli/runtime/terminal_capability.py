"""
Windows / legacy consoles: avoid UnicodeEncodeError from Rich (rounded borders, emoji).

- Prefer UTF-8 on stdout/stderr when possible (reconfigure or TextIOWrapper on buffer).
- If UTF-8 cannot be applied and the console encoding is still legacy, use ASCII UI
  (GHOST_ASCII_UI=1 forces that path for presentation).
"""
from __future__ import annotations

import io
import os
import sys
from typing import Optional

_CONFIGURED: bool = False
_ASCII_UI: Optional[bool] = None
_NOTICE_EMITTED: bool = False


def _env_forces_ascii_ui() -> bool:
    v = os.environ.get("GHOST_ASCII_UI", "").strip().lower()
    return v in ("1", "true", "yes", "on", "force")


def _encoding_is_utf8(enc: Optional[str]) -> bool:
    if not enc:
        return False
    return enc.lower().replace("_", "-") in ("utf-8", "utf8")


def _stream_encoding(stream: object) -> Optional[str]:
    try:
        return getattr(stream, "encoding", None)
    except Exception:
        return None


def _try_reconfigure_utf8(stream: object) -> bool:
    if stream is None or not hasattr(stream, "reconfigure"):
        return False
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
        return _encoding_is_utf8(_stream_encoding(stream))
    except (OSError, ValueError, AttributeError, TypeError):
        return False


def _try_wrap_buffer_utf8(stream: object, *, name: str) -> object:
    buf = getattr(stream, "buffer", None)
    if buf is None:
        return stream
    if _encoding_is_utf8(_stream_encoding(stream)):
        return stream
    try:
        line_buffering = name == "stdout"
        return io.TextIOWrapper(
            buf,
            encoding="utf-8",
            errors="replace",
            line_buffering=line_buffering,
            write_through=False,
        )
    except Exception:
        return stream


def _apply_windows_stdio_utf8() -> None:
    global sys
    _try_reconfigure_utf8(sys.stdout)
    _try_reconfigure_utf8(sys.stderr)
    if _encoding_is_utf8(_stream_encoding(sys.stdout)):
        return
    out = _try_wrap_buffer_utf8(sys.stdout, name="stdout")
    err = _try_wrap_buffer_utf8(sys.stderr, name="stderr")
    if out is not sys.stdout:
        sys.stdout = out
    if err is not sys.stderr:
        sys.stderr = err


def _emit_limited_console_notice(*, user_forced_ascii: bool) -> None:
    global _NOTICE_EMITTED
    if _NOTICE_EMITTED or user_forced_ascii:
        return
    _NOTICE_EMITTED = True
    msg = (
        "GhostLLM: limited console encoding — using plain ASCII borders (no rounded/emoji UI). "
        "For full styling use Windows Terminal, or run via ghost.bat (UTF-8), or set PYTHONUTF8=1.\n"
    )
    try:
        sys.stderr.write(msg)
        sys.stderr.flush()
    except Exception:
        pass


def configure_cli_stdio_early() -> None:
    """
    Call once at process entry (e.g. under ``if __name__ == \"__main__\"``).
    Idempotent. Does not import Rich.
    """
    global _CONFIGURED, _ASCII_UI
    if _CONFIGURED:
        return
    _CONFIGURED = True

    user_forced = _env_forces_ascii_ui()

    if sys.platform == "win32" and not _encoding_is_utf8(_stream_encoding(sys.stdout)):
        _apply_windows_stdio_utf8()

    if sys.platform != "win32":
        _ASCII_UI = bool(user_forced)
        return

    if user_forced:
        _ASCII_UI = True
        return

    if _encoding_is_utf8(_stream_encoding(sys.stdout)):
        _ASCII_UI = False
        return

    _ASCII_UI = True
    _emit_limited_console_notice(user_forced_ascii=False)


def ghost_ui_uses_ascii() -> bool:
    """True when borders/emoji should be avoided for safe rendering."""
    global _CONFIGURED, _ASCII_UI
    if not _CONFIGURED:
        configure_cli_stdio_early()
    return bool(_ASCII_UI)
