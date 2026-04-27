"""Greenhouse adapter — boards.greenhouse.io/<slug>/jobs/<id>

Listing API: https://boards-api.greenhouse.io/v1/boards/<slug>/jobs?content=true
- No auth, returns full job list with HTML descriptions and updated_at timestamps.
- updated_at is ISO-8601 in UTC.
"""
from __future__ import annotations
from datetime import datetime
from typing import Optional
from urllib.parse import parse_qs, urlsplit

from .base import ATSAdapter, Job, SlugInfo, SESSION, REQUEST_TIMEOUT, html_to_text


class GreenhouseAdapter(ATSAdapter):
    platform = "greenhouse"
    LIST_URL = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"

    @classmethod
    def parse_slug(cls, url: str) -> Optional[SlugInfo]:
        host  = cls._host(url)
        parts = cls._path_parts(url)
        if not host.endswith("greenhouse.io"):
            return None
        if parts[:2] == ["embed", "job_app"]:
            qs = parse_qs(urlsplit(url).query)
            for_v = qs.get("for", [""])[0].strip().lower()
            return SlugInfo(slug=for_v) if for_v else None
        if parts:
            return SlugInfo(slug=parts[0].lower())
        return None

    def list_jobs(self, slug: str, extra: Optional[dict] = None) -> list[Job]:
        r = SESSION.get(self.LIST_URL.format(slug=slug), timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        out: list[Job] = []
        for j in data.get("jobs", []):
            ats_id = str(j.get("id", ""))
            if not ats_id:
                continue
            updated_at = j.get("updated_at") or ""
            try:
                ts = int(datetime.fromisoformat(updated_at.replace("Z", "+00:00")).timestamp())
            except Exception:
                ts = 0
            location = (j.get("location") or {}).get("name", "") or ""
            out.append(Job(
                id          = f"{self.platform}:{slug}:{ats_id}",
                title       = j.get("title", "") or "",
                company     = slug,
                location    = location,
                description = html_to_text(j.get("content", "")),
                url         = j.get("absolute_url", "") or "",
                publisher   = self.platform,
                posted_ts   = ts,
            ))
        return out
