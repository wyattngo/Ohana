"""Document text extraction (M2). PDF/docx/xlsx → text, hardened against zip-bomb / XXE / hang."""

from parsing.extract import ParseError, extract_text

__all__ = ["ParseError", "extract_text"]
