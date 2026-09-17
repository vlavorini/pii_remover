"""Text normalisation applied after extraction, before any PII analysis.

Normalisation here is deliberately lossless with respect to data content: it fixes
line endings, spacing and unicode form, but never rewrites numbers or identifiers.
"""
from __future__ import annotations

import re
import unicodedata

from ..core.schemas import ExtractedDocument

_HYPHEN_BREAK = re.compile(r"(\w)-\n(\w)")
_SOFT_HYPHEN = "\u00ad"
_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff"), None)
_MANY_BLANK_LINES = re.compile(r"\n{3,}")
_TRAILING_WS = re.compile(r"[ \t]+$", re.MULTILINE)


def normalize_text(doc: ExtractedDocument) -> ExtractedDocument:
    """NFC-normalise, de-hyphenate, and tidy whitespace across pages and text."""
    if doc.pages:
        doc.pages = [_normalize_chunk(page) for page in doc.pages]
    if doc.text:
        doc.text = _normalize_chunk(doc.text)
    return doc


def _normalize_chunk(text: str) -> str:
    text = unicodedata.normalize("NFC", text)
    text = text.replace(_SOFT_HYPHEN, "").translate(_ZERO_WIDTH)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _HYPHEN_BREAK.sub(r"\1\2", text)  # join words broken across lines
    text = _TRAILING_WS.sub("", text)
    text = _MANY_BLANK_LINES.sub("\n\n", text)
    return text.strip()


def as_source_text(doc: ExtractedDocument) -> str:
    """The canonical text the detector/critic/masker operate on.

    Page markers are inserted so findings keep a locator after masking.
    """
    if doc.pages:
        if len(doc.pages) == 1:
            return doc.pages[0]
        out: list[str] = []
        for index, page in enumerate(doc.pages, start=1):
            out.append(f"<!-- page {index} -->")
            out.append(page)
        return "\n\n".join(out)
    return doc.text or ""
