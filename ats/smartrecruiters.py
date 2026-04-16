"""SmartRecruiters adapter — jobs.smartrecruiters.com/<Company>/<id>-<slug>

Note: SmartRecruiters slugs are case-sensitive in the URL ("Visa" not
"visa"). The API accepts either, but we preserve the URL casing so the
display matches what the user clicks.
"""
from __future__ import annotations
from typing import Optional

from .base import ATSAdapter, SlugInfo


class SmartRecruitersAdapter(ATSAdapter):
    platform = "smartrecruiters"

    @classmethod
    def parse_slug(cls, url: str) -> Optional[SlugInfo]:
        if not cls._host(url).endswith("smartrecruiters.com"):
            return None
        parts = cls._path_parts(url)
        return SlugInfo(slug=parts[0]) if parts else None
