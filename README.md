# PII Remover

Upload any document, get it back with personal data removed, plus a description of
what the cleaned document contains. Built as three explicit stages — **ingestion**,
**processing**, **output** — with cooperating LLM agents and a deterministic
validator layer that does not depend on a model.

```
      ┌─────────────── ingestion ───────────────┐
file ─▶ converter ─▶ visual LLM ─▶ normalisation ─▶ canonical text
      └─────────────────────────────────────────┘
                             │
      ┌────────────────── processing ──────────────────┐
      │ validators (regex+checksums)                   │
      │ detector ⇄ critic  (loop until they agree)     │
      │ masker (verified) + deterministic fallback     │
      │ describer (describes the CLEAN document)       │
      └────────────────────────────────────────────────┘
                             │
      ┌──────────────────── output ────────────────────┐
      │ web UI: cleaned text | description | audit      │
      │ downloads, TLS in transit, encryption at rest   │
      └────────────────────────────────────────────────┘
```

## Quick start (local, offline)

```bash
./setup.sh                 # venv, deps, .env with a generated key, samples, tests
MOCK_MODE=true ./run.sh    # http://127.0.0.1:8000
```

`MOCK_MODE=true` makes **no network calls at all**: the pipeline runs end to end with
the deterministic validator layer plus offline name heuristics, so you can test the
whole UI without an API key. Suitable for demos and CI only — real detection quality
comes from the LLM agents.

## Quick start (local, real models)

```bash
cp .env.example .env       # already done by setup.sh
$EDITOR .env               # set OPENAI_BASE_URL, OPENAI_API_KEY, VISION_MODEL, TEXT_MODEL
./run.sh
```

Docker:

```bash
cp .env.example .env && $EDITOR .env      # ENCRYPTION_KEY is required, see below
docker compose up --build                 # http://127.0.0.1:8000
```

Generate the encryption key (required, the app refuses to start without it):

```bash
python -m app.core.crypto
```

## Configuration

Everything comes from `.env`; nothing about the model endpoint is hardcoded. See
`.env.example` for the annotated list.

| Variable | Purpose |
| --- | --- |
| `OPENAI_BASE_URL` | any OpenAI-compatible `/v1` base URL |
| `OPENAI_API_KEY` | credential for that endpoint |
| `VISION_MODEL` | model that transcribes PDF/image pages (must accept images) |
| `TEXT_MODEL` | model used by detector / critic / masker / describer |
| `INGEST_STRATEGY` | `vision_first` (default) or `text_first` |
| `INGEST_MAX_PAGES` | page cap for PDFs |
| `MAX_UPLOAD_MB` | upload guard limit |
| `MAX_CRITIC_ROUNDS` | max detector ⇄ critic rounds |
| `MASK_STYLE` | `label` → `[PERSON_4A91C2]`, `redact` → `[REDACTED]`, `hash` → `PERSON_4A91C2` |
| `ENCRYPTION_KEY` | Fernet key for artifacts at rest (**required**) |
| `PSEUDONYM_SALT` | salt making pseudonyms stable and non-reversible per deployment |
| `RETENTION_MINUTES` | how long results stay in memory and on disk |
| `ROOT_PATH` | path prefix when served behind a stripping proxy (`/pii_remover`) |
| `MOCK_MODE` | `true` = fully offline deterministic mode |

## The agents

Prompts are files, not code: `app/processing/prompts/*.md`. Each has a `System`,
`Body`, `Output_schema` and `Guardrails` section.

| Agent | File | Job |
| --- | --- | --- |
| detector | `pii_detector.md` | enumerate PII spans, taxonomy-constrained JSON |
| **critic** | `pii_critic.md` | find misses, false positives, wrong kinds; send feedback |
| masker | `pii_masker.md` | apply the mask plan; report residual PII |
| describer | `document_describer.md` | describe the clean document by type |
| name detector | `person_name_detector.md` | dedicated pass for person names |

The detector ⇄ critic loop runs until the critic accepts (or the round limit is hit).
Critic *misses* are always folded in — for PII, recall beats precision. The masker's
output is **verified**: if a planned value survives in the model's output, the
deterministic masker runs instead and the job reports it.

The validator layer (`app/processing/validators.py`) is model-independent ground
truth: IBAN mod-97, Luhn, codice fiscale, Spanish NIF, IPv4 bounds, plus shape rules.
It both audits the model and supplies evidence the model cannot contradict.

## Privacy

* **In transit** — TLS is terminated by the reverse proxy (Traefik). `FORCE_HTTPS`
  makes the app answer `426` to plain HTTP; HSTS, `nosniff`, `SAMEORIGIN`,
  `Referrer-Policy` and a restrictive `Permissions-Policy` are always sent.
* **At rest** — every artifact (result JSON, cleaned text, report) is encrypted with
  Fernet (AES-128-CBC + HMAC-SHA256), written `0600`, as `*.enc`. The raw upload is
  deleted as soon as the job finishes. The app **fails closed** if the key is missing.
* **Pseudonyms** — keyed HMAC-SHA256, so the same value always maps to the same token
  and the original cannot be recovered from it.
* **Audit data** — the UI and the report show categories, counts and rounds; never
  raw values. `MaskChange.original` is reduced to a masked preview on serialisation.
* **Logs** — no PII is written to logs.
* **Retention** — results are dropped from memory and disk after `RETENTION_MINUTES`.

Key rotation after changing `ENCRYPTION_KEY`:

```bash
python scripts/rotate_key.py --old-key-file old.key            # re-encrypt
python scripts/rotate_key.py --old-key-file old.key --purge-unreadable
```

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/` | web UI |
| `GET` | `/health`, `/api/health` | status, models, encryption state |
| `POST` | `/api/upload` | multipart upload → `job_id` |
| `GET` | `/api/jobs/{id}` | job status and full result |
| `GET` | `/api/jobs/{id}/download?kind=` | `cleaned` \| `description` \| `report` |
| `GET` | `/api/architecture` | machine-readable architecture (also rendered in the UI) |
| `GET` | `/api/prompts` | prompt registry the agents loaded |
| `GET` | `/api/docs` | OpenAPI UI |

## Layout

```
app/core/        config, schemas, OpenAI-compatible client, crypto, hashing
app/ingest/      converters (pdf/image/text/spreadsheet/office), normalisation, stage
app/processing/  validators, agents, prompts/, orchestration pipeline
app/api|static|templates/   FastAPI app, UI assets, Jinja templates
deploy/          production compose fragment for the existing Traefik stack
scripts/         sample generator, key rotation
tests/           35 offline tests (validators, crypto, pipeline, API)
```

## Tests

```bash
MOCK_MODE=true pytest -q          # offline, deterministic, no API key needed
```

## Deployment

See `deploy/README.md`. The production compose fragment joins the existing Traefik
network, reuses its certificate resolver, and serves
`https://ns3062692.ip-193-70-34.eu/pii_remover`.
