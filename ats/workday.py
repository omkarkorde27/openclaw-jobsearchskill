"""Workday adapter — <tenant>.wdN.myworkdayjobs.com/<lang>/<board>/job/...

Listing API (POST):
  https://<tenant>.<wd_zone>.myworkdayjobs.com/wday/cxs/<tenant>/<board>/jobs

Body: {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""}
Returns: {"total": N, "jobPostings": [{title, externalPath, locationsText, postedOn, ...}]}

Workday's postedOn field is a human string ("Posted Today", "Posted Yesterday",
"Posted N Days Ago", "Posted N+ Days Ago"). We translate it to an approximate
epoch timestamp; precision-to-the-day is enough for the 24h freshness filter.
"""
from __future__ import annotations
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from .base import ATSAdapter, Job, SlugInfo, SESSION, REQUEST_TIMEOUT

_HOST_RE   = re.compile(r"^([a-z0-9][a-z0-9\-]*)\.(wd\d+)\.myworkdayjobs\.com$")
_LOCALE_RE = re.compile(r"^[a-z]{2}-[A-Z]{2}$")
_POSTED_RE = re.compile(r"posted\s+(today|yesterday|(\d+)\s*\+?\s*days?\s+ago)", re.I)


def _parse_posted_on(s: str) -> int:
    """Workday's fuzzy 'postedOn' → approximate epoch seconds. Returns 0 if unparseable."""
    if not s:
        return 0
    m = _POSTED_RE.search(s)
    if not m:
        return 0
    now = datetime.now(timezone.utc)
    label = m.group(1).lower()
    if "today" in label:
        return int(now.replace(hour=12, minute=0, second=0, microsecond=0).timestamp())
    if "yesterday" in label:
        return int((now - timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0).timestamp())
    days = int(m.group(2))
    return int((now - timedelta(days=days)).replace(hour=12, minute=0, second=0, microsecond=0).timestamp())


class WorkdayAdapter(ATSAdapter):
    platform   = "workday"
    PAGE_LIMIT = 20
    MAX_PAGES  = 5  # 100 jobs/tenant — plenty for "last 24h" since results are date-sorted

    @classmethod
    def parse_slug(cls, url: str) -> Optional[SlugInfo]:
        host = cls._host(url)
        m = _HOST_RE.match(host)
        if not m:
            return None
        tenant, wd_zone = m.group(1), m.group(2)
        parts = cls._path_parts(url)
        board = ""
        if parts:
            if _LOCALE_RE.match(parts[0]) and len(parts) > 1:
                board = parts[1]
            else:
                board = parts[0]
        if not board:
            return None
        return SlugInfo(slug=tenant, extra={"board": board, "wd_zone": wd_zone})

    def list_jobs(self, slug: str, extra: Optional[dict] = None) -> list[Job]:
        if not extra or "board" not in extra or "wd_zone" not in extra:
            raise ValueError(f"workday/{slug} missing board/wd_zone in extra")
        board   = extra["board"]
        wd_zone = extra["wd_zone"]
        url = f"https://{slug}.{wd_zone}.myworkdayjobs.com/wday/cxs/{slug}/{board}/jobs"
        out: list[Job] = []
        for page in range(self.MAX_PAGES):
            body = {
                "appliedFacets": {},
                "limit":         self.PAGE_LIMIT,
                "offset":        page * self.PAGE_LIMIT,
                "searchText":    "",
            }
            r = SESSION.post(url, json=body, timeout=REQUEST_TIMEOUT,
                             headers={"Accept": "application/json"})
            r.raise_for_status()
            data = r.json()
            postings = data.get("jobPostings") or []
            if not postings:
                break
            for j in postings:
                ext_path = j.get("externalPath", "") or ""
                ats_id   = ext_path.rsplit("/", 1)[-1] if ext_path else ""
                if not ats_id:
                    continue
                ts = _parse_posted_on(j.get("postedOn", ""))
                full_url = f"https://{slug}.{wd_zone}.myworkdayjobs.com/{board}{ext_path}"
                out.append(Job(
                    id          = f"{self.platform}:{slug}:{ats_id}",
                    title       = j.get("title", "") or "",
                    company     = slug,
                    location    = j.get("locationsText", "") or "",
                    description = "",   # Workday hides body; would need a per-job GET. Skip for shadow mode.
                    url         = full_url,
                    publisher   = self.platform,
                    posted_ts   = ts,
                ))
            # Stop early once we've gone past the 24h boundary (Workday returns newest-first).
            if ts and ts < (datetime.now(timezone.utc) - timedelta(days=2)).timestamp():
                break
            if len(postings) < self.PAGE_LIMIT:
                break
        return out
