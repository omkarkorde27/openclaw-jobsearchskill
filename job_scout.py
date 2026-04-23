#!/usr/bin/env python3
"""
job_scout.py  v5.0
==================
Serper.dev-powered job scout — no hardcoded company lists.
Queries Google via Serper with site: operators on ATS domains to find
entry-level DS/ML/AI jobs directly from source platforms.

Usage:
  python3 job_scout.py              # normal daily run
  python3 job_scout.py --dry-run    # print to terminal, skip Telegram
  python3 job_scout.py --reset      # wipe DB, run fresh from cutoff date

Requirements:
  pip install requests beautifulsoup4 scikit-learn
  export SERPER_API_KEY="your_serper_key"
  export TELEGRAM_BOT_TOKEN="..."
  export TELEGRAM_CHAT_ID="..."
"""

import os, re, sys, json, time, sqlite3, logging, argparse, hashlib
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit, urlunsplit, unquote

import requests
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

# ─── Paths ────────────────────────────────────────────────────────────────────
BASE        = Path(__file__).parent
DB_PATH     = BASE / "seen_jobs.db"
RESUME_PATH = BASE / "resume.txt"
LOG_PATH    = BASE / "scout.log"
STATE_PATH  = BASE / "last_run.json"

# ─── Credentials ─────────────────────────────────────────────────────────────
SERPER_KEY  = os.getenv("SERPER_API_KEY", "")
BOT_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID     = os.getenv("TELEGRAM_CHAT_ID", "")

# ─── Hard cutoff ─────────────────────────────────────────────────────────────
CUTOFF_DATE = datetime(2026, 3, 30, tzinfo=timezone.utc)

# ─── Tuning ──────────────────────────────────────────────────────────────────
MIN_SCORE          = 0.05
MAX_DIGEST         = 50
REQUEST_TIMEOUT    = 20
IDENTITY_TTL_DAYS  = 30   # fuzzy (title, company) dedup window

# ─── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler()],
)
log = logging.getLogger("scout")

# ═════════════════════════════════════════════════════════════════════════════
# ATS DOMAINS — Tier-3 platforms with no practical native API.
# After Phase 4 these are not polled daily; `discovery.py` hits them weekly.
# Tier 1+2 (greenhouse/lever/ashby/workday + smartrecruiters/workable/breezy)
# use native APIs via fetch_all_ats().
# ═════════════════════════════════════════════════════════════════════════════

ATS_DOMAINS = [
    {"domain": "jobs.jobvite.com",           "platform": "Jobvite"},
    {"domain": "applytojob.com",             "platform": "JazzHR"},
    {"domain": "taleo.net",                  "platform": "Taleo"},
    {"domain": "icims.com",                  "platform": "iCIMS"},
    {"domain": "workforcenow.adp.com",       "platform": "ADP"},
]

# ═════════════════════════════════════════════════════════════════════════════
# TITLE & SPONSORSHIP FILTERS
# ═════════════════════════════════════════════════════════════════════════════

TITLE_INCLUDE = [
    'data scientist', 'data science',
    'machine learning', 'ml engineer',
    'ai engineer', 'gen ai engineer', 'genai', 'artificial intelligence',
    'data analyst', 'analytics engineer',
    'applied scientist', 'research scientist',
    'nlp', 'natural language',
    'computer vision',
    'deep learning',
    'mlops', 'ml ops',
]

TITLE_EXCLUDE_HARD = [
    'senior', 'sr.', 'sr ',
    'staff', 'principal', 'lead',
    'director', 'manager', 'vp ',
    'vice president', 'head of',
    'architect', 'distinguished',
    'executive', 'partner',
    # Mid-level word markers
    'mid-level', 'mid level', 'midlevel',
    'experienced', 'senior-level',
]

# Roman-numeral level markers (II / III / IV) — bordered regex so "IV" inside
# words doesn't match. Catches "Data Scientist II", "Scientist, III", "DS (IV)".
LEVEL_NUMERAL_PATTERN = re.compile(r'[\s,(\[-]+(ii|iii|iv)\b', re.I)

SPONSORSHIP_DENY = [
    'must be a us citizen', 'us citizen or permanent resident',
    'green card required', 'security clearance required',
]

# ─── Resume fallback ────────────────────────────────────────────────────────
FALLBACK_RESUME = """
python pytorch tensorflow keras scikit-learn sklearn pandas numpy scipy
sql mysql mongodb postgresql bigquery snowflake
machine learning deep learning nlp computer vision
transformers llm rag langchain embeddings vector faiss chromadb
data science data engineering etl pipeline airflow mlflow
aws gcp azure s3 ec2 docker kubernetes
flask fastapi rest api mcp
statistics regression classification clustering
neural network cnn rnn lstm attention transformer
xgboost gradient boosting random forest
feature engineering dimensionality reduction
research assistant chatbot indiana university
"""

# ═════════════════════════════════════════════════════════════════════════════
# DATABASE
# ═════════════════════════════════════════════════════════════════════════════

def init_db(reset: bool = False) -> sqlite3.Connection:
    if reset and DB_PATH.exists():
        DB_PATH.unlink()
        log.info("DB reset")
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seen_jobs (
            job_id       TEXT PRIMARY KEY,
            title        TEXT,
            company      TEXT,
            url          TEXT,
            publisher    TEXT,
            posted_ts    INTEGER,
            seen_at      TEXT,
            identity_key TEXT
        )
    """)
    # Migrate pre-existing DBs that predate identity_key
    cols = {row[1] for row in conn.execute("PRAGMA table_info(seen_jobs)")}
    if "identity_key" not in cols:
        conn.execute("ALTER TABLE seen_jobs ADD COLUMN identity_key TEXT")
        rows = list(conn.execute("SELECT job_id, title, company, url FROM seen_jobs"))
        updated = 0
        for job_id, title, company, url in rows:
            # Re-derive Workday company from the URL — historical rows stored
            # "Myworkdayjobs.com" because of the title-parser bug.
            if url:
                m = re.match(
                    r"^https?://([a-z0-9][a-z0-9\-]*)\.wd\d+\.myworkdayjobs\.com",
                    url.lower(),
                )
                if m:
                    company = m.group(1)
            key = identity_key_for({"title": title or "", "company": company or ""})
            if key:
                conn.execute(
                    "UPDATE seen_jobs SET identity_key=? WHERE job_id=?",
                    (key, job_id),
                )
                updated += 1
        log.info(f"DB migration: added identity_key, backfilled {updated}/{len(rows)} rows")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_identity_key ON seen_jobs(identity_key)")
    # ── companies table (Phase 1 of ATS-native polling) ─────────────────────
    conn.execute("""
        CREATE TABLE IF NOT EXISTS companies (
            platform      TEXT NOT NULL,
            slug          TEXT NOT NULL,
            extra         TEXT,                 -- JSON; e.g. Workday board
            discovered_at TEXT,
            last_polled   TEXT,
            last_status   TEXT,                 -- 'ok' | 'http_404' | 'no_jobs' | 'rate_limited' | 'gone'
            fail_count    INTEGER DEFAULT 0,
            PRIMARY KEY (platform, slug)
        )
    """)
    conn.commit()
    return conn

def is_new(conn: sqlite3.Connection, job: dict) -> bool:
    """New if neither the URL hash nor the (company, title) identity key
    has been seen in the last IDENTITY_TTL_DAYS days."""
    if conn.execute(
        "SELECT 1 FROM seen_jobs WHERE job_id=?", (job["id"],)
    ).fetchone() is not None:
        return False
    key = identity_key_for(job)
    if not key:
        return True
    cutoff = (datetime.now(timezone.utc) - timedelta(days=IDENTITY_TTL_DAYS)).isoformat()
    row = conn.execute(
        "SELECT 1 FROM seen_jobs WHERE identity_key=? AND seen_at>=? LIMIT 1",
        (key, cutoff),
    ).fetchone()
    return row is None

def mark_seen(conn: sqlite3.Connection, job: dict):
    conn.execute(
        """INSERT OR IGNORE INTO seen_jobs
             (job_id, title, company, url, publisher, posted_ts, seen_at, identity_key)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            job["id"], job["title"], job["company"],
            job["url"], job.get("publisher", ""),
            job.get("posted_ts", 0),
            datetime.now(timezone.utc).isoformat(),
            identity_key_for(job),
        ),
    )
    conn.commit()

# ═════════════════════════════════════════════════════════════════════════════
# STATE
# ═════════════════════════════════════════════════════════════════════════════

def load_last_run() -> datetime:
    if STATE_PATH.exists():
        try:
            data = json.loads(STATE_PATH.read_text())
            ts = data.get("last_run_utc")
            if ts:
                return datetime.fromisoformat(ts)
        except Exception:
            pass
    return CUTOFF_DATE

def save_last_run():
    STATE_PATH.write_text(
        json.dumps({"last_run_utc": datetime.now(timezone.utc).isoformat()})
    )

# ═════════════════════════════════════════════════════════════════════════════
# SERPER.DEV — Google search via API
# ═════════════════════════════════════════════════════════════════════════════

SERPER_URL = "https://google.serper.dev/search"

ROLE_KEYWORDS = [
    "data scientist",
    "machine learning engineer",
    "data analyst",
    "applied scientist",
    "AI engineer",
]

def build_query(domain: str, role: str) -> str:
    """One role per query — avoids Serper's 'Query not allowed' policy block."""
    return f'site:{domain} "{role}"'

# Serper plan caps num at ~20; num>=50 is rejected as "Query not allowed".
# Also `location` and `tbs` params are plan-gated and trigger the same block.
# US geography is enforced Python-side via passes_location(); freshness via DB dedup.
RESULTS_PER_PAGE     = 20
MAX_PAGES_PER_QUERY  = 2

def _serper_call(domain: str, platform: str, role: str, page: int) -> list[dict]:
    """Single Serper call for one (domain, role, page) tuple."""
    query = build_query(domain, role)
    headers = {
        "X-API-KEY": SERPER_KEY,
        "Content-Type": "application/json",
    }
    payload = {
        "q":    query,
        "gl":   "us",
        "num":  RESULTS_PER_PAGE,
        "page": page,
    }
    log.debug(f"  Serper query ({platform} / {role} / p{page}): {query}")
    try:
        r = requests.post(SERPER_URL, headers=headers, json=payload,
                          timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return r.json().get("organic", [])
    except requests.exceptions.HTTPError as e:
        body = e.response.text[:200] if e.response is not None else ""
        if e.response is not None and e.response.status_code == 429:
            log.warning("  Serper rate limited — sleeping 30s")
            time.sleep(30)
        else:
            log.warning(f"  Serper error {platform}/{role}/p{page}: {e} | body: {body}")
        return []
    except Exception as e:
        log.warning(f"  Serper error {platform}/{role}/p{page}: {e}")
        return []

def fetch_serper(domain: str, platform: str) -> list[dict]:
    """Run one Serper call per role keyword (paginated) for this domain."""
    all_results: list[dict] = []
    for role in ROLE_KEYWORDS:
        for page in range(1, MAX_PAGES_PER_QUERY + 1):
            results = _serper_call(domain, platform, role, page)
            all_results.extend(results)
            if len(results) < RESULTS_PER_PAGE:
                break
            time.sleep(0.3)
        time.sleep(0.5)

    if len(all_results) == 0:
        log.warning(
            f"  Serper {platform} ({domain}): 0 results across {len(ROLE_KEYWORDS)} role queries"
        )
    else:
        log.info(f"  Serper {platform} ({domain}): {len(all_results)} total results")
    return all_results

_URL_TRAIL_SUFFIXES = ("/apply", "/job")

def _canonical_url(url: str) -> str:
    """Strip query/fragment, decode %-escapes, lowercase host, and remove
    trailing ATS navigation segments (/apply, /job) so URL variants of the
    same job collapse to one ID."""
    if not url:
        return url
    url = unquote(url)
    url = url.split("#", 1)[0].split("?", 1)[0]
    try:
        parts = urlsplit(url)
        scheme = (parts.scheme or "https").lower()
        host   = parts.netloc.lower()
        path   = parts.path
    except Exception:
        return url.rstrip("/")
    # Repeatedly strip trailing nav segments (handles /apply/, /apply, /job/)
    while True:
        orig = path
        for suffix in _URL_TRAIL_SUFFIXES:
            if path.endswith(suffix):
                path = path[: -len(suffix)]
                break
            if path.endswith(suffix + "/"):
                path = path[: -len(suffix) - 1]
                break
        if path == orig:
            break
    path = path.rstrip("/")
    return urlunsplit((scheme, host, path, "", ""))

# ─── Identity key (title + company fuzzy dedup) ─────────────────────────────
_TITLE_CUT_RE = re.compile(
    r'\s*(?:[|@(\[]|\s-\s|\s–\s|\s—\s|\sin\s|\sat\s).*$',
    re.I,
)
_PUNCT_RE     = re.compile(r'[^\w\s]')
_WS_RE        = re.compile(r'\s+')
_COMPANY_SUFFIX_RE = re.compile(r'\s+(inc|llc|ltd|co|corp|corporation|limited)$', re.I)

def _normalize_title(s: str) -> str:
    s = (s or "").lower().strip()
    s = _TITLE_CUT_RE.sub("", s)     # drop everything after separators
    s = _PUNCT_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    return s

def _normalize_company(s: str) -> str:
    s = (s or "").lower().strip()
    s = _PUNCT_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    s = _COMPANY_SUFFIX_RE.sub("", s)
    return s

def identity_key_for(job: dict) -> Optional[str]:
    """Stable key from (company, title). Returns None if either is missing —
    the caller should then fall back to URL-hash dedup only."""
    company = _normalize_company(job.get("company", ""))
    title   = _normalize_title(job.get("title", ""))
    if not company or not title:
        return None
    return hashlib.sha1(f"{company}|{title}".encode()).hexdigest()[:16]

def parse_serper_result(r: dict, platform: str) -> Optional[dict]:
    """Convert a Serper organic result to standardized job dict."""
    url     = r.get("link", "")
    title   = r.get("title", "")
    snippet = r.get("snippet", "")

    if not url or not title:
        return None

    url = _canonical_url(url)

    # Extract job title and company from page title
    # Common patterns: "Job Title at Company | Platform"
    #                  "Job Title - Company"
    #                  "Job Title | Company | Platform"
    job_title = title
    company   = ""

    if " at " in title:
        parts = title.split(" at ", 1)
        job_title = parts[0].strip()
        company = parts[1].split(" | ")[0].split(" - ")[0].strip()
    elif " - " in title:
        parts = title.split(" - ", 1)
        job_title = parts[0].strip()
        company = parts[1].split(" | ")[0].split(" - ")[0].strip()
    elif " | " in title:
        parts = title.split(" | ")
        job_title = parts[0].strip()
        if len(parts) > 1:
            company = parts[1].strip()

    # Workday title patterns don't include the company — pull it from the
    # subdomain instead: <company-slug>.wd<N>.myworkdayjobs.com.
    if platform == "Workday":
        try:
            host = urlsplit(url).netloc.lower()
            m = re.match(r"^([a-z0-9][a-z0-9\-]*)\.wd\d+\.myworkdayjobs\.com", host)
            if m:
                company = m.group(1)
        except Exception:
            pass

    # Stable job ID from URL
    job_id = f"sp-{hashlib.md5(url.encode()).hexdigest()[:16]}"

    return {
        "id":          job_id,
        "title":       job_title,
        "company":     company,
        "location":    "",
        "description": snippet,
        "url":         url,
        "publisher":   platform.lower(),
        "posted_ts":   0,        # Option B: rely on tbs=qdr:m + dedup
        "score":       0.0,
    }

def fetch_all_serper() -> list[dict]:
    """Run Serper queries for all ATS domains."""
    if not SERPER_KEY:
        log.error(
            "SERPER_API_KEY is not set.\n"
            "1. Go to: https://serper.dev\n"
            "2. Sign up (free tier: 2,500 queries)\n"
            "3. Copy your API key from the dashboard\n"
            "4. Run: export SERPER_API_KEY='your_key_here'"
        )
        return []

    log.info(f"[Serper] Querying {len(ATS_DOMAINS)} ATS domains")
    all_jobs = []

    for ats in ATS_DOMAINS:
        raw_results = fetch_serper(ats["domain"], ats["platform"])
        for r in raw_results:
            job = parse_serper_result(r, ats["platform"])
            if job:
                all_jobs.append(job)
        time.sleep(1.0)  # respect rate limits

    log.info(f"[Serper] Total parsed: {len(all_jobs)}")
    return all_jobs

# ═════════════════════════════════════════════════════════════════════════════
# FILTERS (shared pipeline)
# ═════════════════════════════════════════════════════════════════════════════

def _lc(s: str) -> str:
    return (s or "").lower()

def passes_title(title: str) -> bool:
    t = _lc(title)
    if not t:
        return False
    if not any(kw in t for kw in TITLE_INCLUDE):
        return False
    for kw in TITLE_EXCLUDE_HARD:
        if kw in t:
            log.debug(f"  title-reject ('{kw}'): {title[:80]}")
            return False
    if LEVEL_NUMERAL_PATTERN.search(title):
        log.debug(f"  title-reject (level-numeral): {title[:80]}")
        return False
    return True

def passes_date(posted_ts: int, since: datetime) -> bool:
    if not posted_ts:
        return True   # no timestamp → include (Serper uses tbs=qdr:m for freshness)
    effective_cutoff = max(since, CUTOFF_DATE)
    posted_dt = datetime.fromtimestamp(posted_ts, tz=timezone.utc)
    return posted_dt >= effective_cutoff

def passes_sponsorship(desc: str) -> bool:
    d = _lc(desc)
    return not any(phrase in d for phrase in SPONSORSHIP_DENY)

# ─── US LOCATION ALLOWLIST ──────────────────────────────────────────────────
# Positive US signals — if any appear in title, snippet, or URL, the job is
# treated as US-based and accepted (even alongside other locations).
US_STATES_FULL = [
    'alabama', 'alaska', 'arizona', 'arkansas', 'california', 'colorado',
    'connecticut', 'delaware', 'florida', 'georgia', 'hawaii', 'idaho',
    'illinois', 'indiana', 'iowa', 'kansas', 'kentucky', 'louisiana',
    'maine', 'maryland', 'massachusetts', 'michigan', 'minnesota',
    'mississippi', 'missouri', 'montana', 'nebraska', 'nevada',
    'new hampshire', 'new jersey', 'new mexico', 'new york', 'north carolina',
    'north dakota', 'ohio', 'oklahoma', 'oregon', 'pennsylvania',
    'rhode island', 'south carolina', 'south dakota', 'tennessee', 'texas',
    'utah', 'vermont', 'virginia', 'washington', 'west virginia',
    'wisconsin', 'wyoming', 'district of columbia',
]

US_CITIES = [
    'new york', 'nyc', 'san francisco', 'sf bay', 'bay area',
    'los angeles', 'chicago', 'seattle', 'boston', 'austin', 'denver',
    'atlanta', 'miami', 'dallas', 'houston', 'philadelphia', 'phoenix',
    'san diego', 'san jose', 'silicon valley', 'palo alto', 'mountain view',
    'menlo park', 'redmond', 'bellevue', 'pittsburgh', 'cambridge',
    'brooklyn', 'manhattan', 'minneapolis', 'detroit', 'baltimore',
    'sacramento', 'orlando', 'tampa', 'nashville', 'charlotte', 'raleigh',
    'durham', 'columbus', 'indianapolis', 'kansas city', 'salt lake city',
    'las vegas', 'portland', 'cincinnati', 'cleveland', 'milwaukee',
    'jacksonville', 'memphis', 'st. louis', 'st louis', 'saint louis',
    'arlington', 'plano', 'sunnyvale', 'cupertino', 'santa clara',
    'santa monica', 'irvine', 'oakland', 'berkeley', 'fremont',
    'jersey city', 'hoboken', 'stamford', 'hartford', 'providence',
    'reston', 'mclean', 'herndon', 'rockville', 'bethesda',
    'washington dc', 'washington d.c.',
]

US_GENERIC = [
    'united states', 'usa', 'u.s.a', 'u.s.', 'us only', 'us-only',
    'us based', 'us-based', 'us remote', 'remote us', 'remote (us)',
    'remote - us', 'remote, us', 'remote-us', 'remote -us',
    'anywhere in the us', 'anywhere in us', 'continental us', 'nationwide',
]

US_SIGNAL_PATTERN = re.compile(
    r'\b(' + '|'.join(re.escape(k) for k in US_STATES_FULL + US_CITIES + US_GENERIC) + r')\b'
)

# State 2-letter codes — only matched when preceded by a comma or open paren,
# to avoid false positives like "or" / "in" / "me" matching common words.
US_STATE_CODE_PATTERN = re.compile(
    r'(?:,|\()\s*'
    r'(al|ak|az|ar|ca|co|ct|de|fl|ga|hi|id|il|in|ia|ks|ky|la|me|md|'
    r'ma|mi|mn|ms|mo|mt|ne|nv|nh|nj|nm|ny|nc|nd|oh|ok|or|pa|ri|sc|'
    r'sd|tn|tx|ut|vt|va|wa|wv|wi|wy|dc)\b'
)

# ─── NON-US SAFETY NET ──────────────────────────────────────────────────────
# This list does NOT need to be exhaustive — it is only consulted when no
# positive US signal was found. Jobs with no location signal at all still pass.
NON_US_LOCATIONS = [
    # Countries / regions
    'india', 'uk', 'united kingdom', 'london', 'canada', 'toronto',
    'vancouver', 'germany', 'berlin', 'munich', 'france', 'paris',
    'ireland', 'dublin', 'singapore', 'japan', 'tokyo', 'australia',
    'sydney', 'melbourne', 'brazil', 'mexico', 'china', 'beijing',
    'shanghai', 'shenzhen', 'hong kong', 'korea', 'seoul', 'taiwan',
    'israel', 'tel aviv', 'netherlands', 'amsterdam', 'sweden',
    'stockholm', 'spain', 'madrid', 'barcelona', 'italy', 'milan',
    'switzerland', 'zurich', 'poland', 'warsaw', 'czech', 'prague',
    'austria', 'vienna', 'belgium', 'brussels', 'portugal', 'lisbon',
    'norway', 'oslo', 'denmark', 'copenhagen', 'finland', 'helsinki',
    'romania', 'bucharest', 'ukraine', 'argentina', 'buenos aires',
    'chile', 'colombia', 'bogota', 'manila', 'philippines',
    'indonesia', 'jakarta', 'vietnam', 'bangkok', 'thailand',
    'malaysia', 'kuala lumpur', 'new zealand', 'auckland',
    'south africa', 'cape town', 'nigeria', 'lagos', 'kenya',
    'nairobi', 'egypt', 'cairo', 'dubai', 'uae', 'saudi arabia',
    'qatar', 'pakistan', 'karachi', 'lahore', 'bangladesh', 'dhaka',
    'bengaluru', 'bangalore', 'hyderabad', 'mumbai', 'pune', 'chennai',
    'gurgaon', 'gurugram', 'noida',
    # Common non-US markers in URLs/titles
    '.co.uk', '.de', '.fr', '.ca', '.in', '.sg', '.jp', '.au',
    'europe', 'emea', 'apac', 'latam',
]

NON_US_PATTERN = re.compile(
    r'\b(' + '|'.join(re.escape(k) for k in NON_US_LOCATIONS) + r')\b'
)

def has_us_signal(text: str) -> bool:
    return bool(US_SIGNAL_PATTERN.search(text)) or bool(US_STATE_CODE_PATTERN.search(text))

def passes_location(job: dict) -> bool:
    """
    Strict US allowlist (tightened in v5.1 — query already enforces US clause):
      • PASS if any positive US signal is present (multi-location like
        'New York / London' still passes).
      • REJECT if an explicit non-US signal is present and NO US signal.
      • REJECT if no location signal at all — a legit US job should carry
        some US marker in its title/snippet/url given the query clause.
    """
    text = _lc(job["title"] + " " + job.get("location", "") + " " +
               job["description"] + " " + job["url"])
    if has_us_signal(text):
        return True
    if NON_US_PATTERN.search(text):
        log.debug(f"  location-reject (non-US): {job['title'][:60]} | {job['url']}")
        return False
    log.debug(f"  location-reject (no-signal): {job['title'][:60]} | {job['url']}")
    return False

# ═════════════════════════════════════════════════════════════════════════════
# EXPERIENCE FILTER — entry-level / new grad / 0–2 years
# ═════════════════════════════════════════════════════════════════════════════

# Positive entry-level signals. Job must contain at least one of these in
# title or snippet to pass passes_experience().
ENTRY_LEVEL_INCLUDE = [
    # Internships / co-ops / apprenticeships
    'intern', 'interns', 'internship', 'internships',
    'co-op', 'coop', 'apprentice', 'apprenticeship',
    # New grad / recent grad
    'new grad', 'new grads', 'new graduate', 'new graduates',
    'recent grad', 'recent grads', 'recent graduate', 'recent graduates',
    'fresh graduate', 'fresh grad', 'fresher',
    # Entry-level / junior
    'entry level', 'entry-level', 'entrylevel',
    'junior', 'jr.',
    'early career', 'early-career', 'early in career', 'early in your career',
    # University programs
    'university grad', 'university graduate', 'university hire',
    'university program', 'university recruit', 'university recruiting',
    'graduate program', 'graduate scheme', 'graduate hire',
    'graduate engineer', 'graduate analyst', 'graduate scientist',
    'graduate developer', 'graduate associate',
    'campus hire', 'campus recruit', 'campus program', 'campus recruiting',
    # Programs aimed at new entrants
    'rotational program', 'rotation program', 'rotational analyst',
    'leadership development program', 'development program',
    'trainee', 'training program',
    # Years-of-experience phrasing
    '0-2 years', '0 to 2 years', '0-1 year', '0-1 years',
    '1-2 years', '1 to 2 years', '0+ years', '1+ years', '2+ years',
    'no experience required', 'no prior experience',
    # Additional variants
    'grad', 'graduating', 'graduate', '0 years',
    'recent college', 'college hire', 'college graduate',
    'emerging talent', 'early talent',
    'associate scientist', 'associate engineer', 'associate analyst',
]

ENTRY_PATTERN = re.compile(
    r'\b(' + '|'.join(re.escape(k) for k in ENTRY_LEVEL_INCLUDE) + r')\b'
)

# Detect explicit experience requirements of 3+ years.
YEARS_PLUS_PATTERN  = re.compile(r'\b(\d+)\s*\+\s*(?:years?|yrs?)', re.I)
YEARS_RANGE_PATTERN = re.compile(r'\b(\d+)\s*(?:-|–|to)\s*\d+\s*(?:years?|yrs?)', re.I)
YEARS_MIN_PATTERN   = re.compile(r'(?:minimum|min\.?|at\s+least)\s+(?:of\s+)?(\d+)\s*(?:years?|yrs?)', re.I)

def has_senior_years_requirement(text: str) -> bool:
    """True if the text mentions an explicit experience requirement of 3+ years."""
    for pat in (YEARS_PLUS_PATTERN, YEARS_RANGE_PATTERN, YEARS_MIN_PATTERN):
        for m in pat.finditer(text):
            try:
                if int(m.group(1)) >= 3:
                    return True
            except (ValueError, IndexError):
                continue
    return False

def passes_experience(job: dict) -> bool:
    """
    Layered entry-level filter (option a — silent snippets pass through):
      1. Positive override: if title or description has an entry-level phrase,
         ALWAYS pass (even if YOE>=3 also appears — the entry phrase wins).
      2. Reject: explicit 3+ YOE requirement in the snippet.
      3. Pass: silent snippet — let scoring + title filter do the work.
    Senior/Roman-numeral/mid-level titles already removed by passes_title().
    """
    title_lc = _lc(job["title"])
    desc_lc  = _lc(job["description"])
    full     = title_lc + " " + desc_lc

    if ENTRY_PATTERN.search(full):
        return True

    if has_senior_years_requirement(full):
        log.debug(f"  exp-reject (YOE≥3, no entry signal): {job['title'][:80]}")
        return False

    return True

# ═════════════════════════════════════════════════════════════════════════════
# RESUME SCORING
# ═════════════════════════════════════════════════════════════════════════════

def load_resume() -> str:
    if RESUME_PATH.exists():
        text = RESUME_PATH.read_text(errors="ignore")
        log.info(f"Resume loaded ({len(text)} chars)")
        return text
    log.info("No resume.txt — using fallback keywords")
    return FALLBACK_RESUME

def build_scorer(resume_text: str):
    vectorizer = TfidfVectorizer(
        ngram_range=(1, 2),
        min_df=1,
        stop_words="english",
        max_features=8000,
    )
    vectorizer.fit([resume_text])

    def score(job_text: str) -> float:
        try:
            vecs = vectorizer.transform([resume_text, job_text])
            return float(round(cosine_similarity(vecs[0:1], vecs[1:2])[0][0], 4))
        except Exception:
            return 0.0

    return score

# ═════════════════════════════════════════════════════════════════════════════
# TELEGRAM
# ═════════════════════════════════════════════════════════════════════════════

def tg_send(text: str, dry_run: bool = False):
    if dry_run:
        print(text)
        return
    if not BOT_TOKEN or not CHAT_ID:
        log.warning("Telegram not configured — printing to stdout")
        print(text)
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={
                "chat_id":                  CHAT_ID,
                "text":                     text,
                "parse_mode":               "Markdown",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        r.raise_for_status()
    except Exception as e:
        log.error(f"Telegram error: {e}")

def build_digest(jobs: list[dict], since: datetime) -> list[str]:
    since_str = since.strftime("%b %d")
    now_str = datetime.now().strftime("%b %d, %Y")

    if not jobs:
        return [
            f"🦞 *Job Scout v5* — {now_str}\n"
            f"No new matching jobs since {since_str}."
        ]

    # Count by platform
    by_platform: dict[str, int] = {}
    for j in jobs:
        by_platform[j["publisher"]] = by_platform.get(j["publisher"], 0) + 1

    by_company: dict[str, list] = {}
    for j in jobs:
        by_company.setdefault(j["company"] or "Unknown", []).append(j)

    platform_summary = " · ".join(
        f"{p.capitalize()}: {c}" for p, c in sorted(by_platform.items())
    )

    header = (
        f"🦞 *Job Scout v5* — {now_str}\n"
        f"*{len(jobs)} new job{'s' if len(jobs)!=1 else ''}* "
        f"since {since_str} · {len(by_company)} companies\n"
        f"📡 {platform_summary}\n"
        f"{'─'*28}\n\n"
    )

    body = ""
    for company, cjobs in sorted(by_company.items()):
        body += f"🏢 *{company}*\n"
        for j in sorted(cjobs, key=lambda x: x["score"], reverse=True):
            pct = int(j["score"] * 100)
            loc = j["location"] or "US"
            via = j["publisher"].capitalize()
            body += (
                f"  • [{j['title']}]({j['url']})\n"
                f"    📍 {loc}  🎯 {pct}%  via {via}\n"
            )
        body += "\n"

    full = header + body
    chunks = []
    while len(full) > 4000:
        cut = full.rfind("\n", 0, 4000)
        if cut == -1:
            cut = 4000
        chunks.append(full[:cut])
        full = full[cut:]
    if full.strip():
        chunks.append(full)
    return chunks

# ═════════════════════════════════════════════════════════════════════════════
# PHASE 2 SHADOW COMPARISON — runs ATS-API path alongside Serper, logs only
# ═════════════════════════════════════════════════════════════════════════════

def shadow_compare(conn: sqlite3.Connection, score_fn, serper_matches: list[dict]):
    """Run the Tier-1 ATS-API fetch in parallel with Serper. Apply the same
    static filters, restrict to the last 24h, and log how the two feeds compare.
    Does NOT write to seen_jobs and does NOT send to Telegram."""
    from ats.fetch import fetch_all_ats, job_to_dict

    log.info("─" * 28)
    log.info("[shadow] Phase 2 ATS-API parallel fetch")
    try:
        ats_jobs = fetch_all_ats(conn)
    except Exception as e:
        log.warning(f"[shadow] ATS fetch failed entirely: {e}")
        return

    cutoff_24h = (datetime.now(timezone.utc) - timedelta(hours=24)).timestamp()
    rejects = {"date": 0, "title": 0, "location": 0, "sponsor": 0, "exp": 0, "score": 0, "no_ts": 0}
    ats_passed: list[dict] = []
    for j in ats_jobs:
        d = job_to_dict(j)
        # Strict 24h filter — treat unknown timestamp as "old" for shadow mode
        if not d["posted_ts"]:
            rejects["no_ts"] += 1
            continue
        if d["posted_ts"] < cutoff_24h:
            rejects["date"] += 1
            continue
        if not passes_title(d["title"]):
            rejects["title"] += 1
            continue
        if not passes_location(d):
            rejects["location"] += 1
            continue
        if not passes_sponsorship(d["description"]):
            rejects["sponsor"] += 1
            continue
        if not passes_experience(d):
            rejects["exp"] += 1
            continue
        s = score_fn(d["title"] + " " + d["description"])
        if s < MIN_SCORE:
            rejects["score"] += 1
            continue
        d["score"] = s
        ats_passed.append(d)

    serper_urls = {_canonical_url(j["url"]) for j in serper_matches}
    ats_urls    = {_canonical_url(j["url"]) for j in ats_passed}
    intersect   = serper_urls & ats_urls
    ats_only    = ats_urls    - serper_urls
    serper_only = serper_urls - ats_urls

    log.info(f"[shadow] ATS rejects — date:{rejects['date']} title:{rejects['title']} "
             f"location:{rejects['location']} sponsor:{rejects['sponsor']} "
             f"exp:{rejects['exp']} score:{rejects['score']} no_ts:{rejects['no_ts']}")
    log.info(f"[shadow] ATS-API would-send (24h, all filters): {len(ats_passed)}")
    log.info(f"[shadow] Serper actually sent:                  {len(serper_matches)}")
    log.info(f"[shadow]   intersection (both found):           {len(intersect)}")
    log.info(f"[shadow]   ATS-only (Serper missed):            {len(ats_only)}")
    log.info(f"[shadow]   Serper-only (ATS missed):            {len(serper_only)}")

    if ats_passed:
        log.info("[shadow] Top 5 ATS-API matches by score:")
        for d in sorted(ats_passed, key=lambda x: x["score"], reverse=True)[:5]:
            tag = "BOTH" if _canonical_url(d["url"]) in intersect else "ATS!"
            age_h = (datetime.now(timezone.utc).timestamp() - d["posted_ts"]) / 3600
            log.info(f"  [{tag}] {int(d['score']*100):>3}% {d['publisher']:>10} | "
                     f"{d['company'][:18]:<18} | {age_h:>4.1f}h | {d['title'][:55]}")


# ═════════════════════════════════════════════════════════════════════════════
# SHARED PIPELINE (reused by daily run() and weekly discovery.py)
# ═════════════════════════════════════════════════════════════════════════════

def process_and_send(
    jobs: list[dict],
    conn: sqlite3.Connection,
    score_fn,
    since: datetime,
    dry_run: bool = False,
) -> tuple[list[dict], list[dict], dict]:
    """Dedup → filter → score → dedup-vs-DB → digest → Telegram.
    Returns (new_matches, unique_jobs, stats)."""
    seen_ids: set[str] = set()
    unique_jobs: list[dict] = []
    for j in jobs:
        if j["id"] not in seen_ids:
            seen_ids.add(j["id"])
            unique_jobs.append(j)
    collisions = len(jobs) - len(unique_jobs)
    log.info(f"After dedup: {len(unique_jobs)} unique ({collisions} collisions)")

    new_matches: list[dict] = []
    stats = {"date": 0, "title": 0, "location": 0, "sponsor": 0, "exp": 0, "seen": 0, "score": 0}

    for job in unique_jobs:
        if not passes_date(job["posted_ts"], since):
            stats["date"] += 1
            continue
        if not passes_title(job["title"]):
            stats["title"] += 1
            continue
        if not passes_location(job):
            stats["location"] += 1
            continue
        if not passes_sponsorship(job["description"]):
            stats["sponsor"] += 1
            continue
        if not passes_experience(job):
            stats["exp"] += 1
            continue
        if not is_new(conn, job):
            stats["seen"] += 1
            continue
        s = score_fn(job["title"] + " " + job["description"])
        if s < MIN_SCORE:
            stats["score"] += 1
            continue
        job["score"] = s
        if not dry_run:
            mark_seen(conn, job)
        new_matches.append(job)

    log.info(
        f"Filter stats — date:{stats['date']} title:{stats['title']} "
        f"location:{stats['location']} sponsor:{stats['sponsor']} "
        f"exp:{stats['exp']} seen:{stats['seen']} score:{stats['score']}"
    )

    new_matches.sort(key=lambda j: j["score"], reverse=True)
    new_matches = new_matches[:MAX_DIGEST]
    log.info(f"New matches to send: {len(new_matches)}")

    for chunk in build_digest(new_matches, since):
        tg_send(chunk, dry_run=dry_run)
        time.sleep(0.5)

    return new_matches, unique_jobs, stats


# ═════════════════════════════════════════════════════════════════════════════
# MAIN
# ═════════════════════════════════════════════════════════════════════════════

def run(dry_run: bool = False, reset: bool = False, shadow: bool = False):
    log.info("═══ Job Scout v5 starting ═══")

    conn     = init_db(reset=reset)
    since    = load_last_run() if not reset else CUTOFF_DATE
    score_fn = build_scorer(load_resume())

    db_count_before = conn.execute("SELECT COUNT(*) FROM seen_jobs").fetchone()[0]
    log.info(f"DB contains {db_count_before} seen jobs")
    log.info(f"Fetching jobs posted since: {since.strftime('%Y-%m-%d %H:%M UTC')}")

    # ── Daily = native ATS APIs only (Tier 1 + Tier 2). ──
    # Tier 3 (iCIMS/Taleo/Jobvite/JazzHR/ADP) runs weekly from discovery.py.
    from ats.fetch import fetch_all_ats, job_to_dict
    try:
        ats_raw = fetch_all_ats(conn)
    except Exception as e:
        log.error(f"[ats] fetch failed entirely: {e}")
        ats_raw = []
    all_jobs = [job_to_dict(j) for j in ats_raw]
    log.info(f"Total jobs before filtering: {len(all_jobs)}")

    new_matches, unique_jobs, stats = process_and_send(
        all_jobs, conn, score_fn, since, dry_run=dry_run,
    )

    # ── Save run timestamp ──
    if not dry_run:
        try:
            save_last_run()
            log.info(f"State saved → {STATE_PATH}")
        except Exception as e:
            log.error(f"Failed to save state file {STATE_PATH}: {e}")
    else:
        log.info("(dry-run) skipping state save and DB writes")

    # ── Optional: retained shadow_compare() for ad-hoc parity debugging only ──
    if shadow:
        try:
            shadow_compare(conn, score_fn, new_matches)
        except Exception as e:
            log.warning(f"[shadow] comparison failed: {e}")

    db_count_after = conn.execute("SELECT COUNT(*) FROM seen_jobs").fetchone()[0]
    conn.close()

    log.info("═══ Run Summary ═══")
    log.info(f"  ATS-API raw:           {len(all_jobs)}")
    log.info(f"  After URL dedup:       {len(unique_jobs)}")
    log.info(f"  Dropped — date:        {stats['date']}")
    log.info(f"  Dropped — title:       {stats['title']}")
    log.info(f"  Dropped — location:    {stats['location']}  (US filter)")
    log.info(f"  Dropped — sponsorship: {stats['sponsor']}")
    log.info(f"  Dropped — experience:  {stats['exp']}")
    log.info(f"  Dropped — seen in DB:  {stats['seen']}")
    log.info(f"  Dropped — low score:   {stats['score']}")
    log.info(f"  Sent to Telegram:      {len(new_matches)}")
    log.info(f"  DB size (before/after): {db_count_before} / {db_count_after}")
    log.info("═══ Done ═══")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Job Scout v5 — Serper-powered ATS search")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print digest to terminal, don't send Telegram or update state")
    parser.add_argument("--reset", action="store_true",
                        help="Wipe seen-jobs DB, reset to hard cutoff date (2026-03-30)")
    parser.add_argument("--debug", action="store_true",
                        help="Verbose logging: print exact query strings and filter rejections")
    parser.add_argument("--shadow", action="store_true",
                        help="Re-run the ATS pipeline in shadow mode for ad-hoc parity debugging")
    args = parser.parse_args()
    if args.debug:
        log.setLevel(logging.DEBUG)
        for h in log.handlers:
            h.setLevel(logging.DEBUG)
        logging.getLogger().setLevel(logging.DEBUG)
    run(dry_run=args.dry_run, reset=args.reset, shadow=args.shadow)
