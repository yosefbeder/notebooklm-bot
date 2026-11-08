# NotebookLM Telegram study bot

A first implementation of the flow in [DESIGN.md](DESIGN.md). The bot receives source files, creates a notebook through MCP, exposes an Open Notebook link, and generates English MCQs and flashcards with count/difficulty settings. Study sessions and jobs survive restarts.

## Run locally

Requires Python 3.12 or later. This workspace already has a virtual environment and dependencies installed.

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev,notebook]'
cp .env.example .env
```

Set `BOT_TOKEN` and preferably `ALLOWED_USER_IDS` in `.env`. Leave `TELEGRAM_API_BASE` empty to use Telegram's standard API. The token and Google session files are ignored by Git.

Authenticate the dedicated Google account using the same storage directory as the bot:

```sh
export NOTEBOOKLM_MCP_CLI_PATH="$PWD/data/notebooklm"
mkdir -p "$NOTEBOOKLM_MCP_CLI_PATH"
nlm login
study-bot check
study-bot
```

The bot starts the MCP server as a child process; no separate MCP service is needed. `check` verifies Telegram connectivity and required MCP tools, but does not create a notebook or prove Google authentication. Verify a real small source upload before using the bot for study.

`nlm login` requires an interactive browser. For a headless VPS, follow the server's [authentication guide](https://github.com/jacob-bd/gemini-notebook-mcp-cli/blob/main/docs/AUTHENTICATION.md) and keep its session storage private. The pinned server package is `notebooklm-mcp-cli==0.16.1`; it relies on community integration with NotebookLM's internal endpoints.

## Telegram flow

1. Forward/upload a document, photo, recording, or video. Album items are collected for three seconds into one notebook. Late items are rejected with a request to send them separately.
2. The control message advances through download, notebook creation, and source upload/processing. Generation waits for source readiness.
3. The notebook receives read-only public link access. Open Notebook stays on the control message. Anyone holding this link and signed into Google can view its sources; the initial deployment uses one operator account. Sharing failures offer a retry while studying remains available.
4. Generate MCQs / Generate flashcards opens count presets, custom count (1-100), and Easy/Medium/Hard. English is fixed. Confirming creates one durable job.
5. Start MCQs sends one quiz poll at a time. Leave/resume, scoring, explanations, and review of incorrect answers are supported. Flashcards use spoiler answers and Got it / Missed it ratings, retaining each card in the chat history.
6. PDF buttons export the same validated items with a separate answer section. Delete notebook and files asks for confirmation before deleting upstream and local data.

Commands: `/start`, `/help`, `/notebooks`, `/resume`. On a restart, active study sessions are paused; use `/resume` to continue. `/resume` can also recover a session interrupted by a Telegram send failure.

## Your existing VPS Bot API

For production on the same VPS as your self-hosted Telegram API:

```dotenv
TELEGRAM_API_BASE=http://127.0.0.1:8081
TELEGRAM_LOCAL_FILES=true
```

Use `TELEGRAM_LOCAL_FILES=true` only when the API runs in local mode and this process can read the absolute paths returned by `getFile`. If the API runs in a container, mount its download directory for the bot and configure both roots:

```dotenv
TELEGRAM_FILE_ROOT=/var/lib/telegram-bot-api
TELEGRAM_MOUNT_ROOT=/mnt/telegram-bot-api
```

With `TELEGRAM_LOCAL_FILES=false`, files are downloaded through the configured API's file endpoint. All API calls use the configured server; it does not silently switch servers after a connection error. A blank base URL selects Telegram's standard API, whose 20 MB bot download limit is checked before queuing known oversized files.

The SDK targets the current Bot API (aiogram 3.31 / Bot API 10.3). Update an older self-hosted API before using it with this SDK; older poll responses can fail SDK validation.

## Persistence and deployment

The default database is SQLite at `data/bot.sqlite3`. PostgreSQL is supported through SQLAlchemy and asyncpg:

```dotenv
DATABASE_URL=postgresql+asyncpg://user:password@127.0.0.1:5432/studybot
```

Keep the database, `DATA_DIR`, and the configured Google session directory on persistent storage. The bot needs write access there and read access to shared Telegram files. Run one bot process per deployment: polling and study progression use a process lock, and the worker claims durable jobs with leases and heartbeats. Multiple workers/replicas are not supported yet. The schema is initialized on startup; a migration framework is not included in this first version.

A [systemd unit](deploy/studybot.service) is provided for `/opt/notebooklm-bot` under a `notebookbot` service account. Adapt the account and paths, install dependencies, and authenticate Google as that account before enabling the unit. No VPS changes have been performed by this implementation.

## Recovery

Safe read/download failures retry up to three times with backoff. Upstream notebook/source/artifact writes are checkpointed before the request. If a request has an ambiguous result, the job enters `needs_review` rather than repeating the write and creating duplicates. The job ID appears in worker logs.

Stop the service before operator reconciliation. Inspect NotebookLM to determine whether the operation completed, then supply its verified ID:

```sh
study-bot reconcile JOB_ID --upstream-id NOTEBOOK_OR_SOURCE_OR_ARTIFACT_ID
```

For source uploads, verify that the source is fully processed before supplying its ID. Only if you have established that the write did not take place:

```sh
study-bot reconcile JOB_ID --confirmed-absent
```

Restart the service afterward. Completed checkpoints are preserved, so earlier steps are not recreated. Failed generation with a known artifact can retry polling/downloading that artifact; invalid or non-English exports require a new, user-confirmed generation.

## Images, count, and current limits

- NotebookLM count is a target. The quiz amount field is mapped to fewer/standard/more and the requested number is included in the focus instructions. Flashcard count is requested through instructions. The UI displays the actual count returned.
- Source images and illustrated PDF pages can be attached to flashcard answers as spoiler images. PDF matching uses conservative text overlap; it is not semantic figure identification. Unsupported extraction falls back to text cards. Original figure crops, labeled-image question fronts, slide/document figure extraction, and video frame matching still need a richer extraction/matching pipeline.
- Non-English generated prose is rejected using a language check. This is a heuristic, not an educational correctness check. Source images retain their original labels/language.
- PDF rendering supports images and escaped text. Set `PDF_FONT_PATH` to a Unicode TrueType font for scientific symbols. Very large PDFs are not automatically split into volumes in this version.
- Source processing is stage-based; the selected MCP upload does not stream byte progress. Every extension in the supplied screenshot is admitted locally, but actual acceptance/processing still depends on the pinned MCP and Google backend. Basic PDF/document/text signature checks are implemented; comprehensive MIME sniffing remains future work.
- Tests run against SQLite and local dependency stand-ins. A live Telegram/Google flow and PostgreSQL deployment still require verification with your credentials and environment.

## Tests

```sh
pytest -q
ruff check src tests
```

Unit/state tests cover ownership, duplicate input/votes/ratings, album grouping, settings, leave/resume, ambiguous-write recovery, local VPS file mapping, illustrated PDF exports, and source validation. MCP tests run a real stdio MCP client against `tests/fake_mcp.py`, which reproduces the selected server's tool/export shapes without Google access.

For actual Telegram HTTP serialization and end-to-end state progression, start the local Postman simulation in another terminal:

```sh
postman simulation run postman/simulations/studybot.sim.yaml --port 4010 --no-history --output ndjson
```

Then run:

```sh
SIM_BASE=http://127.0.0.1:4010 pytest -q
```

This includes real aiogram requests, file download, both generation paths, poll answers, card ratings, PDF sends, and a simulated Telegram 429 with retry. It uses only fake credentials. Provider assumptions and verification evidence are recorded in [the simulation context](postman/simulations/studybot.context.yaml).
