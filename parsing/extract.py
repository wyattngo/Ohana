"""Text extraction hardened for user-controlled bytes.

Defenses:
- zip-bomb: reject if total uncompressed size > max_decompressed, BEFORE inflating.
- XXE / billion-laughs: docx XML via defusedxml (forbid_dtd/entities/external → raises, no expand).
- classify by CONTENT (zip namelist / %PDF-), never trust the declared MIME.
- caller wraps extract_text() in a timeout (backstop for malformed-parser hangs).
Caps (max_decompressed, max_text_chars) are INJECTED → module stays pure/testable (no settings).

Supports PDF + docx. xlsx path stripped (openpyxl removed at bootstrap, Wyatt approved
2026-07-16). Re-add openpyxl + _xlsx() when an xlsx upload flow lands.
"""

from __future__ import annotations

import io
import zipfile

from defusedxml import defuse_stdlib
from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring as defused_fromstring
from pypdf import PdfReader

defuse_stdlib()  # harden stdlib xml.* process-wide

_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


class ParseError(Exception):
    """Parse failure with a stable code (UNSUPPORTED|FILE_TOO_COMPLEX|ENCRYPTED|PARSE_FAILED)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _open_zip(data: bytes) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ParseError("PARSE_FAILED", "not a valid zip container") from exc


def _zip_bomb_guard(zf: zipfile.ZipFile, max_decompressed: int) -> None:
    total = sum(
        zi.file_size for zi in zf.infolist()
    )  # uncompressed; reading infolist does NOT inflate
    if total > max_decompressed:
        raise ParseError("FILE_TOO_COMPLEX", "decompressed size exceeds limit")


def _classify(data: bytes) -> str:
    if data[:5] == b"%PDF-":
        return "pdf"
    if data[:4] == b"PK\x03\x04":
        names = set(_open_zip(data).namelist())
        if "word/document.xml" in names:
            return "docx"
        raise ParseError("UNSUPPORTED", "zip is not docx (xlsx support deferred)")
    raise ParseError("UNSUPPORTED", "unrecognized file content")


def _docx(data: bytes, max_decompressed: int) -> str:
    zf = _open_zip(data)
    _zip_bomb_guard(zf, max_decompressed)
    try:
        xml_bytes = zf.read("word/document.xml")
    except KeyError as exc:
        raise ParseError("PARSE_FAILED", "missing word/document.xml") from exc
    try:
        root = defused_fromstring(
            xml_bytes, forbid_dtd=True, forbid_entities=True, forbid_external=True
        )
    except DefusedXmlException as exc:
        raise ParseError("PARSE_FAILED", f"xml blocked: {type(exc).__name__}") from exc
    paras = []
    for p in root.iter(f"{_W_NS}p"):
        runs = "".join(t.text for t in p.iter(f"{_W_NS}t") if t.text)
        if runs:
            paras.append(runs)
    return "\n".join(paras)


def _pdf(data: bytes) -> str:
    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        raise ParseError("ENCRYPTED", "pdf is encrypted")
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def extract_text(data: bytes, *, max_decompressed: int, max_text_chars: int) -> str:
    """Classify by content → extract → truncate. Any failure → ParseError (never a raw crash)."""
    try:
        kind = _classify(data)
        if kind == "pdf":
            text = _pdf(data)
        else:  # docx (xlsx branch stripped at bootstrap — see module docstring)
            text = _docx(data, max_decompressed)
    except ParseError:
        raise
    except Exception as exc:  # catch-all boundary: malformed input → structured error, không crash
        raise ParseError("PARSE_FAILED", type(exc).__name__) from exc
    if len(text) > max_text_chars:
        text = text[:max_text_chars] + "\n…[truncated]"
    return text
