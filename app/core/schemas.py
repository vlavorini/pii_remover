"""Shared data contracts between ingestion, processing and output stages."""
from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

PiiKind = Literal[
    "PERSON",
    "EMAIL",
    "PHONE",
    "ADDRESS",
    "SSN_NATIONAL_ID",
    "PASSPORT",
    "DRIVING_LICENCE",
    "TAX_ID",
    "IBAN",
    "CREDIT_CARD",
    "DOB",
    "BANK_ACCOUNT",
    "MEDICAL",
    "BIOMETRIC",
    "USERNAME",
    "IP_ADDRESS",
    "LICENCE_PLATE",
    "EMPLOYER",
    "PHOTO",
    "OTHER",
]

PII_KINDS: tuple[str, ...] = PiiKind.__args__  # type: ignore[attr-defined]


def _now() -> float:
    return time.time()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


@dataclass
class ExtractedDocument:
    """Normalised, model-readable view of the uploaded file."""

    doc_id: str = field(default_factory=lambda: new_id("doc"))
    filename: str = ""
    mime_type: str = ""
    kind: str = "text"  # pdf | image | text | spreadsheet | document
    text: str = ""
    pages: list[str] = field(default_factory=list)
    extraction_method: str = ""  # vision_llm | pdf_text_layer | plain_text | spreadsheet | mock
    extraction_notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    truncated: bool = False
    char_count: int = 0
    created_at: float = field(default_factory=_now)

    def finalise(self) -> "ExtractedDocument":
        if not self.pages and self.text:
            self.pages = [self.text]
        if not self.text and self.pages:
            self.text = "\n\n".join(self.pages)
        self.char_count = len(self.text or "")
        return self

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PiiFinding:
    """One detected PII occurrence in the extracted text."""

    kind: str
    value: str
    start: int | None = None
    end: int | None = None
    context: str = ""
    confidence: float = 0.5
    source: str = "detector"  # detector | critic | normaliser
    finding_id: str = field(default_factory=lambda: new_id("find"))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Critique:
    """Critic agent verdict over one detection round."""

    round_index: int = 0
    agreement_score: float = 0.0
    missing: list[dict[str, Any]] = field(default_factory=list)  # likely-missed PII (critic proposal)
    false_positives: list[str] = field(default_factory=list)  # finding_ids critic rejects
    over_masking: list[str] = field(default_factory=list)
    feedback: list[str] = field(default_factory=list)
    accepted: bool = False
    raw: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MaskChange:
    """One applied replacement in the cleaned text."""

    finding_id: str = ""
    kind: str = ""
    original: str = ""
    placeholder: str = ""
    start: int | None = None
    end: int | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # never ship raw PII in the masking map exposed to the UI audit panel
        d["original"] = _preview(self.original)
        return d


def _preview(value: str, keep: int = 2) -> str:
    """Short, non-reversible preview used only in audit metadata."""
    if not value:
        return ""
    if len(value) <= keep * 2:
        return "*" * len(value)
    return f"{value[:keep]}{'*' * (len(value) - keep * 2)}{value[-keep:]}"


@dataclass
class MaskResult:
    cleaned_text: str = ""
    changes: list[MaskChange] = field(default_factory=list)
    by_kind: dict[str, int] = field(default_factory=dict)
    residual_findings: list[dict[str, Any]] = field(default_factory=list)
    rounds_used: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def total_masked(self) -> int:
        """Distinct values replaced (one MaskChange per distinct value)."""
        return len(self.changes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "cleaned_text": self.cleaned_text,
            "changes": [c.to_dict() for c in self.changes],
            "by_kind": self.by_kind,
            "residual_findings": self.residual_findings,
            "notes": self.notes,
            "rounds_used": self.rounds_used,
            "total_masked": self.total_masked,
        }


@dataclass
class PipelineTrace:
    """Step-by-step record rendered in the Architecture panel."""

    stages: list[dict[str, Any]] = field(default_factory=list)
    started_at: float = field(default_factory=_now)
    finished_at: float | None = None

    def add(self, stage: str, status: str, detail: str = "", **extra: Any) -> None:
        entry: dict[str, Any] = {
            "stage": stage,
            "status": status,
            "detail": detail,
            "at": _now(),
            **extra,
        }
        self.stages.append(entry)

    def finish(self) -> None:
        self.finished_at = _now()

    @property
    def duration_seconds(self) -> float:
        return (self.finished_at or _now()) - self.started_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "stages": self.stages,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": round(self.duration_seconds, 3),
        }


@dataclass
class JobResult:
    job_id: str = field(default_factory=lambda: new_id("job"))
    doc_id: str = ""
    status: str = "pending"  # pending | done | error
    error: str = ""
    extraction: dict[str, Any] = field(default_factory=dict)
    masking: dict[str, Any] = field(default_factory=dict)
    trace: dict[str, Any] = field(default_factory=dict)
    detections_rounds: list[dict[str, Any]] = field(default_factory=list)
    encryption: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
