"""Multi-provider prompt registry.

Prompt source-of-truth lives in app/processing/prompts/*.md files so it can be
edited without touching code. Each prompt declares the model role it targets, the
required output schema, and the guardrails that stop the model from leaking PII.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


@dataclass(frozen=True)
class PromptSpec:
    name: str
    version: str
    role: str
    system: str
    body: str
    output_schema: str
    guardrails: str

    @property
    def template(self) -> str:
        return "\n\n".join(
            part for part in (self.body, self.output_schema, self.guardrails) if part
        )

    def render(self, **values: object) -> str:
        """Safe formatting: unknown placeholders are left untouched."""
        text = self.template
        for key, value in values.items():
            text = text.replace("{{" + key + "}}", str(value))
        return text


def _parse(path: Path) -> PromptSpec:
    raw = path.read_text(encoding="utf-8")
    meta: dict[str, str] = {}
    sections: dict[str, list[str]] = {}
    current: str | None = None

    if raw.startswith("---"):
        _, front, raw = raw.split("---", 2)
        for line in front.strip().splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                meta[key.strip()] = value.strip()

    for line in raw.splitlines():
        if line.startswith("## "):
            current = line[3:].strip().lower().replace(" ", "_")
            sections[current] = []
            continue
        if current:
            sections[current].append(line)

    def section(name: str) -> str:
        return "\n".join(sections.get(name, [])).strip()

    return PromptSpec(
        name=meta.get("name", path.stem),
        version=meta.get("version", "0"),
        role=meta.get("role", "text"),
        system=section("system"),
        body=section("body"),
        output_schema=section("output_schema"),
        guardrails=section("guardrails"),
    )


@lru_cache(maxsize=64)
def load(name: str) -> PromptSpec:
    path = PROMPTS_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"prompt not found: {name} (looked in {PROMPTS_DIR})")
    return _parse(path)


def available() -> list[str]:
    return sorted(p.stem for p in PROMPTS_DIR.glob("*.md"))


def reload() -> None:
    load.cache_clear()
