"""SmartRecruiters adapter — jobs.smartrecruiters.com/<Company>/<id>-<slug>

Listing API (paginated, no auth):
  GET https://api.smartrecruiters.com/v1/companies/<slug>/postings?limit=100&offset=N
Returns {"totalFound": N, "content": [{id, name, releasedDate, location, ...}]}

- `releasedDate` is ISO-8601 UTC.
- Listing does NOT include description — fetching per-job is too expensive
  for daily polling, so we leave `description=""` like Workday. The title
  filter + location + (weak) title-only scoring still apply.

Slug casing matters in the URL ("AbbVie" ≠ "abbvie") but the API accepts
either. We preserve the original casing from `parse_slug`.
"""
from __future__ import annotations
from datetime import datetime
from typing import Optional

from .base import ATSAdapter, Job, SlugInfo, SESSION, REQUEST_TIMEOUT


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
    if loc.get("fullLocation"):
        return loc["fullLocation"]
    parts = [loc.get("city") or "", loc.get("region") or "", loc.get("country") or ""]
    return ", ".join(p for p in parts if p)


class SmartRecruitersAdapter(ATSAdapter):
    platform    = "smartrecruiters"
    LIST_URL    = "https://api.smartrecruiters.com/v1/companies/{slug}/postings"
    JOB_URL_FMT = "https://jobs.smartrecruiters.com/{slug}/{ats_id}"
    PAGE_LIMIT  = 100
    MAX_PAGES   = 5   # 500/company is plenty for 24h freshness (sorted newest first)

    @classmethod
    def parse_slug(cls, url: str) -> Optional[SlugInfo]:
        if not cls._host(url).endswith("smartrecruiters.com"):
            return None
        parts = cls._path_parts(url)
        return SlugInfo(slug=parts[0]) if parts else None

    def list_jobs(self, slug: str, extra: Optional[dict] = None) -> list[Job]:
        out: list[Job] = []
        for page in range(self.MAX_PAGES):
            params = {"limit": self.PAGE_LIMIT, "offset": page * self.PAGE_LIMIT}
            r = SESSION.get(self.LIST_URL.format(slug=slug),
                            params=params, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            data = r.json()
            content = data.get("content") or []
            if not content:
                break
            for j in content:
                ats_id = str(j.get("id") or "")
                if not ats_id:
                    continue
                out.append(Job(
                    id          = f"{self.platform}:{slug}:{ats_id}",
                    title       = j.get("name", "") or "",
                    company     = slug,
                    location    = _fmt_location(j.get("location") or {}),
                    description = "",
                    url         = self.JOB_URL_FMT.format(slug=slug, ats_id=ats_id),
                    publisher   = self.platform,
                    posted_ts   = _parse_iso(j.get("releasedDate", "")),
                ))
            if len(content) < self.PAGE_LIMIT:
                break
        return out
