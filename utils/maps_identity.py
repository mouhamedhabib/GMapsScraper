"""Canonical Google Maps place identities shared by discovery and lead building."""

import re
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit


PLACE_DATA_ID = re.compile(r"!1s([^!/?&#]+)", re.IGNORECASE)


def _clean(value):
    return " ".join((value or "").split()).strip()


def normalize_place_id(value):
    """Extract a stable Google place/cid identity when one is present."""
    value = _clean(value)
    if not value:
        return ""
    lowered = value.casefold()
    if lowered.startswith(("place_id:", "cid:", "data:")):
        return lowered
    if all(marker not in value for marker in ("://", "?", "!", "/")):
        return "place_id:" + lowered
    parsed = urlsplit(value if "://" in value else "//" + value)
    query = parse_qs(parsed.query)
    for key in ("place_id", "query_place_id", "cid"):
        candidate = _clean((query.get(key) or [""])[0])
        if candidate:
            identity_type = "place_id" if key in {"place_id", "query_place_id"} else key
            return f"{identity_type}:{candidate.casefold()}"
    match = PLACE_DATA_ID.search(value)
    if match:
        candidate = unquote(match.group(1)).casefold()
        return ("place_id:" if candidate.startswith("chij") else "data:") + candidate
    return ""


def normalize_place_url(value):
    """Normalize an exact Maps place URL without treating its name as identity."""
    value = _clean(value)
    if not value:
        return ""
    parsed = urlsplit(value if "://" in value else "//" + value)
    host = (parsed.hostname or "").casefold()
    lowered_path = parsed.path.casefold()
    if (
        "google." not in host
        or "/maps/" not in lowered_path
        or "/place/" not in lowered_path
        or ("/@" not in lowered_path and "/data=" not in lowered_path)
    ):
        return ""
    path = unquote(parsed.path).rstrip("/")
    return urlunsplit(("https", "google.com", path, "", "")) if path else ""


def maps_identities_for(row):
    """Return the canonical place-id token and normalized place URL for a row."""
    maps_url = _clean(
        row.get("map_link") or row.get("place_url") or row.get("maps_url")
    )
    audit_identity = _clean(row.get("maps_identity"))
    explicit_place_id = _clean(row.get("place_id") or row.get("map_place_id"))
    place_id = normalize_place_id(explicit_place_id or maps_url or audit_identity)
    place_url = normalize_place_url(maps_url or audit_identity)
    return {"place_id": place_id, "place_url": place_url}
