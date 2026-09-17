"""Central configuration, loaded from environment (.env in dev, env_file in Docker)."""
from __future__ import annotations

import json
import os
import logging
from dataclasses import dataclass, field, replace
from pathlib import Path

try:  # optional in prod (env vars already injected), required for local runs
    from dotenv import load_dotenv

    load_dotenv(override=False)
except Exception:  # pragma: no cover
    pass


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name, str(default)).lower()
    return raw in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


BASE_DIR = Path(__file__).resolve().parents[2]

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class LLMSettings:
    """OpenAI-compatible endpoint. Nothing here is hardcoded by the app."""

    base_url: str = field(default_factory=lambda: _env("OPENAI_BASE_URL", "https://api.openai.com/v1"))
    api_key: str = field(default_factory=lambda: _env("OPENAI_API_KEY"))
    vision_model: str = field(default_factory=lambda: _env("VISION_MODEL", "gpt-4o"))
    text_model: str = field(default_factory=lambda: _env("TEXT_MODEL", "gpt-4o-mini"))
    timeout_seconds: float = field(default_factory=lambda: _env_float("LLM_TIMEOUT_SECONDS", 180.0))
    max_retries: int = field(default_factory=lambda: _env_int("LLM_MAX_RETRIES", 2))
    #: output token ceiling per call; a reasoning model spends this on its trace first
    max_tokens: int = field(default_factory=lambda: _env_int("LLM_MAX_TOKENS", 1500))
    #: runtime context the server was started with (llama.cpp --ctx-size), used to
    #: warn before a prompt silently overflows. 0 disables the check.
    context_window: int = field(default_factory=lambda: _env_int("LLM_CONTEXT_WINDOW", 0))
    #: ask the server to skip the model's reasoning trace where it supports it.
    #: On llama.cpp this is chat_template_kwargs.enable_thinking, worth ~8x the
    #: output budget; on other providers it is omitted entirely.
    disable_thinking: bool = field(default_factory=lambda: _env_bool("LLM_DISABLE_THINKING", False))
    #: query params for the thinking switch, JSON; empty = strip the field
    thinking_kwargs: str = field(
        default_factory=lambda: _env("LLM_THINKING_KWARGS", '{"chat_template_kwargs":{"enable_thinking":false}}')
    )

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    @property
    def thinking_payload(self) -> dict[str, Any]:
        """Extra request fields that switch the reasoning trace off.

        Providers that do not understand these keys ignore them, and providers
        that reject unknown fields are handled by LLMClient.chat, which retries
        once without them.
        """
        if not self.disable_thinking:
            return {}
        raw = self.thinking_kwargs.strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("LLM_THINKING_KWARGS is not valid JSON, ignoring: %s", raw[:120])
            return {}
        return parsed if isinstance(parsed, dict) else {}


@dataclass(frozen=True)
class IngestionSettings:
    """Visual-LLM-first extraction with a text-layer fallback."""

    # `vision_first` | `text_first`
    strategy: str = field(default_factory=lambda: _env("INGEST_STRATEGY", "vision_first"))
    max_pages: int = field(default_factory=lambda: _env_int("INGEST_MAX_PAGES", 15))
    render_dpi: int = field(default_factory=lambda: _env_int("INGEST_RENDER_DPI", 150))
    max_image_edge: int = field(default_factory=lambda: _env_int("INGEST_MAX_IMAGE_EDGE", 1800))
    jpeg_quality: int = field(default_factory=lambda: _env_int("INGEST_JPEG_QUALITY", 85))
    max_upload_mb: int = field(default_factory=lambda: _env_int("MAX_UPLOAD_MB", 25))


@dataclass(frozen=True)
class ProcessingSettings:
    """Agentic pipeline knobs."""

    #: max critic<->detector rounds before forcing convergence
    max_critic_rounds: int = field(default_factory=lambda: _env_int("MAX_CRITIC_ROUNDS", 3))
    #: below this many findings still flagged as uncertain, do one more round
    agreement_threshold: float = field(default_factory=lambda: _env_float("AGREEMENT_THRESHOLD", 0.9))
    mask_style: str = field(default_factory=lambda: _env("MASK_STYLE", "label"))  # label|redact|hash
    max_agent_steps: int = field(default_factory=lambda: _env_int("MAX_AGENT_STEPS", 24))
    agent_temperature: float = field(default_factory=lambda: _env_float("AGENT_TEMPERATURE", 0.0))


@dataclass(frozen=True)
class PrivacySettings:
    """Encryption at rest + transport expectations."""

    #: 32-byte urlsafe base64 Fernet key
    encryption_key: str = field(default_factory=lambda: _env("ENCRYPTION_KEY"))
    encrypt_artifacts: bool = field(default_factory=lambda: _env_bool("ENCRYPT_ARTIFACTS", True))
    #: minutes after which an upload/result is purged
    retention_minutes: int = field(default_factory=lambda: _env_int("RETENTION_MINUTES", 60))
    force_https: bool = field(default_factory=lambda: _env_bool("FORCE_HTTPS", False))
    trust_proxy_headers: bool = field(default_factory=lambda: _env_bool("TRUST_PROXY_HEADERS", True))
    hsts_max_age: int = field(default_factory=lambda: _env_int("HSTS_MAX_AGE", 31536000))


@dataclass(frozen=True)
class AppSettings:
    #: path prefix when served behind Traefik StripPrefix
    root_path: str = field(default_factory=lambda: _env("ROOT_PATH", ""))
    upload_dir: Path = field(default_factory=lambda: Path(_env("UPLOAD_DIR", str(BASE_DIR / "data" / "uploads"))))
    output_dir: Path = field(default_factory=lambda: Path(_env("OUTPUT_DIR", str(BASE_DIR / "data" / "outputs"))))
    scratch_dir: Path = field(default_factory=lambda: Path(_env("SCRATCH_DIR", str(BASE_DIR / "data" / "scratch"))))
    public_base_url: str = field(default_factory=lambda: _env("PUBLIC_BASE_URL", "http://127.0.0.1:8000"))
    #: deterministic offline mode: no network calls at all
    mock_mode: bool = field(default_factory=lambda: _env_bool("MOCK_MODE", False))

    def ensure_dirs(self) -> None:
        for d in (self.upload_dir, self.output_dir, self.scratch_dir):
            d.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class Settings:
    llm: LLMSettings = field(default_factory=LLMSettings)
    ingestion: IngestionSettings = field(default_factory=IngestionSettings)
    processing: ProcessingSettings = field(default_factory=ProcessingSettings)
    privacy: PrivacySettings = field(default_factory=PrivacySettings)
    app: AppSettings = field(default_factory=AppSettings)


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
        _settings.app.ensure_dirs()
    return _settings


def reset_settings_cache() -> None:
    """Tests only."""
    global _settings
    _settings = None
