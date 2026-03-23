# Jira → ADO Bug Pipeline — Claude Project Instructions

You are assisting with the ongoing development of a Python/Flask web application that automates the creation of Azure DevOps (ADO) bugs from Jira issues. The app lives in a GitHub repo and is deployed on Railway.

---

## Project Overview

**What it does:**
1. Fetches Jira issues matching a JQL filter
2. Checks each issue for video attachments and description quality
3. Transcribes video attachments via OpenAI Whisper
4. Uses Claude AI to assess whether there is enough information to file a high-quality ADO bug
5. Queues passing items for user review in a web UI
6. User edits the draft bug fields and submits to ADO
7. Also supports auto-ingest via Jira webhook (new issues POST directly to the app)

**Stack:** Python 3 / Flask, SQLite (persistent), Jinja2 templates, vanilla JS frontend (no framework)

---

## Repository & Deployment

| Item | Value |
|------|-------|
| GitHub repo | `https://github.com/jarrettkersten/Jira-to-ADO-Bug-Automation` |
| Branch | `main` |
| Live URL | `https://web-production-be84d.up.railway.app` |
| Railway project | `aware-upliftment` (production environment) |
| Railway service | `web` |
| Railway region | asia-southeast1 |

**Deployment:** Railway auto-deploys on every push to `main`. Use `push.bat` in the repo root to stage, commit, version-tag, and push in one step.

---

## File Structure

```
/
├── app.py                          # Flask backend — all routes + pipeline logic
├── templates/
│   └── index.html                  # Single-page frontend (HTML + CSS + JS, ~1700 lines)
├── static/                         # Static assets
├── requirements.txt                # flask, gunicorn, anthropic, openai, requests, python-dotenv
├── Procfile                        # web: gunicorn app:app --bind 0.0.0.0:$PORT --timeout 600 --workers 1
├── nixpacks.toml                   # Tells Railway to install ffmpeg
├── .gitignore                      # Excludes .env, *.db, __pycache__, start.bat, .venv/
├── push.bat                        # Windows script: commit + version tag + push to GitHub
├── start.bat                       # Windows script: run app locally
└── pipeline.db                     # LOCAL SQLite DB (gitignored — Railway uses /data/pipeline.db)
```

> ⚠️ `--workers 1` in Procfile is intentional — the `_jobs` dict is in-memory and cannot be shared across multiple gunicorn workers.

---

## Environment Variables

Set in Railway (Variables tab on the `web` service). Also kept locally in `.env` (gitignored).

| Variable | Purpose |
|----------|---------|
| `ANTHROPIC_API_KEY` | Claude API key (claude-3-5-sonnet used for bug quality assessment) |
| `OPENAI_API_KEY` | OpenAI key (Whisper used for video transcription) |
| `ADO_ORG_URL` | `https://dev.azure.com/CoraSystems` |
| `ADO_PAT` | Azure DevOps Personal Access Token |
| `ADO_PROJECT` | `PPM` |
| `JIRA_BASE_URL` | `https://corasystems.atlassian.net` |
| `JIRA_EMAIL` | `jkersten@corasystems.com` |
| `JIRA_API_TOKEN` | Jira API token |
| `JIRA_JQL` | `parent = PSUS-80 AND issuetype = Task` |
| `DB_PATH` | `/data/pipeline.db` — points SQLite to Railway persistent volume |

---

## Database — SQLite

- **Local:** `pipeline.db` next to `app.py` (gitignored)
- **Railway:** `/data/pipeline.db` on a persistent Volume mounted at `/data`
- The `DB_PATH` env var controls which path is used (`os.getenv("DB_PATH", ...)`)
- Railway volume is named `web-volume`, attached to the `web` service

### Schema — `queue_items` table

```sql
CREATE TABLE IF NOT EXISTS queue_items (
    jira_key              TEXT PRIMARY KEY,
    jira_title            TEXT,
    jira_url              TEXT,
    has_video             INTEGER DEFAULT 0,
    has_desc              INTEGER DEFAULT 0,
    confidence            INTEGER DEFAULT 0,
    bug_json              TEXT,           -- JSON: all ADO bug draft fields
    jira_fields_json      TEXT,           -- JSON: raw Jira fields
    screenshots_json      TEXT,           -- JSON: list of screenshot attachments
    video_filename        TEXT,
    video_size            INTEGER DEFAULT 0,
    extra_video_count     INTEGER DEFAULT 0,
    other_attachments_json TEXT,
    transcript            TEXT,
    status                TEXT DEFAULT 'queued',   -- queued | submitted | rejected
    ado_url               TEXT,
    created_at            TEXT,
    updated_at            TEXT
)
```

---

## Key API Routes (app.py)

| Method | Route | Purpose |
|--------|-------|---------|
| GET | `/` | Serves the main UI (index.html) |
| POST | `/api/run` | Kicks off the Jira pipeline (fetches + processes issues) |
| GET | `/api/jobs` | Returns current job list + queue state |
| GET | `/api/queue` | Returns full queue + submitted items |
| POST | `/api/queue/<key>/submit` | Submits a bug to ADO |
| POST | `/api/queue/<key>/reject` | Rejects/dismisses a queue item |
| POST | `/api/queue/<key>/save-draft` | PATCH: saves draft form fields into `bug_json` |
| GET | `/api/config` | Returns config values including `webhook_url` |
| POST | `/api/webhook/jira` | Jira webhook endpoint — only handles `jira:issue_created` |
| GET | `/api/webhook/status` | Returns last webhook received timestamp + key |

---

## Jira Webhook

- **Webhook URL:** `https://web-production-be84d.up.railway.app/api/webhook/jira`
- **Configured in:** Jira Admin → System → WebHooks → "Jira to ADO_New Bug Creation" (ENABLED)
- **Event:** Issue → created
- **JQL filter:** `parent = PSUS-80 AND issuetype = Task`
- The `_webhook_last_received` dict tracks the last event for the UI status badge (polled every 30s)
- Non-`jira:issue_created` events are ignored (returns 200 `{"status": "ignored"}`)
- Already-processed keys (in DB) are skipped (returns 200 `{"status": "already_processed"}`)

---

## Frontend (index.html) — Key JS Functions

| Function | Purpose |
|----------|---------|
| `init()` | On load: fetch config, start polling, render queue |
| `runPipeline()` | POST /api/run, show job progress |
| `renderQueueRows(items)` | Renders the Queue tab table |
| `renderSubmittedRows(items)` | Renders the Submitted tab table |
| `applyFilterSort()` | Client-side search/filter/sort across all columns |
| `openModal(item)` | Opens the bug review/edit modal for a queue item |
| `saveDraft()` | POST /api/queue/<key>/save-draft with all form field values |
| `submitBug()` | POST /api/queue/<key>/submit |
| `refreshWebhookStatus()` | GET /api/webhook/status, updates the status badge |
| `copyWebhookUrl()` | Copies webhook URL to clipboard |
| `fmtDateShort(iso)` | Formats ISO date → "Mar 22, 2026" |

### Filter Toolbar CSS (single-row, non-wrapping)
```css
.filter-toolbar { display:flex; gap:8px; align-items:center; padding:10px 0 6px; flex-wrap:nowrap; overflow-x:auto }
.filter-toolbar input[type=text] { flex:1 1 160px; min-width:0; max-width:260px; }
.filter-toolbar select { flex-shrink:0; }
.filter-toolbar .clear-filters { flex-shrink:0; white-space:nowrap }
.filter-count { flex-shrink:0; margin-left:auto; white-space:nowrap }
```

---

## Queue List View Columns

**Queue tab:** Jira Key | Jira Title | ADO Bug Title | Status | Confidence | Has Video | Created | Updated

**Submitted tab:** Same columns + ADO URL

The "ADO Bug Title" column (`item.bug?.title`) is sortable (`adotitle`) and included in full-text search.

---

## saveDraft() — Fields Saved

When the user edits the modal form, `saveDraft()` POSTs these fields to `bug_json`:

```javascript
{ title, repro_combined, acceptance_criteria, severity, customer_impact,
  test_site, product_area, found_in_build, tags, primary_customer,
  area_path, iteration_path }
```

`openModal()` restores drafts via `b.repro_combined || combined` (prioritises saved draft over generated content).

---

## nixpacks.toml — ffmpeg on Railway

```toml
[phases.setup]
nixPkgs = ["ffmpeg"]
```

Required because Railway's default nixpacks build does not include ffmpeg, which is needed for video transcription via OpenAI Whisper.

---

## push.bat — Release Workflow

Running `push.bat` from the repo folder will:
1. Show `git status`
2. Detect current version tag (e.g. `v1.0.2`)
3. Ask: Patch / Minor / Major / Skip
4. Ask for a commit message (defaults to "Release vX.X.X")
5. `git add -A` → `git commit` → `git push origin main`
6. Create annotated tag → `git push origin vX.X.X`
7. Railway auto-deploys within ~2 minutes

---

## Local Development

```bat
start.bat        # Installs deps, checks ffmpeg, runs app at http://localhost:5000
```

`.env` file is required locally (gitignored). Copy structure from environment variables table above.

---

## Known Behaviours / Gotchas

- **First-time Railway run showed all previously processed items** — this is expected, it's because the Railway DB started empty. Once items are processed/dismissed, they're stored in the persistent volume and won't re-appear.
- **Workers must stay at 1** — `_jobs` is an in-memory dict; using multiple gunicorn workers would cause job state to be invisible across workers.
- **Video timeout** — gunicorn timeout is set to 600s to allow time for large video downloads + Whisper transcription.
- **Jira only accepts HTTPS webhook URLs** — local `http://localhost:5000` is rejected by Jira Cloud. Railway deployment solves this.
