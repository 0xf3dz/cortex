# WhatsApp semantic memory

A private WhatsApp service that saves future text notes and public links for one allowlisted user.

Send a natural-language query later. The service returns one original note as a contextual WhatsApp reply.

The service does not use a generative language model. It creates all search embeddings on the virtual private server (VPS).

## Features

- Save text notes and links from WhatsApp.
- Confirm each durable note with `Saved.`.
- Search notes with lexical and semantic retrieval.
- Return the best original note as a quoted reply.
- Preserve the original WhatsApp message ID and saved timestamp.
- Enrich public HTML links with the final URL, title, and meta description.
- Delete the most recent note.
- Recover queued work after an application or VPS restart.
- Reject users who are not on the allowlist.
- Deduplicate Meta webhook retries by WhatsApp message ID.
- Keep model inference local and offline after the first model download.

## WhatsApp commands

| Input | Result |
|---|---|
| Any text without a prefix | Save one note and reply with `Saved.` |
| `? <natural-language query>` | Return the best saved note as a contextual reply |
| `/help` | Show command help |
| `/delete-last` | Delete the most recent saved note |
| Any other `/` command | Return an unknown-command response |

Commands and queries do not create notes.

## Search behavior

The service uses two search paths:

1. SQLite FTS5 finds exact terms, URLs, names, and identifiers.
2. The local `intfloat/multilingual-e5-small` model creates normalized query and note embeddings.
3. NumPy calculates cosine similarity across the stored note embeddings.
4. Reciprocal Rank Fusion combines the lexical and vector ranks.
5. The service returns one best note.

The reply body contains the exact saved date. Its WhatsApp context contains the source note message ID.

## Link behavior

The service extracts HTTP and HTTPS URLs from a note. It keeps the original note even if metadata collection fails.

For a public HTML page, the service stores only:

- the final public URL;
- the HTML title;
- the meta description.

The link client rejects credentials, loopback addresses, private addresses, reserved addresses, and unsafe redirects. It limits redirects, response time, URL length, and response size.

## Architecture

```text
Personal WhatsApp account
          |
          v
Meta WhatsApp Cloud API
          |
          v
HTTPS reverse proxy
          |
          v
FastAPI webhook -> SQLite durable queue -> one worker
                                         |        |
                                         |        +-> Meta Graph API reply
                                         |
                                         +-> FTS5 + local embeddings + NumPy
                                         |
                                         +-> safe public-link metadata client
```

| Part | Implementation |
|---|---|
| HTTP service | FastAPI and Uvicorn |
| Webhook authentication | Meta `X-Hub-Signature-256` HMAC validation |
| Durable store | SQLite with write-ahead logging (WAL) |
| Lexical search | SQLite FTS5 |
| Vector search | Local Sentence Transformers model and NumPy cosine similarity |
| Rank merge | Reciprocal Rank Fusion |
| Outbound messages | Meta Graph API |
| Deployment | Docker Compose with one application worker |
| HTTPS | Existing Nginx reverse proxy |

## Repository layout

```text
app/
  backup.py             Consistent SQLite backup command
  config.py             Environment validation
  db.py                 Database access and transactions
  embedding.py          Local embedding adapter
  link_enrichment.py    Public-link validation and metadata collection
  main.py                FastAPI application and worker lifecycle
  migrations.py          SQLite schema migrations
  search.py              Hybrid retrieval and rank fusion
  webhook.py             Meta verification and webhook routes
  webhook_security.py    Raw-body signature validation
  whatsapp_client.py     Meta Graph API client
  worker.py              Durable note, command, search, and retry work
compose.yml               Production container configuration
Dockerfile                CPU-only application image
tests/                    Contract and security tests
```

## Requirements

Prepare these resources:

- Python 3.12 for local development;
- Docker Engine and the Docker Compose plugin for deployment;
- a Meta developer app with the WhatsApp use case;
- a WhatsApp Business Account and a connected test or business number;
- one approved personal WhatsApp recipient;
- a system-user access token with `whatsapp_business_messaging` permission;
- a public HTTPS host for the Meta webhook;
- Nginx or another HTTPS reverse proxy.

Use one Uvicorn worker. Multiple workers load duplicate models and create duplicate in-process worker loops.

## Configure Meta

1. Create a Meta app with the WhatsApp use case.
2. Connect the app to the correct Business Portfolio and WhatsApp Business Account.
3. Add your personal number as an approved test recipient.
4. Create a system-user token with WhatsApp message permission.
5. Confirm that the WhatsApp number status is `CONNECTED`.
6. Set the callback URL to `https://<host>/webhooks/whatsapp`.
7. Enter the same verify token that you put in `.env`.
8. Subscribe the app to the `messages` webhook field.

Do not put an access token, an app secret, or a personal number in this file or a commit.

## Configure the environment

Copy the environment template and restrict its permissions:

```bash
cp .env.example .env
chmod 600 .env
```

Set these values in `.env`:

| Variable | Required | Purpose |
|---|---:|---|
| `META_ACCESS_TOKEN` | Yes | System-user token for outbound Graph API calls |
| `META_APP_SECRET` | Yes | HMAC key for inbound webhook signatures |
| `META_VERIFY_TOKEN` | Yes | Private value for Meta callback verification; minimum 16 characters |
| `WHATSAPP_PHONE_NUMBER_ID` | Yes | Numeric sender phone-number ID from Meta |
| `ALLOWED_WHATSAPP_WA_ID` | Yes | Numeric WhatsApp ID for the only accepted sender |
| `WHATSAPP_GRAPH_API_VERSION` | Yes | Graph API version, such as `v25.0` |
| `DATABASE_PATH` | Yes | SQLite file path; use `/data/agent.sqlite3` in Docker |
| `BACKUP_PATH` | No | Backup destination; default is `/data/backups/agent-latest.sqlite3` |
| `EMBEDDING_MODEL` | No | Local model; default is `intfloat/multilingual-e5-small` |
| `USER_TIME_ZONE` | No | IANA time zone for saved dates; default is `Australia/Brisbane` |
| `WEBHOOK_MAX_BODY_BYTES` | No | Maximum webhook body size; default is 1 MiB |
| `WORKER_ENABLED` | No | Enable the durable worker; default is `true` |
| `WORKER_POLL_INTERVAL_SECONDS` | No | Queue poll interval; default is `0.25` |
| `WORKER_STALE_AFTER_SECONDS` | No | Stale-work recovery limit; default is `300` |

Do not add spaces around values. Phone IDs and WhatsApp IDs must contain ASCII digits only.

## Run the tests

Create a virtual environment and install the CPU version of PyTorch:

```bash
python3.12 -m venv .venv
.venv/bin/pip install --index-url https://download.pytorch.org/whl/cpu torch
.venv/bin/pip install -e '.[test]'
.venv/bin/pytest
```

## Run locally

The first local start downloads the embedding model if the model is not in the local cache.

```bash
DATABASE_PATH=./data/agent.sqlite3 .venv/bin/uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000
```

Check the service:

```bash
curl --fail http://127.0.0.1:8000/health
```

Expected response:

```json
{"status":"ok"}
```

## Deploy with Docker Compose

Build the image:

```bash
docker compose build app
```

Preload the model into the persistent model volume before the first offline start:

```bash
docker compose run --rm \
  -e HF_HUB_OFFLINE=0 \
  -e TRANSFORMERS_OFFLINE=0 \
  app python -c 'from app.config import Settings; from sentence_transformers import SentenceTransformer; s = Settings(); SentenceTransformer(s.embedding_model, device="cpu")'
```

Start the service:

```bash
docker compose up -d app
```

The Compose service:

- binds FastAPI to `127.0.0.1:8090`;
- stores SQLite data in `whatsapp_data`;
- stores model files in `model_cache`;
- starts the container after a VPS restart;
- checks `/health` every 30 seconds;
- forces Hugging Face and Transformers offline mode.

Do not remove the model volume while offline mode is active. The service cannot load the model without the cached files.

## Configure the HTTPS proxy

Forward the public host to the private application port. Keep port `8090` closed to public traffic.

Example Nginx location:

```nginx
location / {
    proxy_pass http://127.0.0.1:8090;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

Permit only Secure Shell, HTTP, and HTTPS traffic at the host firewall. If Cloudflare proxies the host, limit HTTP and HTTPS input to current Cloudflare networks.

## Operations

Check the public service:

```bash
curl --fail https://<host>/health
```

Check the container:

```bash
docker compose ps
docker compose logs --since 10m app
```

Rebuild and restart after an application change:

```bash
docker compose up -d --build app
```

Create a consistent SQLite backup:

```bash
docker compose exec -T app python -m app.backup
```

The command uses the SQLite backup API, runs an integrity check, and replaces the destination atomically. It sets the backup file mode to `0600`.

Copy the latest backup from the container:

```bash
docker compose cp app:/data/backups/agent-latest.sqlite3 ./agent-latest.sqlite3
chmod 600 ./agent-latest.sqlite3
```

The default command keeps one latest backup. Copy it to separate storage if you need backup history or host-loss protection.

## Security properties

- The service validates the raw webhook signature before JSON parsing.
- The service accepts notes from one configured WhatsApp ID.
- The database deduplicates inbound events by WhatsApp message ID.
- The webhook body has a fixed size limit.
- FastAPI listens on a private loopback port at the host.
- The container runs as an unprivileged user.
- The `.env` file stays outside the image and Git history.
- Link metadata requests reject local and private network destinations.
- Model inference works without network access after the model preload.
- Logs exclude access tokens and full message bodies.

The service still makes required network calls to Meta. It also requests public URLs that the user sends for link metadata.

## Reliability properties

- The webhook stores an inbound event before it returns HTTP 200.
- SQLite uses WAL mode and foreign-key checks.
- One durable queue handles note and reply work.
- The worker retries Graph API and processing errors with bounded backoff.
- A restart recovers stale events and stale link work.
- A repeated Meta webhook does not create a duplicate note.
- A failed link request does not remove its note.

## Limits

- The prototype serves one allowlisted WhatsApp user.
- It accepts future text messages only.
- It does not import WhatsApp history.
- It does not process images, audio, video, documents, reactions, or locations.
- It returns one original note instead of a generated answer.
- Source-message navigation depends on WhatsApp client behavior.
- Link enrichment reads HTML metadata only. It does not crawl articles or run JavaScript.
- The NumPy vector scan fits a small personal collection. It does not target a large multi-user database.
- The Meta test number is a sandbox asset. It has no production availability guarantee.
- The prototype has no public user registration, analytics, billing, or browser interface.
