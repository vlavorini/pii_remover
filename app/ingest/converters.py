"""File conversion: PDF / image / text / spreadsheet / office -> text + page images."""
from __future__ import annotations

import io
import logging
import mimetypes
from dataclasses import dataclass, field
from pathlib import Path

from ..core.config import IngestionSettings

log = logging.getLogger(__name__)


class ConversionError(RuntimeError):
    pass


@dataclass
class ConvertedFile:
    kind: str = "text"
    mime_type: str = "text/plain"
    text: str = ""
    page_images: list[bytes] = field(default_factory=list)
    fallback_method: str = ""
    warnings: list[str] = field(default_factory=list)


_TEXT_EXT = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".yaml", ".yml",
             ".log", ".rtf", ".html", ".htm", ".xml"}
_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".heic"}
_SHEET_EXT = {".xlsx", ".xlsm", ".xls", ".ods"}
_DOC_EXT = {".docx", ".odt", ".pptx", ".epub"}

#: refuse anything that is neither text-ish, image-ish nor a known document container
_MAX_TEXT_CHARS = 400_000


def detect_kind(path: Path, mime_hint: str = "") -> tuple[str, str]:
    """Return (kind, mime_type). Prefers content sniffing over the extension."""
    suffix = path.suffix.lower()
    mime = mime_hint or mimetypes.guess_type(path.name)[0] or ""

    header = b""
    try:
        with path.open("rb") as fh:
            header = fh.read(4096)
    except OSError as exc:
        raise ConversionError(f"cannot read file: {exc}") from exc

    # 1) real PDFs carry binary markers; check them BEFORE the text heuristic,
    #    because a PDF's first 4 KB are mostly ASCII and can fool it
    if _has_pdf_binary_marker(header):
        return "pdf", "application/pdf"

    if _is_probably_text(header):
        # a text file whose content decides the kind wins over the extension
        if suffix in _SHEET_EXT:
            return "spreadsheet", mime
        if suffix in _DOC_EXT:
            return "document", mime
        return "text", mime or "text/plain"

    if _looks_like_zip(header) and suffix in _SHEET_EXT | _DOC_EXT:
        return ("spreadsheet" if suffix in _SHEET_EXT else "document"), mime
    if _looks_like_image(header) or suffix in _IMAGE_EXT:
        return "image", mime or "image/png"

    if suffix in _SHEET_EXT:
        return "spreadsheet", mime
    if suffix in _DOC_EXT:
        return "document", mime
    # a `%PDF-` prefix whose body is plain ASCII (hand-written fixtures) is not a
    # real PDF: treat it as text instead of failing the reader
    if suffix in _TEXT_EXT or mime.startswith("text/"):
        return "text", mime or "text/plain"
    if header.startswith(b"%PDF-"):
        return "pdf", "application/pdf"

    raise ConversionError(
        f"unsupported file type (extension={suffix or 'none'}, sniffed mime={mime or 'unknown'}). "
        "Supported: PDF, images, plain text/CSV/Markdown/JSON, XLSX/ODS, DOCX/ODT/PPTX."
    )


def convert(path: Path, *, mime_hint: str = "", settings: IngestionSettings) -> ConvertedFile:
    kind, mime = detect_kind(path, mime_hint)
    if kind == "pdf":
        return _convert_pdf(path, settings)
    if kind == "image":
        return _convert_image(path, settings)
    if kind == "spreadsheet":
        return _convert_spreadsheet(path)
    if kind == "document":
        return _convert_document(path, settings)
    return _convert_text(path)


# --------------------------------------------------------------------- helpers
def _looks_like_zip(header: bytes) -> bool:
    return header.startswith(b"PK\x03\x04")


def _looks_like_image(header: bytes) -> bool:
    signatures = (
        b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a",
        b"BM", b"II*\x00", b"MM\x00*", b"RIFF",
    )
    return any(header.startswith(sig) for sig in signatures)


def _is_probably_text(header: bytes) -> bool:
    if not header:
        return False
    if b"\x00" in header:
        return False
    printable = sum(1 for b in header if 9 <= b <= 13 or 32 <= b <= 126 or b >= 128)
    return printable / len(header) > 0.92


def _has_pdf_binary_marker(header: bytes) -> bool:
    """A textual `%PDF-` prefix is not enough: real PDFs carry binary markers.

    Files whose first bytes are ASCII (e.g. fixtures written with `%PDF-1.4\n` as
    text) must not be treated as PDFs - otherwise the reader fails and the file
    is ingested as plain text.
    """
    if not header.startswith(b"%PDF-"):
        return False
    return b"obj" in header or b"stream" in header or b"endobj" in header


def _downscale(raw: bytes, settings: IngestionSettings, fmt: str = "JPEG") -> bytes:
    """Bound image size so base64 payloads stay inside model limits."""
    try:
        from PIL import Image
    except Exception:  # pragma: no cover
        return raw
    try:
        img = Image.open(io.BytesIO(raw))
        img = img.convert("RGB") if fmt == "JPEG" else img
        edge = max(img.size)
        if edge > settings.max_image_edge:
            ratio = settings.max_image_edge / edge
            img = img.resize((max(1, int(img.width * ratio)), max(1, int(img.height * ratio))))
        buf = io.BytesIO()
        img.save(buf, format=fmt, quality=settings.jpeg_quality, optimize=True)
        return buf.getvalue()
    except Exception as exc:  # noqa: BLE001
        log.debug("image downscale skipped: %s", exc)
        return raw


# ----------------------------------------------------------------------- kinds
def _convert_pdf(path: Path, settings: IngestionSettings) -> ConvertedFile:
    result = ConvertedFile(kind="pdf", mime_type="application/pdf")
    pages = _pdf_page_count(path)
    limit = min(pages or settings.max_pages, settings.max_pages)
    if pages and pages > settings.max_pages:
        result.warnings.append(
            f"PDF has {pages} pages; only the first {settings.max_pages} were processed"
        )

    # 1) embedded text layer (cheap fallback, also kept for audit)
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        texts = []
        for page in reader.pages[:limit]:
            texts.append((page.extract_text() or "").strip())
        joined = "\n\n".join(t for t in texts if t).strip()
        if joined:
            result.text = joined
            result.fallback_method = "pdf_text_layer"
    except Exception as exc:  # noqa: BLE001
        result.warnings.append(f"text layer extraction failed: {type(exc).__name__}")

    # 2) page images for the visual LLM
    try:
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(str(path))
        for index in range(limit):
            page = pdf[index]
            scale = settings.render_dpi / 72.0
            bitmap = page.render(scale=scale)
            pil = bitmap.to_pil()
            buf = io.BytesIO()
            pil.convert("RGB").save(buf, format="JPEG", quality=settings.jpeg_quality,
                                    optimize=True)
            result.page_images.append(_downscale(buf.getvalue(), settings))
    except Exception as exc:  # noqa: BLE001
        result.warnings.append(f"page rendering failed: {type(exc).__name__}: {exc}")

    if not result.page_images and not result.text:
        raise ConversionError("PDF produced neither page images nor extractable text")
    return result


def _pdf_page_count(path: Path) -> int:
    try:
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(str(path))
        return len(pdf)
    except Exception:  # noqa: BLE001
        return 0


def _convert_image(path: Path, settings: IngestionSettings) -> ConvertedFile:
    raw = path.read_bytes()
    result = ConvertedFile(kind="image", mime_type=mimetypes.guess_type(path.name)[0] or "image/png")
    result.page_images.append(_downscale(raw, settings))
    return result


def _convert_text(path: Path) -> ConvertedFile:
    raw = path.read_bytes()
    for encoding in ("utf-8", "utf-16", "latin-1"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:  # pragma: no cover
        raise ConversionError("text file is not decodable as utf-8/utf-16/latin-1")

    if not text.strip():
        raise ConversionError("the file contains no readable text (it is empty or blank)")

    result = ConvertedFile(kind="text", mime_type="text/plain", text=text.strip(),
                           fallback_method="plain_text")
    if len(text) > _MAX_TEXT_CHARS:
        result.text = text[:_MAX_TEXT_CHARS]
        result.warnings.append(f"text truncated to {_MAX_TEXT_CHARS} characters")
    return result


def _convert_spreadsheet(path: Path) -> ConvertedFile:
    result = ConvertedFile(kind="spreadsheet",
                           mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    try:
        import pandas as pd
    except Exception as exc:  # pragma: no cover
        raise ConversionError(f"spreadsheet support unavailable: {exc}") from exc

    suffix = path.suffix.lower()
    engine = None
    if suffix in {".xlsx", ".xlsm"}:
        engine = "openpyxl"
    elif suffix == ".ods":
        engine = "odf"
    elif suffix == ".xls":
        engine = "xlrd"

    sheets = pd.read_excel(path, sheet_name=None, engine=engine, dtype=str)
    chunks: list[str] = []
    for name, frame in sheets.items():
        frame = frame.fillna("")
        preview_rows = frame.head(200)
        chunks.append(f"## Sheet: {name}")
        chunks.append(f"rows: {len(frame)}, columns: {len(frame.columns)}")
        chunks.append(f"column names: {', '.join(str(c) for c in frame.columns)}")
        chunks.append("")
        chunks.append(preview_rows.to_markdown(index=False))
        if len(frame) > len(preview_rows):
            chunks.append(f"\n_(preview of first {len(preview_rows)} rows only)_")
        chunks.append("")
    result.text = "\n".join(chunks).strip()
    result.fallback_method = "spreadsheet_tabular"
    return result


def _convert_document(path: Path, settings: IngestionSettings) -> ConvertedFile:
    """DOCX / ODT / PPTX: extract text, never touch macros."""
    suffix = path.suffix.lower()
    result = ConvertedFile(kind="document",
                           mime_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream")
    if suffix in {".docx", ".pptx"}:
        try:
            from docx import Document as DocxDocument  # python-docx
        except Exception:  # pragma: no cover
            DocxDocument = None  # type: ignore[assignment]
        if DocxDocument is not None and suffix == ".docx":
            doc = DocxDocument(str(path))
            lines: list[str] = [p.text for p in doc.paragraphs if p.text.strip()]
            for table in doc.tables:
                for row in table.rows:
                    lines.append(" | ".join(cell.text.strip() for cell in row.cells))
            result.text = "\n".join(lines)
            result.fallback_method = "docx_text"
            return result
        try:
            from pptx import Presentation  # python-pptx
        except Exception:  # pragma: no cover
            Presentation = None  # type: ignore[assignment]
        if Presentation is not None and suffix == ".pptx":
            prs = Presentation(str(path))
            chunks = []
            for idx, slide in enumerate(prs.slides, start=1):
                chunks.append(f"## Slide {idx}")
                for shape in slide.shapes:
                    if hasattr(shape, "text") and shape.text.strip():
                        chunks.append(shape.text.strip())
            result.text = "\n".join(chunks)
            result.fallback_method = "pptx_text"
            return result

    # generic fallback: strip XML from the zip payload
    try:
        import zipfile

        with zipfile.ZipFile(path) as zf:
            parts = [n for n in zf.namelist() if n.endswith((".xml", ".html", ".txt"))]
            pieces = []
            for name in parts[:40]:
                try:
                    raw = zf.read(name).decode("utf-8", errors="ignore")
                except Exception:  # noqa: BLE001
                    continue
                import re

                text = re.sub(r"<[^>]+>", " ", raw)
                text = re.sub(r"\s+", " ", text).strip()
                if text:
                    pieces.append(text)
            result.text = "\n".join(pieces)[:200_000]
            result.fallback_method = "office_xml_text"
    except Exception as exc:  # noqa: BLE001
        raise ConversionError(f"cannot extract text from {suffix} file: {exc}") from exc

    if not result.text.strip():
        raise ConversionError(f"no text found inside {suffix} file")
    return result
