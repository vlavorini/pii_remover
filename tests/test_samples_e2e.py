"""End-to-end shape checks on the generated sample documents.

These run offline (MOCK_MODE) and assert the property that actually matters for a
privacy tool: no known personal value survives into the cleaned output.
"""
from pathlib import Path

import pytest

from app.core.config import get_settings
from app.core.llm import LLMClient
from app.processing.pipeline import DocumentPipeline

SAMPLES = Path(__file__).resolve().parents[1] / "samples"

#: values that must never appear in a cleaned document
FORBIDDEN = [
    "mario.rossi@example.com",
    "IT60X0542811101000000123456",
    "RSSMRA85C14F205Z",
    "+39 333 1234567",
    "Giulia Bianchi",
    "DE89370400440532013000",
    "paolo.ferrari@bank.example",
    "laura.conti@unimi.example.it",
]

CASES = [
    ("cv_mario_rossi.txt", "text"),
    ("cv_mario_rossi.pdf", "pdf"),
    ("invoices_ledger.csv", "text"),
    ("invoices_ledger.xlsx", "spreadsheet"),
    ("form_scanned.png", "image"),
    ("bank_statement_giulia_bianchi.txt", "text"),
]


@pytest.fixture(scope="module")
def pipeline():
    settings = get_settings()
    return DocumentPipeline(settings, LLMClient(settings.llm))


@pytest.mark.parametrize("filename,expected_kind", CASES)
def test_sample_is_processed_and_cleaned(pipeline, filename, expected_kind):
    path = SAMPLES / filename
    if not path.exists():
        pytest.skip(f"{filename} not generated; run scripts/make_samples.py")

    result = pipeline.process(path, filename=filename)

    assert result.status == "done", result.error
    assert result.extraction["kind"] == expected_kind

    cleaned = result.masking["cleaned_text"]
    for secret in FORBIDDEN:
        assert secret not in cleaned, f"{secret!r} leaked into the cleaned output"

    assert result.masking["total_masked"] > 0
    assert result.description["markdown"]


@pytest.mark.parametrize("filename,_kind", CASES)
def test_report_never_contains_original_values(pipeline, filename, _kind):
    path = SAMPLES / filename
    if not path.exists():
        pytest.skip(f"{filename} not generated")

    from app.processing.pipeline import render_report

    report = render_report(pipeline.process(path, filename=filename))
    for secret in FORBIDDEN:
        assert secret not in report
