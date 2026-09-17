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
_TRAILING_COMMA = re.compile(r",(\s*[}\]])")
_MAX_JSON_DEPTH = 64

#: tokens reserved for the chat template and counting drift when clamping a request
_BUDGET_SAFETY_MARGIN = 256
#: never clamp below this, or the model cannot emit a minimal JSON object
_MIN_COMPLETION_TOKENS = 256


class LLMError(RuntimeError):
    """Raised when the upstream model cannot be reached or returns garbage."""


class LLMTruncatedError(LLMError):
    """The model hit the output ceiling before emitting an answer.

    Reasoning models spend their output budget on an internal trace that is not
    returned in ``message.content``, so a truncated call arrives as an empty
    string with ``finish_reason == "length"``. Reported distinctly so operators
    see the real cause instead of a parse error.
    """


@dataclass
class LLMResponse:
    text: str
    model: str
    usage: dict[str, Any]
    latency_seconds: float
    finish_reason: str = ""
    truncated: bool = False


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
    def _estimate_tokens(self, messages: list[dict[str, Any]]) -> int:
        """Rough prompt size, ~4 chars/token. Diagnostic only, never authoritative."""
        chars = 0
        for message in messages:
            content = message.get("content")
            if isinstance(content, str):
                chars += len(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict):
                        chars += len(str(part.get("text", "")))
                        # base64 image payloads dominate vision prompts
                        url = str(part.get("image_url", {}).get("url", ""))
                        chars += len(url) // 4
        return max(1, chars // 4)

    def _fit_budget(self, messages: list[dict[str, Any]], requested: int) -> int:
        """Clamp the completion budget so the prompt fits the server window.

        llama.cpp rejects (or silently truncates) a request whose prompt plus
        max_tokens exceeds --ctx-size. When the window is known we shrink the
        completion budget to whatever is left, keeping a small floor so the
        model can still emit a minimal JSON object.
        """
        window = self.settings.context_window
        if window <= 0:
            return requested
        estimate = self._estimate_tokens(messages)
        room = window - estimate - _BUDGET_SAFETY_MARGIN
        if room >= requested:
            return requested
        clamped = max(_MIN_COMPLETION_TOKENS, room)
        log.warning(
            "completion budget clamped from %d to %d tokens to fit the %d-token "
            "window (prompt ~%d tokens)",
            requested, clamped, window, estimate,
        )
        return clamped

    def _warn_if_prompt_too_large(self, messages: list[dict[str, Any]], max_tokens: int) -> None:
        window = self.settings.context_window
        if window <= 0:
            return
        estimate = self._estimate_tokens(messages)
        if estimate + max_tokens > window:
            log.warning(
                "prompt (~%d tokens) + max_tokens (%d) may exceed the server context "
                "window (%d); the model will be truncated. Reduce the document size, "
                "LLM_MAX_TOKENS, or raise the server's --ctx-size.",
                estimate, max_tokens, window,
            )

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

        budget = self._fit_budget(messages, max_tokens or self.settings.max_tokens)
        self._warn_if_prompt_too_large(messages, budget)

        payload: dict[str, Any] = {
            "model": model or self.settings.text_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": budget,
        }
        if json_mode:
            # widely supported; providers that ignore it still get a prompt-level instruction
            payload["response_format"] = {"type": "json_object"}

        thinking = self.settings.thinking_payload
        if thinking:
            payload.update(thinking)

        resp = self._post(payload)

        # A provider that rejects unknown fields fails the whole call; drop the
        # thinking switch once and let the operator see the warning.
        if resp.status_code >= 400 and thinking and self._looks_like_unknown_field(resp.text):
            log.warning(
                "endpoint rejected the thinking switch (%s); retrying without it",
                list(thinking),
            )
            for key in thinking:
                payload.pop(key, None)
            resp = self._post(payload)

        if resp.status_code >= 400:
            raise LLMError(f"HTTP {resp.status_code}: {resp.text[:400]}")

        data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        text = choice.get("message", {}).get("content") or ""
        if isinstance(text, list):  # some providers return content parts
            text = "\n".join(part.get("text", "") for part in text if isinstance(part, dict))
        finish_reason = choice.get("finish_reason") or ""
        truncated = finish_reason == "length"

        if truncated and not text.strip():
            usage = data.get("usage") or {}
            raise LLMTruncatedError(
                "the model spent its entire output budget (%d tokens, finish_reason=length) "
                "without emitting an answer. This endpoint is running a reasoning model: "
                "raise LLM_MAX_TOKENS, lower the document size, or set "
                "LLM_DISABLE_THINKING=true to skip the reasoning trace. "
                "completion_tokens=%s" % (budget, usage.get("completion_tokens")),
            )

        return LLMResponse(
            text=text,
            model=data.get("model", payload["model"]),
            usage=data.get("usage") or {},
            latency_seconds=resp.elapsed.total_seconds(),
            finish_reason=finish_reason,
            truncated=truncated,
        )

    def _post(self, payload: dict[str, Any]) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(self.settings.max_retries + 1):
            try:
                return self.client.post("/chat/completions", json=payload)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt < self.settings.max_retries:
                    time.sleep(min(2 ** attempt, 8))
                    continue
                raise LLMError(f"{type(exc).__name__}: {exc}") from exc
        raise LLMError(str(last_error))

    @staticmethod
    def _looks_like_unknown_field(body: str) -> bool:
        """Detect an endpoint rejecting the thinking switch as an unknown field.

        Phrasings differ per vendor: llama.cpp-style servers say "unexpected
        keyword", OpenAI says "unrecognized" / "additional properties", and
        Google's OpenAI-compatible layer says 'Unknown name "...": Cannot find
        field'. All of them mean the same thing: drop the field and retry.
        """
        lowered = body.lower()
        return any(
            marker in lowered
            for marker in ("unexpected keyword", "extra inputs", "unknown field",
                           "unknown name", "cannot find field", "unrecognized",
                           "additional propert", "invalid parameter")
        )

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
        raise LLMError(
            "empty model response where JSON was expected (the model returned no "
            "content; check LLM_MAX_TOKENS and LLM_DISABLE_THINKING for this endpoint)"
        )
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
