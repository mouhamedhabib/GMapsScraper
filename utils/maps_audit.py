"""Deterministic, offline quality and geography checks for Maps audit rows."""

from __future__ import annotations

import re

from utils.build_leads import clean_value
from utils.geography import extract_address_geography


TARGET_COMPANY = "TARGET_COMPANY"
POSSIBLE_TARGET = "POSSIBLE_TARGET"
NOISE = "NOISE"

_TARGET_TERMS = (
    "software company", "software development", "web development",
    "technology company", "information technology company",
)
_POSSIBLE_TERMS = (
    "it company", "it services", "computer support", "computer service",
    "digital agency", "web agency", "technology consultant",
)
_NOISE_TERMS = (
    "restaurant", "hotel", "school", "university", "retail", "store",
    "supermarket", "shopping", "cafe", "coffee shop", "real estate",
    "car dealer", "medical", "dentist", "beauty salon",
    "travel agency", "tour agency", "tour operator", "marketing agency",
    "advertising agency",
)
_MALTA_LOCALITIES = (
    "malta", "valletta", "sliema", "birkirkara", "mosta", "qormi",
    "gżira", "gzira", "msida", "san ġwann", "san gwann",
    "st julian", "saint julian",
)


def _contains(text: str, terms) -> bool:
    return any(term in text for term in terms)


def classify_maps_company(row: dict) -> str:
    """Classify Maps evidence without network calls or probabilistic models."""
    category = clean_value(row.get("category")).casefold()
    name = clean_value(row.get("title") or row.get("name")).casefold()
    website = clean_value(row.get("webpage") or row.get("website"))
    evidence = " ".join(value for value in (category, name) if value)
    if not evidence and not website:
        return NOISE
    if _contains(category, _NOISE_TERMS):
        return NOISE
    if _contains(evidence, _TARGET_TERMS):
        return TARGET_COMPANY
    if _contains(evidence, _POSSIBLE_TERMS):
        return POSSIBLE_TARGET
    # A matching query is discovery intent, not evidence that the result is a
    # software company. Keep a named/linked result reviewable rather than target.
    query = clean_value(row.get("source_query")).casefold()
    if (name or website) and _contains(query, _TARGET_TERMS + _POSSIBLE_TERMS):
        return POSSIBLE_TARGET
    return NOISE


def expected_maps_geography(source_query: str) -> str:
    query = clean_value(source_query).casefold()
    if re.search(r"(?<!\w)sfax(?!\w)", query):
        return "SFAX"
    if re.search(r"(?<!\w)malta(?!\w)", query):
        return "MALTA"
    return ""


def validate_maps_geography(row: dict) -> str:
    """Return MATCH, MISMATCH, NOT_CHECKED, or UNKNOWN from Maps evidence."""
    expected = expected_maps_geography(row.get("source_query", ""))
    if not expected:
        return "NOT_CHECKED"
    address = clean_value(row.get("address") or row.get("location"))
    if not address:
        # The scraper can fill country/city from the query when Maps supplies
        # no address. Those derived fields cannot validate their own query.
        return "UNKNOWN"
    country = clean_value(row.get("country")).casefold()
    city = clean_value(row.get("city")).casefold()
    text = " ".join((address, country, city)).casefold()
    if expected == "SFAX":
        if re.search(r"(?<!\w)sfax(?!\w)", text):
            return "MATCH"
        address_geo = extract_address_geography(address)
        if address_geo["country"] or address_geo["city"] or country or city:
            return "MISMATCH"
        return "UNKNOWN"
    if _contains(text, _MALTA_LOCALITIES):
        return "MATCH"
    address_geo = extract_address_geography(address)
    if address_geo["country"] or country:
        return "MISMATCH"
    return "UNKNOWN"
