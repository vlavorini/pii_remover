"""The three cooperating agents: detector, critic, masker."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from ..core.config import ProcessingSettings, Settings
from ..core.hashing import pseudonym
from ..core.llm import LLMClient, LLMError, extract_json
from ..core.schemas import (
    Critique,
    MaskChange,
    MaskResult,
    PiiFinding,
)
from . import prompt_registry, validators

log = logging.getLogger(__name__)


def _clip(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    head = text[:int(limit * 0.7)]
    tail = text[-int(limit * 0.3):]
    return f"{head}\n\n[... {len(text) - limit} characters omitted ...]\n\n{tail}", True


# ═══════════════════════════════════════════════════════════════════════ DETECTOR
@dataclass
class DetectorAgent:
    settings: Settings
    llm: LLMClient
    name: str = "detector"

    def detect(self, document: str, *, feedback: list[str] | None = None,
               previous: list[PiiFinding] | None = None,
               max_chars: int = 60_000) -> tuple[list[PiiFinding], dict[str, Any], list[str]]:
        spec = prompt_registry.load("pii_detector")
        doc_text, truncated = _clip(document, max_chars)
        notes: list[str] = []
        if truncated:
            notes.append(f"document truncated to {max_chars} characters for the detector")

        if self.settings.app.mock_mode:
            findings = _mock_findings(doc_text)
            return findings, {"document_type_guess": "mock document", "mock": True}, notes

        body = spec.render(document=doc_text)
        if feedback:
            body += (
                "\n\nCRITIC FEEDBACK FROM THE PREVIOUS ROUND (address every point):\n- "
                + "\n- ".join(feedback)
            )
        if previous:
            seen = {f.value for f in previous}
            body += (
                "\n\nYou already reported these values in a previous round; keep them if valid, "
                "and do not lose them:\n- " + "\n- ".join(sorted(seen)[:80])
            )

        payload, resp = self.llm.chat_json(
            [
                {"role": "system", "content": spec.system},
                {"role": "user", "content": body},
            ],
            model=self.settings.llm.text_model,
            temperature=self.settings.processing.agent_temperature,
        )
        findings, extra_notes = _findings_from_payload(payload, source="detector")
        notes.extend(extra_notes)
        notes.append(f"detector returned {len(findings)} findings via {resp.model}")
        return findings, payload, notes


    # ------------------------------------------------------------------- names
    def detect_names(
        self,
        document: str,
        *,
        max_chars: int = 40_000,
        regex_hints: list[dict[str, Any]] | None = None,
    ) -> tuple[list[PiiFinding], dict[str, Any]]:
        """Second, deliberately narrow pass focused on person names.

        Names are the hardest PII category for a generalist detector to enumerate
        exhaustively, and a missed name is a privacy failure. If this pass returns
        nothing usable, the caller keeps the general detector's name findings.
        """
        spec = prompt_registry.load("person_name_detector")
        doc_text, _ = _clip(document, max_chars)
        body = spec.render(document=doc_text)
        if regex_hints:
            body += (
                "\n\nSURNAMES SUGGESTED BY A STRUCTURED VALIDATOR (verify each one; "
                "include it only if it really is a person's surname):\n- "
                + "\n- ".join(json.dumps(h, ensure_ascii=False) for h in regex_hints[:20])
            )
        payload, resp = self.llm.chat_json(
            [
                {"role": "system", "content": spec.system},
                {"role": "user", "content": body},
            ],
            model=self.settings.llm.text_model,
            temperature=0.0,
        )

        raw_names = payload.get("names")
        if raw_names is None and isinstance(payload.get("findings"), list):
            raw_names = payload["findings"]
        findings: list[PiiFinding] = []
        for entry in raw_names or []:
            if not isinstance(entry, dict):
                continue
            value = str(entry.get("value") or "").strip()
            if not value:
                continue
            try:
                confidence = float(entry.get("confidence", 0.75))
            except (TypeError, ValueError):
                confidence = 0.75
            findings.append(
                PiiFinding(
                    kind="PERSON",
                    value=value,
                    context=str(entry.get("context") or "")[:240],
                    confidence=max(0.0, min(1.0, confidence)),
                    source="name_detector",
                )
            )
        return findings, {"model": resp.model, "count": len(findings)}


# ═════════════════════════════════════════════════════════════════════════ CRITIC
@dataclass
class CriticAgent:
    settings: Settings
    llm: LLMClient
    name: str = "critic"

    def review(
        self,
        document: str,
        findings: list[PiiFinding],
        *,
        round_index: int,
        validated: list[Any] | None = None,
        max_chars: int = 60_000,
    ) -> tuple[Critique, list[PiiFinding]]:
        spec = prompt_registry.load("pii_critic")
        doc_text, _ = _clip(document, max_chars)
        validated_payload = [
            {"kind": v.kind, "value": v.value, "start": v.start, "end": v.end, "reason": v.reason}
            for v in (validated or [])
        ]

        if self.settings.app.mock_mode:
            critique = _mock_critique(findings, round_index)
            return critique, _merge_findings(findings, critique)

        body = spec.render(
            document=doc_text,
            findings=json.dumps([f.to_dict() for f in findings], ensure_ascii=False, indent=1),
            validated=json.dumps(validated_payload, ensure_ascii=False, indent=1),
            round_index=round_index,
            max_rounds=self.settings.processing.max_critic_rounds,
        )
        payload, _ = self.llm.chat_json(
            [
                {"role": "system", "content": spec.system},
                {"role": "user", "content": body},
            ],
            model=self.settings.llm.text_model,
            temperature=0.0,
        )

        critique = Critique(
            round_index=round_index,
            agreement_score=float(payload.get("agreement_score") or 0.0),
            missing=[m for m in payload.get("missing") or [] if isinstance(m, dict)],
            false_positives=[
                _value_of(fp) for fp in payload.get("false_positives") or []
            ],
            over_masking=[_value_of(om) for om in payload.get("over_masking") or []],
            feedback=[str(f) for f in payload.get("feedback") or []],
            accepted=bool(payload.get("accept")),
            raw=json.dumps(payload, ensure_ascii=False),
        )
        return critique, _reviewable_findings(_merge_findings(findings, critique), document)


# ═════════════════════════════════════════════════════════════════════════ MASKER
@dataclass
class MaskerAgent:
    settings: Settings
    llm: LLMClient
    name: str = "masker"

    def mask(self, document: str, findings: list[PiiFinding], *,
             max_chars: int = 60_000) -> MaskResult:
        plan = _build_plan(findings, self.settings.processing.mask_style)
        if self.settings.app.mock_mode or not self.settings.llm.configured:
            return _deterministic_mask(document, plan, notes=(
                ["MOCK_MODE: deterministic masking applied"] if self.settings.app.mock_mode
                else ["LLM not configured: deterministic masking applied"]
            ))

        spec = prompt_registry.load("pii_masker")
        doc_text, _ = _clip(document, max_chars)
        try:
            payload, _ = self.llm.chat_json(
                [
                    {"role": "system", "content": spec.system},
                    {
                        "role": "user",
                        "content": spec.render(
                            document=doc_text,
                            plan=json.dumps(plan, ensure_ascii=False, indent=1),
                        ),
                    },
                ],
                model=self.settings.llm.text_model,
                temperature=0.0,
            )
        except LLMError as exc:
            log.warning("masker LLM failed (%s); using deterministic masking", exc)
            return _deterministic_mask(document, plan, notes=[f"masker fallback: {exc}"])

        if not isinstance(payload, dict):
            log.warning("masker returned %s, not an object; using deterministic masking",
                        type(payload).__name__)
            return _deterministic_mask(
                document, plan,
                notes=[f"masker fallback: unexpected {type(payload).__name__} payload"],
            )

        cleaned = payload.get("cleaned_text") or ""
        residual = [r for r in payload.get("residual_pii") or [] if isinstance(r, dict)]
        # verify: every planned literal must be gone from the model output
        leaks = [item["value"] for item in plan if item["value"] in cleaned]
        if leaks or not cleaned.strip():
            log.warning("masker output failed verification (%d leaks); using deterministic pass",
                        len(leaks))
            result = _deterministic_mask(document, plan,
                                         notes=["masker output rejected by verifier"])
            result.residual_findings.extend(residual)
            return result

        result = _deterministic_mask(document, plan, notes=["masker output verified"])
        result.cleaned_text = cleaned
        result.residual_findings.extend(residual)
        return result


# ═══════════════════════════════════════════════════════════════════════ helpers
def _value_of(entry: Any) -> str:
    if isinstance(entry, dict):
        return str(entry.get("value") or "")
    return str(entry)


def _findings_from_payload(payload: dict[str, Any], *, source: str) -> tuple[list[PiiFinding], list[str]]:
    notes: list[str] = []
    raw_findings = payload.get("findings")
    if raw_findings is None and isinstance(payload.get("items"), list):
        raw_findings = payload["items"]
    if not isinstance(raw_findings, list):
        notes.append("model response contained no 'findings' array")
        raw_findings = []

    findings: list[PiiFinding] = []
    seen: set[tuple[str, str]] = set()
    for entry in raw_findings:
        if not isinstance(entry, dict):
            continue
        value = str(entry.get("value") or entry.get("text") or "").strip()
        if not value:
            continue
        kind = validators.normalize_kind(str(entry.get("kind") or entry.get("type") or ""))
        key = (kind, value)
        if key in seen:
            continue
        seen.add(key)
        try:
            confidence = float(entry.get("confidence", 0.6))
        except (TypeError, ValueError):
            confidence = 0.6
        findings.append(
            PiiFinding(
                kind=kind,
                value=value,
                start=_int_or_none(entry.get("start")),
                end=_int_or_none(entry.get("end")),
                context=str(entry.get("context") or "")[:240],
                confidence=max(0.0, min(1.0, confidence)),
                source=source,
            )
        )
    if payload.get("notes"):
        notes.append(str(payload["notes"])[:400])
    return findings, notes


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _merge_findings(findings: list[PiiFinding], critique: Critique) -> list[PiiFinding]:
    """Apply the critic's verdict: drop false positives, add misses, fix kinds."""
    rejected = {v for v in critique.false_positives if v}
    corrected: dict[str, str] = {}
    merged: list[PiiFinding] = []
    seen = {(f.kind, f.value) for f in findings}

    for finding in findings:
        if finding.value in rejected:
            continue
        merged.append(finding)

    for entry in critique.missing:
        value = str(entry.get("value") or "").strip()
        if not value:
            continue
        kind = validators.normalize_kind(str(entry.get("kind") or ""))
        if (kind, value) in seen:
            continue
        try:
            confidence = float(entry.get("confidence", 0.6))
        except (TypeError, ValueError):
            confidence = 0.6
        merged.append(
            PiiFinding(
                kind=kind,
                value=value,
                context=str(entry.get("context") or "")[:240],
                confidence=max(0.0, min(1.0, confidence)),
                source="critic",
            )
        )
        seen.add((kind, value))

    for entry in getattr(critique, "wrong_kind", []) or []:
        if isinstance(entry, dict):
            corrected[str(entry.get("value"))] = validators.normalize_kind(
                str(entry.get("correct_kind") or "")
            )
    for finding in merged:
        if finding.value in corrected and corrected[finding.value]:
            finding.kind = corrected[finding.value]

    return merged


def _build_plan(findings: list[PiiFinding], mask_style: str) -> list[dict[str, Any]]:
    """Assign a stable placeholder to each distinct value."""
    plan: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for finding in sorted(findings, key=lambda f: (-f.confidence, f.kind, f.value)):
        key = (finding.kind, finding.value)
        if key in seen:
            continue
        seen.add(key)
        plan.append(
            {
                "kind": finding.kind,
                "value": finding.value,
                "placeholder": pseudonym(finding.value, finding.kind, label_style=mask_style),
            }
        )
    return plan


def _deterministic_mask(document: str, plan: list[dict[str, Any]],
                       notes: list[str] | None = None) -> MaskResult:
    """Model-independent masking. Guarantees no planned literal survives."""
    cleaned = document
    changes: list[MaskChange] = []
    by_kind: dict[str, int] = {}

    # longest values first so "Mario Rossi" wins over "Mario"
    for item in sorted(plan, key=lambda i: -len(i["value"])):
        value, placeholder, kind = item["value"], item["placeholder"], item["kind"]
        if not value:
            continue
        occurrences = cleaned.count(value)
        if occurrences == 0:
            continue
        cleaned = cleaned.replace(value, placeholder)
        by_kind[kind] = by_kind.get(kind, 0) + occurrences
        changes.append(MaskChange(kind=kind, original=value, placeholder=placeholder))

    result = MaskResult(cleaned_text=cleaned, changes=changes, by_kind=by_kind, notes=notes or [])
    return result


def _reviewable_findings(findings: list[PiiFinding], document: str) -> list[PiiFinding]:
    """Trim an over-eager critic: keep only values that really appear in the text.

    A critic/merger artefact makes the pipeline safer, not noisier: values that are
    absent from the document cannot be masked and must not appear in the plan.
    """
    kept: list[PiiFinding] = []
    for finding in findings:
        if not finding.value or finding.value not in document:
            continue
        kept.append(finding)
    return kept


_EMAIL_LOCAL = re.compile(r"(?<![\w.])[A-Za-z]{2,}(?:[._-][A-Za-z]{2,})+(?=@)")
#: multi-word capitalised sequences, restricted to a single line so a name at the end
#: of a line cannot swallow the heading that starts the next one
_MULTIWORD_NAME = re.compile(
    r"^[ \t]*([A-Z][a-zà-öø-ÿ]{1,20}(?:[ \t]+[A-Z][a-zà-öø-ÿ]{1,20}){1,3})[ \t]*$",
    re.MULTILINE,
)
#: field labels whose capitalised value is a person name
_NAME_LABEL = re.compile(
    r"(?im)^[ \t]*(?:full[ \t]+name|name|candidate|holder|account[ \t]+holder|"
    r"employee|patient|advisor|contact[ \t]+person)[ \t]*:[ \t]*([A-Z][^\n|]{2,60})"
)
#: labels whose capitalised value is definitely NOT a person
_NOT_A_NAME_LABEL = re.compile(
    r"(?im)^[ \t]*(?:place[ \t]+of[ \t]+birth|nationality|country|city|currency|language|"
    r"languages|skill|skills|company|employer|organization|organisation|street|address|"
    r"period|status|service|product|department|team|project|customer|client|supplier)[ \t]*:"
)

#: capitalised pairs that are organisations, places or products, not people
_NON_PERSON_CAPITALISED = {
    "acme analytics", "beta consulting", "gamma retail", "delta finance", "epsilon media",
    "zeta logistics", "eta retail", "theta health", "iota bank", "kappa manufacturing",
    "immobiliare sole", "supermarket milano", "trattoria da gino", "enel energia",
    "politecnico di milano", "via roma", "curriculum vitae", "personal details",
    "professional summary", "work experience", "opening balance", "closing balance",
    "total credits", "total debits", "value date", "card payment", "account holder",
    "account number", "mobile phone", "date of birth", "place of birth",
    "statement of account", "movements", "summary", "contacts", "experience",
    "education", "skills", "languages", "references", "notes", "holder",
    "salary january", "rent january", "transfer out", "utility bill", "tax id",
}

#: tokens that may never be the whole of a person name
_NON_PERSON_TOKENS = {
    "ibm", "api", "sql", "cto", "ceo", "cv", "it", "uk", "usa", "eu", "vat", "id",
    "mobile", "email", "phone", "iban", "bic", "srl", "spa", "ltd", "gmbh", "sa", "ag",
    "python", "spark", "kafka", "airflow", "aws", "terraform", "january", "february",
}


def _surname_hints(validated: list) -> list[dict[str, Any]]:
    """Give the name pass the surname hidden inside structured identifiers.

    A codice fiscale/national ID encodes SURNAME+NAME, which is a strong hint that a
    person name exists in the document even when the name is written elsewhere.
    """
    hints: list[dict[str, Any]] = []
    for span in validated:
        if getattr(span, "kind", "") == "TAX_ID" and len(span.value) >= 6:
            hints.append(
                {
                    "kind": "TAX_ID",
                    "value": span.value,
                    "surname_letters": span.value[:3].upper(),
                    "name_letters": span.value[3:6].upper(),
                }
            )
    return hints


def _mock_name_findings(document: str, validated: list) -> list[PiiFinding]:
    """Offline, model-free name pass used by MOCK_MODE and unit tests.

    Deliberately conservative: it only proposes a string when the document itself
    gives evidence that the string is a person (an email local part, a label such as
    "Name:", or a capitalised single-line entry in a document that also carries
    structured PII).
    """
    findings: list[PiiFinding] = []
    seen: set[str] = set()
    blocked_label_lines = {m.end() for m in _NOT_A_NAME_LABEL.finditer(document)}

    def add(value: str, confidence: float, reason: str) -> None:
        value = " ".join(value.split())
        if not value or value.lower() in seen or value.lower() in _NON_PERSON_CAPITALISED:
            return
        tokens = value.lower().split()
        if any(token.strip(".,") in _NON_PERSON_TOKENS for token in tokens):
            return
        seen.add(value.lower())
        findings.append(
            PiiFinding(kind="PERSON", value=value, confidence=confidence,
                       source="name_detector", context=reason)
        )

    # 1) full names written after an explicit name label
    for match in _NAME_LABEL.finditer(document):
        add(match.group(1).strip(" \t.|,"), 0.9, "explicit name label")

    # 2) names embedded in email addresses
    for match in _EMAIL_LOCAL.finditer(document):
        add(match.group(0).replace(".", " ").replace("_", " ").replace("-", " "),
            0.8, "local part of an email address")

    # 3) capitalised single-line entries, only in documents that also carry
    #    structured PII; job titles are excluded because a capitalised line is the
    #    normal shape of both a person's name and a role description
    if validated:
        candidates = [(m.end(), m.group(1)) for m in _MULTIWORD_NAME.finditer(document)]
        lowered = [value.lower() for _, value in candidates]
        for end_pos, value in candidates:
            if end_pos in blocked_label_lines:
                continue
            if any(other != value.lower() and other.startswith(value.lower() + " ")
                   for other in lowered):
                continue
            if _looks_like_job_title(value):
                continue
            add(value, 0.7, "capitalised entry in a document with structured PII")

    return findings


#: words that reveal a capitalised line as a role, not a person
_ROLE_WORDS = {
    "engineer", "manager", "director", "consultant", "analyst", "developer", "designer",
    "architect", "officer", "specialist", "assistant", "coordinator", "supervisor",
    "advisor", "administrator", "scientist", "accountant", "lawyer", "nurse", "doctor",
    "professor", "student", "intern", "technician", "operator", "lead", "head", "chief",
    "senior", "junior", "principal", "staff", "teacher", "researcher", "counsel",
}


def _looks_like_job_title(value: str) -> bool:
    """True when a capitalised line is a role description rather than a person."""
    tokens = [token.strip(".,").lower() for token in value.split()]
    return any(token in _ROLE_WORDS for token in tokens)


def _reviewable_names(findings: list[PiiFinding]) -> list[PiiFinding]:
    """Drop name candidates the generic detector already covers in another category."""
    other_values = {f.value for f in findings if f.kind != "PERSON"}
    return [f for f in findings if f.kind == "PERSON" and f.value not in other_values]


def _mock_findings(document: str) -> list[PiiFinding]:
    """Deterministic detector used by MOCK_MODE / unit tests."""
    findings: list[PiiFinding] = []
    for span in validators.scan(document):
        findings.append(
            PiiFinding(kind=span.kind, value=span.value, start=span.start, end=span.end,
                       confidence=span.confidence, source="detector")
        )
    return findings


def _mock_critique(findings: list[PiiFinding], round_index: int) -> Critique:
    return Critique(
        round_index=round_index,
        agreement_score=1.0,
        accepted=True,
        feedback=["mock mode: critic accepted the detector output"],
    )

