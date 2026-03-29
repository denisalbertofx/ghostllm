from __future__ import annotations

import locale
import os
from typing import Optional, Tuple

_TEXT_FILE_EXTENSIONS = {
    ".bat",
    ".cjs",
    ".cfg",
    ".cmd",
    ".conf",
    ".css",
    ".csv",
    ".env",
    ".gitignore",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".jsx",
    ".md",
    ".mjs",
    ".ps1",
    ".py",
    ".pyi",
    ".sql",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".yaml",
    ".yml",
}

_TEXT_FILE_BASENAMES = {
    "dockerfile",
    "license",
    "makefile",
    "pipfile",
    "pyproject.toml",
    "pytest.ini",
    "readme",
    "readme.md",
    "requirements.txt",
    "setup.cfg",
    "setup.py",
    "tox.ini",
}


def path_looks_textual(path: str) -> bool:
    raw = str(path or "").strip()
    if not raw:
        return False
    base = os.path.basename(raw).lower()
    root, ext = os.path.splitext(base)
    if base in _TEXT_FILE_BASENAMES or root in _TEXT_FILE_BASENAMES:
        return True
    return ext.lower() in _TEXT_FILE_EXTENSIONS


def _likely_utf16_bytes(blob: bytes) -> bool:
    if len(blob) < 4:
        return False
    if blob.startswith((b"\xff\xfe", b"\xfe\xff")):
        return True
    even = blob[::2]
    odd = blob[1::2]
    if not even or not odd:
        return False
    even_ratio = even.count(0) / len(even)
    odd_ratio = odd.count(0) / len(odd)
    return max(even_ratio, odd_ratio) >= 0.45 and blob.count(0) / len(blob) >= 0.2


def _likely_utf32_bytes(blob: bytes) -> bool:
    if len(blob) < 8:
        return False
    if blob.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        return True
    quads = len(blob) // 4
    if quads < 2:
        return False
    a = blob[0::4].count(0) / len(blob[0::4])
    b = blob[1::4].count(0) / len(blob[1::4])
    c = blob[2::4].count(0) / len(blob[2::4])
    d = blob[3::4].count(0) / len(blob[3::4])
    zeros = sorted((a, b, c, d), reverse=True)
    return zeros[0] >= 0.6 and zeros[1] >= 0.6 and zeros[2] >= 0.6


def _is_probably_binary(blob: bytes, *, path: str = "") -> bool:
    if not blob:
        return False
    if _likely_utf16_bytes(blob) or _likely_utf32_bytes(blob):
        return False
    if b"\x00" in blob:
        return True
    control_bytes = sum(1 for b in blob if b < 9 or (13 < b < 32))
    if control_bytes and control_bytes / max(len(blob), 1) > 0.2 and not path_looks_textual(path):
        return True
    return False


def _decoded_text_looks_reasonable(text: str) -> bool:
    if "\x00" in text:
        return False
    if not text:
        return True
    bad_controls = sum(1 for ch in text if ord(ch) < 32 and ch not in "\n\r\t\f\b")
    return (bad_controls / len(text)) <= 0.02


def _candidate_encodings(blob: bytes, *, path: str = "") -> list[str]:
    encodings: list[str] = ["utf-8-sig", "utf-8"]
    if _likely_utf32_bytes(blob):
        encodings.extend(["utf-32", "utf-32-le", "utf-32-be"])
    if _likely_utf16_bytes(blob):
        encodings.extend(["utf-16", "utf-16-le", "utf-16-be"])
    preferred = str(locale.getpreferredencoding(False) or "").strip()
    if preferred:
        encodings.append(preferred)
    if path_looks_textual(path):
        encodings.extend(["cp1252", "latin-1"])
    seen: set[str] = set()
    ordered: list[str] = []
    for enc in encodings:
        key = enc.lower()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(enc)
    return ordered


def decode_text_bytes(blob: bytes, *, path: str = "") -> Tuple[Optional[str], Optional[str]]:
    if not blob:
        return "", "utf-8"
    if _is_probably_binary(blob, path=path):
        return None, None
    for enc in _candidate_encodings(blob, path=path):
        try:
            text = blob.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        if _decoded_text_looks_reasonable(text):
            return text, enc
    return None, None


def read_text_file_with_fallback(path: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    try:
        with open(path, "rb") as fh:
            blob = fh.read()
    except OSError as exc:
        return None, None, str(exc)
    text, encoding = decode_text_bytes(blob, path=path)
    if text is None:
        return None, None, f"File is not UTF-8 text: {os.path.basename(path)}"
    return text, encoding, None


def normalize_text_file_to_utf8(path: str) -> Tuple[bool, Optional[str], Optional[str]]:
    text, encoding, error = read_text_file_with_fallback(path)
    if text is None:
        return False, encoding, error
    normalized_encoding = str(encoding or "").strip().lower()
    if normalized_encoding.startswith("utf-8"):
        return False, encoding, None
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    return True, encoding, None
