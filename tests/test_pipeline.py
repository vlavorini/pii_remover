"""The detect -> critique -> mask -> describe pipeline, offline (MOCK_MODE)."""
from pathlib import Path

from app.core.config import get_settings
from app.core.crypto import generate_key
from app.core.llm import LLMClient
from app.processing.pipeline import DocumentPipeline, render_report

CV = """Curriculum Vitae

Name: Mario Rossi
Email: mario.rossi@example.com
Mobile: +39 333 1234567
Address: Via Roma 42, 20100 Milano, Italy
Date of birth: 14/03/1985
IBAN: IT60X0542811101000000123456

Experience
2019-2024 Senior Data Engineer, Acme Analytics S.r.l.
"""


def _pipeline() -> DocumentPipeline:
    settings = get_settings()
    assert settings.app.mock_mode is True, "these tests must run offline"
    return DocumentPipeline(settings, LLMClient(settings.llm))


def test_pipeline_masks_pii_and_keeps_structure(tmp_path):
    source = tmp_path / "cv.txt"
    source.write_text(CV, encoding="utf-8")

    result = _pipeline().process(source, filename="cv.txt")

    assert result.status == "done", result.error
    cleaned = result.masking["cleaned_text"]

    # PII is gone
    assert "mario.rossi@example.com" not in cleaned
    assert "IT60X0542811101000000123456" not in cleaned
    assert "333 1234567" not in cleaned

    # structure is preserved
    assert "Curriculum Vitae" in cleaned
    assert "Senior Data Engineer" in cleaned

    # masking metadata is PII-free
    report = render_report(result)
    assert "mario.rossi@example.com" not in report
    assert "IT60X0542811101000000123456" not in report
    assert "EMAIL" in report


def test_pipeline_records_rounds_and_trace(tmp_path):
    source = tmp_path / "cv.txt"
    source.write_text(CV, encoding="utf-8")
    result = _pipeline().process(source, filename="cv.txt")

    assert result.detections_rounds, "the critic loop must record its rounds"
    assert result.detections_rounds[0]["accepted"] is True
    stages = [s["stage"] for s in result.trace["stages"]]
    for expected in ("ingest", "validators", "detect", "critique", "mask", "describe"):
        assert expected in stages


def test_pipeline_produces_description_and_clean_text(tmp_path):
    source = tmp_path / "cv.txt"
    source.write_text(CV, encoding="utf-8")
    result = _pipeline().process(source, filename="cv.txt")

    description = result.description
    assert description["markdown"]
    assert description["doc_type"]
    assert result.masking["total_masked"] > 0
    assert set(result.masking["by_kind"]).issubset(
        set(__import__("app.core.schemas", fromlist=["PII_KINDS"]).PII_KINDS)
    )


def test_persist_writes_encrypted_artifacts(tmp_path):
    from app.core.crypto import ArtifactCipher

    cipher = ArtifactCipher(generate_key())
    settings = get_settings()
    source = tmp_path / "cv.txt"
    source.write_text(CV, encoding="utf-8")

    pipeline = _pipeline()
    result = pipeline.process(source, filename="cv.txt")
    written = pipeline.persist(result, cipher, tmp_path / "out")

    assert "result" in written and "report" in written
    for path in written.values():
        assert path.endswith(".enc")
        assert Path(path).exists()
    # decrypted content round-trips
    loaded = cipher.load_json(tmp_path / "out" / f"{result.job_id}.json")
    assert loaded["job_id"] == result.job_id


def test_empty_document_reports_error_not_crash(tmp_path):
    source = tmp_path / "empty.txt"
    source.write_text("   \n\n", encoding="utf-8")
    result = _pipeline().process(source, filename="empty.txt")
    assert result.status == "error"
    assert result.error
    assert result.trace["stages"][-1]["status"] == "failed"


def test_unsupported_extension_is_rejected_by_converter(tmp_path):
    source = tmp_path / "binary.bin"
    source.write_bytes(bytes(range(256)) * 8)
    result = _pipeline().process(source, filename="binary.bin")
    assert result.status == "error"
    assert "ConversionError" in result.error or "unsupported" in result.error.lower()
