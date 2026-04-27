#!/usr/bin/env python3
"""
discovery.py — weekly Serper job. Two responsibilities:

  1. Slug discovery (Tier 1+2): run Serper site: queries on greenhouse,
     lever, ashby, workday, smartrecruiters, workable, breezy. Parse each
     result URL, upsert the (platform, slug) into `companies`. Feeds the
     daily ATS-API fetcher.

  2. Tier 3 job fetch: run Serper queries on iCIMS / Taleo / Jobvite / JazzHR
     / ADP. Those platforms have no practical public listing API, so we
     keep them on Serper at weekly cadence. Results flow through the same
     filter/score/dedup pipeline and produce a Telegram digest.

Usage:
  python3 discovery.py                  # slug discovery + tier3 digest + bootstrap
  python3 discovery.py --bootstrap      # bootstrap from seen_jobs URLs only
  python3 discovery.py --serper         # slug discovery via Serper only (no digest)
  python3 discovery.py --tier3          # tier3 job fetch + digest only
  python3 discovery.py --dry-run        # no DB writes, no Telegram send

Cron target: weekly, e.g. Sun 04:00.
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

from job_scout import (
    init_db,
    fetch_serper,
    parse_serper_result,
    ATS_DOMAINS,
    SERPER_KEY,
    build_scorer,
    load_resume,
    process_and_send,
)
from ats import REGISTRY, ATSAdapter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
)
log = logging.getLogger("discovery")


# ─── Slug extraction ────────────────────────────────────────────────────────

def slug_from_url(url: str) -> tuple[str, str, dict] | None:
    """Try every adapter; first hit wins. Returns (platform, slug, extra)."""
    if not url:
        return None
    for adapter_cls in REGISTRY.values():
        info = adapter_cls.parse_slug(url)
        if info and info.slug:
            return adapter_cls.platform, info.slug, info.extra
    return None


# ─── DB upsert ──────────────────────────────────────────────────────────────

def upsert(conn: sqlite3.Connection, platform: str, slug: str, extra: dict,
           dry_run: bool = False) -> bool:
    """Insert if new. Returns True if a new row was added."""
    row = conn.execute(
        "SELECT 1 FROM companies WHERE platform=? AND slug=?",
        (platform, slug),
    ).fetchone()
    if row:
        return False
    if dry_run:
        return True
    conn.execute(
        """INSERT INTO companies
             (platform, slug, extra, discovered_at, last_polled, last_status, fail_count)
           VALUES (?, ?, ?, ?, NULL, NULL, 0)""",
        (
            platform, slug,
            json.dumps(extra) if extra else None,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    return True


# ─── Mode A: bootstrap from existing seen_jobs ──────────────────────────────

def bootstrap_from_seen_jobs(conn: sqlite3.Connection, dry_run: bool) -> dict:
    """Walk seen_jobs.url rows, parse each URL, upsert into companies."""
    added: dict[str, int] = {}
    skipped_unknown = 0
    rows = list(conn.execute("SELECT url FROM seen_jobs WHERE url IS NOT NULL"))
    log.info(f"[bootstrap] scanning {len(rows)} existing seen_jobs URLs")
    for (url,) in rows:
        hit = slug_from_url(url)
        if hit is None:
            skipped_unknown += 1
            continue
        platform, slug, extra = hit
        if upsert(conn, platform, slug, extra, dry_run=dry_run):
            added[platform] = added.get(platform, 0) + 1
    if not dry_run:
        conn.commit()
    log.info(f"[bootstrap] +{sum(added.values())} new companies "
             f"(by platform: {dict(sorted(added.items()))}); "
             f"skipped {skipped_unknown} URLs from non-Tier-1/2 ATSes")
    return added


# ─── Mode B: Serper slug discovery (Tier 1+2) ───────────────────────────────

# These are the platforms whose slugs feed the daily ATS-API fetcher.
# Kept here (not in job_scout.ATS_DOMAINS) because job_scout no longer
# queries them at all — they're ATS-native now.
SLUG_DISCOVERY_DOMAINS = [
    {"domain": "boards.greenhouse.io",     "platform": "greenhouse"},
    {"domain": "jobs.lever.co",            "platform": "lever"},
    {"domain": "jobs.ashbyhq.com",         "platform": "ashby"},
    {"domain": "myworkdayjobs.com",        "platform": "workday"},
    {"domain": "jobs.smartrecruiters.com", "platform": "smartrecruiters"},
    {"domain": "workable.com",             "platform": "workable"},
    {"domain": "breezy.hr",                "platform": "breezy"},
]


def discover_via_serper(conn: sqlite3.Connection, dry_run: bool) -> dict:
    """Run Serper slug queries for every Tier 1+2 platform and upsert slugs."""
    if not SERPER_KEY:
        log.warning("[serper] SERPER_API_KEY not set; skipping serper discovery")
        return {}
    added: dict[str, int] = {}
    for entry in SLUG_DISCOVERY_DOMAINS:
        adapter_cls = REGISTRY[entry["platform"]]
        log.info(f"[serper] querying {entry['platform']} ({entry['domain']})")
        results = fetch_serper(entry["domain"], entry["platform"].capitalize())
        for r in results:
            url = r.get("link", "")
            info = adapter_cls.parse_slug(url)
            if not info or not info.slug:
                continue
            if upsert(conn, adapter_cls.platform, info.slug, info.extra,
                      dry_run=dry_run):
                added[adapter_cls.platform] = added.get(adapter_cls.platform, 0) + 1
        time.sleep(0.5)
    if not dry_run:
        conn.commit()
    log.info(f"[serper] +{sum(added.values())} new companies "
             f"(by platform: {dict(sorted(added.items()))})")
    return added


# ─── Mode C: Tier 3 job fetch + digest ─────────────────────────────────────

def tier3_digest(conn: sqlite3.Connection, dry_run: bool) -> int:
    """Fetch jobs from Tier 3 ATSes via Serper, run the same filter+score
    pipeline as the daily run, and send a Telegram digest. Returns count sent.

    Tier 3 (iCIMS/Taleo/Jobvite/JazzHR/ADP) has no cheap native listing API,
    so we pay for Google's index at weekly cadence."""
    if not SERPER_KEY:
        log.warning("[tier3] SERPER_API_KEY not set; skipping tier-3 digest")
        return 0
    all_jobs: list[dict] = []
    log.info(f"[tier3] querying {len(ATS_DOMAINS)} Tier-3 domains via Serper")
    for ats in ATS_DOMAINS:
        results = fetch_serper(ats["domain"], ats["platform"])
        for r in results:
            job = parse_serper_result(r, ats["platform"])
            if job:
                all_jobs.append(job)
        time.sleep(1.0)
    log.info(f"[tier3] parsed {len(all_jobs)} jobs")

    # Weekly cadence → 7-day freshness window.
    since = datetime.now(timezone.utc) - timedelta(days=7)
    score_fn = build_scorer(load_resume())
    new_matches, _, _ = process_and_send(
        all_jobs, conn, score_fn, since, dry_run=dry_run,
    )
    return len(new_matches)


# ─── CLI ────────────────────────────────────────────────────────────────────

def summarize(conn: sqlite3.Connection) -> None:
    log.info("─" * 60)
    log.info("Companies table state:")
    rows = list(conn.execute(
        "SELECT platform, COUNT(*) FROM companies GROUP BY platform ORDER BY platform"
    ))
    total = 0
    for platform, cnt in rows:
        log.info(f"  {platform:<18} {cnt:>4}")
        total += cnt
    log.info(f"  {'TOTAL':<18} {total:>4}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Weekly Serper job: slug discovery + Tier 3 digest")
    ap.add_argument("--bootstrap", action="store_true",
                    help="Seed companies table from existing seen_jobs URLs (no network)")
    ap.add_argument("--serper", action="store_true",
                    help="Tier 1+2 slug discovery via Serper (uses API quota)")
    ap.add_argument("--tier3", action="store_true",
                    help="Run Tier 3 job fetch via Serper and send digest")
    ap.add_argument("--dry-run", action="store_true",
                    help="Don't write to DB and don't send Telegram")
    args = ap.parse_args()

    any_mode_picked = args.bootstrap or args.serper or args.tier3
    do_bootstrap = args.bootstrap or not any_mode_picked
    do_serper    = args.serper    or not any_mode_picked
    do_tier3     = args.tier3     or not any_mode_picked

    conn = init_db(reset=False)
    if do_bootstrap:
        bootstrap_from_seen_jobs(conn, dry_run=args.dry_run)
    if do_serper:
        discover_via_serper(conn, dry_run=args.dry_run)
    if do_tier3:
        tier3_digest(conn, dry_run=args.dry_run)
    summarize(conn)
    conn.close()


if __name__ == "__main__":
    sys.exit(main())
