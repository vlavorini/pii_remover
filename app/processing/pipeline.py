"""The orchestrated pipeline: ingestion -> detect/critique loop -> mask -> describe."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from ..core.config import Settings
from ..core.crypto import ArtifactCipher
from ..core.hashing import get_salt
from ..core.llm import LLMClient
from ..core.schemas import (
    DocumentDescription,
    ExtractedDocument,
    JobResult,
    MaskResult,
    PipelineTrace,
    PiiFinding,
)
from ..ingest.normalize import as_source_text
from ..ingest.pipeline import IngestionStage
from . import validators
from .agents import (
    CriticAgent,
    DescriberAgent,
    DetectorAgent,
    MaskerAgent,
    _mock_name_findings,
    _reviewable_findings,
    _surname_hints,
)

log = logging.getLogger(__name__)

#: rounds after which we stop even without critic acceptance
HARD_ROUND_CAP = 6


class DocumentPipeline:
    """Single entry point used by the API layer."""

    def __init__(self, settings: Settings, llm: LLMClient | None = None) -> None:
        self.settings = settings
        self.llm = llm or LLMClient(settings.llm)
        self.ingestion = IngestionStage(settings, self.llm)
        self.detector = DetectorAgent(settings, self.llm)
        self.critic = CriticAgent(settings, self.llm)
        self.masker = MaskerAgent(settings, self.llm)
        self.describer = DescriberAgent(settings, self.llm)

    # ------------------------------------------------------------------ public
    def process(self, path: Path, *, filename: str | None = None,
                mime_hint: str = "", job_id: str | None = None) -> JobResult:
        settings = self.settings
        trace = PipelineTrace()
        result = JobResult(doc_id="", status="pending")
        if job_id:
            result.job_id = job_id

        try:
            # ── 1. ingestion ────────────────────────────────────────────────
            trace.add("ingest", "running", f"converting {filename or path.name}")
            document = self.ingestion.ingest(path, filename=filename, mime_hint=mime_hint)
            result.doc_id = document.doc_id
            trace.add(
                "ingest",
                "done",
                f"kind={document.kind}, method={document.extraction_method}, "
                f"{document.char_count} chars",
                warnings=document.warnings,
                method=document.extraction_method,
            )

            source_text = as_source_text(document)
            if not source_text.strip():
                raise ValueError(
                    "extraction produced no text; the file may be empty, corrupt, or "
                    "require a vision model"
                )
            result.extraction = {
                "doc_id": document.doc_id,
                "filename": document.filename,
                "kind": document.kind,
                "mime_type": document.mime_type,
                "extraction_method": document.extraction_method,
                "extraction_notes": document.extraction_notes,
                "warnings": document.warnings,
                "char_count": document.char_count,
                "page_count": len(document.pages) or 1,
                "truncated": document.truncated,
            }

            # ── 2. deterministic validators (model-independent evidence) ────
            validated = validators.scan(source_text)
            trace.add(
                "validators",
                "done",
                f"{len(validated)} span(s) confirmed by regex/checksum",
                kinds=_kind_counts([v.kind for v in validated]),
            )

            # ── 3. detect <-> critique loop ────────────────────────────────
            findings = self._detect_critique_loop(source_text, validated, trace, result)

            # ── 4. masking ─────────────────────────────────────────────────
            trace.add("mask", "running", f"applying {len(findings)} finding(s)")
            mask_result = self.masker.mask(source_text, findings)
            residual = [f for f in mask_result.residual_findings if f.get("value")]
            trace.add(
                "mask",
                "done",
                f"{mask_result.total_masked} distinct value(s) masked",
                by_kind=mask_result.by_kind,
                residual=len(residual),
            )
            result.masking = mask_result.to_dict()

            # ── 5. description of the clean document ───────────────────────
            trace.add("describe", "running", "describing the cleaned document")
            description = self.describer.describe(
                mask_result.cleaned_text,
                document_type_guess=getattr(self.detector, "_last_type_guess", ""),
                by_kind=mask_result.by_kind,
                extraction_notes=document.extraction_notes,
            )
            trace.add("describe", "done", f"doc_type={description.doc_type}")
            result.description = description.to_dict()

            result.status = "done"
            trace.finish()
            result.trace = trace.to_dict()
            return result

        except Exception as exc:  # noqa: BLE001 - surfaced to the UI as a job error
            log.exception("pipeline failed for %s", path)
            result.status = "error"
            result.error = f"{type(exc).__name__}: {exc}"
            trace.add("error", "failed", result.error)
            trace.finish()
            result.trace = trace.to_dict()
            return result

    # ---------------------------------------------------------------- internals
    def _detect_critique_loop(
        self,
        source_text: str,
        validated: list,
        trace: PipelineTrace,
        result: JobResult,
    ) -> list[PiiFinding]:
        cfg = self.settings.processing
        findings: list[PiiFinding] = []
        feedback: list[str] = []
        rounds_payload: list[dict] = []

        for round_index in range(1, cfg.max_critic_rounds + 1):
            trace.add("detect", "running", f"round {round_index}: detector pass")
            findings, detector_payload, notes = self.detector.detect(
                source_text, feedback=feedback, previous=findings
            )
            self.detector._last_type_guess = detector_payload.get("document_type_guess", "")  # type: ignore[attr-defined]

            # second, deliberately narrow pass: person names are the category the
            # generalist detector misses most often, and a missed name is a leak
            findings = self._augment_names(source_text, findings, validated, trace)

            trace.add("critique", "running", f"round {round_index}: critic review")
            critique, merged = self.critic.review(
                source_text, findings, round_index=round_index, validated=validated
            )
            rounds_payload.append(
                {
                    "round": round_index,
                    "detector_findings": len(findings),
                    "critic_agreement": critique.agreement_score,
                    "critic_missing": len(critique.missing),
                    "critic_false_positives": len(critique.false_positives),
                    "critic_feedback": critique.feedback,
                    "accepted": critique.accepted,
                }
            )
            trace.add(
                "critique",
                "done",
                f"round {round_index}: agreement={critique.agreement_score:.2f}, "
                f"missing={len(critique.missing)}, "
                f"false_positives={len(critique.false_positives)}, "
                f"accepted={critique.accepted}",
            )

            # critic misses are always folded in (recall over precision for PII)
            findings = _reviewable_findings(
                self._reconcile(merged, critique, validated), source_text
            )

            if self._converged(critique, findings):
                trace.add("converge", "done",
                          f"detector and critic agree after {round_index} round(s)")
                break

            feedback = critique.feedback or [
                "Review your previous findings and add anything the critic flagged as missing."
            ]
            if round_index >= HARD_ROUND_CAP:
                trace.add("converge", "warn", "hard round cap reached without full agreement")
                break
        else:
            trace.add("converge", "warn",
                      f"round limit ({cfg.max_critic_rounds}) reached without critic acceptance")

        result.detections_rounds = rounds_payload
        return findings

    def _augment_names(self, source_text: str, findings: list[PiiFinding], validated: list,
                       trace: PipelineTrace) -> list[PiiFinding]:
        """Union the general detector's names with a dedicated name pass.

        Falls back to the general detector when the specialist returns nothing, so
        a weak or unavailable name model can never make recall worse than before.
        """
        generic_names = [f for f in findings if f.kind == "PERSON"]

        if self.settings.app.mock_mode:
            # offline: derive person names from surname-bearing structured values
            extra = _mock_name_findings(source_text, validated)
            trace.add("names", "done", f"mock name pass added {len(extra)} name(s)")
        elif not self.settings.llm.configured:
            trace.add("names", "skipped", "LLM not configured")
            return findings
        else:
            try:
                extra, stats = self.detector.detect_names(
                    source_text, regex_hints=_surname_hints(validated)
                )
                trace.add(
                    "names",
                    "done",
                    f"name pass added {len(extra)} name(s) ({stats.get('model', '?')})",
                )
            except Exception as exc:  # noqa: BLE001 - never lose the generic findings
                log.warning("name detector failed: %s", exc)
                trace.add("names", "warn", f"name pass failed: {exc}")
                return findings

        if not extra:
            return findings

        by_key = {(f.kind, f.value): f for f in findings}
        for finding in extra:
            key = (finding.kind, finding.value)
            existing = by_key.get(key)
            if existing is None:
                by_key[key] = finding
                continue
            # a name inside an email address is itself masked as EMAIL; to avoid
            # replacing the shorter string first, prefer the generic finding
            if finding.kind == "PERSON":
                existing.confidence = max(existing.confidence, finding.confidence)
        return list(by_key.values())

    def _reconcile(self, findings: list[PiiFinding], critique, validated: list) -> list[PiiFinding]:
        """Union of model findings + critic misses + validator hits; drop rejections."""
        rejected = {v for v in critique.false_positives if v}
        over = {v for v in critique.over_masking if v}

        by_key: dict[tuple[str, str], PiiFinding] = {}
        for finding in findings:
            if finding.value in rejected:
                continue
            by_key[(finding.kind, finding.value)] = finding

        # validator evidence outranks an LLM false-positive claim for structured kinds
        hard_kinds = {"EMAIL", "IBAN", "CREDIT_CARD", "PHONE", "SSN_NATIONAL_ID", "TAX_ID",
                      "IP_ADDRESS"}
        for span in validated:
            key = (span.kind, span.value)
            if key in by_key or span.value in over:
                continue
            if span.value in rejected and span.kind not in hard_kinds:
                continue
            by_key[key] = PiiFinding(kind=span.kind, value=span.value, start=span.start,
                                     end=span.end, confidence=span.confidence,
                                     source="normaliser")
        return list(by_key.values())

    def _converged(self, critique, findings: list[PiiFinding]) -> bool:
        cfg = self.settings.processing
        if critique.accepted:
            return True
        # accept when nothing substantive is missing and agreement is high
        if not critique.missing and critique.agreement_score >= cfg.agreement_threshold:
            return True
        return False

    # ------------------------------------------------------------ persistence
    def persist(self, result: JobResult, cipher: ArtifactCipher,
                output_dir: Path) -> dict[str, str]:
        """Encrypt the job artifacts at rest; return the paths written."""
        written: dict[str, str] = {}
        json_path = output_dir / f"{result.job_id}.json"
        cipher.dump_json(json_path, result.to_dict())
        written["result"] = str(json_path) + (".enc" if cipher.enabled else "")

        cleaned = (result.masking or {}).get("cleaned_text", "")
        if cleaned:
            text_path = output_dir / f"{result.job_id}.cleaned.txt"
            cipher.write_file(text_path, cleaned.encode("utf-8"))
            written["cleaned_text"] = str(text_path) + (".enc" if cipher.enabled else "")

        report = render_report(result)
        report_path = output_dir / f"{result.job_id}.report.md"
        cipher.write_file(report_path, report.encode("utf-8"))
        written["report"] = str(report_path) + (".enc" if cipher.enabled else "")
        return written


def _kind_counts(kinds: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for kind in kinds:
        counts[kind] = counts.get(kind, 0) + 1
    return counts


def render_report(result: JobResult) -> str:
    """Human-readable, PII-free report (safe to share or export)."""
    extraction = result.extraction or {}
    masking = result.masking or {}
    description = result.description or {}
    trace = result.trace or {}

    lines: list[str] = [
        "# PII removal report",
        "",
        f"- Job: `{result.job_id}`",
        f"- Status: {result.status}",
        f"- Source file: {extraction.get('filename', 'n/a')}",
        f"- Document kind: {extraction.get('kind', 'n/a')}",
        f"- Extraction method: {extraction.get('extraction_method', 'n/a')}",
        f"- Characters: {extraction.get('char_count', 'n/a')}",
        f"- Distinct values masked: {masking.get('total_masked', 0)}",
        f"- Critic rounds: {len(result.detections_rounds or [])}",
        f"- Duration: {trace.get('duration_seconds', 'n/a')} s",
        "",
        "## Masked categories",
        "",
    ]
    by_kind = masking.get("by_kind") or {}
    if by_kind:
        for kind, count in sorted(by_kind.items(), key=lambda kv: -kv[1]):
            lines.append(f"- {kind}: {count}")
    else:
        lines.append("- none detected")
    lines += ["", "## Description of the cleaned document", "",
              (description.get("markdown") or description.get("summary") or "n/a").strip(), ""]
    if extraction.get("warnings"):
        lines += ["## Warnings", ""] + [f"- {w}" for w in extraction["warnings"]] + [""]
    lines += [
        "## Note",
        "",
        "This report intentionally contains no original PII. The cleaned document is",
        "stored encrypted at rest and is never written as plaintext by the application.",
    ]
    return "\n".join(lines)
