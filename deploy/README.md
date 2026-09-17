# Deployment

Target: `https://ns3062692.ip-193-70-34.eu/pii_remover`, alongside the services
already running behind Traefik in `/home/ubuntu/backend_orchestra`.

Nothing in this file has been executed against the server. It is the prepared
procedure; the app has only been run and tested locally.

## 0. What the existing stack gives us

Read from `/home/ubuntu/backend_orchestra/docker_compose.yml`:

* Traefik `v3.6` with `--providers.docker=true` and `exposedbydefault=false`.
* Entrypoints `web` (:80), `websecure` (:443), `dbsecure` (:5432).
* Cert resolver named `myresolver` (ACME TLS challenge).
* Sibling services are exposed with the pattern:
  `Host(...) && PathPrefix('/<service>')` + `stripprefix` + `websecure` + `tls.certresolver=myresolver`.

`deploy/docker-compose.pii_remover.yml` follows exactly that pattern, so it merges
into the existing file instead of competing with it.

## 1. Copy the application to the server

```bash
rsync -av --exclude .venv --exclude data --exclude .git \
  pii-remover/ ubuntu@ns3062692.ip-193-70-34.eu:/home/ubuntu/backend_orchestra/pii_remover/
```

## 2. Create the production `.env` on the server

```bash
ssh ubuntu@ns3062692.ip-193-70-34.eu
cd /home/ubuntu/backend_orchestra/pii_remover
cp .env.example .env
python3 -m app.core.crypto          # prints a Fernet key, paste into ENCRYPTION_KEY
```

Then set, in `/home/ubuntu/backend_orchestra/pii_remover/.env`:

```ini
OPENAI_BASE_URL=<your OpenAI-compatible endpoint>
OPENAI_API_KEY=<your key>
VISION_MODEL=<vision-capable model>
TEXT_MODEL=<text model>

ENCRYPTION_KEY=<generated above>
PSEUDONYM_SALT=<long random string, unique to this deployment>
RETENTION_MINUTES=60

ROOT_PATH=/pii_remover     # set by the compose file too; keep them consistent
FORCE_HTTPS=false          # Traefik terminates TLS; the container sees HTTP
MOCK_MODE=false
```

Generate a salt with:
`python3 -c "import secrets;print(secrets.token_urlsafe(32))"`

Lock the file down: `chmod 600 .env`.

## 3. Confirm the network name Traefik listens on

The fragment expects the network created by the existing stack:

```bash
cd /home/ubuntu/backend_orchestra
docker compose -f docker_compose.yml config --format json \
  | python3 -c "import json,sys;print([n for n in json.load(sys.stdin)['networks']])"

docker network ls | grep backend_orchestra
```

If the network is named differently (for example `backend_orchestra_default2`),
update `networks.backend_orchestra_default.name` in the fragment and the
`traefik.docker.network` label.

## 4. Deploy

```bash
cd /home/ubuntu/backend_orchestra
docker compose -f docker_compose.yml -f pii_remover/docker-compose.pii_remover.yml \
  up -d --build pii_remover
```

## 5. Verify

```bash
# container is healthy
docker compose -f docker_compose.yml -f pii_remover/docker-compose.pii_remover.yml ps pii_remover

# application answers locally (loopback publish is 8035 -> 8000)
curl -s http://127.0.0.1:8035/health | python3 -m json.tool

# certificate and path work through the proxy
curl -sI https://ns3062692.ip-193-70-34.eu/pii_remover/ | head -1
```

Then open `https://ns3062692.ip-193-70-34.eu/pii_remover` and run one document
end to end.

## 6. Notes and gotchas

* **`ROOT_PATH`** must be `/pii_remover` because Traefik strips the prefix before
  forwarding. It is set in the compose fragment; the app uses it to build
  `/pii_remover/static/...` and `/pii_remover/api/...` URLs in the HTML.
* **Uploads** are capped at `MAX_UPLOAD_MB` (default 25). The Traefik middleware
  `pii_remover-buffering` raises the proxy body limit to 48 MB; add it to the
  router's middleware chain if you need larger files.
* **No new proxy** is created. Do not add a second Traefik: the fragment only adds
  labels to the existing one.
* **Data volume** `pii_remover_data` holds encrypted artifacts only; the container
  filesystem is read-only and the raw upload is deleted per job.
* **Backups**: back up `.env` (specifically `ENCRYPTION_KEY`). Without it the
  artifacts are unrecoverable by design. Use `scripts/rotate_key.py` when rotating.
* **Update**: `git pull` on the server, then re-run the `up -d --build pii_remover`
  command from step 4.
