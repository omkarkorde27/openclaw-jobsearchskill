"""ATS adapter base class + shared Job dataclass."""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlsplit

import requests


# Single Session reused across adapters for connection pooling.
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "job-scout/5.1 (+ats-native)"})

REQUEST_TIMEOUT = 20

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE  = re.compile(r"\s+")


def html_to_text(s: str) -> str:
    """Cheap HTML strip + entity decode. Greenhouse double-escapes their
    content (&lt;h2&gt;), so unescape first, then strip tags, then unescape
    again to catch any nested entities."""
    if not s:
        return ""
    s = html.unescape(s)
    s = _TAG_RE.sub(" ", s)
    s = html.unescape(s)
    return _WS_RE.sub(" ", s).strip()


@dataclass
class Job:
    """Normalized job posting from any ATS."""
    id:          str          # f"{platform}:{slug}:{ats_id}"  — globally unique
    title:       str
    company:     str
    location:    str
    description: str          # full text from API, not a Google snippet
    url:         str
    publisher:   str          # platform name, lowercased
    posted_ts:   int          # real epoch seconds; 0 only if API gave nothing


@dataclass
class SlugInfo:
    """What `parse_slug()` returns. `extra` carries platform-specific bits
    (e.g. Workday needs both tenant *and* board)."""
    slug:  str
    extra: dict = field(default_factory=dict)


class ATSAdapter:
    """Base class. Subclasses set `platform` and implement the methods."""
    platform: str = ""        # override in subclasses

    @classmethod
    def parse_slug(cls, url: str) -> Optional[SlugInfo]:
        """Extract company slug from a known-platform URL. Return None if the
        URL doesn't look like ours."""
        raise NotImplementedError

    def list_jobs(self, slug: str, extra: Optional[dict] = None) -> list[Job]:
        """Fetch all current jobs for one company. Phase 2."""
        raise NotImplementedError(f"{self.platform}.list_jobs() not implemented yet")

    @staticmethod
    def _host(url: str) -> str:
        try:
            return urlsplit(url).netloc.lower()
        except Exception:
            return ""

    @staticmethod
    def _path_parts(url: str) -> list[str]:
        try:
            return [p for p in urlsplit(url).path.split("/") if p]
        except Exception:
            return []
