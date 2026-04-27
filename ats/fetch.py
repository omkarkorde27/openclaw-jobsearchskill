"""Phase 2 fetch orchestrator: poll every Tier-1 company in `companies`
and return a merged Job list. Per-company errors are isolated so one bad
tenant can't break the whole run.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from typing import Iterable

from . import REGISTRY
from .base import Job

log = logging.getLogger("scout.ats")

TIER1 = ("greenhouse", "lever", "ashby", "workday")
TIER2 = ("smartrecruiters", "workable", "breezy")
DAILY = TIER1 + TIER2


def fetch_all_ats(conn: sqlite3.Connection,
                  platforms: Iterable[str] = DAILY,
                  sleep_s: float = 0.3) -> list[Job]:
    plats = tuple(platforms)
    placeholders = ",".join(["?"] * len(plats))
    rows = list(conn.execute(
        f"SELECT platform, slug, extra FROM companies "
        f"WHERE platform IN ({placeholders}) AND COALESCE(last_status,'') != 'gone'",
        plats,
    ))
    log.info(f"[ats] polling {len(rows)} companies across {len(set(r[0] for r in rows))} platforms")
    out: list[Job] = []
    now_iso = datetime.now(timezone.utc).isoformat()
    counts: dict[str, int] = {}
    fails:  dict[str, int] = {}

    for platform, slug, extra_json in rows:
        adapter_cls = REGISTRY.get(platform)
        if not adapter_cls:
            continue
        adapter = adapter_cls()
        extra   = json.loads(extra_json) if extra_json else None
        status  = "ok"
        try:
            jobs = adapter.list_jobs(slug, extra)
            out.extend(jobs)
            counts[platform] = counts.get(platform, 0) + len(jobs)
            if not jobs:
                status = "no_jobs"
        except Exception as e:
            status = f"err:{type(e).__name__}"
            fails[platform] = fails.get(platform, 0) + 1
            log.debug(f"  {platform}/{slug}: {e}")
        conn.execute(
            "UPDATE companies "
            "   SET last_polled = ?,"
            "       last_status = ?,"
            "       fail_count  = CASE WHEN ?='ok' THEN 0 ELSE fail_count+1 END "
            " WHERE platform = ? AND slug = ?",
            (now_iso, status, status, platform, slug),
        )
        time.sleep(sleep_s)
    conn.commit()
    log.info(f"[ats] fetched {len(out)} jobs total: {dict(sorted(counts.items()))}")
    if fails:
        log.warning(f"[ats] failures by platform: {dict(sorted(fails.items()))}")
    return out


def job_to_dict(j: Job) -> dict:
    """Convert dataclass to the dict shape the existing filter pipeline expects."""
    return {
        "id":          j.id,
        "title":       j.title,
        "company":     j.company,
        "location":    j.location,
        "description": j.description,
        "url":         j.url,
        "publisher":   j.publisher,
        "posted_ts":   j.posted_ts,
        "score":       0.0,
    }
