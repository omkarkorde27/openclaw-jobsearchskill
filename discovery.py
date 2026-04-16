#!/usr/bin/env python3
"""
discovery.py — find ATS company slugs and persist them to `companies` table.

Two modes (combine or pick one):

  --bootstrap   Scan existing `seen_jobs.url` rows. Free, no network.
  --serper      Run the existing Serper site:<ats> queries to find new slugs.

Default (no flags) = both modes.

Usage:
  python3 discovery.py                  # bootstrap + serper (full sync)
  python3 discovery.py --bootstrap      # bootstrap only (no API calls)
  python3 discovery.py --serper         # serper only
  python3 discovery.py --dry-run        # show would-be inserts, don't write

This is the seed for the daily ATS-API fetch path. After Phase 1 it can run
weekly via cron; Phase 2 will add the daily fetch script that consumes
`companies`.
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import time
from datetime import datetime, timezone

from job_scout import (
    init_db,
    fetch_serper,
    parse_serper_result,
    ATS_DOMAINS,
    SERPER_KEY,
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


# ─── Mode B: Serper discovery ───────────────────────────────────────────────

# Map Serper-side platform names to adapter `platform` keys.
SERPER_TO_ADAPTER = {
    "Greenhouse":      "greenhouse",
    "Lever":           "lever",
    "Ashby":           "ashby",
    "Workday":         "workday",
    "SmartRecruiters": "smartrecruiters",
    "Workable":        "workable",
    "Breezy":          "breezy",
}


def discover_via_serper(conn: sqlite3.Connection, dry_run: bool) -> dict:
    """Run the existing Serper queries; parse slugs from result URLs."""
    if not SERPER_KEY:
        log.warning("[serper] SERPER_API_KEY not set; skipping serper discovery")
        return {}
    added: dict[str, int] = {}
    for ats in ATS_DOMAINS:
        adapter_key = SERPER_TO_ADAPTER.get(ats["platform"])
        if not adapter_key:
            log.debug(f"[serper] skipping Tier-3 platform {ats['platform']}")
            continue
        adapter_cls = REGISTRY[adapter_key]
        log.info(f"[serper] querying {ats['platform']} ({ats['domain']})")
        results = fetch_serper(ats["domain"], ats["platform"])
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
    ap = argparse.ArgumentParser(description="Phase 1: ATS slug discovery + seeding")
    ap.add_argument("--bootstrap", action="store_true",
                    help="Seed from existing seen_jobs URLs (no network)")
    ap.add_argument("--serper", action="store_true",
                    help="Run Serper queries to discover slugs (uses API quota)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Don't write to DB; show what would be added")
    args = ap.parse_args()

    do_bootstrap = args.bootstrap or not (args.bootstrap or args.serper)
    do_serper    = args.serper    or not (args.bootstrap or args.serper)

    conn = init_db(reset=False)
    if do_bootstrap:
        bootstrap_from_seen_jobs(conn, dry_run=args.dry_run)
    if do_serper:
        discover_via_serper(conn, dry_run=args.dry_run)
    summarize(conn)
    conn.close()


if __name__ == "__main__":
    sys.exit(main())
