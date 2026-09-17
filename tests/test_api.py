"""API surface: upload guard, job lifecycle, downloads, architecture endpoint."""
import io
import time

import pytest
from fastapi.testclient import TestClient

from app.main import JOBS, app

CV = b"""Curriculum Vitae

Name: Mario Rossi
Email: mario.rossi@example.com
IBAN: IT60X0542811101000000123456
"""


@pytest.fixture()
def client():
    with TestClient(app) as test_client:
        yield test_client


def _wait_for_job(client, job_id, timeout=25.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        payload = client.get(f"/api/jobs/{job_id}").json()
        if payload["status"] in {"done", "error"}:
            return payload
        time.sleep(0.15)
    raise AssertionError(f"job {job_id} did not finish in {timeout}s")


def test_health(client):
    payload = client.get("/api/health").json()
    assert payload["status"] == "ok"
    assert payload["mock_mode"] is True
    assert payload["encryption_at_rest"] is True


def test_index_renders_two_output_panels_and_architecture(client):
    html = client.get("/").text
    assert "Cleaned data" in html
    assert "Description of the cleaned data" in html
    assert "How this application works" in html
    assert "Ingestion" in html and "Processing" in html and "Output" in html
    assert "<details" in html  # collapsible sections


def test_architecture_endpoint_lists_four_agents(client):
    payload = client.get("/api/architecture").json()
    names = [a["name"] for a in payload["agents"]]
    assert names == ["detector", "critic", "masker", "describer"]
    assert [s["name"] for s in payload["stages"]] == ["Ingestion", "Processing", "Output"]


def test_prompts_endpoint_exposes_registry(client):
    payload = client.get("/api/prompts").json()
    for name in ("pii_detector", "pii_critic", "pii_masker", "document_describer"):
        assert name in payload["available"]


def test_upload_requires_a_file(client):
    assert client.post("/api/upload").status_code == 422


def test_rejects_unsupported_extension(client):
    response = client.post(
        "/api/upload",
        files={"file": ("payload.exe", io.BytesIO(b"MZ\x90\x00"), "application/octet-stream")},
    )
    assert response.status_code == 415


def test_rejects_oversized_file(client):
    """The upload guard must reject anything above MAX_UPLOAD_MB with 413."""
    from app.core.config import get_settings

    limit = get_settings().ingestion.max_upload_mb
    oversized = io.BytesIO(b"a" * ((limit * 1024 * 1024) + 4096))
    response = client.post("/api/upload", files={"file": ("big.txt", oversized, "text/plain")})
    assert response.status_code == 413
    assert "MAX_UPLOAD_MB" in response.text


def test_full_upload_flow_returns_clean_text_and_description(client):
    response = client.post(
        "/api/upload", files={"file": ("cv.txt", io.BytesIO(CV), "text/plain")}
    )
    assert response.status_code == 200
    job_id = response.json()["job_id"]

    payload = _wait_for_job(client, job_id)
    assert payload["status"] == "done", payload.get("error")

    cleaned = payload["masking"]["cleaned_text"]
    assert "mario.rossi@example.com" not in cleaned
    assert "Curriculum Vitae" in cleaned
    assert payload["description"]["markdown"]

    # audit data exposed to the UI never contains raw PII
    assert "mario.rossi@example.com" not in str(payload["masking"]["changes"])
    assert "mario.rossi@example.com" not in str(payload["detections_rounds"])
    assert payload["encryption"]["encrypted_at_rest"] is True


def test_downloads_are_available_after_completion(client):
    job_id = client.post(
        "/api/upload", files={"file": ("cv.txt", io.BytesIO(CV), "text/plain")}
    ).json()["job_id"]
    _wait_for_job(client, job_id)

    for kind in ("cleaned", "description", "report"):
        response = client.get(f"/api/jobs/{job_id}/download", params={"kind": kind})
        assert response.status_code == 200
        assert response.content


def test_unknown_job_is_404(client):
    assert client.get("/api/jobs/job_does_not_exist").status_code == 404


def test_upload_is_deleted_after_processing(client):
    from app.core.config import get_settings

    settings = get_settings()
    job_id = client.post(
        "/api/upload", files={"file": ("cv.txt", io.BytesIO(CV), "text/plain")}
    ).json()["job_id"]
    _wait_for_job(client, job_id)
    leftovers = list(settings.app.upload_dir.glob(f"{job_id}*"))
    assert leftovers == []
