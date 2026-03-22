# Jira → ADO Bug Pipeline

Fetches Jira items from a JQL query, checks each item for video attachments and description quality, transcribes videos, asks Claude to assess whether there is sufficient info to fill out an ADO bug form, and queues passing items for user review and submission.

## Quick Start

1. Copy `.env.example` to `.env` and fill in your keys (see below).
2. Double-click **`start.bat`** — it installs dependencies and starts the server.
3. Open **http://localhost:5000** in your browser.

## Prerequisites

- **Python 3.11+**
- **ffmpeg** on your system PATH (required for video transcription)
  - Windows: download from https://ffmpeg.org/download.html and add the `bin/` folder to PATH
- API keys for Anthropic, OpenAI, Azure DevOps, and Jira (see `.env.example`)

## Configuration (`.env`)

| Variable | Description |
|---|---|
| `ANTHROPIC_API_KEY` | Claude API key — used for bug extraction and sufficiency assessment |
| `OPENAI_API_KEY` | OpenAI key — used for Whisper audio transcription |
| `ADO_ORG_URL` | Azure DevOps org URL, e.g. `https://dev.azure.com/CoraSystems` |
| `ADO_PAT` | ADO Personal Access Token (Work Items: Read & Write) |
| `ADO_PROJECT` | ADO project name, e.g. `PPM` |
| `JIRA_BASE_URL` | Jira Cloud base URL, e.g. `https://acme.atlassian.net` |
| `JIRA_EMAIL` | Your Jira account email |
| `JIRA_API_TOKEN` | Jira API token — generate at https://id.atlassian.com/manage-profile/security/api-tokens |

## How It Works

1. **Enter a JQL query** and click **Run Pipeline**.
2. The app fetches matching Jira issues and processes each one in the background:
   - Downloads any video attachment and transcribes it via OpenAI Whisper.
   - Sends the Jira description + transcript + screenshots to Claude for a **sufficiency assessment**.
3. **Sufficient items** appear in the "Ready for Review" tab — click **Review & Submit** to open a pre-filled, editable ADO bug form.
4. **Insufficient items** appear in the "Needs More Info" tab — a comment is automatically posted to the Jira ticket listing exactly what's missing.

## Pipeline Sufficiency Check

Claude considers a bug report sufficient if it can confidently determine:
- A specific bug title
- Step-by-step reproduction steps
- Expected vs. actual behavior
- A meaningful description

Items that pass are shown with a **confidence score**. You can edit any field in the review form before submitting.
