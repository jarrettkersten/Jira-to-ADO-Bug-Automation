"""
Jira → ADO Bug Pipeline
=======================
Fetches Jira items from a JQL query, checks for video attachments and
description quality, transcribes videos via OpenAI Whisper, uses Claude
to assess whether there is sufficient info to create an ADO bug, and queues
passing items for user review + submission.

Stack: Python / Flask  (same tech stack as cora-bug-creator-shared)
Requires: ffmpeg, ffprobe on PATH
"""

import os, json, io, re, tempfile, traceback, base64, subprocess, uuid, threading, sqlite3
from datetime import datetime, timedelta
import requests
from flask import Flask, render_template, request, jsonify
from dotenv import load_dotenv
import openai
import anthropic

load_dotenv()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 600 * 1024 * 1024  # 600 MB (large video downloads)

# ── Config ─────────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY    = os.getenv("OPENAI_API_KEY", "")

ADO_ORG_URL  = os.getenv("ADO_ORG_URL", "https://dev.azure.com/CoraSystems").rstrip("/")
ADO_PAT      = os.getenv("ADO_PAT", "")
ADO_PROJECT  = os.getenv("ADO_PROJECT", "PPM")

JIRA_BASE_URL  = os.getenv("JIRA_BASE_URL", "").rstrip("/")   # e.g. https://acme.atlassian.net
JIRA_EMAIL     = os.getenv("JIRA_EMAIL", "")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN", "")

# Static JQL query — always run against this filter
JIRA_JQL = os.getenv("JIRA_JQL", "parent = PSUS-80 AND issuetype = Task")

VIDEO_MIME_TYPES = {
    "video/mp4", "video/quicktime", "video/x-msvideo",
    "video/webm", "video/x-matroska", "video/x-m4v",
}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".webm", ".mkv", ".m4v"}


# ══════════════════════════════════════════════════════════════════════════════
# Persistent queue — SQLite
# ══════════════════════════════════════════════════════════════════════════════

# DB_PATH can be overridden via env var — set to e.g. /data/pipeline.db when using
# a Railway persistent volume mounted at /data
DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "pipeline.db"))


def _db():
    """Open a SQLite connection (thread-safe via check_same_thread=False + short-lived connections)."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS queue_items (
                jira_key              TEXT PRIMARY KEY,
                jira_title            TEXT,
                jira_url              TEXT,
                has_video             INTEGER DEFAULT 0,
                has_desc              INTEGER DEFAULT 0,
                confidence            INTEGER DEFAULT 0,
                bug_json              TEXT,
                jira_fields_json      TEXT,
                screenshots_json      TEXT,
                video_filename        TEXT,
                video_size            INTEGER DEFAULT 0,
                extra_video_count     INTEGER DEFAULT 0,
                other_attachments_json TEXT,
                submitted             INTEGER DEFAULT 0,
                ado_work_item_id      TEXT,
                ado_url               TEXT,
                processed_at          TEXT,
                submitted_at          TEXT
            )
        """)
        # Migrate existing DBs that predate these columns
        for col, defn in [("extra_video_count", "INTEGER DEFAULT 0"),
                          ("other_attachments_json", "TEXT")]:
            try:
                conn.execute(f"ALTER TABLE queue_items ADD COLUMN {col} {defn}")
            except Exception:
                pass  # column already exists
        conn.execute("""
            CREATE TABLE IF NOT EXISTS skipped_items (
                jira_key    TEXT PRIMARY KEY,
                jira_title  TEXT,
                jira_url    TEXT,
                has_video   INTEGER DEFAULT 0,
                confidence  INTEGER DEFAULT 0,
                missing     TEXT,
                explanation TEXT,
                commented   INTEGER DEFAULT 0,
                processed_at TEXT
            )
        """)
        conn.commit()


init_db()


def db_save_queue_item(item: dict):
    """Insert or replace a queued item in the persistent store."""
    with _db() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO queue_items
            (jira_key, jira_title, jira_url, has_video, has_desc, confidence,
             bug_json, jira_fields_json, screenshots_json,
             video_filename, video_size, extra_video_count, other_attachments_json,
             submitted, ado_work_item_id, ado_url, processed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL, ?)
        """, (
            item["jira_key"],
            item.get("jira_title", ""),
            item.get("jira_url"),
            1 if item.get("has_video") else 0,
            1 if item.get("has_desc") else 0,
            item.get("confidence", 0),
            json.dumps(item.get("bug") or {}),
            json.dumps(item.get("jira_fields") or {}),
            json.dumps(item.get("screenshots") or []),
            item.get("video_filename"),
            item.get("video_size", 0),
            item.get("extra_video_count", 0),
            json.dumps(item.get("other_attachments") or []),
            datetime.utcnow().isoformat(),
        ))
        conn.commit()


def db_save_skipped_item(item: dict):
    """Insert or replace a skipped item in the persistent store."""
    with _db() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO skipped_items
            (jira_key, jira_title, jira_url, has_video, confidence,
             missing, explanation, commented, processed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            item["jira_key"],
            item.get("jira_title", ""),
            item.get("jira_url"),
            1 if item.get("has_video") else 0,
            item.get("confidence", 0),
            json.dumps(item.get("missing") or []),
            item.get("explanation", ""),
            1 if item.get("commented") else 0,
            datetime.utcnow().isoformat(),
        ))
        conn.commit()


def db_get_all_queue_items() -> list[dict]:
    """Return all queue items WITHOUT screenshots (screenshots are fetched on demand)."""
    with _db() as conn:
        rows = conn.execute(
            """SELECT jira_key, jira_title, jira_url, has_video, has_desc, confidence,
                      bug_json, jira_fields_json, other_attachments_json,
                      video_filename, video_size, extra_video_count,
                      submitted, ado_work_item_id, ado_url, processed_at, submitted_at
               FROM queue_items ORDER BY submitted ASC, processed_at DESC"""
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["bug"]               = json.loads(item.pop("bug_json")              or "{}")
        item["jira_fields"]       = json.loads(item.pop("jira_fields_json")      or "{}")
        item["other_attachments"] = json.loads(item.pop("other_attachments_json") or "[]")
        item["has_video"]         = bool(item["has_video"])
        item["has_desc"]          = bool(item["has_desc"])
        item["submitted"]         = bool(item["submitted"])
        item["extra_video_count"] = item.get("extra_video_count") or 0
        result.append(item)
    return result


def db_get_screenshots(jira_key: str) -> list[dict]:
    """Fetch just the screenshots blob for a single queue item (called on modal open)."""
    with _db() as conn:
        row = conn.execute(
            "SELECT screenshots_json FROM queue_items WHERE jira_key=?", (jira_key,)
        ).fetchone()
    if not row:
        return []
    return json.loads(row["screenshots_json"] or "[]")


def db_get_all_skipped_items() -> list[dict]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM skipped_items ORDER BY processed_at DESC"
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["missing"]   = json.loads(item.pop("missing") or "[]")
        item["has_video"] = bool(item["has_video"])
        item["commented"] = bool(item["commented"])
        result.append(item)
    return result


def db_is_processed(jira_key: str) -> bool:
    """Return True if this key exists in either the queue or skipped table."""
    with _db() as conn:
        q = conn.execute(
            "SELECT jira_key FROM queue_items WHERE jira_key=?", (jira_key,)
        ).fetchone()
        if q:
            return True
        s = conn.execute(
            "SELECT jira_key FROM skipped_items WHERE jira_key=?", (jira_key,)
        ).fetchone()
        return s is not None


def db_mark_submitted(jira_key: str, work_item_id, url: str):
    with _db() as conn:
        conn.execute("""
            UPDATE queue_items
            SET submitted=1, ado_work_item_id=?, ado_url=?, submitted_at=?
            WHERE jira_key=?
        """, (
            str(work_item_id) if work_item_id else None,
            url,
            datetime.utcnow().isoformat(),
            jira_key,
        ))
        conn.commit()


def db_delete_item(jira_key: str):
    """Remove from both tables so the item can be reprocessed."""
    with _db() as conn:
        conn.execute("DELETE FROM queue_items  WHERE jira_key=?", (jira_key,))
        conn.execute("DELETE FROM skipped_items WHERE jira_key=?", (jira_key,))
        conn.commit()


# ══════════════════════════════════════════════════════════════════════════════
# ADF (Atlassian Document Format) → plain text
# ══════════════════════════════════════════════════════════════════════════════

def adf_to_text(node, depth=0) -> str:
    """Recursively extract plain text from a Jira ADF document node."""
    if not node:
        return ""
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        return ""

    node_type = node.get("type", "")

    # Leaf text node
    if node_type == "text":
        return node.get("text", "")

    # Hard break / paragraph / heading → newline
    parts = [adf_to_text(child, depth + 1) for child in node.get("content", [])]
    joined = "".join(parts)

    if node_type in ("paragraph", "heading", "blockquote", "listItem",
                     "bulletList", "orderedList", "rule"):
        return joined.strip() + "\n"
    if node_type == "hardBreak":
        return "\n"

    return joined


def jira_description_to_text(description) -> str:
    """Convert Jira description (ADF dict or plain string) to readable text."""
    if not description:
        return ""
    if isinstance(description, str):
        return description.strip()
    if isinstance(description, dict):
        return adf_to_text(description).strip()
    return ""


# ══════════════════════════════════════════════════════════════════════════════
# Jira helpers
# ══════════════════════════════════════════════════════════════════════════════

def jira_auth() -> tuple[str, str]:
    return (JIRA_EMAIL, JIRA_API_TOKEN)


def fetch_jira_items(jql: str, max_results: int = 50) -> list[dict]:
    """Run a JQL query and return a list of Jira issue dicts."""
    url = f"{JIRA_BASE_URL}/rest/api/3/search/jql"
    params = {
        "jql": jql,
        "maxResults": max_results,
        "fields": "summary,description,attachment,comment,status,priority,labels,issuetype,created,updated,reporter,assignee",
    }
    r = requests.get(url, auth=jira_auth(), params=params, timeout=30)
    r.raise_for_status()
    return r.json().get("issues", [])


def get_video_attachments(issue: dict) -> list[dict]:
    """Return a list of video attachment dicts from a Jira issue."""
    attachments = issue.get("fields", {}).get("attachment", []) or []
    videos = []
    for att in attachments:
        mime = att.get("mimeType", "").lower()
        fname = att.get("filename", "")
        ext = os.path.splitext(fname)[1].lower()
        if mime in VIDEO_MIME_TYPES or ext in VIDEO_EXTENSIONS:
            videos.append(att)
    return videos


def get_other_attachments(issue: dict) -> list[dict]:
    """Return non-video attachments as lightweight metadata dicts (no content URL)."""
    attachments = issue.get("fields", {}).get("attachment", []) or []
    result = []
    for att in attachments:
        mime = att.get("mimeType", "").lower()
        fname = att.get("filename", "")
        ext = os.path.splitext(fname)[1].lower()
        if mime not in VIDEO_MIME_TYPES and ext not in VIDEO_EXTENSIONS:
            result.append({
                "filename": fname,
                "size":     att.get("size", 0),
                "mimeType": att.get("mimeType", ""),
            })
    return result


def download_attachment(attachment: dict) -> str:
    """Download a Jira attachment to a temp file. Returns the temp file path."""
    content_url = attachment.get("content", "")
    filename = attachment.get("filename", "video.mp4")
    ext = os.path.splitext(filename)[1].lower() or ".mp4"

    r = requests.get(content_url, auth=jira_auth(), stream=True, timeout=300)
    r.raise_for_status()

    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        for chunk in r.iter_content(chunk_size=8192):
            tmp.write(chunk)
        return tmp.name


def post_jira_comment(issue_key: str, comment_text: str):
    """Post an ADF-formatted comment to a Jira issue."""
    url = f"{JIRA_BASE_URL}/rest/api/3/issue/{issue_key}/comment"
    body = {
        "body": {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": comment_text}]
                }
            ]
        }
    }
    r = requests.post(url, auth=jira_auth(), json=body,
                      headers={"Content-Type": "application/json"}, timeout=15)
    r.raise_for_status()


# ══════════════════════════════════════════════════════════════════════════════
# Video processing  (mirrors cora-bug-creator-shared)
# ══════════════════════════════════════════════════════════════════════════════

def extract_audio_mp3(video_path: str) -> str:
    """Extract compressed MP3 audio. Returns temp file path (caller must delete)."""
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
        mp3_path = tmp.name
    subprocess.run(
        ["ffmpeg", "-i", video_path, "-vn", "-ar", "16000", "-ac", "1",
         "-b:a", "32k", "-f", "mp3", "-y", mp3_path],
        capture_output=True, timeout=120
    )
    return mp3_path


def transcribe_video(file_path: str) -> tuple[str, list[float]]:
    """Transcribe via OpenAI Whisper API. Returns (transcript, screenshot_timestamps)."""
    mp3_path = extract_audio_mp3(file_path)
    try:
        file_size_mb = os.path.getsize(mp3_path) / (1024 * 1024)
        if file_size_mb > 24:
            raise ValueError(
                f"Audio is {file_size_mb:.1f} MB after compression — exceeds Whisper's 25 MB limit. "
                "Please use a shorter recording (under ~60 minutes)."
            )

        print(f"[Whisper] Sending {file_size_mb:.1f} MB for transcription…")
        client = openai.OpenAI(api_key=OPENAI_API_KEY)
        with open(mp3_path, "rb") as f:
            response = client.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                response_format="verbose_json",
                timestamp_granularities=["word"],
            )

        transcript = response.text.strip()

        screenshot_times: list[float] = []
        if "screenshot here" in transcript.lower():
            words = getattr(response, "words", []) or []
            for i, w in enumerate(words):
                word = w.word.lower().strip().strip(".,!?;:")
                if word == "screenshot" and i + 1 < len(words):
                    nw = words[i + 1].word.lower().strip().strip(".,!?;:")
                    if nw == "here":
                        screenshot_times.append(round(w.start, 1))

        return transcript, screenshot_times

    finally:
        try:
            os.unlink(mp3_path)
        except Exception:
            pass


def get_video_duration(video_path: str) -> float:
    probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", video_path],
        capture_output=True, text=True, timeout=15
    )
    try:
        return float(probe.stdout.strip())
    except (ValueError, AttributeError):
        return 30.0


def extract_frame_at(video_path: str, timestamp: float) -> dict | None:
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        subprocess.run(
            ["ffmpeg", "-ss", str(timestamp), "-i", video_path,
             "-vframes", "1", "-q:v", "3", "-y", tmp_path],
            capture_output=True, timeout=20
        )
        if os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 0:
            with open(tmp_path, "rb") as f:
                return {
                    "base64":        base64.b64encode(f.read()).decode(),
                    "timestamp_sec": round(timestamp, 1),
                    "media_type":    "image/jpeg",
                }
    except Exception:
        pass
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
    return None


def extract_screenshots(video_path: str, timestamps: list[float] = None,
                        num_frames: int = 3) -> list[dict]:
    if timestamps:
        return [f for ts in timestamps if (f := extract_frame_at(video_path, ts))]

    duration = get_video_duration(video_path)
    margin = max(duration * 0.10, 2.0)
    usable = duration - 2 * margin
    if usable < 1:
        ts_list = [duration / 2]
    else:
        step = usable / (num_frames + 1)
        ts_list = [margin + step * (i + 1) for i in range(num_frames)]

    return [f for ts in ts_list if (f := extract_frame_at(video_path, ts))]


# ══════════════════════════════════════════════════════════════════════════════
# Claude AI — sufficiency assessment + bug extraction
# ══════════════════════════════════════════════════════════════════════════════

SYSTEM_PROMPT = """You are a senior QA engineer for a Cora PPM implementation consulting team.

You receive a Jira issue (key, title, description) and optionally a video transcript
and screenshots from a screen recording attached to the ticket.

Your job:
1. Assess whether there is SUFFICIENT information to create a complete ADO bug report.
2. If sufficient, extract all bug fields.
3. If not sufficient, list exactly what information is missing.

A bug report is SUFFICIENT if you can confidently determine ALL of the following:
- A clear, specific bug title
- Step-by-step reproduction steps (at least 2 steps)
- What the expected behavior should be
- What the actual (buggy) behavior is
- A meaningful description of the problem

Pay special attention to screenshots:
- Read the browser address bar for the exact URL path
- Read breadcrumbs/page titles to identify the Cora PPM module
- Note visible error messages, UI state, or relevant on-screen content

Return a JSON object with EXACTLY this structure:
{
  "is_sufficient": true,
  "confidence": 85,
  "missing_fields": [],
  "missing_explanation": "",
  "bug": {
    "title": "Concise bug title under 120 characters",
    "description": "Clear 2-3 sentence description of the bug and its business impact",
    "repro_steps": "Numbered step-by-step instructions, one per line",
    "expected_result": "What should happen according to design",
    "actual_result": "What actually happens — the bug behavior",
    "acceptance_criteria": "Clear, testable criteria for this bug to be fixed (1-3 bullet points starting with •)",
    "severity": "3 - Medium",
    "priority": "3",
    "system_info": "Browser name and version visible in screenshots, environment (prod/staging/dev)",
    "url_path": "Exact URL path if domain contains corasystems.com, else empty string",
    "test_site": "Base URL if domain contains corasystems.com, else empty string",
    "product_area": "Cora PPM module (e.g. Projects, Timesheets, Resource Management)",
    "found_in_build": "Build or version if visible, else empty string",
    "tags": "Cora PPM",
    "area_path": "Best guess ADO area path (e.g. PPM\\Projects\\Schedule)",
    "iteration_path": ""
  }
}

If is_sufficient is false, set "bug" to null and populate missing_fields and missing_explanation.

Rules:
- severity MUST be exactly one of: 1 - Critical, 2 - High, 3 - Medium, 4 - Low
- priority MUST be exactly one of: 1, 2, 3, 4 (as a string)
- repro_steps MUST be a numbered list with each step on its own line
- test_site and url_path MUST only come from URLs containing corasystems.com
- Return valid JSON only — no markdown, no code fences, no extra text"""


def assess_and_extract_bug(jira_key: str, jira_title: str,
                            description_text: str,
                            transcript: str,
                            screenshots: list[dict]) -> dict:
    """
    Ask Claude to assess sufficiency and extract bug fields.
    Returns the parsed JSON dict from the model.
    """
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    content = []

    # Screenshots first (if any)
    for i, frame in enumerate(screenshots):
        content.append({
            "type": "text",
            "text": f"Screenshot {i+1} (at {frame['timestamp_sec']}s into the recording):"
        })
        content.append({
            "type": "image",
            "source": {
                "type":       "base64",
                "media_type": frame["media_type"],
                "data":       frame["base64"],
            }
        })

    # Build the text block
    parts = [f"Jira Issue: {jira_key}",
             f"Title: {jira_title}",
             ""]
    if description_text:
        parts.append("Description / Steps from Jira:")
        parts.append(description_text)
        parts.append("")
    if transcript:
        parts.append("Video Transcript:")
        parts.append(transcript)
    else:
        parts.append("(No video transcript available for this item.)")

    content.append({"type": "text", "text": "\n".join(parts)})

    message = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=1800,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}]
    )
    raw = message.content[0].text.strip()
    raw = re.sub(r'^```(?:json)?\s*', '', raw)
    raw = re.sub(r'\s*```$', '', raw.strip())
    return json.loads(raw)


# ══════════════════════════════════════════════════════════════════════════════
# ADO helpers
# ══════════════════════════════════════════════════════════════════════════════

def ado_auth_headers(pat: str = None) -> dict:
    token = base64.b64encode(f":{pat or ADO_PAT}".encode()).decode()
    return {"Authorization": f"Basic {token}", "Content-Type": "application/json"}


def ado_patch_headers(pat: str = None) -> dict:
    token = base64.b64encode(f":{pat or ADO_PAT}".encode()).decode()
    return {"Authorization": f"Basic {token}",
            "Content-Type": "application/json-patch+json"}


def flatten_nodes(node, prefix="") -> list[str]:
    name = node.get("name", "")
    current = f"{prefix}\\{name}" if prefix else name
    paths = [current]
    for child in node.get("children", []):
        paths.extend(flatten_nodes(child, current))
    return paths


def combined_text_to_html(text: str) -> str:
    """Convert combined Description/Steps/Expected/Actual textarea to HTML for ADO."""
    SECTION_HEADERS = {"Description", "Steps to Reproduce", "Expected Result", "Actual Result"}
    lines = text.split("\n")
    parts: list[str] = []
    ol_open = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if ol_open:
                parts.append("</ol>")
                ol_open = False
            continue
        header_match = re.match(r'^([A-Za-z ]+):$', stripped)
        if header_match and header_match.group(1) in SECTION_HEADERS:
            if ol_open:
                parts.append("</ol>")
                ol_open = False
            parts.append(f"<p><b>{stripped}</b></p>")
            continue
        if re.match(r'^\d+\.', stripped):
            if not ol_open:
                parts.append("<ol>")
                ol_open = True
            item_text = re.sub(r'^\d+\.\s*', '', stripped)
            parts.append(f"<li>{item_text}</li>")
            continue
        if ol_open:
            parts.append("</ol>")
            ol_open = False
        parts.append(f"<p>{stripped}</p>")
    if ol_open:
        parts.append("</ol>")
    return "".join(parts)


def submit_bug_to_ado(data: dict, project: str = None, pat: str = None) -> dict:
    """Create an ADO Bug work item — exact field mapping as original app."""
    proj  = project or ADO_PROJECT
    severity_map = {
        "1 - Critical": "1 - Critical", "2 - High": "2 - High",
        "3 - Medium":   "3 - Medium",   "4 - Low":  "4 - Low",
    }
    severity    = severity_map.get(data.get("severity", ""), "3 - Medium")
    repro_html  = combined_text_to_html(data.get("repro_steps", ""))
    ac_text     = data.get("acceptance_criteria", "").strip()
    ac_lines    = [l.strip().lstrip("•-").strip() for l in ac_text.split("\n") if l.strip()]
    accept_html = "<ul>" + "".join(f"<li>{l}</li>" for l in ac_lines) + "</ul>" if ac_lines else ""

    patch = [
        {"op": "add", "path": "/fields/System.Title",                   "value": data.get("title", "")},
        {"op": "add", "path": "/fields/System.Description",             "value": ""},
        {"op": "add", "path": "/fields/Microsoft.VSTS.TCM.ReproSteps",  "value": repro_html},
        {"op": "add", "path": "/fields/Microsoft.VSTS.Common.Severity", "value": severity},
        {"op": "add", "path": "/fields/System.Tags",                    "value": data.get("tags", "Cora PPM")},
    ]
    if accept_html:
        patch.append({"op": "add", "path": "/fields/Microsoft.VSTS.Common.AcceptanceCriteria", "value": accept_html})
    if data.get("found_in_build"):
        patch.append({"op": "add", "path": "/fields/Microsoft.VSTS.Build.FoundIn",  "value": data["found_in_build"]})
    if data.get("area_path"):
        patch.append({"op": "add", "path": "/fields/System.AreaPath",               "value": data["area_path"]})
    if data.get("iteration_path"):
        patch.append({"op": "add", "path": "/fields/System.IterationPath",          "value": data["iteration_path"]})

    # Custom fields — same paths as original app
    custom_fields = []
    if data.get("test_site"):
        custom_fields.append({"op": "add", "path": "/fields/Custom.TestSite",        "value": data["test_site"]})
    if data.get("product_area"):
        custom_fields.append({"op": "add", "path": "/fields/Custom.ProductArea",     "value": data["product_area"]})
    if data.get("jira_id"):
        custom_fields.append({"op": "add", "path": "/fields/Custom.UATLogReference", "value": data["jira_id"]})
    if data.get("primary_customer"):
        custom_fields.append({"op": "add", "path": "/fields/Custom.Customer",        "value": data["primary_customer"]})
    if data.get("customer_impact"):
        custom_fields.append({"op": "add", "path": "/fields/Custom.CustomerImpact",  "value": data["customer_impact"]})

    def _post(patch_body):
        r = requests.post(
            f"{ADO_ORG_URL}/{proj}/_apis/wit/workitems/$Bug?api-version=7.0",
            headers=ado_patch_headers(pat=pat),
            json=patch_body, timeout=15
        )
        r.raise_for_status()
        return r.json()

    # First attempt: all fields including custom
    try:
        return _post(patch + custom_fields)
    except requests.HTTPError as e:
        err_msg = ""
        try:    err_msg = e.response.json().get("message", str(e))
        except: err_msg = str(e)
        # Retry without custom fields if a field isn't found on this ADO project
        if "TF51535" in err_msg or "Cannot find field" in err_msg:
            print(f"[ADO] Custom field error — retrying without custom fields: {err_msg}")
            return _post(patch)
        raise


# ══════════════════════════════════════════════════════════════════════════════
# Background pipeline job
# ══════════════════════════════════════════════════════════════════════════════

_jobs: dict = {}


def _new_job(jql: str) -> str:
    job_id = str(uuid.uuid4())
    _jobs[job_id] = {
        "status":        "running",
        "jql":           jql,
        "total":         0,
        "processed":     0,
        "cached_count":  0,   # items skipped because already in DB
        "current_item":  None,
        "queue":         [],  # newly processed items ready for review
        "skipped":       [],  # newly processed items lacking sufficient info
        "errors":        [],
    }
    return job_id


def _safe_delete(path: str):
    try:
        if path and os.path.exists(path):
            os.unlink(path)
    except Exception:
        pass


def process_single_item(job_id: str, issue: dict):
    """
    Process one Jira issue:
      - Check description / transcript quality
      - Transcribe video if present
      - Ask Claude for sufficiency assessment
      - Add to queue or post Jira comment
    """
    job = _jobs[job_id]
    key   = issue["key"]
    fields = issue.get("fields", {})
    title  = fields.get("summary", key)

    job["current_item"] = key
    print(f"[Pipeline] Processing {key}: {title}")

    description_text  = jira_description_to_text(fields.get("description"))
    video_attachments = get_video_attachments(issue)
    other_attachments = get_other_attachments(issue)
    extra_video_count = max(0, len(video_attachments) - 1)

    transcript  = ""
    screenshots = []
    video_path  = None

    try:
        # ── Video: download → transcribe → screenshots ─────────────
        if video_attachments:
            print(f"[Pipeline] {key} has {len(video_attachments)} video attachment(s)")
            att = video_attachments[0]   # process first video
            try:
                video_path = download_attachment(att)
                print(f"[Pipeline] {key} video downloaded → {video_path}")
                transcript, ts = transcribe_video(video_path)
                print(f"[Pipeline] {key} transcription done ({len(transcript)} chars)")
                screenshots = extract_screenshots(video_path, timestamps=ts or None)
                print(f"[Pipeline] {key} extracted {len(screenshots)} screenshots")
            except Exception as ve:
                print(f"[Pipeline] {key} video processing error: {ve}")
                job["errors"].append({"key": key, "error": f"Video processing: {ve}"})
                # Continue — we'll still assess with just the description

        # ── Claude assessment ──────────────────────────────────────
        result = assess_and_extract_bug(
            jira_key=key,
            jira_title=title,
            description_text=description_text,
            transcript=transcript,
            screenshots=screenshots,
        )

        has_video    = bool(video_attachments)
        has_desc     = bool(description_text and len(description_text.strip()) > 20)
        jira_url     = f"{JIRA_BASE_URL}/browse/{key}" if JIRA_BASE_URL else None

        if result.get("is_sufficient"):
            queue_item = {
                "jira_key":       key,
                "jira_title":     title,
                "jira_url":       jira_url,
                "has_video":      has_video,
                "has_desc":       has_desc,
                "confidence":     result.get("confidence", 0),
                "bug":            result["bug"],
                # Video attachment metadata (for the "Files to be Attached" section)
                "video_filename":    video_attachments[0].get("filename") if video_attachments else None,
                "video_size":        video_attachments[0].get("size", 0)  if video_attachments else 0,
                "extra_video_count": extra_video_count,
                "other_attachments": other_attachments,
                # Screenshot thumbnails captured from the video
                "screenshots": [
                    {
                        "data_uri":      f"data:{s['media_type']};base64,{s['base64']}",
                        "timestamp_sec": s.get("timestamp_sec", 0),
                    }
                    for s in screenshots
                ],
                # Raw Jira fields for the left-panel comparison view
                "jira_fields": {
                    "summary":     title,
                    "description": description_text,
                    "status":      (fields.get("status") or {}).get("name", ""),
                    "priority":    (fields.get("priority") or {}).get("name", ""),
                    "issuetype":   (fields.get("issuetype") or {}).get("name", ""),
                    "reporter":    ((fields.get("reporter") or {}).get("displayName", "")),
                    "assignee":    ((fields.get("assignee") or {}).get("displayName", "Unassigned")),
                    "labels":      fields.get("labels") or [],
                    "has_video":   has_video,
                    "transcript":  transcript,
                    "created":     fields.get("created", ""),
                    "updated":     fields.get("updated", ""),
                },
            }
            job["queue"].append(queue_item)
            db_save_queue_item(queue_item)   # persist to SQLite
            print(f"[Pipeline] {key} → QUEUED (confidence {result.get('confidence')}%)")
        else:
            missing      = result.get("missing_fields", [])
            explanation  = result.get("missing_explanation", "")

            # Post comment to Jira
            comment_lines = [
                "🤖 Bug Pipeline — Missing Information",
                "",
                "This ticket was processed by the ADO Bug Pipeline but could not be automatically "
                "queued because the following information is missing:",
                "",
            ]
            for m in missing:
                comment_lines.append(f"  • {m}")
            if explanation:
                comment_lines.append("")
                comment_lines.append(explanation)
            comment_lines += [
                "",
                "Please update this ticket with the missing information and re-run the pipeline.",
            ]
            comment_text = "\n".join(comment_lines)

            try:
                post_jira_comment(key, comment_text)
                commented = True
            except Exception as ce:
                print(f"[Pipeline] {key} could not post Jira comment: {ce}")
                commented = False

            skipped_item = {
                "jira_key":    key,
                "jira_title":  title,
                "jira_url":    jira_url,
                "has_video":   has_video,
                "has_desc":    has_desc,
                "confidence":  result.get("confidence", 0),
                "missing":     missing,
                "explanation": explanation,
                "commented":   commented,
            }
            job["skipped"].append(skipped_item)
            db_save_skipped_item(skipped_item)   # persist to SQLite
            print(f"[Pipeline] {key} → SKIPPED (missing: {missing})")

    finally:
        _safe_delete(video_path)

    job["processed"] += 1


def run_pipeline(job_id: str, jql: str):
    """Background thread entry point."""
    job = _jobs[job_id]
    try:
        issues = fetch_jira_items(jql)
        print(f"[Pipeline] Job {job_id}: {len(issues)} issues for JQL: {jql}")

        # Split into new vs already-cached
        new_issues    = [i for i in issues if not db_is_processed(i.get("key", ""))]
        cached_count  = len(issues) - len(new_issues)
        job["total"]        = len(new_issues)
        job["cached_count"] = cached_count
        print(f"[Pipeline] Job {job_id}: {len(new_issues)} new, {cached_count} already cached")

        if not new_issues:
            job["status"] = "done"
            return

        for issue in new_issues:
            if job["status"] == "cancelled":
                break
            try:
                process_single_item(job_id, issue)
            except Exception as e:
                key = issue.get("key", "?")
                print(f"[Pipeline] Unhandled error for {key}: {e}\n{traceback.format_exc()}")
                job["errors"].append({"key": key, "error": str(e)})
                job["processed"] += 1

        job["status"] = "done"
        job["current_item"] = None
        print(f"[Pipeline] Job {job_id} complete. New queue: {len(job['queue'])}, Skipped: {len(job['skipped'])}, Cached: {cached_count}")

    except Exception as e:
        job["status"] = "error"
        job["error_message"] = str(e)
        print(f"[Pipeline] Job {job_id} failed: {e}\n{traceback.format_exc()}")


# ══════════════════════════════════════════════════════════════════════════════
# Flask routes
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/start-pipeline", methods=["POST"])
def start_pipeline():
    """Start a background pipeline job using the static JIRA_JQL filter."""
    missing_cfg = []
    if not ANTHROPIC_API_KEY: missing_cfg.append("ANTHROPIC_API_KEY")
    if not OPENAI_API_KEY:    missing_cfg.append("OPENAI_API_KEY")
    if not ADO_PAT:           missing_cfg.append("ADO_PAT")
    if not JIRA_BASE_URL:     missing_cfg.append("JIRA_BASE_URL")
    if not JIRA_EMAIL:        missing_cfg.append("JIRA_EMAIL")
    if not JIRA_API_TOKEN:    missing_cfg.append("JIRA_API_TOKEN")
    if missing_cfg:
        return jsonify({"error": f"Missing config: {', '.join(missing_cfg)}"}), 500

    job_id = _new_job(JIRA_JQL)
    t = threading.Thread(target=run_pipeline, args=(job_id, JIRA_JQL), daemon=True)
    t.start()
    return jsonify({"job_id": job_id})


@app.route("/api/job-status/<job_id>")
def job_status(job_id: str):
    """Poll for pipeline job progress."""
    job = _jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    return jsonify({
        "status":       job["status"],
        "jql":          job["jql"],
        "total":        job["total"],
        "processed":    job["processed"],
        "current_item": job["current_item"],
        "queue":        job["queue"],
        "skipped":      job["skipped"],
        "errors":       job["errors"],
        "error_message": job.get("error_message", ""),
    })


@app.route("/api/submit-bug", methods=["POST"])
def submit_bug():
    """Submit a reviewed bug to ADO — same field mapping as cora-bug-creator."""
    data = request.get_json(silent=True) or {}
    if not data.get("title"):
        return jsonify({"error": "title is required"}), 400
    try:
        wi     = submit_bug_to_ado(data, project=data.get("project") or ADO_PROJECT)
        wi_id  = wi.get("id")
        wi_url = wi.get("_links", {}).get("html", {}).get("href", "")
        # Mark as submitted in the persistent store
        jira_key = data.get("jira_key") or data.get("jira_id") or ""
        if jira_key:
            db_mark_submitted(jira_key, wi_id, wi_url)
        return jsonify({"success": True, "id": wi_id, "url": wi_url})
    except requests.HTTPError as e:
        try:    msg = e.response.json().get("message", str(e))
        except: msg = str(e)
        return jsonify({"error": msg}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/queue")
def get_queue():
    """Return the full persistent queue + skipped list from SQLite."""
    return jsonify({
        "queue":   db_get_all_queue_items(),
        "skipped": db_get_all_skipped_items(),
    })


@app.route("/api/queue/<jira_key>/screenshots")
def get_item_screenshots(jira_key: str):
    """Return only the screenshots array for one item (lazy-loaded by the modal)."""
    return jsonify({"screenshots": db_get_screenshots(jira_key)})


@app.route("/api/queue/<jira_key>/save-draft", methods=["POST"])
def save_draft(jira_key: str):
    """Merge user-edited form values back into bug_json without submitting to ADO."""
    data = request.get_json(force=True) or {}
    bug_update = data.get("bug") or {}
    with _db() as conn:
        row = conn.execute(
            "SELECT bug_json FROM queue_items WHERE jira_key=?", (jira_key,)
        ).fetchone()
        if not row:
            return jsonify({"error": "Item not found"}), 404
        existing = json.loads(row["bug_json"] or "{}")
        # Merge draft fields — only overwrite keys that are present in the update
        existing.update({k: v for k, v in bug_update.items() if v is not None})
        conn.execute(
            "UPDATE queue_items SET bug_json=? WHERE jira_key=?",
            (json.dumps(existing), jira_key)
        )
        conn.commit()
    return jsonify({"success": True})


@app.route("/api/queue/<jira_key>", methods=["DELETE"])
def delete_queue_item(jira_key: str):
    """Remove an item from the persistent store (allows it to be reprocessed)."""
    db_delete_item(jira_key)
    return jsonify({"success": True})


@app.route("/api/queue/reprocess/<jira_key>", methods=["POST"])
def reprocess_item(jira_key: str):
    """Delete item from DB and re-run the pipeline for just this one ticket."""
    db_delete_item(jira_key)
    job_id = _new_job(f"reprocess:{jira_key}")
    t = threading.Thread(target=run_reprocess, args=(job_id, jira_key), daemon=True)
    t.start()
    return jsonify({"job_id": job_id})


# ── Jira webhook ──────────────────────────────────────────────────────────────
# Track the last webhook receipt for the UI status indicator
_webhook_last_received: dict = {"ts": None, "key": None}

@app.route("/api/webhook/jira", methods=["POST"])
def jira_webhook():
    """
    Jira webhook receiver for issue_created events.

    Set up in Jira: Project Settings → Webhooks → URL: <your host>/api/webhook/jira
    Select event: Issue → created
    Optionally add a JQL filter on the webhook to match your pipeline scope.

    When a new issue arrives that hasn't been processed yet, it is automatically
    run through the same pipeline as the manual Run Pipeline button.
    """
    try:
        data = request.get_json(force=True) or {}
    except Exception:
        return jsonify({"error": "invalid JSON"}), 400

    webhook_event = data.get("webhookEvent", "")

    # Only handle new-issue creation — ignore updates, deletions, etc.
    if webhook_event != "jira:issue_created":
        return jsonify({"status": "ignored", "event": webhook_event}), 200

    issue = data.get("issue")
    if not issue:
        return jsonify({"error": "no issue in payload"}), 400

    key = issue.get("key", "")
    if not key:
        return jsonify({"error": "no issue key in payload"}), 400

    # Record for the UI status badge
    _webhook_last_received["ts"]  = datetime.utcnow().isoformat()
    _webhook_last_received["key"] = key

    # Skip if already in the pipeline (e.g. duplicate delivery)
    if db_is_processed(key):
        print(f"[Webhook] {key} already processed — skipping")
        return jsonify({"status": "already_processed", "key": key}), 200

    # Fetch the full issue so we have all fields (webhook payload is often minimal)
    try:
        full_issue = fetch_jira_issue(key)
        if not full_issue:
            return jsonify({"error": f"Issue {key} not found in Jira"}), 404
    except Exception as e:
        print(f"[Webhook] Could not fetch {key}: {e}")
        return jsonify({"error": f"Could not fetch issue: {e}"}), 500

    # Process in a background thread so we return quickly to Jira (< 5 s required)
    job_id = _new_job(f"webhook:{key}")
    t = threading.Thread(target=run_webhook_item, args=(job_id, full_issue), daemon=True)
    t.start()
    print(f"[Webhook] {key} queued for processing (job {job_id})")
    return jsonify({"status": "queued", "key": key, "job_id": job_id}), 202


def run_webhook_item(job_id: str, issue: dict):
    """Background entry point for a single webhook-triggered issue."""
    job  = _jobs[job_id]
    key  = issue.get("key", "?")
    job["total"] = 1
    try:
        process_single_item(job_id, issue)
        job["status"]       = "done"
        job["current_item"] = None
        print(f"[Webhook] {key} processing complete")
    except Exception as e:
        job["status"]        = "error"
        job["error_message"] = str(e)
        print(f"[Webhook] {key} failed: {e}\n{traceback.format_exc()}")


@app.route("/api/webhook/status")
def webhook_status():
    """Return info about the webhook endpoint for the UI."""
    return jsonify({
        "endpoint":      "/api/webhook/jira",
        "last_received": _webhook_last_received["ts"],
        "last_key":      _webhook_last_received["key"],
    })


def fetch_jira_issue(key: str) -> dict | None:
    """Fetch a single Jira issue by key."""
    url = f"{JIRA_BASE_URL}/rest/api/3/issue/{key}"
    r = requests.get(
        url, auth=jira_auth(),
        params={"fields": "summary,description,attachment,comment,status,priority,labels,issuetype,created,updated,reporter,assignee"},
        timeout=30
    )
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def run_reprocess(job_id: str, jira_key: str):
    """Background entry point for reprocessing a single Jira ticket."""
    job = _jobs[job_id]
    try:
        issue = fetch_jira_issue(jira_key)
        if not issue:
            job["status"] = "error"
            job["error_message"] = f"Issue {jira_key} not found in Jira"
            return
        job["total"] = 1
        process_single_item(job_id, issue)
        job["status"] = "done"
        job["current_item"] = None
        print(f"[Reprocess] {jira_key} complete")
    except Exception as e:
        job["status"] = "error"
        job["error_message"] = str(e)
        print(f"[Reprocess] {jira_key} failed: {e}\n{traceback.format_exc()}")


@app.route("/api/ado/area-paths")
def ado_area_paths():
    project = request.args.get("project", ADO_PROJECT).strip()
    try:
        r = requests.get(
            f"{ADO_ORG_URL}/{project}/_apis/wit/classificationnodes/areas"
            f"?$depth=10&api-version=7.0",
            headers=ado_auth_headers(), timeout=10
        )
        r.raise_for_status()
        return jsonify({"area_paths": flatten_nodes(r.json())})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/ado/iterations")
def ado_iterations():
    project = request.args.get("project", ADO_PROJECT).strip()
    try:
        r = requests.get(
            f"{ADO_ORG_URL}/{project}/_apis/wit/classificationnodes/iterations"
            f"?$depth=5&api-version=7.0",
            headers=ado_auth_headers(), timeout=10
        )
        r.raise_for_status()
        return jsonify({"iterations": flatten_nodes(r.json())})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/ado/field-picklist")
def ado_field_picklist():
    project   = request.args.get("project", ADO_PROJECT).strip()
    field_ref = request.args.get("field", "").strip()
    if not field_ref:
        return jsonify({"error": "field parameter required"}), 400
    try:
        proj_r = requests.get(
            f"{ADO_ORG_URL}/_apis/projects/{project}?api-version=7.0",
            headers=ado_auth_headers(), timeout=10
        )
        proj_r.raise_for_status()
        project_id = proj_r.json().get("id", project)
        field_url  = (f"{ADO_ORG_URL}/{project_id}/_apis/wit/workitemtypes/bug/fields/{field_ref}"
                      f"?$expand=allowedValues&api-version=7.1")
        field_r = requests.get(field_url, headers=ado_auth_headers(), timeout=10)
        field_r.raise_for_status()
        values = field_r.json().get("allowedValues", [])
        return jsonify({"values": values, "field": field_ref})
    except Exception as e:
        return jsonify({"error": str(e), "values": []}), 200


@app.route("/fetch-version")
def fetch_version():
    """Auto-detect the Cora PPM build number from a live site URL."""
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    url = request.args.get("url", "").strip()
    if not url:
        return jsonify({"error": "url parameter required"}), 400
    try:
        r = requests.get(url, timeout=10, verify=False,
                         headers={"User-Agent": "Mozilla/5.0 (Cora PPM Bug Pipeline)"})
        match = re.search(
            r'<meta[^>]+Cora-PPM-Version=["\'](\d+\.\d+\.\d+\.\d+_\d+)["\']',
            r.text, re.IGNORECASE
        )
        if match:
            return jsonify({"version": match.group(1)})
        return jsonify({"version": None, "note": "Cora-PPM-Version meta tag not found"})
    except Exception as e:
        return jsonify({"version": None, "error": str(e)})


@app.route("/api/ado/projects")
def ado_projects():
    try:
        r = requests.get(
            f"{ADO_ORG_URL}/_apis/projects?api-version=7.0&$top=100",
            headers=ado_auth_headers(), timeout=10
        )
        r.raise_for_status()
        projects = sorted([p["name"] for p in r.json().get("value", [])])
        return jsonify({"projects": projects})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/health")
def health():
    return jsonify({
        "status": "ok",
        "config": {
            "anthropic":   bool(ANTHROPIC_API_KEY),
            "openai":      bool(OPENAI_API_KEY),
            "ado":         bool(ADO_PAT),
            "jira":        bool(JIRA_BASE_URL and JIRA_EMAIL and JIRA_API_TOKEN),
        }
    })


@app.route("/api/config")
def config():
    """Return current app configuration for the UI."""
    # Build the public-facing webhook URL using the current request host
    host = request.host_url.rstrip("/")
    return jsonify({
        "jql":          JIRA_JQL,
        "jira_url":     JIRA_BASE_URL,
        "ado_project":  ADO_PROJECT,
        "webhook_url":  f"{host}/api/webhook/jira",
    })


@app.route("/api/test-jira")
def test_jira():
    """Test the Jira connection and return raw results for the configured JQL."""
    try:
        url = f"{JIRA_BASE_URL}/rest/api/3/search/jql"
        params = {"jql": JIRA_JQL, "maxResults": 5,
                  "fields": "summary,attachment,description,issuetype,status"}
        r = requests.get(url, auth=jira_auth(), params=params, timeout=15)
        data = r.json()
        issues = data.get("issues", [])
        return jsonify({
            "ok":          r.ok,
            "status_code": r.status_code,
            "jql":         JIRA_JQL,
            "total":       data.get("total", 0),
            "returned":    len(issues),
            "issues": [
                {
                    "key":         i["key"],
                    "summary":     i["fields"].get("summary"),
                    "issuetype":   i["fields"].get("issuetype", {}).get("name"),
                    "status":      i["fields"].get("status", {}).get("name"),
                    "attachments": len(i["fields"].get("attachment") or []),
                }
                for i in issues
            ],
            "error": data.get("errorMessages") or data.get("errors"),
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(debug=False, host="0.0.0.0", port=port)
