"""Lever adapter — jobs.lever.co/<slug>/<uuid>

Listing API: https://api.lever.co/v0/postings/<slug>?mode=json
- No auth. Returns a top-level array of posting dicts.
- createdAt is epoch milliseconds.
"""
from __future__ import annotations
from typing import Optional

from .base import ATSAdapter, Job, SlugInfo, SESSION, REQUEST_TIMEOUT, html_to_text


class LeverAdapter(ATSAdapter):
    platform = "lever"
    LIST_URL = "https://api.lever.co/v0/postings/{slug}?mode=json"

    @classmethod
    def parse_slug(cls, url: str) -> Optional[SlugInfo]:
        if not cls._host(url).endswith("lever.co"):
            return None
        parts = cls._path_parts(url)
        return SlugInfo(slug=parts[0].lower()) if parts else None

    def list_jobs(self, slug: str, extra: Optional[dict] = None) -> list[Job]:
        r = SESSION.get(self.LIST_URL.format(slug=slug), timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        out: list[Job] = []
        for p in data:
            ats_id = p.get("id") or ""
            if not ats_id:
                continue
            created_ms = p.get("createdAt") or 0
            ts = int(created_ms // 1000) if created_ms else 0
            cats = p.get("categories") or {}
            location = cats.get("location", "") or ""
            description = (
                p.get("descriptionPlain")
                or html_to_text(p.get("description", ""))
            )
            out.append(Job(
                id          = f"{self.platform}:{slug}:{ats_id}",
                title       = p.get("text", "") or "",
                company     = slug,
                location    = location,
                description = description,
                url         = p.get("hostedUrl", "") or "",
                publisher   = self.platform,
                posted_ts   = ts,
            ))
        return out
