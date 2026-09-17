"""Thin OpenAI-compatible client: chat, vision, and JSON-mode helpers.

Works against any /v1/chat/completions endpoint (OpenAI, DeepSeek, vLLM, Ollama,
LiteLLM, ...). All endpoint details come from configuration - never hardcoded.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .config import LLMSettings

log = logging.getLogger(__name__)

_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


class LLMError(RuntimeError):
    """Raised when the upstream model cannot be reached or returns garbage."""


@dataclass
class LLMResponse:
    text: str
    model: str
    usage: dict[str, Any]
    latency_seconds: float


class LLMClient:
    def __init__(self, settings: LLMSettings) -> None:
        self.settings = settings
        self._client: httpx.Client | None = None

    # ---------------------------------------------------------------- plumbing
    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            headers = {"Content-Type": "application/json"}
            if self.settings.api_key:
                headers["Authorization"] = f"Bearer {self.settings.api_key}"
            self._client = httpx.Client(
                base_url=self.settings.base_url.rstrip("/"),
                headers=headers,
                timeout=httpx.Timeout(self.settings.timeout_seconds, connect=15.0),
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    @property
    def configured(self) -> bool:
        return self.settings.configured

    def health(self) -> dict[str, Any]:
        """Cheap reachability probe. Never raises."""
        if not self.configured:
            return {"ok": False, "reason": "no_api_key", "base_url": self.settings.base_url}
        try:
            resp = self.client.get("/models")
            return {
                "ok": resp.status_code < 400,
                "status_code": resp.status_code,
                "base_url": self.settings.base_url,
            }
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            return {"ok": False, "reason": type(exc).__name__, "detail": str(exc)[:200],
                    "base_url": self.settings.base_url}

    # ------------------------------------------------------------------ calls
    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        temperature: float = 0.0,
        json_mode: bool = False,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        if not self.configured:
            raise LLMError("LLM is not configured (missing OPENAI_API_KEY)")

        payload: dict[str, Any] = {
            "model": model or self.settings.text_model,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if json_mode:
            # widely supported; providers that ignore it still get a prompt-level instruction
            payload["response_format"] = {"type": "json_object"}

        last_error: Exception | None = None
        for attempt in range(self.settings.max_retries + 1):
            started = time.perf_counter()
            try:
                resp = self.client.post("/chat/completions", json=payload)
                if resp.status_code >= 400:
                    raise LLMError(f"HTTP {resp.status_code}: {resp.text[:400]}")
                data = resp.json()
                text = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
                if isinstance(text, list):  # some providers return content parts
                    text = "\n".join(part.get("text", "") for part in text if isinstance(part, dict))
                return LLMResponse(
                    text=text,
                    model=data.get("model", payload["model"]),
                    usage=data.get("usage") or {},
                    latency_seconds=time.perf_counter() - started,
                )
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt < self.settings.max_retries:
                    time.sleep(min(2 ** attempt, 8))
                    continue
                raise LLMError(str(exc)) from exc
        raise LLMError(str(last_error))

    def chat_json(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> tuple[dict[str, Any], LLMResponse]:
        resp = self.chat(messages, model=model, temperature=temperature,
                        json_mode=True, max_tokens=max_tokens)
        return extract_json(resp.text), resp

    def vision(
        self,
        prompt: str,
        images: list[bytes],
        *,
        model: str | None = None,
        mime: str = "image/jpeg",
        temperature: float = 0.0,
        system: str = "",
    ) -> LLMResponse:
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for raw in images:
            b64 = base64.b64encode(raw).decode("ascii")
            content.append(
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}
            )
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": content})
        return self.chat(messages, model=model or self.settings.vision_model,
                         temperature=temperature)


def extract_json(text: str) -> dict[str, Any]:
    """Best-effort JSON recovery from model output (fences, prose, trailing commas)."""
    if not text or not text.strip():
        raise LLMError("empty model response where JSON was expected")
    candidates: list[str] = []
    stripped = text.strip()
    candidates.append(stripped)
    for match in _JSON_FENCE.finditer(text):
        candidates.append(match.group(1).strip())
    start, end = stripped.find("{"), stripped.rfind("}")
    if start != -1 and end > start:
        candidates.append(stripped[start : end + 1])
    start, end = stripped.find("["), stripped.rfind("]")
    if start != -1 and end > start:
        candidates.append(stripped[start : end + 1])

    for candidate in candidates:
        for attempt in (candidate, re.sub(r",(\s*[}\]])", r"\1", candidate)):
            try:
                parsed = json.loads(attempt)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
            return {"items": parsed}
    raise LLMError(f"could not parse JSON from model output: {text[:300]}")
