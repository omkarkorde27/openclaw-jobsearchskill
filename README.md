# Job Scout

A self-hosted, hybrid job-discovery pipeline that polls public ATS APIs
(Greenhouse, Lever, Ashby, Workday, SmartRecruiters, Workable, Breezy) for
fresh DS / ML / AI / Data-Analyst openings, scores them against your résumé
with TF-IDF cosine similarity, and ships a ranked digest to Telegram on a
cron schedule.

A weekly companion job (`discovery.py`) finds new companies on those ATSes
via Serper.dev (a Google search API) and additionally pulls jobs from the
Tier-3 platforms that don't expose listing APIs (iCIMS, Taleo, Jobvite,
JazzHR, ADP).

---

## Architecture

```
┌────────────────────────────┐         ┌────────────────────────────┐
│  Daily 06:00               │         │  Weekly Sun 04:00          │
│  job_scout.py              │         │  discovery.py              │
│                            │         │                            │
│  Polls 7 ATSes' native     │         │  Serper queries:           │
│  APIs for last-24h jobs.   │         │  • New slugs for ATSes     │
│  Filter, score, Telegram.  │         │  • Tier-3 jobs (iCIMS …)   │
│  Zero Serper spend.        │         │  Same filter/score/digest. │
└────────────┬───────────────┘         └────────────┬───────────────┘
             │ reads                                │ writes
             ▼                                      ▼
       ┌─────────────────────────────────────────────┐
       │  seen_jobs.db (SQLite)                      │
       │  ├─ companies   (platform, slug, extra)     │
       │  └─ seen_jobs   (job_id, url, identity_key) │
       └─────────────────────────────────────────────┘
```

Three tiers of source platforms:

| Tier | Platforms                                        | Source           | Cadence |
| ---- | ------------------------------------------------ | ---------------- | ------- |
| 1    | Greenhouse, Lever, Ashby, Workday                | Native ATS API   | Daily   |
| 2    | SmartRecruiters, Workable, Breezy                | Native ATS API   | Daily   |
| 3    | iCIMS, Taleo, Jobvite, JazzHR, ADP               | Serper (Google)  | Weekly  |

Tier 1+2 carry real `posted_at` timestamps, full job descriptions, and
stable IDs — so dedup is cheap and the 24-hour freshness filter is
trustworthy. Tier 3 has no clean public API, so it stays on Serper at
weekly cadence (low volume, acceptable freshness).

---

## Repo layout

```
job_scout_v4/
├── job_scout.py        # daily cron entrypoint (ATS-only)
├── discovery.py        # weekly cron entrypoint (slug discovery + tier-3)
├── ats/
│   ├── base.py             # ATSAdapter base, Job dataclass, shared Session
│   ├── fetch.py            # fetch_all_ats(): orchestrates daily polling
│   ├── greenhouse.py       # Tier 1
│   ├── lever.py            # Tier 1
│   ├── ashby.py            # Tier 1
│   ├── workday.py          # Tier 1
│   ├── smartrecruiters.py  # Tier 2
│   ├── workable.py         # Tier 2
│   └── breezy.py           # Tier 2
├── requirements.txt
├── resume.txt          # YOU PROVIDE — plain-text résumé for scoring
├── seen_jobs.db        # SQLite, auto-created on first run
├── last_run.json       # auto-managed run-timestamp state
├── scout.log           # rolling log
└── .env                # YOU PROVIDE — credentials (see below)
```

---

## Prerequisites

- **Python 3.10+** (uses `dict[str, …]` PEP-585 generics).
- **SQLite 3** (bundled with Python).
- A **Telegram bot** with a chat ID to deliver to.
- A **Serper.dev API key** (free tier: 2,500 queries; only used by the
  weekly `discovery.py` — daily runs need none).
- A plain-text version of your résumé.

---

## 1. Clone and install

```bash
git clone <your-repo-url> job_scout_v4
cd job_scout_v4

python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

`requirements.txt` is minimal — `requests` and `scikit-learn` (plus their
transitive dependencies: `numpy`, `scipy`, `joblib`, `threadpoolctl`).

---

## 2. Provide your résumé

Drop a plain-text export of your résumé into the project root as
`resume.txt`:

```bash
# from a PDF
pdftotext resume.pdf resume.txt

# or from a docx
pandoc resume.docx -t plain -o resume.txt
```

The TF-IDF scorer learns vocabulary from this file once at startup; the
quality of matches is driven by how rich and specific the text is. Bullet
lists of tools / techniques / domains give the strongest signal.

---

## 3. Get credentials

### 3a. Serper.dev (only for the weekly job)

1. Sign up at <https://serper.dev>.
2. Copy your API key from the dashboard.

### 3b. Telegram bot

1. Open Telegram, message `@BotFather`, run `/newbot`, follow the prompts,
   and copy the bot token it gives you.
2. Message `@userinfobot` and copy your numeric chat ID. (For a group
   digest, add the bot to the group and use the group's chat ID instead.)
3. Send the bot any message at least once so it can DM you.

### 3c. Write the `.env`

Copy `.env.example` → `.env` and fill in:

```env
SERPER_API_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TELEGRAM_BOT_TOKEN=123456789:AA-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TELEGRAM_CHAT_ID=987654321
```

Then export the variables before running the scripts:

```bash
set -a && source .env && set +a
```

(or use `direnv`, `systemd EnvironmentFile=`, or whatever your host
prefers).

---

## 4. First run — seed the companies table

`discovery.py` populates the `companies` table that `job_scout.py` polls.
The first run can be done offline by harvesting slugs from any URLs already
in `seen_jobs.db` (handy if you have an old DB) — otherwise use Serper:

```bash
# offline seed from seen_jobs.url (free, no network)
python3 discovery.py --bootstrap

# OR Serper-driven slug discovery only (no digest)
python3 discovery.py --serper

# OR everything — bootstrap + Serper discovery + Tier-3 digest
python3 discovery.py
```

Verify it worked:

```bash
sqlite3 seen_jobs.db "SELECT platform, COUNT(*) FROM companies GROUP BY platform;"
```

You should see a few dozen companies across the seven Tier-1+2 platforms.

---

## 5. Test the daily run

```bash
python3 job_scout.py --dry-run
```

The `--dry-run` flag:
- prints the digest to stdout instead of sending it to Telegram,
- does not write to `seen_jobs.db` or update `last_run.json`.

Expected output: `═══ Job Scout v5 starting ═══` … `[ats] polling N
companies …` … `═══ Run Summary ═══`. Polling ~175 companies takes ~10
minutes on the first cold run; subsequent runs reuse HTTP connections and
are faster.

If the dry-run looks sane, do a real run:

```bash
python3 job_scout.py
```

You should receive a Telegram digest within a minute or two of completion.

---

## 6. Schedule the cron

Two cron entries: daily ATS poll + weekly Serper sweep.

```cron
# m h dom mon dow command
0  6 * * *   cd /home/you/job_scout_v4 && set -a && . ./.env && set +a && ./venv/bin/python3 job_scout.py >> scout.cron.log 2>&1
0  4 * * 0   cd /home/you/job_scout_v4 && set -a && . ./.env && set +a && ./venv/bin/python3 discovery.py >> scout.cron.log 2>&1
```

Or, with a `systemd` timer, use the same two commands as `ExecStart=`.

---

## CLI reference

### `job_scout.py`

| Flag         | Purpose                                                                              |
| ------------ | ------------------------------------------------------------------------------------ |
| `--dry-run`  | Print digest, skip Telegram and DB writes.                                           |
| `--reset`    | Wipe `seen_jobs.db` and re-run from `CUTOFF_DATE` (`2026-03-30`). Destructive.       |
| `--debug`    | Verbose logging — print every filter rejection.                                      |
| `--shadow`   | After the real run, also re-run the pipeline in a no-write mode for parity debugging.|

### `discovery.py`

| Flag           | Purpose                                                                |
| -------------- | ---------------------------------------------------------------------- |
| `--bootstrap`  | Seed `companies` from existing `seen_jobs.url` rows. No network calls. |
| `--serper`     | Tier 1+2 slug discovery via Serper (uses API quota).                   |
| `--tier3`      | Tier-3 job fetch via Serper + filter / score / Telegram digest.        |
| `--dry-run`    | No DB writes, no Telegram.                                             |

With no flags, `discovery.py` runs all three modes: bootstrap, Serper
discovery, and Tier-3 digest.

---

## Configuration

The most useful constants are at the top of [`job_scout.py`](job_scout.py):

| Constant            | Default        | Meaning                                                |
| ------------------- | -------------- | ------------------------------------------------------ |
| `MIN_SCORE`         | `0.05`         | TF-IDF cosine cutoff. Higher = stricter résumé match.  |
| `MAX_DIGEST`        | `50`           | Hard cap on jobs per digest.                           |
| `IDENTITY_TTL_DAYS` | `30`           | Window for fuzzy `(title, company)` dedup.             |
| `CUTOFF_DATE`       | `2026-03-30`   | Earliest `posted_at` ever considered.                  |
| `REQUEST_TIMEOUT`   | `20`           | Per-request timeout in seconds.                        |

The title / location / sponsorship / experience filters are pure regex
and data lists in the same file — edit `TITLE_INCLUDE`,
`TITLE_EXCLUDE_HARD`, `US_STATES_FULL`, `NON_US_LOCATIONS`,
`ENTRY_LEVEL_INCLUDE`, etc. to taste.

---

## How dedup works

The pipeline uses a two-layer key:

1. **`job_id`** — a stable, platform-native ID for ATS-API jobs
   (e.g. `greenhouse:stripe:4012345`) or an `md5(canonical_url)` for Serper
   jobs. Catches exact repeats.
2. **`identity_key`** — `sha1(normalize(company) + "|" + normalize(title))`
   with a 30-day TTL. Catches the case where a company reposts the same
   role under a fresh ATS ID, or moves it across platforms. Whitespace,
   punctuation, location suffixes (` - Remote`, ` in San Francisco`) are
   stripped before hashing.

A job is considered "seen" if **either** key is in the DB. Both keys are
written every time a job is sent to Telegram.

---

## Troubleshooting

**A company is in `companies` but yields zero jobs every run** — check its
`last_status` and `fail_count`:

```bash
sqlite3 seen_jobs.db "SELECT platform, slug, last_status, fail_count FROM companies WHERE last_status NOT IN ('ok','no_jobs');"
```

After three consecutive failures the row is left intact for audit but the
adapter will skip it (`last_status='gone'`).

**Telegram returns 400** — most often a Markdown escaping issue in a job
title. The script handles the common cases; if you hit one, paste the
offending message into the issue tracker.

**Resetting completely** — `python3 job_scout.py --reset` drops
`seen_jobs.db` and starts over from `CUTOFF_DATE`. The `companies` table
is recreated empty; re-run `discovery.py --bootstrap` (if you have a
backup of seen URLs) or `discovery.py --serper` to repopulate it.

**The daily run logs `[ats] failures by platform: {...}`** — that's
informational, not fatal. One adapter erroring on one slug doesn't break
the run; the failing company gets its `fail_count` bumped and the rest of
the run proceeds normally.

---

## Adding a new ATS

1. Create `ats/<platform>.py` with a class that subclasses `ATSAdapter`
   and implements `parse_slug(url)` and `list_jobs(slug, extra)`. Use
   [`ats/greenhouse.py`](ats/greenhouse.py) as the reference shape — it's
   the cleanest example.
2. Register it in [`ats/__init__.py`](ats/__init__.py) — import the class
   and add it to the `REGISTRY` tuple.
3. Add the `platform` string to `DAILY` in [`ats/fetch.py`](ats/fetch.py)
   so the daily cron picks it up.
4. Add an entry to `SLUG_DISCOVERY_DOMAINS` in
   [`discovery.py`](discovery.py) so the weekly Serper crawl can find new
   companies on it.

That's the whole contract — the rest of the pipeline (filters, scoring,
dedup, Telegram) is platform-agnostic.

---

## License

Personal project — fork it, hack it, do whatever you want with it.
