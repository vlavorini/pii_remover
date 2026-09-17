"""Validators for structured PII (regex + checksum). Model-independent ground truth."""
from __future__ import annotations

import re
from dataclasses import dataclass

# --------------------------------------------------------------------- patterns
EMAIL = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
PHONE = re.compile(
    r"(?:(?<!\d)\+\d{1,3}[\s.\-]?)?(?:\(?\d{2,4}\)?[\s.\-]?){2,4}\d{2,4}(?!\d)"
)
IBAN = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b")
CARD = re.compile(r"\b(?:\d[ \-]?){13,19}\b")
SSN_US = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
IPV6 = re.compile(r"\b(?:[0-9a-fA-F]{1,4}:){2,7}[0-9a-fA-F]{1,4}\b")
DATE = re.compile(
    r"\b(?:\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}|\d{4}-\d{2}-\d{2}|"
    r"\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{2,4})\b",
    re.IGNORECASE,
)
POSTAL_IT = re.compile(r"\b\d{5}\b")
POSTAL_UK = re.compile(r"\b[A-Z]{1,2}\d{1,2}[A-Z]?\s?\d[A-Z]{2}\b")
PLATE_IT = re.compile(r"\b[A-Z]{2}\s?\d{3}\s?[A-Z]{2}\b")
URL = re.compile(r"\bhttps?://[^\s<>\"')]+")
# Italian fiscal code / VAT, Spanish NIF, French NIR-ish
CF_IT = re.compile(r"\b[A-Z]{6}\d{2}[A-Z]\d{2}[A-Z]\d{3}[A-Z]\b", re.IGNORECASE)
VAT_IT = re.compile(r"\bIT\d{11}\b")
NIF_ES = re.compile(r"\b[XYZ]?\d{7,8}[A-Z]\b")
PASSPORT = re.compile(r"\b[A-Z]{1,2}\d{6,9}\b")
NHS_UK = re.compile(r"\b\d{3}\s?\d{3}\s?\d{4}\b")

KIND_PATTERNS: dict[str, re.Pattern[str]] = {
    "EMAIL": EMAIL,
    "PHONE": PHONE,
    "IBAN": IBAN,
    "CREDIT_CARD": CARD,
    "SSN_NATIONAL_ID": SSN_US,
    "IP_ADDRESS": IPV4,
    "DATE": DATE,
    "LICENCE_PLATE": PLATE_IT,
    "TAX_ID": CF_IT,
    "PASSPORT": PASSPORT,
}

#: kinds the detectors are allowed to emit - used to constrain the model output
ALLOWED_KINDS = {
    "PERSON", "EMAIL", "PHONE", "ADDRESS", "SSN_NATIONAL_ID", "PASSPORT",
    "DRIVING_LICENCE", "TAX_ID", "IBAN", "CREDIT_CARD", "DOB", "BANK_ACCOUNT",
    "MEDICAL", "BIOMETRIC", "USERNAME", "IP_ADDRESS", "LICENCE_PLATE",
    "EMPLOYER", "PHOTO", "OTHER",
}


@dataclass
class ValidatedSpan:
    kind: str
    value: str
    start: int
    end: int
    confidence: float
    reason: str = "checksum"


# --------------------------------------------------------------------- checksums
def luhn_ok(number: str) -> bool:
    digits = [int(c) for c in number if c.isdigit()]
    if len(digits) < 13:
        return False
    checksum, parity = 0, len(digits) % 2
    for index, digit in enumerate(digits):
        if index % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        checksum += digit
    return checksum % 10 == 0


def iban_ok(value: str) -> bool:
    compact = re.sub(r"\s+", "", value).upper()
    if not (15 <= len(compact) <= 34) or not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]+", compact):
        return False
    rearranged = compact[4:] + compact[:4]
    digits = "".join(str(int(ch, 36)) for ch in rearranged)
    return int(digits) % 97 == 1


def italian_fiscal_code_ok(value: str) -> bool:
    """Shape + check character for the Italian codice fiscale.

    The check character is **informational, not authoritative**: a wrong check
    character means "not checksum-clean", not "not a fiscal code". The pipeline
    needs recall on PII, so this function returns True for any value with the
    correct shape - callers that need the stronger signal use
    `italian_fiscal_code_checksum_ok`.
    """
    return bool(re.fullmatch(r"[A-Z]{6}\d{2}[A-Z]\d{2}[A-Z]\d{3}[A-Z]", value.strip().upper()))


def italian_fiscal_code_checksum_ok(value: str) -> bool:
    """Strict variant: shape AND a valid check character."""
    cf = value.strip().upper()
    if not italian_fiscal_code_ok(cf):
        return False
    odd_map = {
        **{str(d): v for d, v in zip("0123456789", [1, 0, 5, 7, 9, 13, 15, 17, 19, 21])},
        **{c: v for c, v in zip("ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                                [1, 0, 5, 7, 9, 13, 15, 17, 19, 21, 2, 4, 18, 20, 11, 3, 6, 8,
                                 12, 14, 16, 10, 22, 25, 24, 23])},
    }
    even_map = {**{str(d): int(d) for d in "0123456789"},
                **{c: i for i, c in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ")}}
    total = 0
    for index, ch in enumerate(cf[:15]):
        total += (odd_map if index % 2 == 0 else even_map).get(ch, 0)
    return "ABCDEFGHIJKLMNOPQRSTUVWXYZ"[total % 26] == cf[15]


def nif_es_ok(value: str) -> bool:
    compact = value.strip().upper()
    letter = compact[-1]
    body = compact[:-1]
    if not body.isdigit():
        return False
    return "TRWAGMYFPDXBNJZSQVHLCKE"[int(body) % 23] == letter


def ipv4_ok(value: str) -> bool:
    parts = value.split(".")
    return len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)


# --------------------------------------------------------------------- scanning
def scan(text: str) -> list[ValidatedSpan]:
    """Run every validator with its checksum gate; returns non-overlapping spans."""
    candidates: list[ValidatedSpan] = []

    def add(kind: str, match: re.Match[str], confidence: float, reason: str) -> None:
        candidates.append(ValidatedSpan(kind, match.group(0), match.start(), match.end(),
                                        confidence, reason))

    for match in EMAIL.finditer(text):
        add("EMAIL", match, 0.99, "regex")
    for match in IBAN.finditer(text):
        if iban_ok(match.group(0)):
            add("IBAN", match, 0.99, "iban_mod97")
    for match in CARD.finditer(text):
        if luhn_ok(match.group(0)):
            add("CREDIT_CARD", match, 0.98, "luhn")
    for match in CF_IT.finditer(text):
        if italian_fiscal_code_ok(match.group(0)):
            confidence = 0.98 if italian_fiscal_code_checksum_ok(match.group(0)) else 0.85
            reason = "codice_fiscale_checksum" if confidence == 0.98 else "codice_fiscale_shape"
            add("TAX_ID", match, confidence, reason)
    for match in VAT_IT.finditer(text):
        add("TAX_ID", match, 0.9, "vat_shape")
    for match in NIF_ES.finditer(text):
        if nif_es_ok(match.group(0)):
            add("SSN_NATIONAL_ID", match, 0.95, "nif_checksum")
    for match in SSN_US.finditer(text):
        add("SSN_NATIONAL_ID", match, 0.95, "us_ssn_shape")
    for match in IPV4.finditer(text):
        if ipv4_ok(match.group(0)):
            add("IP_ADDRESS", match, 0.85, "ipv4")
    for match in IPV6.finditer(text):
        add("IP_ADDRESS", match, 0.8, "ipv6")
    for match in PHONE.finditer(text):
        digits = re.sub(r"\D", "", match.group(0))
        if 8 <= len(digits) <= 15 and not CARD.fullmatch(match.group(0).strip()):
            add("PHONE", match, 0.8, "digit_shape")
    for match in DATE.finditer(text):
        add("DOB", match, 0.5, "date_shape_needs_context")
    for match in PLATE_IT.finditer(text):
        add("LICENCE_PLATE", match, 0.7, "it_plate_shape")
    for match in URL.finditer(text):
        add("URL", match, 0.6, "regex")

    return _dedupe(candidates)


def _dedupe(spans: list[ValidatedSpan]) -> list[ValidatedSpan]:
    """Keep the highest-confidence span when two overlap."""
    ordered = sorted(spans, key=lambda s: (s.start, -(s.end - s.start), -s.confidence))
    kept: list[ValidatedSpan] = []
    for span in ordered:
        if any(span.start < k.end and k.start < span.end for k in kept):
            continue
        kept.append(span)
    return sorted(kept, key=lambda s: s.start)


def normalize_kind(raw: str) -> str:
    """Map a model-supplied label onto the fixed taxonomy."""
    value = (raw or "").strip().upper().replace(" ", "_").replace("-", "_")
    aliases = {
        "NAME": "PERSON", "FULL_NAME": "PERSON", "PERSON_NAME": "PERSON",
        "MAIL": "EMAIL", "E_MAIL": "EMAIL",
        "TELEPHONE": "PHONE", "MOBILE": "PHONE", "PHONE_NUMBER": "PHONE",
        "BIRTH_DATE": "DOB", "DATE_OF_BIRTH": "DOB",
        "STREET_ADDRESS": "ADDRESS", "LOCATION": "ADDRESS",
        "SOCIAL_SECURITY_NUMBER": "SSN_NATIONAL_ID", "SSN": "SSN_NATIONAL_ID",
        "NATIONAL_ID": "SSN_NATIONAL_ID", "ID_NUMBER": "SSN_NATIONAL_ID",
        "FISCAL_CODE": "TAX_ID", "VAT": "TAX_ID", "TAX_CODE": "TAX_ID",
        "CARD": "CREDIT_CARD", "PAN": "CREDIT_CARD", "CREDITCARD": "CREDIT_CARD",
        "BANK_ACCOUNT_NUMBER": "BANK_ACCOUNT", "ACCOUNT_NUMBER": "BANK_ACCOUNT",
        "LICENSE_PLATE": "LICENCE_PLATE", "PLATE": "LICENCE_PLATE",
        "PHONE_OR_FAX": "PHONE", "HEALTH": "MEDICAL", "DIAGNOSIS": "MEDICAL",
        "FACE": "BIOMETRIC", "PORTRAIT": "PHOTO", "IMAGE_OF_PERSON": "PHOTO",
        "COMPANY": "EMPLOYER", "ORGANISATION": "EMPLOYER", "ORGANIZATION": "EMPLOYER",
    }
    value = aliases.get(value, value)
    return value if value in ALLOWED_KINDS else ("OTHER" if value else "OTHER")
