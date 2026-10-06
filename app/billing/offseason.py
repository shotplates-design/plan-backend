"""Offseason handling — STUB.

BILLING_OFFSEASON_MODE selects the intended strategy; nothing is enforced yet:
  none        — packs sold year-round; purchased tries never expire (current behaviour)
  pause       — future: hide/disable pack purchases Feb-Aug
  season_pass — future: an extra one-time "Season Pass" (unlimited until Super Bowl) via
                usage_accounts.unlimited_until
"""

from __future__ import annotations

from typing import Any

from app.config import get_settings

_NOTES = {
    "none": "Packs are sold year-round; purchased tries do not expire.",
    "pause": "Planned: pause pack sales in the offseason. Not implemented; packs remain on sale.",
    "season_pass": "Planned: one-time Season Pass (unlimited for the season). Not implemented.",
}


def offseason_policy() -> dict[str, Any]:
    mode = get_settings().billing_offseason_mode
    return {"mode": mode, "implemented": mode == "none", "note": _NOTES[mode]}
