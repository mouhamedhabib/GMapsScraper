"""Shared timezone-aware discovery timestamp helpers."""

from datetime import datetime
from zoneinfo import ZoneInfo


LOCAL_TIMEZONE = ZoneInfo("Africa/Tunis")


def discovery_timestamp():
    """Return the current Tunisia time as an ISO 8601 timestamp."""
    return datetime.now(LOCAL_TIMEZONE).replace(microsecond=0).isoformat()


def parse_added_at(value):
    """Parse a valid timezone-aware ISO timestamp, otherwise return ``None``."""
    value = str(value or "").strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def earliest_added_at(values):
    """Return the original text of the earliest valid aware timestamp."""
    valid = []
    for value in values:
        parsed = parse_added_at(value)
        if parsed is not None:
            valid.append((parsed, str(value).strip()))
    return min(valid, key=lambda item: item[0])[1] if valid else ""
