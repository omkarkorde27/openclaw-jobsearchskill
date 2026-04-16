"""Breezy adapter — <slug>.breezy.hr/p/...   (slug from subdomain)"""
from __future__ import annotations
import re
from typing import Optional

from .base import ATSAdapter, SlugInfo

_HOST_RE = re.compile(r"^([a-z0-9][a-z0-9\-]*)\.breezy\.hr$")


class BreezyAdapter(ATSAdapter):
    platform = "breezy"

    @classmethod
    def parse_slug(cls, url: str) -> Optional[SlugInfo]:
        m = _HOST_RE.match(cls._host(url))
        return SlugInfo(slug=m.group(1)) if m else None
