"""Small deterministic geography helpers for the current prospecting markets.

The pipeline deliberately uses address and query text already collected by the
scrapers.  It does not geocode, call an external service, or infer geography
from company website content.
"""

from re import IGNORECASE, compile, escape, sub


UNAVAILABLE_VALUES = {"", "n/a", "none", "not available", "null", "unknown"}

# Long aliases are checked first so ``United Kingdom`` is consumed as one
# country rather than leaving a word behind that could be mistaken for a city.
COUNTRY_ALIASES = (
    ("United Kingdom", ("united kingdom", "royaume-uni", "royaume uni", "u.k.", "uk")),
    ("Switzerland", ("switzerland", "suisse")),
    ("Belgium", ("belgium", "belgique")),
    ("Tunisia", ("tunisia", "tunisie", "tn")),
    ("Ireland", ("ireland", "irlande")),
    ("France", ("france",)),
    ("Canada", ("canada",)),
)

QUERY_NOISE_WORDS = {
    "agency", "agence", "business", "businesses", "company", "companies",
    "consulting", "development", "développement", "developer", "developers",
    "digital", "entreprise", "entreprises", "firm", "firms", "marketing",
    "saas", "service", "services", "software", "web",
}

# These are deliberately limited to the Tunisian markets used by the current
# query files. A known city can establish Tunisia; a four-digit number alone
# never can. Postal codes only strengthen which city occurrence is preferred.
TUNISIAN_CITIES = (
    "Ben Arous",
    "Monastir",
    "Ariana",
    "Sousse",
    "Tunis",
    "Sfax",
)
TUNISIAN_CITY_PATTERNS = tuple(
    (city, compile(rf"(?<!\w){escape(city).replace(r'\ ', r'\s+')}(?!\w)", IGNORECASE))
    for city in TUNISIAN_CITIES
)
TUNISIAN_POSTAL_CODE = compile(r"(?<!\d)\d{4}(?!\d)")


def clean_geo_value(value):
    cleaned = " ".join(str(value or "").split()).strip(" ,")
    return "" if cleaned.casefold() in UNAVAILABLE_VALUES else cleaned


def _alias_pattern(alias):
    escaped = sub(r"\\[ -]", r"[\\s-]", escape(alias))
    return compile(rf"(?<!\w){escaped}(?!\w)", IGNORECASE)


COUNTRY_PATTERNS = tuple(
    (canonical, alias, _alias_pattern(alias))
    for canonical, aliases in COUNTRY_ALIASES
    for alias in aliases
)


def normalize_country(value):
    """Return a supported canonical English country name or an empty string."""
    cleaned = clean_geo_value(value)
    for canonical, alias, _ in COUNTRY_PATTERNS:
        if cleaned.casefold().replace("-", " ") == alias.casefold().replace("-", " "):
            return canonical
    return ""


def format_location(city, country):
    city = clean_geo_value(city)
    country = normalize_country(country)
    if city and country:
        return f"{city}, {country}"
    return country or city


def _country_matches(text):
    matches = []
    for canonical, _, pattern in COUNTRY_PATTERNS:
        for match in pattern.finditer(text):
            matches.append((match.start(), match.end(), canonical))
    # Prefer the longest match at a position and suppress aliases contained in
    # it (for example, none of the short aliases can fragment a full name).
    matches.sort(key=lambda item: (item[0], -(item[1] - item[0])))
    filtered = []
    for candidate in matches:
        if any(candidate[0] >= kept[0] and candidate[1] <= kept[1] for kept in filtered):
            continue
        filtered.append(candidate)
    return filtered


def extract_query_geography(query):
    """Extract a country and conservative preceding city from one search query."""
    query = clean_geo_value(query)
    if query.casefold().startswith(("http://", "https://")):
        return {"country": "", "city": "", "location": ""}
    matches = _country_matches(query)
    countries = {match[2] for match in matches}
    if len(countries) != 1:
        return {"country": "", "city": "", "location": ""}

    country = next(iter(countries))
    match = next(match for match in matches if match[2] == country)
    prefix = query[:match[0]].strip(" ,;:-")
    words = compile(r"[^\W\d_]+(?:[-'’][^\W\d_]+)*", IGNORECASE).findall(prefix)
    city = words[-1] if words and words[-1].casefold() not in QUERY_NOISE_WORDS else ""
    return {
        "country": country,
        "city": city,
        "location": format_location(city, country),
    }


def extract_queries_geography(source_queries):
    """Resolve compatible semicolon-separated queries without hiding conflicts."""
    candidates = [
        extract_query_geography(query)
        for query in str(source_queries or "").split(";")
        if clean_geo_value(query)
    ]
    countries = {candidate["country"] for candidate in candidates if candidate["country"]}
    if len(countries) != 1:
        return {"country": "", "city": "", "location": ""}
    country = next(iter(countries))
    cities = {
        candidate["city"] for candidate in candidates
        if candidate["country"] == country and candidate["city"]
    }
    city = next(iter(cities)) if len(cities) == 1 else ""
    return {"country": country, "city": city, "location": format_location(city, country)}


def extract_address_geography(address):
    """Extract conservative geography while retaining the full Maps address."""
    original = clean_geo_value(address)
    parts = [clean_geo_value(part) for part in original.split(",")]
    parts = [part for part in parts if part]
    if not parts:
        return {"country": "", "city": "", "location": ""}

    country_index = next(
        (index for index in range(len(parts) - 1, -1, -1) if normalize_country(parts[index])),
        None,
    )
    if country_index is not None:
        country = normalize_country(parts[country_index])
        city = ""
        # Region abbreviations such as QC are useful address components but are
        # not cities. Walk backward to the nearest plausible city component.
        for part in reversed(parts[:country_index]):
            candidate = sub(r"^\s*\d[\d\s-]*\s+", "", part)
            candidate = sub(r"\s+\d{3,6}\s*$", "", candidate).strip()
            if not candidate or any(character.isdigit() for character in candidate):
                continue
            if 2 <= len(candidate) <= 3 and candidate.isupper():
                continue
            if 1 <= len(candidate.split()) <= 4:
                city = candidate
                break
        return {"country": country, "city": city, "location": original}

    tunisian_matches = []
    for part_index, part in enumerate(parts):
        has_postal_code = bool(TUNISIAN_POSTAL_CODE.search(part))
        for city, pattern in TUNISIAN_CITY_PATTERNS:
            if pattern.search(part):
                tunisian_matches.append((has_postal_code, part_index, city))
    if tunisian_matches:
        # Later address components usually represent the locality; a city and
        # postal code in the same component is stronger supporting evidence.
        _, _, city = max(tunisian_matches)
        return {"country": "Tunisia", "city": city, "location": original}

    return {"country": "", "city": "", "location": original}


def resolve_maps_geography(address="", source_query="", country="", city=""):
    """Prefer Maps/address values, then fill compatible gaps from the query."""
    direct_country = normalize_country(country)
    direct_city = clean_geo_value(city)
    address_geo = extract_address_geography(address)
    query_geo = extract_queries_geography(source_query)

    resolved_country = address_geo["country"] or direct_country
    resolved_city = address_geo["city"] or direct_city
    if not resolved_country:
        resolved_country = query_geo["country"]
    if not resolved_city and (
        not resolved_country or query_geo["country"] == resolved_country
    ):
        resolved_city = query_geo["city"]
    return {
        "country": resolved_country,
        "city": resolved_city,
        "location": address_geo["location"] or format_location(
            resolved_city, resolved_country
        ),
    }
