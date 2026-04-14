# OpenClaw Job Discovery Agent

An automated job discovery pipeline built as an OpenClaw skill, deployed on a 
self-hosted VPS with Tailscale VPN.

Queries 10 ATS platforms (Greenhouse, Lever, Workday, SmartRecruiters, etc.) 
directly via Google Search API with `site:` operators — surfacing DS/ML/AI 
entry-level roles before they aggregate to LinkedIn.

## How it works
- Serper.dev API queries each ATS domain with targeted search strings
- SQLite deduplication ensures each job appears exactly once
- TF-IDF cosine similarity scores jobs against your resume
- Ranked digest delivered to Telegram daily via cron

## Setup
1. Clone the repo
2. Create a virtual environment: `python3 -m venv venv && source venv/bin/activate`
3. Install deps: `pip install -r requirements.txt`
4. Copy `.env.example` to `.env` and fill in your API keys
5. Add your resume as `resume.txt`
6. Run: `python3 job_scout.py --dry-run`

## Environment variables
| Variable | Description |
|---|---|
| `SERPER_API_KEY` | From serper.dev |
| `TELEGRAM_BOT_TOKEN` | From @BotFather on Telegram |
| `TELEGRAM_CHAT_ID` | From @userinfobot on Telegram |