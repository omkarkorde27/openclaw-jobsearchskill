"""ATS adapters package.

Each `<platform>.py` module defines an `Adapter` subclass of `ATSAdapter`
in `base.py`. `REGISTRY` maps platform name → adapter class so callers
(`discovery.py`, the future `fetch.py`) can dispatch by name.
"""
from .base import Job, ATSAdapter
from .greenhouse      import GreenhouseAdapter
from .lever           import LeverAdapter
from .ashby           import AshbyAdapter
from .workday         import WorkdayAdapter
from .smartrecruiters import SmartRecruitersAdapter
from .workable        import WorkableAdapter
from .breezy          import BreezyAdapter

REGISTRY: dict[str, type[ATSAdapter]] = {
    a.platform: a for a in (
        GreenhouseAdapter, LeverAdapter, AshbyAdapter, WorkdayAdapter,
        SmartRecruitersAdapter, WorkableAdapter, BreezyAdapter,
    )
}

__all__ = ["Job", "ATSAdapter", "REGISTRY"]
