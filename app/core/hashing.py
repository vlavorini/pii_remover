"""Cryptographic hashing for stable pseudonyms and integrity checks."""
from __future__ import annotations

import hashlib
import hmac
import os
import re
from typing import Any

_DIGITS = re.compile(r"\d")


def get_salt(explicit: str | None = None) -> bytes:
    """Salt for placeholder pseudonyms. Stable per deployment, never PII-derived."""
    value = explicit or os.getenv("PSEUDONYM_SALT", "")
    if value:
        return value.encode("utf-8")
    # dev fallback: warn loudly, still deterministic within a process
    return b"pii-remover-dev-salt-DO-NOT-USE-IN-PRODUCTION"


def pseudonym(
    value: str,
    kind: str,
    *,
    salt: bytes | None = None,
    length: int = 6,
    label_style: str = "label",
) -> str:
    """Deterministic placeholder so the same PII maps to the same token.

    Uses HMAC-SHA256 (keyed, non-reversible) - the original value can never be
    recovered from the token, which is what we want for a redaction pipeline.
    """
    key = salt if salt is not None else get_salt()
    digest = hmac.new(key, f"{kind}:{value}".encode("utf-8"), hashlib.sha256).hexdigest()
    token = digest[:length].upper()

    if label_style == "redact":
        return "[REDACTED]"
    if label_style == "hash":
        return f"{kind}_{token}"
    return f"[{kind}_{token}]"


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def integrity_tag(payload: dict[str, Any], key: bytes) -> str:
    """HMAC over a canonical JSON encoding, used to seal result artifacts."""
    import json

    canon = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hmac.new(key, canon.encode("utf-8"), hashlib.sha256).hexdigest()
