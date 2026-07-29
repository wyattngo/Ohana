"""Text → chunks for embedding.

Paragraph-first splitter: split on blank-line boundaries, then break paragraphs longer
than `max_chars` on sentence boundaries. Deliberately naive for GĐ0 — good enough to hit
the Wiki-RAG happy-path gate; will get replaced by a semantic-aware splitter (spec 03+)
once real wiki content is on-disk and we can measure recall.
"""

from __future__ import annotations

import re

_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")


def chunk_text(text: str, *, max_chars: int = 800) -> list[str]:
    """Split `text` into chunks of at most `max_chars`. Returns [] for empty input."""
    if not text or not text.strip():
        return []
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")

    chunks: list[str] = []
    for para in (p.strip() for p in text.split("\n\n")):
        if not para:
            continue
        if len(para) <= max_chars:
            chunks.append(para)
            continue
        # Long paragraph → walk sentences, packing greedily up to max_chars.
        buf = ""
        for sent in _SENT_SPLIT.split(para):
            if not sent:
                continue
            if len(buf) + len(sent) + 1 > max_chars and buf:
                chunks.append(buf.strip())
                buf = sent
            else:
                buf = f"{buf} {sent}".strip() if buf else sent
        if buf:
            chunks.append(buf.strip())
    return chunks
