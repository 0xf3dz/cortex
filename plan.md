# WhatsApp semantic-memory agent

## Execution instruction

Implement this plan in phase order. Do not start a later phase until the current phase meets its acceptance checks.

Use current repository conventions when a repository already exists. If the target directory is empty, create the project structure specified in this plan.

Do not publish the repository or deploy a public user service. This prototype serves one allowlisted WhatsApp user.

Do not add features from the non-goals section. Do not add an OpenAI dependency or a generative large language model.

Human action is necessary for Meta account setup, Domain Name System (DNS) configuration, and the final WhatsApp client checks. Stop only at a named human gate when the required value is unavailable.

## Goal

Build a self-hosted WhatsApp agent that stores future text notes and links. The user can later describe one note with a natural-language query and receive the best original note as a contextual WhatsApp reply.

The response must quote the original WhatsApp message. The user must be able to tap the quote and attempt to jump to the source message and its surrounding conversation.

## Fixed scope

- Serve one user.
- Use the user's existing personal WhatsApp account as the client.
- Use a Meta WhatsApp Business Platform test number as the agent during development.
- Accept future text messages and links.
- Do not import existing WhatsApp history.
- Treat a message that starts with `?` as a search query.
- Treat other supported text messages as notes.
- Return one best source note.
- Do not generate an answer or summary.
- Run embeddings on the virtual private server (VPS).
- Use Python and FastAPI.
- Use SQLite and SQLite FTS5.
- Deploy with Docker Compose and Caddy.
- Target a VPS with at least 4 GB of random-access memory (RAM).

## User interaction

### Save a note

Input:

```text
Try sqlite-vec for small local retrieval projects
```

Reaction:

```text
👍
```

### Search notes

Input:

```text
? vector database for small projects
```

Response behavior:

1. Find the best source note.
2. Send a contextual reply with the source note's WhatsApp message ID.
3. Put `Best match.` in the new response body.
4. Let WhatsApp display the original note in the quoted-message section.

Example response body:

```text
Best match.
```

### Empty collection

Input:

```text
? database
```

Response:

```text
You have no saved notes yet.
```

### Supported commands

```text
? <query>       Search the notes
/help           Show usage instructions
/delete-last    Delete the most recently saved note
```

Do not save command messages as notes.

## Important WhatsApp limitation

WhatsApp does not provide a normal permalink for an individual message.

Use a contextual reply instead:

```json
{
  "context": {
    "message_id": "<ORIGINAL_NOTE_WAMID>"
  }
}
```

The original inbound `wamid` must remain attached to its note. WhatsApp clients normally make the quoted section selectable and use it to navigate to the original message.

Treat source navigation as an early proof requirement. Do not claim that navigation works until it passes on the user's mobile WhatsApp client and WhatsApp Web.

If navigation fails, keep the quoted reply. Do not invent an unsupported message URL.

Official contextual-reply reference:

- <https://www.postman.com/meta/whatsapp-business-platform/request/b6qw6hm/send-reply-to-contact-message>

## No generative model

A generative large language model is not necessary for this prototype.

For a query, the service must:

1. Remove the leading `?`.
2. Embed the remaining natural-language query.
3. run lexical retrieval;
4. run vector retrieval;
5. combine the two rankings;
6. return the best original note.

Use a compact multilingual embedding encoder. Start with:

```text
intfloat/multilingual-e5-small
```

Use the model's required text prefixes:

```text
passage: <stored note>
query: <search query>
```

Run one application process. Multiple processes would load multiple model copies into RAM and could start competing workers.

Do not send note text, query text, or embeddings to OpenAI or another inference provider.

## Architecture

```text
Personal WhatsApp
       |
       | text message
       v
Meta WhatsApp Cloud API
       |
       | signed webhook
       v
Caddy HTTPS reverse proxy
       |
       v
FastAPI application
       |
       |-- verify Meta signature
       |-- enforce sender allowlist
       |-- deduplicate by wamid
       |-- persist event before HTTP 200
       v
Local durable worker
       |-- save note
       |-- enrich public links
       |-- create local embedding
       |-- run hybrid retrieval
       |-- send contextual reply
       v
SQLite + FTS5 + embedding blobs
```

## Webhook contract

### Verification endpoint

Provide:

```text
GET /webhooks/whatsapp
```

Validate `hub.verify_token`. Return the raw `hub.challenge` when validation succeeds.

### Event endpoint

Provide:

```text
POST /webhooks/whatsapp
```

For each request:

1. Read the raw request body.
2. Validate `X-Hub-Signature-256` with `META_APP_SECRET`.
3. Reject an invalid signature.
4. Parse the JSON only after signature validation.
5. Ignore delivery and read-status events.
6. Ignore unsupported message types.
7. Ignore senders other than `ALLOWED_WHATSAPP_WA_ID`.
8. Insert the inbound message into SQLite with `INSERT OR IGNORE` on `wamid`.
9. Commit the transaction.
10. Return HTTP 200 without waiting for embedding inference or a Graph API response.

A local worker must process durable pending events. Do not use an in-memory FastAPI background task as the only queue because the application could lose acknowledged notes during a restart.

## Outbound WhatsApp client

Create one client module for the Meta Graph API.

It must support:

- plain text responses;
- contextual text responses;
- configured Graph API version;
- request timeout;
- explicit handling of non-success responses;
- redacted error logs.

Use the source note's inbound `wamid` in `context.message_id` for a search result.

A user query opens the WhatsApp customer-service window. Search responses therefore do not require a proactive message template.

## Database

Enable these SQLite settings:

```text
journal_mode = WAL
foreign_keys = ON
busy_timeout = 5000
```

Use schema migrations. Keep all persistent application data in a mounted Docker volume.

### `inbound_events`

Use this table as the durable queue and webhook deduplication boundary:

```text
wamid               TEXT PRIMARY KEY
sender_wa_id        TEXT NOT NULL
message_type        TEXT NOT NULL
body                TEXT
whatsapp_timestamp  INTEGER NOT NULL
received_at         INTEGER NOT NULL
processing_state    TEXT NOT NULL
failure_reason      TEXT
attempt_count       INTEGER NOT NULL DEFAULT 0
next_attempt_at     INTEGER
```

Use these processing states:

```text
pending
processing
completed
failed
```

Recover stale `processing` records after a process restart. Use bounded retries for transient Graph API and URL errors.

After a query completes, remove its body from the durable queue or remove the completed query row. Do not keep a second permanent query history.

### `notes`

```text
wamid                  TEXT PRIMARY KEY
sender_wa_id           TEXT NOT NULL
body                   TEXT NOT NULL
searchable_text        TEXT NOT NULL
urls_json              TEXT NOT NULL
link_title             TEXT
link_description       TEXT
whatsapp_timestamp     INTEGER NOT NULL
created_at             INTEGER NOT NULL
embedding              BLOB NOT NULL
embedding_dimensions   INTEGER NOT NULL
embedding_model        TEXT NOT NULL
```

Store normalized `float32` embedding arrays as binary data. Validate the recorded dimensions when loading them.

### `notes_fts`

Create an SQLite FTS5 virtual table over:

- note body;
- URL text;
- link title;
- link description.

Keep the FTS5 data synchronized with note creation, enrichment, and deletion.

## Note processing

For a supported note message:

1. Persist the inbound event.
2. Store the original message body.
3. Extract public HTTP and HTTPS URLs.
4. Build the first `searchable_text` value.
5. Embed `passage: <searchable_text>` locally.
6. insert the note and FTS5 row in one transaction;
7. apply a thumbs-up reaction to the original message;
8. enrich URLs asynchronously;
9. update `searchable_text`, FTS5, and the embedding after successful enrichment.

A failure to enrich a URL must not remove or reject the note.

Use the WhatsApp message timestamp as the saved timestamp. Do not use the worker-processing time as the source date.

## Link enrichment

A bare URL has little semantic information. For a public HTML page, collect only:

- the final public URL;
- the HTML title;
- the meta description.

Do not crawl the full article. Do not execute JavaScript. Do not download PDFs or other documents.

The fetcher must:

- accept only `http` and `https` URLs;
- reject embedded credentials;
- reject loopback destinations;
- reject private, link-local, multicast, and reserved Internet Protocol addresses;
- validate each redirect destination;
- limit redirects;
- use a short connection and response timeout;
- limit response bytes;
- require an HTML content type;
- send a defined user agent;
- fail closed when host validation is uncertain.

The sender allowlist does not remove the need for these controls. The fetcher runs inside the VPS network boundary.

## Retrieval

### Lexical retrieval

Use SQLite FTS5 for exact terms such as:

- names;
- URLs;
- filenames;
- database names;
- model names;
- identifiers.

### Vector retrieval

At application startup:

1. Load note IDs and embeddings from SQLite.
2. Validate the model name and dimensions.
3. Build one NumPy matrix.
4. Keep rows normalized for cosine similarity.

Update or rebuild the matrix after a note is added, enriched, or deleted.

For a query:

1. Embed `query: <query text>`.
2. Calculate vector similarity with one NumPy matrix operation.
3. Produce a semantic ranking.
4. Produce an FTS5 ranking.
5. combine rankings with Reciprocal Rank Fusion;
6. select one best note.

Use:

```text
RRF score = sum(1 / (60 + rank))
```

Do not add a minimum-score threshold before the real query evaluation. Return the best note when at least one note exists.

## Logging

Use structured logs.

Log:

- event type;
- `wamid`;
- processing state;
- elapsed time;
- external response status;
- redacted error category.

Do not log:

- Meta access tokens;
- app secrets;
- full message bodies;
- full query text;
- full webhook payloads;
- embedding arrays.

## Security

Implement these controls before deployment:

- Verify every Meta webhook signature.
- Allow only the configured personal WhatsApp `wa_id`.
- Deduplicate every webhook by `wamid`.
- Keep the FastAPI application port private to Docker.
- Expose only Secure Shell (SSH), HTTP, and HTTPS ports.
- Use SSH keys.
- Disable SSH password authentication after key access works.
- Store secrets outside the image and repository.
- Restrict the secret-file permissions.
- Pin the Meta Graph API version through configuration.
- Apply request body limits.
- Apply timeouts to all external requests.
- Do not expose SQLite through a network service.
- Do not include message bodies in health responses or metrics.

## Configuration

Provide `.env.example` with names but no secret values:

```text
META_ACCESS_TOKEN=
META_APP_SECRET=
META_VERIFY_TOKEN=
WHATSAPP_PHONE_NUMBER_ID=
ALLOWED_WHATSAPP_WA_ID=
WHATSAPP_GRAPH_API_VERSION=
DATABASE_PATH=/data/agent.sqlite3
EMBEDDING_MODEL=intfloat/multilingual-e5-small
PUBLIC_BASE_URL=
LOG_LEVEL=INFO
```

Validate all required configuration at startup. Stop with a concise error when a value is absent or malformed.

## Suggested repository structure

```text
.
|-- app/
|   |-- __init__.py
|   |-- main.py
|   |-- config.py
|   |-- db.py
|   |-- schema.py
|   |-- migrations.py
|   |-- webhook.py
|   |-- webhook_security.py
|   |-- worker.py
|   |-- whatsapp_client.py
|   |-- note_service.py
|   |-- embedding_service.py
|   |-- retrieval.py
|   |-- link_enrichment.py
|   `-- logging_config.py
|-- tests/
|   |-- fixtures/
|   |-- test_webhook_verification.py
|   |-- test_webhook_signature.py
|   |-- test_webhook_idempotency.py
|   |-- test_message_routing.py
|   |-- test_note_service.py
|   |-- test_retrieval.py
|   |-- test_contextual_reply.py
|   `-- test_link_security.py
|-- Dockerfile
|-- compose.yml
|-- Caddyfile
|-- pyproject.toml
|-- .env.example
|-- .gitignore
`-- README.md
```

Do not create an administrative interface or a separate frontend.

## Phase 1 — Local service

### Work

- [ ] Create the Python project.
- [ ] Add FastAPI and configuration validation.
- [ ] Add SQLite initialization and migrations.
- [ ] Add the health endpoint.
- [ ] Add webhook verification.
- [ ] Add raw-body signature validation.
- [ ] Add Meta webhook fixtures.
- [ ] Add a fake outbound WhatsApp client.
- [ ] Add durable event insertion and deduplication.
- [ ] Add allowlist enforcement.

### Acceptance checks

- [ ] A correct verification token returns the challenge.
- [ ] An incorrect verification token is rejected.
- [ ] A correctly signed fixture is accepted.
- [ ] An invalid signature is rejected.
- [ ] A repeated `wamid` creates one event.
- [ ] A non-allowlisted sender does not create a note.
- [ ] A process restart retains a pending event.

## Phase 2 — Meta test-number connection

### Human gate: Meta account

The user must complete or provide access for these actions:

1. Register at Meta for Developers.
2. Create an app with the **Connect with customers through WhatsApp** use case.
3. Create or attach a Meta Business Portfolio.
4. Create or attach a WhatsApp Business Account.
5. Select Meta's test business number.
6. Add the user's personal WhatsApp number as the test recipient.
7. Provide the app secret, test phone-number ID, Graph API version, and temporary token.

Official guide:

- <https://developers.facebook.com/documentation/business-messaging/whatsapp/get-started/>

### Work

- [ ] Add the real Graph API client.
- [ ] Deploy the current echo service behind temporary or final HTTPS.
- [ ] Configure the Meta webhook callback.
- [ ] Subscribe to the `messages` webhook field.
- [ ] Receive a real inbound text message.
- [ ] Send a real outbound text response.
- [ ] Record the user's inbound `wa_id` for the allowlist.
- [ ] Replace the temporary token with a system-user token before unattended VPS use.

### Acceptance checks

- [ ] A message from the personal phone reaches the VPS.
- [ ] The webhook signature passes.
- [ ] The service records the inbound `wamid`.
- [ ] The test business number replies successfully.
- [ ] Logs contain no access token or full message body.

## Phase 3 — Note capture

### Work

- [ ] Route `?` messages to search handling.
- [ ] Route `/help` and `/delete-last` to command handling.
- [ ] Route other supported text to note handling.
- [ ] Store notes and embeddings.
- [ ] Apply a thumbs-up reaction only after durable note storage succeeds.
- [ ] Add durable worker retries.
- [ ] Recover stale jobs after restart.

### Acceptance checks

- [ ] A normal text message creates one note.
- [ ] A query does not create a note.
- [ ] A command does not create a note.
- [ ] A webhook retry does not create a duplicate note.
- [ ] `/delete-last` removes the correct note and its search data.
- [ ] A restart does not lose an acknowledged note.

## Phase 4 — Hybrid retrieval

### Work

- [ ] Load the local embedding model once.
- [ ] Add passage embeddings for notes.
- [ ] Add query embeddings for searches.
- [ ] Add FTS5 indexing and lexical retrieval.
- [ ] Add NumPy cosine retrieval.
- [ ] Add Reciprocal Rank Fusion.
- [ ] Return one best note.

### Evaluation data

Create at least 20 meaningfully different notes. Write 10 queries that paraphrase the intended notes instead of copying their words.

### Acceptance checks

- [ ] The intended note ranks first for at least 8 of 10 queries.
- [ ] Exact URLs remain searchable.
- [ ] Exact technical names remain searchable.
- [ ] A warm query completes within two seconds on the VPS.
- [ ] Search makes no external inference request.
- [ ] An empty collection returns the defined response.

## Phase 5 — Source-message navigation

### Work

- [ ] Send the search response with `context.message_id` set to the source note's `wamid`.
- [ ] Use `Best match.` as the response body.

### Human gate: client behavior

The user must perform these checks:

1. Save a note from the mobile WhatsApp application.
2. Send enough later messages to move it into older history.
3. Find it with a paraphrased `?` query.
4. Confirm that the result quotes the original note.
5. Tap the quote on mobile.
6. Confirm whether WhatsApp jumps to the original message.
7. Repeat the check in WhatsApp Web.

### Acceptance checks

- [ ] The response quotes the correct source note.
- [ ] Mobile navigation behavior is recorded from a real check.
- [ ] WhatsApp Web navigation behavior is recorded from a real check.
- [ ] The response body is `Best match.`.
- [ ] The service does not invent a permalink.

## Phase 6 — Link enrichment

### Work

- [ ] Extract HTTP and HTTPS URLs.
- [ ] Validate public destinations.
- [ ] Add redirect, timeout, content-type, and byte limits.
- [ ] Parse HTML title and meta description.
- [ ] Update the note, FTS5 row, and embedding.
- [ ] Preserve the note when enrichment fails.

### Acceptance checks

- [ ] A captioned link is searchable by its caption.
- [ ] A bare public link is searchable by its page title or description.
- [ ] A failed URL remains saved.
- [ ] A loopback URL is rejected.
- [ ] A private-network URL is rejected.
- [ ] A redirect to a private destination is rejected.
- [ ] An oversized response is stopped.

## Phase 7 — VPS deployment

### Human gate: VPS and DNS

The user must provide:

- VPS SSH access;
- a domain or subdomain;
- DNS control;
- the final Meta configuration values.

### Work

- [ ] Install Docker Engine and the Docker Compose plugin.
- [ ] Configure the host firewall.
- [ ] Configure SSH key access.
- [ ] Create the application image.
- [ ] Create the Caddy reverse-proxy configuration.
- [ ] Create persistent data and model-cache volumes.
- [ ] Configure automatic HTTPS.
- [ ] Configure container health checks.
- [ ] Configure restart policies.
- [ ] Configure secrets outside the repository.
- [ ] Configure a database backup command.
- [ ] Update the Meta webhook callback to the final HTTPS URL.

### Deployment shape

```text
Docker Compose
|-- app
`-- caddy
```

Run one application process and one durable worker within the controlled application lifecycle. Do not run multiple web workers that each load the embedding model.

### Acceptance checks

- [ ] HTTPS validates without a browser warning.
- [ ] The FastAPI port is not public.
- [ ] SQLite persists outside the container filesystem.
- [ ] The model cache persists outside the container filesystem.
- [ ] A VPS reboot restarts the service.
- [ ] Notes saved before a reboot remain searchable.
- [ ] Meta can deliver webhooks after the reboot.
- [ ] A backup copy of SQLite can be created consistently.

## Final end-to-end proof

Do not declare the prototype complete until this scenario passes:

1. Send this note from the personal WhatsApp mobile application:

   ```text
   sqlite-vec could be useful for small local semantic-search projects
   ```

2. Receive a thumbs-up reaction on the note.

3. Send several unrelated notes.
4. Restart the VPS.
5. Send:

   ```text
   ? what lightweight vector storage did I want to investigate?
   ```

6. Receive one response that quotes the original `sqlite-vec` note.
7. Tap the quoted note on mobile.
8. Observe whether WhatsApp navigates to the source message and surrounding messages.
9. Repeat the search and navigation check in WhatsApp Web.
10. Confirm that no OpenAI or external model API was called.
11. Confirm that the correct note ranked first.
12. Confirm that the full flow works after another service restart.

## Non-goals

Do not implement:

- existing-history import;
- multiple users;
- user registration;
- voice notes;
- image processing;
- PDF processing;
- generated answers;
- summaries;
- reminders;
- proactive messages;
- multiple search results;
- note editing;
- a web dashboard;
- a mobile application;
- a browser extension;
- production phone-number onboarding;
- billing;
- analytics;
- public access.

## Completion definition

The prototype is complete when one allowlisted personal WhatsApp account can:

1. send a future text note or public link to the Meta test business number;
2. receive a durable save confirmation;
3. search with a natural-language `?` query;
4. receive the correct best note as a contextual reply;
5. attempt source navigation through the quoted message on mobile and WhatsApp Web;
6. retain all notes across application and VPS restarts;
7. perform all embedding inference on the VPS.

Do not replace missing behavior with a stub, mock, placeholder, silent fallback, or fabricated result.

