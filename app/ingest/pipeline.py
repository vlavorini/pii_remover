"""Ingestion stage: any file in, normalised readable text out.

Pipeline: MIME sniff -> converter (PDF/image/text/spreadsheet) -> visual LLM
extraction (primary) -> embedded text layer (fallback) -> normalisation.
"""
from __future__ import annotations

import base64
import io
import logging
from pathlib import Path

from ..core.config import IngestionSettings, Settings
from ..core.llm import LLMClient, LLMError
from ..core.schemas import ExtractedDocument
from .converters import ConversionError, convert
from .normalize import normalize_text

log = logging.getLogger(__name__)

VISION_SYSTEM = (
    "You are a precise document transcription engine used in a data-privacy pipeline. "
    "Transcribe documents verbatim, preserving structure. Never summarise, never omit, "
    "never invent. If a region is illegible, write [illegible]."
)

VISION_PROMPT = """Transcribe this document page completely and faithfully.

Rules:
1. Output ONLY the document content as structured Markdown.
2. Reproduce every visible field, including names, addresses, account numbers, dates,
   identifiers, phone numbers and email addresses EXACTLY as printed. Do not redact.
3. Preserve layout semantics: tables as Markdown tables (repeat the header row),
   forms as `Field: value` lines, lists as bullet lists, headings as headings.
4. For images/logos/stamps/portraits, emit a short bracketed annotation such as
   [photo: portrait of a person] or [stamp: red circular seal].
5. Do not add commentary, preamble or closing remarks."""

VISION_PROMPT_PAGE = (
    VISION_PROMPT
    + "\n\nThis is page {page} of {total} of the same document. "
    "Transcribe only what is visible on this page."
)


class IngestionStage:
    def __init__(self, settings: Settings, llm: LLMClient) -> None:
        self.settings = settings
        self.llm = llm

    # ------------------------------------------------------------------ public
    def ingest(self, path: Path, *, filename: str | None = None,
               mime_hint: str = "") -> ExtractedDocument:
        ing: IngestionSettings = self.settings.ingestion
        doc = ExtractedDocument(filename=filename or path.name, mime_type=mime_hint)

        try:
            converted = convert(path, mime_hint=mime_hint, settings=ing)
        except ConversionError as exc:
            raise

        doc.kind = converted.kind
        doc.mime_type = converted.mime_type
        doc.warnings.extend(converted.warnings)

        # ---- primary: visual LLM -------------------------------------------------
        vision_ok = False
        if ing.strategy == "vision_first" and converted.page_images:
            if self.settings.app.mock_mode:
                doc.text = _mock_transcript(converted)
                doc.extraction_method = "mock"
                doc.extraction_notes.append("MOCK_MODE: vision LLM call skipped")
                vision_ok = True
            elif self.llm.configured:
                try:
                    pages = self._vision_extract(converted)
                    if pages and any(p.strip() for p in pages):
                        doc.pages = pages
                        doc.extraction_method = "vision_llm"
                        doc.extraction_notes.append(
                            f"vision model={self.llm.settings.vision_model}, "
                            f"pages={len(pages)}"
                        )
                        vision_ok = True
                except LLMError as exc:
                    doc.warnings.append(f"vision extraction failed: {exc}")
                    log.warning("vision extraction failed: %s", exc)
            else:
                doc.warnings.append("OPENAI_API_KEY not set - vision extraction skipped")

        # ---- fallback: embedded text layer / plain text --------------------------
        if not vision_ok:
            if converted.text:
                doc.text = converted.text
                doc.extraction_method = converted.fallback_method or "pdf_text_layer"
                doc.extraction_notes.append("fell back to embedded text layer")
            elif self.settings.app.mock_mode:
                doc.text = _mock_transcript(converted)
                doc.extraction_method = "mock"
            elif converted.page_images:
                try:
                    pages = self._vision_extract(converted)
                    doc.pages = pages
                    doc.extraction_method = "vision_llm"
                    vision_ok = True
                except LLMError as exc:
                    doc.warnings.append(f"fallback vision extraction failed: {exc}")
            if not doc.text and not doc.pages:
                doc.warnings.append("no text could be extracted from this file")
                doc.text = ""
        else:
            # text-first providers may still want the cheap layer recorded
            if converted.text and ing.strategy == "text_first":
                doc.text = converted.text

        doc = normalize_text(doc)
        return doc.finalise()

    # ----------------------------------------------------------------- vision
    def _vision_extract(self, converted) -> list[str]:
        total = len(converted.page_images)
        pages: list[str] = []
        for index, raw in enumerate(converted.page_images, start=1):
            prompt = VISION_PROMPT if total == 1 else VISION_PROMPT_PAGE.format(
                page=index, total=total
            )
            resp = self.llm.vision(
                prompt,
                [raw],
                mime="image/jpeg",
                temperature=0.0,
                system=VISION_SYSTEM,
            )
            pages.append((resp.text or "").strip())
        return pages


def _mock_transcript(converted) -> str:
    """Deterministic offline transcript so the pipeline is testable without a model."""
    if converted.text:
        return converted.text
    lines = [
        "# (mock transcript)",
        "",
        f"Mock mode produced no OCR text for {len(converted.page_images)} page image(s).",
        "",
        "## Sample Record",
        "",
        "| Field | Value |",
        "| --- | --- |",
        "| Full name | Mario Rossi |",
        "| Email | mario.rossi@example.com |",
        "| Phone | +39 333 1234567 |",
        "| IBAN | IT60X0542811101000000123456 |",
        "| Date of birth | 14/03/1985 |",
        "| Address | Via Roma 42, 20100 Milano, Italy |",
        "",
        "## Notes",
        "",
        "Mock-mode content used to exercise the PII pipeline end to end.",
    ]
    return "\n".join(lines)
