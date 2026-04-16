"""Workable adapter — two URL forms for the same company:
  apply.workable.com/<slug>[/j/<code>]                       — slug is path[0]
  jobs.workable.com/view/<uuid>/<title-slug>-at-<company>     — slug from "-at-<co>" suffix

We only attempt the second form when the first won't match. Slugs are
lowercased so both forms collapse to the same companies-table row.
"""
from __future__ import annotations
import re
from typing import Optional

from .base import ATSAdapter, SlugInfo

_AT_SUFFIX_RE = re.compile(r"-at-([a-z0-9][a-z0-9\-]*)/?$")


class WorkableAdapter(ATSAdapter):
    platform = "workable"

    @classmethod
    def parse_slug(cls, url: str) -> Optional[SlugInfo]:
        host = cls._host(url)
        if not host.endswith("workable.com"):
            return None
        parts = cls._path_parts(url)
        # apply.workable.com/<slug>...
        if host.startswith("apply.") and parts:
            return SlugInfo(slug=parts[0].lower())
        # jobs.workable.com/view/<uuid>/<title>-at-<company>
        if host.startswith("jobs.") and parts and parts[0] == "view" and len(parts) >= 3:
            m = _AT_SUFFIX_RE.search(parts[-1].lower())
            if m:
                return SlugInfo(slug=m.group(1))
        return None
