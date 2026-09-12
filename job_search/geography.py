"""Deterministic normalization of job locations into countries and regions."""

from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata


EUROPE_COUNTRIES = {
    "Albania", "Austria", "Belgium", "Bosnia and Herzegovina", "Bulgaria",
    "Croatia", "Cyprus", "Czechia", "Denmark", "Estonia", "Finland",
    "France", "Germany", "Greece", "Hungary", "Iceland", "Ireland", "Italy",
    "Latvia", "Lithuania", "Luxembourg", "Malta", "Moldova", "Montenegro",
    "Netherlands", "North Macedonia", "Norway", "Poland", "Portugal",
    "Romania", "Serbia", "Slovakia", "Slovenia", "Spain", "Sweden",
    "Switzerland", "Ukraine", "United Kingdom",
}

COUNTRY_ALIASES = {
    "us": "United States", "usa": "United States", "u s": "United States",
    "united states": "United States", "united states of america": "United States",
    "tn": "Tunisia", "tun": "Tunisia", "tunisia": "Tunisia",
    "fr": "France", "fra": "France", "france": "France",
    "be": "Belgium", "bel": "Belgium", "belgium": "Belgium",
    "ch": "Switzerland", "che": "Switzerland", "switzerland": "Switzerland",
    "de": "Germany", "deu": "Germany", "germany": "Germany",
    "nl": "Netherlands", "nld": "Netherlands", "netherlands": "Netherlands",
    "ie": "Ireland", "irl": "Ireland", "ireland": "Ireland",
    "gb": "United Kingdom", "gbr": "United Kingdom", "uk": "United Kingdom",
    "united kingdom": "United Kingdom", "great britain": "United Kingdom",
    "ro": "Romania", "rou": "Romania", "romania": "Romania",
    "pl": "Poland", "pol": "Poland", "poland": "Poland",
    "mt": "Malta", "mlt": "Malta", "malta": "Malta",
    "ca": "Canada", "can": "Canada", "canada": "Canada",
    "in": "India", "ind": "India", "india": "India",
    "england": "United Kingdom", "scotland": "United Kingdom",
    "wales": "United Kingdom", "northern ireland": "United Kingdom",
}
COUNTRY_ALIASES.update({country.casefold(): country for country in EUROPE_COUNTRIES})

# Only intentionally unambiguous, curated cities belong here. Unknown cities are
# preserved by callers without inventing a country.
CITY_COUNTRIES = {
    "seattle": "United States", "new york": "United States",
    "san francisco": "United States", "austin": "United States",
    "boston": "United States", "sliema": "Malta", "iasi": "Romania",
    "breda": "Netherlands", "paris": "France", "brussels": "Belgium",
    "brussel": "Belgium", "tunis": "Tunisia",
    "chennai": "India",
}
CITY_NAMES = {
    "seattle": "Seattle", "new york": "New York", "san francisco": "San Francisco",
    "austin": "Austin", "boston": "Boston", "sliema": "Sliema", "iasi": "Iași",
    "breda": "Breda", "paris": "Paris", "brussels": "Brussels",
    "brussel": "Brussel", "tunis": "Tunis", "chennai": "Chennai",
}


@dataclass(frozen=True)
class Geography:
    country: str
    region: str


@dataclass(frozen=True)
class CanonicalLocationEvidence:
    location_text: str
    country: str
    city: str
    source: str


def _fold(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value or "")
    folded = "".join(char for char in normalized if not unicodedata.combining(char)).casefold()
    return " ".join(re.findall(r"[a-z]+", folded))


def _country_in_text(value: str, allow_codes: bool = False) -> str:
    folded = _fold(value)
    if not folded:
        return ""
    # Prefer longer aliases and require token boundaries; this prevents codes
    # such as US, MT, and IE from matching inside ordinary words.
    for alias in sorted(COUNTRY_ALIASES, key=len, reverse=True):
        if len(alias) <= 3 and not allow_codes:
            continue
        if re.search(rf"(?:^|\s){re.escape(alias)}(?:$|\s)", folded):
            return COUNTRY_ALIASES[alias]
    return ""


def infer_title_location(title: str = "") -> CanonicalLocationEvidence | None:
    """Return canonical region evidence only for explicit, curated title text."""
    folded = _fold(title)
    if re.search(r"(?:^|\s)ile de france(?:$|\s)", folded):
        return CanonicalLocationEvidence(
            location_text="Île-de-France",
            country="France",
            city="",
            source="title_structured_location",
        )
    return None


def normalize_geography(location_text: str = "", city: str = "", country: str = "", title: str = "") -> Geography:
    """Return a conservative normalized country/region from explicit fields."""
    title_location = infer_title_location(title)
    normalized_country = _country_in_text(country, allow_codes=True)
    if not normalized_country:
        normalized_country = _country_in_text(location_text, allow_codes=True)
    if not normalized_country:
        normalized_country = (
            title_location.country if title_location
            else _country_in_text(title, allow_codes=False)
        )
    if not normalized_country:
        for candidate in (city, location_text):
            folded = _fold(candidate)
            if folded in CITY_COUNTRIES:
                normalized_country = CITY_COUNTRIES[folded]
                break

    if normalized_country == "Tunisia":
        region = "TUNISIA"
    elif normalized_country == "United States":
        region = "UNITED_STATES"
    elif normalized_country in EUROPE_COUNTRIES:
        region = "EUROPE"
    elif normalized_country:
        region = "OTHER"
    else:
        region = "UNKNOWN"
    return Geography(normalized_country, region)


def infer_known_city(value: str) -> str:
    """Return a curated city only when it appears as complete words."""
    folded = _fold(value)
    for city in sorted(CITY_NAMES, key=len, reverse=True):
        if re.search(rf"(?:^|\s){re.escape(city)}(?:$|\s)", folded):
            return CITY_NAMES[city]
    return ""
