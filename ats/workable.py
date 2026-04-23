"""Workable adapter — two URL forms for the same company:
  apply.workable.com/<slug>[/j/<code>]                       — slug is path[0]
  jobs.workable.com/view/<uuid>/<title-slug>-at-<company>     — slug from "-at-<co>" suffix

Listing API (public widget): GET https://apply.workable.com/api/v1/widget/accounts/<slug>?details=true
Returns {"name": ..., "jobs": [{title, shortcode, description(HTML), published_on, country, city, state, url, ...}]}
- `published_on` is YYYY-MM-DD (no time-of-day). We pin to 12:00 UTC for the
  freshness filter — day-granular is fine for a 24h cutoff.
"""
from __future__ import annotations
import re
from datetime import datetime, timezone
from typing import Optional

from .base import ATSAdapter, Job, SlugInfo, SESSION, REQUEST_TIMEOUT, html_to_text

_AT_SUFFIX_RE = re.compile(r"-at-([a-z0-9][a-z0-9\-]*)/?$")


def _parse_ymd_noon(s: str) -> int:
    """YYYY-MM-DD → epoch seconds anchored at 12:00 UTC. 0 if unparseable."""
    if not s:
        return 0
    try:
        d = datetime.strptime(s[:10], "%Y-%m-%d").replace(
            hour=12, minute=0, second=0, tzinfo=timezone.utc
        )
        return int(d.timestamp())
    except Exception:
        return 0


def _fmt_location(j: dict) -> str:
    parts = [j.get("city") or "", j.get("state") or "", j.get("country") or ""]
    joined = ", ".join(p for p in parts if p)
    if joined:
        return joined
    locs = j.get("locations") or []
    if locs and isinstance(locs, list):
        l0 = locs[0]
        sub = [l0.get("city") or "", l0.get("region") or "", l0.get("country") or ""]
        return ", ".join(p for p in sub if p)
    return ""


class WorkableAdapter(ATSAdapter):
    platform = "workable"
    LIST_URL = "https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true"

    @classmethod
    def parse_slug(cls, url: str) -> Optional[SlugInfo]:
        host = cls._host(url)
        if not host.endswith("workable.com"):
            return None
        parts = cls._path_parts(url)
        if host.startswith("apply.") and parts:
            return SlugInfo(slug=parts[0].lower())
        if host.startswith("jobs.") and parts and parts[0] == "view" and len(parts) >= 3:
            m = _AT_SUFFIX_RE.search(parts[-1].lower())
            if m:
                return SlugInfo(slug=m.group(1))
        return None

    def list_jobs(self, slug: str, extra: Optional[dict] = None) -> list[Job]:
        r = SESSION.get(self.LIST_URL.format(slug=slug), timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        out: list[Job] = []
        for j in data.get("jobs", []) or []:
            code = j.get("shortcode") or ""
            if not code:
                continue
            out.append(Job(
                id          = f"{self.platform}:{slug}:{code}",
                title       = j.get("title", "") or "",
                company     = slug,
                location    = _fmt_location(j),
                description = html_to_text(j.get("description", "")),
                url         = j.get("url") or j.get("shortlink") or f"https://apply.workable.com/j/{code}",
                publisher   = self.platform,
                posted_ts   = _parse_ymd_noon(j.get("published_on") or j.get("created_at") or ""),
            ))
        return out
