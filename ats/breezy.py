"""Breezy adapter — <slug>.breezy.hr/p/...   (slug from subdomain)

Listing API: GET https://<slug>.breezy.hr/json
- No auth. Returns an array of posting objects.
- `published_date` is ISO-8601 UTC.
- The listing endpoint does NOT include description text — postings here
  are title/location-only. Downstream scoring falls back to the title,
  same as Workday.
"""
from __future__ import annotations
import re
from datetime import datetime
from typing import Optional

from .base import ATSAdapter, Job, SlugInfo, SESSION, REQUEST_TIMEOUT

_HOST_RE = re.compile(r"^([a-z0-9][a-z0-9\-]*)\.breezy\.hr$")


def _parse_iso(s: str) -> int:
    if not s:
        return 0
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except Exception:
        return 0


def _fmt_location(loc: dict) -> str:
    if not loc:
        return ""
    if loc.get("name"):
        return loc["name"]
    parts = [
        loc.get("city") or "",
        (loc.get("state") or {}).get("name") if isinstance(loc.get("state"), dict) else loc.get("state") or "",
        (loc.get("country") or {}).get("name") if isinstance(loc.get("country"), dict) else loc.get("country") or "",
    ]
    return ", ".join(p for p in parts if p)


class BreezyAdapter(ATSAdapter):
    platform = "breezy"
    LIST_URL = "https://{slug}.breezy.hr/json"

    @classmethod
    def parse_slug(cls, url: str) -> Optional[SlugInfo]:
        m = _HOST_RE.match(cls._host(url))
        return SlugInfo(slug=m.group(1)) if m else None

    def list_jobs(self, slug: str, extra: Optional[dict] = None) -> list[Job]:
        r = SESSION.get(self.LIST_URL.format(slug=slug), timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        out: list[Job] = []
        for j in data or []:
            ats_id = j.get("id") or j.get("friendly_id") or ""
            if not ats_id:
                continue
            out.append(Job(
                id          = f"{self.platform}:{slug}:{ats_id}",
                title       = j.get("name", "") or "",
                company     = slug,
                location    = _fmt_location(j.get("location") or {}),
                description = "",
                url         = j.get("url", "") or "",
                publisher   = self.platform,
                posted_ts   = _parse_iso(j.get("published_date", "") or j.get("creation_date", "")),
            ))
        return out
