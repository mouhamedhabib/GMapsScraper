"""Opt-in SQLite-authoritative coordination for Google Search discovery."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from company_registry.maps_authoritative import AuthoritativeMapsSession
from company_registry.models import IdentityObservation
from company_registry.normalization import canonical_json, normalize_domain


DEFAULT_AUTHORITATIVE_SEARCH_EXPORT_DIRECTORY = Path("data/authoritative_search")


def search_observation(
    row: dict,
    *,
    query: str,
    observed_at: str,
) -> IdentityObservation:
    """Build a stable Search observation without sharing Maps fingerprints."""
    name = row.get("company_name") or row.get("title") or row.get("name") or ""
    website = row.get("source_url") or row.get("website") or ""
    domain = normalize_domain(website)
    identity_seed = {
        "name": name,
        "website": website,
        "place_id": row.get("place_id") or row.get("map_place_id") or "",
        "phone": row.get("phone_number") or row.get("phone") or "",
        "address": row.get("address") or "",
    }
    source_key = (
        f"domain:{domain}"
        if domain
        else "identity:" + sha256(
            canonical_json(identity_seed).encode("utf-8")
        ).hexdigest()
    )
    raw = dict(row)
    raw.setdefault("source", "google_search")
    raw.setdefault("source_query", query)
    return IdentityObservation(
        source_system="GOOGLE_SEARCH",
        source_record_key=source_key,
        observed_at=observed_at,
        name=str(name or ""),
        place_id=str(identity_seed["place_id"] or ""),
        website_url=str(website or ""),
        phone=str(identity_seed["phone"] or ""),
        # Query-derived geography is provenance, not a branch address.
        address=str(identity_seed["address"] or ""),
        raw_payload=raw,
    )


class AuthoritativeSearchSession(AuthoritativeMapsSession):
    """Own one non-resumable Search run using the shared registry contract."""

    def __init__(
        self,
        database: str | Path,
        export_directory: str | Path = DEFAULT_AUTHORITATIVE_SEARCH_EXPORT_DIRECTORY,
        *,
        mode="authoritative-canary",
        run_id: str | None = None,
    ) -> None:
        super().__init__(
            database,
            export_directory,
            mode=mode,
            run_id=run_id or f"search-{uuid4()}",
        )
