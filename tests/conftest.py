"""Pytest fixtures. MOCK_MODE keeps every test offline and deterministic."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("MOCK_MODE", "true")
os.environ.setdefault("ENCRYPT_ARTIFACTS", "true")
os.environ.setdefault("ENCRYPTION_KEY", "")
os.environ.setdefault("PSEUDONYM_SALT", "test-salt")

from app.core.config import reset_settings_cache  # noqa: E402
from app.core.crypto import generate_key  # noqa: E402

os.environ["ENCRYPTION_KEY"] = generate_key()
reset_settings_cache()


@pytest.fixture(scope="session", autouse=True)
def _settings():
    reset_settings_cache()
    from app.core.config import get_settings

    settings = get_settings()
    yield settings


@pytest.fixture()
def sample_cv_text() -> str:
    return """Curriculum Vitae

Name: Mario Rossi
Email: mario.rossi@example.com
Mobile: +39 333 1234567
Address: Via Roma 42, 20100 Milano, Italy
Date of birth: 14/03/1985
Codice Fiscale: RSSMRA85C14F205Z
IBAN: IT60X0542811101000000123456

Experience
2019-2024 Senior Data Engineer, Acme Analytics S.r.l.

Skills: Python, Spark, SQL
"""


@pytest.fixture()
def sample_bank_text() -> str:
    return """Account statement - current account

Holder: Giulia Bianchi
IBAN: DE89370400440532013000
Period: 01/01/2024 - 31/01/2024
Opening balance: 4,120.55 EUR
Closing balance: 3,890.10 EUR

Movements
| Date | Description | Amount |
| --- | --- | --- |
| 03/01/2024 | Salary | 2,450.00 |
| 07/01/2024 | Rent | -1,200.00 |
| 15/01/2024 | Transfer to IT60X0542811101000000123456 | -600.00 |
"""
