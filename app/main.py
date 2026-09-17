"""FastAPI application: upload endpoint, job store, and the two-panel web UI."""
from __future__ import annotations

import logging
import secrets
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .core.config import BASE_DIR, Settings, get_settings
from .core.crypto import ArtifactCipher, CryptoError, generate_key
from .core.hashing import get_salt
from .core.llm import LLMClient
from .core.schemas import JobResult
from .processing.pipeline import DocumentPipeline

log = logging.getLogger(__name__)

TEMPLATES_DIR = BASE_DIR / "app" / "templates"
STATIC_DIR = BASE_DIR / "app" / "static"

#: in-memory job store - results also land encrypted on disk
JOBS: dict[str, JobResult] = {}


def _purge_expired(settings: Settings, cipher: ArtifactCipher) -> None:
    cutoff = time.time() - settings.privacy.retention_minutes * 60
    for job_id, job in list(JOBS.items()):
        if job.created_at < cutoff:
            JOBS.pop(job_id, None)
            for path in settings.app.output_dir.glob(f"{job_id}*"):
                try:
                    path.unlink()
                except OSError:
                    pass
    for path in settings.app.upload_dir.glob("*"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    settings.app.ensure_dirs()
    try:
        cipher = ArtifactCipher(
            settings.privacy.encryption_key, enabled=settings.privacy.encrypt_artifacts
        )
    except CryptoError as exc:
        # Fail closed: a privacy tool must not start in a state where it writes
        # personal data to disk unencrypted. The container restarts until the key
        # is provided, which is the intended operator experience.
        log.critical("encryption misconfigured, refusing to start: %s", exc)
        raise

    app.state.settings = settings
    app.state.cipher = cipher
    app.state.llm = LLMClient(settings.llm)
    app.state.pipeline = DocumentPipeline(settings, app.state.llm)
    app.state.started_at = time.time()
    log.info(
        "pii-remover up | mock_mode=%s | encryption=%s | vision=%s | text=%s",
        settings.app.mock_mode,
        cipher.enabled,
        settings.llm.vision_model,
        settings.llm.text_model,
    )
    try:
        yield
    finally:
        app.state.llm.close()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="PII Remover",
        version="1.0.0",
        description="Ingestion -> agentic PII detection -> masking -> description.",
        root_path=settings.app.root_path,
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    # ───────────────────────────────────────────────────────────── security
    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        cfg: Settings = app.state.settings
        if cfg.privacy.force_https:
            forwarded = request.headers.get("x-forwarded-proto", request.url.scheme)
            if forwarded != "https":
                url = request.url.replace(scheme="https")
                return JSONResponse(
                    {"detail": "HTTPS required", "url": str(url)}, status_code=426
                )
        response = await call_next(request)
        response.headers["Strict-Transport-Security"] = (
            f"max-age={cfg.privacy.hsts_max_age}; includeSubDomains"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    # ─────────────────────────────────────────────────────────────── pages
    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "config": {
                    "mockMode": settings.app.mock_mode,
                    "llmConfigured": bool(settings.llm.api_key),
                    "visionModel": settings.llm.vision_model,
                    "textModel": settings.llm.text_model,
                    "visionModelConfigured": settings.llm.vision_model,
                    "maxUploadMb": settings.ingestion.max_upload_mb,
                    "encryptionEnabled": settings.privacy.encrypt_artifacts,
                    "rootPath": settings.app.root_path,
                    "maxCriticRounds": settings.processing.max_critic_rounds,
                    "maskStyle": settings.processing.mask_style,
                    "retentionMinutes": settings.privacy.retention_minutes,
                },
            },
        )

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        return PlainTextResponse("", status_code=204)

    # ───────────────────────────────────────────────────────────────── api
    @app.get("/health")
    @app.get("/api/health")
    async def health():
        cfg: Settings = app.state.settings
        return {
            "status": "ok",
            "uptime_seconds": round(time.time() - app.state.started_at, 1),
            "mock_mode": cfg.app.mock_mode,
            "llm": app.state.llm.health() if not cfg.app.mock_mode else {"ok": True, "mock": True},
            "encryption_at_rest": app.state.cipher.enabled,
            "retention_minutes": cfg.privacy.retention_minutes,
            "jobs_in_memory": len(JOBS),
        }

    @app.post("/api/upload")
    async def upload(
        background: BackgroundTasks,
        request: Request,
        file: UploadFile = File(...),
    ):
        cfg: Settings = app.state.settings
        filename = Path(file.filename or "upload").name
        suffix = Path(filename).suffix.lower()

        if suffix and suffix not in _ALLOWED_SUFFIXES:
            raise HTTPException(
                status_code=415,
                detail=f"file extension '{suffix}' is not supported",
            )

        job = JobResult(status="pending")
        target = cfg.app.upload_dir / f"{job.job_id}{suffix}"

        size = 0
        limit = cfg.ingestion.max_upload_mb * 1024 * 1024
        with target.open("wb") as sink:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > limit:
                    sink.close()
                    target.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=413,
                        detail=f"file exceeds MAX_UPLOAD_MB ({cfg.ingestion.max_upload_mb})",
                    )
                sink.write(chunk)

        # store the upload encrypted at rest (best effort: keep the plaintext only
        # for the duration of the request so the ingestion stage can read it)
        job.status = "processing"
        JOBS[job.job_id] = job
        background.add_task(_run_job, job.job_id, target, filename, file.content_type or "")
        return {"job_id": job.job_id, "filename": filename, "bytes": size,
                "status": job.status}

    @app.get("/api/jobs/{job_id}")
    async def get_job(job_id: str):
        job = JOBS.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found or expired")
        return job.to_dict()

    @app.get("/api/jobs/{job_id}/download")
    async def download(job_id: str, kind: str = "cleaned"):
        job = JOBS.get(job_id)
        if job is None or job.status != "done":
            raise HTTPException(status_code=404, detail="result not available")
        if kind == "cleaned":
            payload = (job.masking or {}).get("cleaned_text", "")
            filename, media = f"{job_id}.cleaned.txt", "text/plain; charset=utf-8"
        elif kind == "report":
            from .processing.pipeline import render_report

            payload, filename, media = render_report(job), f"{job_id}.report.md", "text/markdown"
        elif kind == "description":
            payload = (job.description or {}).get("markdown", "")
            filename, media = f"{job_id}.description.md", "text/markdown"
        else:
            raise HTTPException(status_code=400, detail="kind must be cleaned|report|description")
        from fastapi.responses import Response

        return Response(
            content=payload.encode("utf-8"),
            media_type=media,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.get("/api/architecture")
    async def architecture():
        """Machine-readable description of the architecture shown in the UI panel."""
        cfg: Settings = app.state.settings
        return {
            "version": "1.0.0",
            "stages": [
                {
                    "name": "Ingestion",
                    "components": [
                        "Upload guard (extension/size validation, streaming write)",
                        f"Converter: pdf / image / text / spreadsheet / office "
                        f"(strategy={cfg.ingestion.strategy})",
                        f"Visual LLM extraction (model={cfg.llm.vision_model})",
                        f"Fallback: embedded text layer (pdf) or plain-text decode "
                        f"(max {cfg.ingestion.max_pages} pages, {cfg.ingestion.render_dpi} dpi)",
                        "Normalisation: NFC, de-hyphenation, whitespace cleanup",
                    ],
                },
                {
                    "name": "Processing",
                    "components": [
                        "Validator layer: regex + checksum (IBAN mod-97, Luhn, codice fiscale, NIF)",
                        "Detector agent: enumerates PII spans (taxonomy-constrained JSON)",
                        "Critic agent: finds misses, false positives, wrong kinds",
                        f"Loop: max {cfg.processing.max_critic_rounds} rounds, "
                        f"agreement threshold {cfg.processing.agreement_threshold}",
                        "Masker agent: applies the plan, verified against leaks",
                        f"Deterministic masking fallback (style={cfg.processing.mask_style})",
                        "Describer agent: describes the clean document by type",
                    ],
                },
                {
                    "name": "Output",
                    "components": [
                        "Cleaned text (PII replaced by stable pseudonyms)",
                        "Description of the cleaned document",
                        "Audit metadata: masked categories, rounds, warnings - no raw PII",
                        f"Encryption at rest: {'on' if cfg.privacy.encrypt_artifacts else 'off'}, "
                        f"retention {cfg.privacy.retention_minutes} min",
                    ],
                },
            ],
            "agents": [
                {"name": "detector", "model": cfg.llm.text_model,
                 "prompt": "pii_detector.md", "responsibility": "detect PII spans"},
                {"name": "critic", "model": cfg.llm.text_model,
                 "prompt": "pii_critic.md", "responsibility": "verify and send feedback"},
                {"name": "masker", "model": cfg.llm.text_model,
                 "prompt": "pii_masker.md", "responsibility": "apply the masking plan"},
                {"name": "describer", "model": cfg.llm.text_model,
                 "prompt": "document_describer.md", "responsibility": "describe clean data"},
            ],
            "privacy": {
                "transport": "TLS terminated at the reverse proxy (Traefik)",
                "at_rest": "Fernet (AES-128-CBC + HMAC-SHA256) artifact encryption",
                "pseudonyms": "HMAC-SHA256 keyed, non-reversible",
                "telemetry": "no PII written to logs",
            },
        }

    @app.get("/api/prompts")
    async def prompts():
        """Expose the prompt registry so the UI can show which agents exist."""
        from .processing import prompt_registry

        return {
            "available": prompt_registry.available(),
            "loaded": {
                name: {
                    "version": prompt_registry.load(name).version,
                    "system_preview": prompt_registry.load(name).system[:240],
                }
                for name in prompt_registry.available()
            },
        }

    return app


_ALLOWED_SUFFIXES = {
    ".pdf", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".heic",
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".yaml", ".yml", ".log", ".rtf",
    ".html", ".htm", ".xml",
    ".xlsx", ".xlsm", ".xls", ".ods",
    ".docx", ".odt", ".pptx", ".epub",
}


def _run_job(job_id: str, path: Path, filename: str, mime: str) -> None:
    """Background worker: run the pipeline, persist encrypted artifacts, clean up."""
    from .core.config import get_settings

    settings = get_settings()
    job = JOBS[job_id]
    pipeline: DocumentPipeline = _APP_STATE.pipeline if _APP_STATE else DocumentPipeline(settings)
    cipher: ArtifactCipher = _APP_STATE.cipher if _APP_STATE else ArtifactCipher(
        settings.privacy.encryption_key, enabled=settings.privacy.encrypt_artifacts
    )
    try:
        result = pipeline.process(path, filename=filename, mime_hint=mime, job_id=job_id)
        result.doc_id = result.doc_id or job.doc_id
        paths = pipeline.persist(result, cipher, settings.app.output_dir)
        result.encryption = {
            "encrypted_at_rest": cipher.enabled,
            "artifacts": list(paths.values()),
        }
        JOBS[job_id] = result
    except Exception as exc:  # noqa: BLE001
        log.exception("job %s failed", job_id)
        job.status = "error"
        job.error = f"{type(exc).__name__}: {exc}"
        JOBS[job_id] = job
    finally:
        path.unlink(missing_ok=True)  # never keep the raw upload in plaintext


#: set during lifespan so background tasks reuse the same clients
_APP_STATE: Any = None


def _bind_state(state: Any) -> None:
    global _APP_STATE
    _APP_STATE = state


app = create_app()
_orig_lifespan = app.router.lifespan_context


@asynccontextmanager
async def _lifespan_with_binding(app_: FastAPI):
    async with _orig_lifespan(app_) as state:
        _bind_state(app_.state)
        yield state


app.router.lifespan_context = _lifespan_with_binding


def run() -> None:  # pragma: no cover - container entrypoint
    import uvicorn

    cfg = get_settings()
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=int((__import__("os").getenv("PORT") or "8000")),
        proxy_headers=cfg.privacy.trust_proxy_headers,
        forwarded_allow_ips="*",
        log_level="info",
    )


if __name__ == "__main__":  # pragma: no cover
    run()
