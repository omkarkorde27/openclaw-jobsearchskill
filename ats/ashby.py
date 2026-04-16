"""Ashby adapter — jobs.ashbyhq.com/<slug>[/<uuid>]

Listing API: https://api.ashbyhq.com/posting-api/job-board/<slug>?includeCompensation=false
- No auth. Returns {"jobs": [...]}.
- publishedAt is ISO-8601 UTC.
"""
from __future__ import annotations
from datetime import datetime
from typing import Optional

from .base import ATSAdapter, Job, SlugInfo, SESSION, REQUEST_TIMEOUT, html_to_text


class AshbyAdapter(ATSAdapter):
    platform = "ashby"
    LIST_URL = "https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=false"

    @classmethod
    def parse_slug(cls, url: str) -> Optional[SlugInfo]:
        if not cls._host(url).endswith("ashbyhq.com"):
            return None
        parts = cls._path_parts(url)
        return SlugInfo(slug=parts[0].lower()) if parts else None

    def list_jobs(self, slug: str, extra: Optional[dict] = None) -> list[Job]:
        r = SESSION.get(self.LIST_URL.format(slug=slug), timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        out: list[Job] = []
        for j in data.get("jobs", []):
            ats_id = j.get("id") or ""
            if not ats_id:
                continue
            published = j.get("publishedAt") or ""
            try:
                ts = int(datetime.fromisoformat(published.replace("Z", "+00:00")).timestamp())
            except Exception:
                ts = 0
            location = j.get("location") or ""
            description = (
                j.get("descriptionPlain")
                or html_to_text(j.get("descriptionHtml", ""))
            )
            out.append(Job(
                id          = f"{self.platform}:{slug}:{ats_id}",
                title       = j.get("title", "") or "",
                company     = slug,
                location    = location,
                description = description,
                url         = j.get("jobUrl", "") or "",
                publisher   = self.platform,
                posted_ts   = ts,
            ))
        return out
