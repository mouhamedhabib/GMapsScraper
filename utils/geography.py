"""Small deterministic geography helpers for Maps and prospecting pipelines.

The pipeline deliberately uses address and query text already collected by the
scrapers.  It does not geocode, call an external service, or infer geography
from company website content.
"""

from dataclasses import dataclass
from pathlib import Path
from re import IGNORECASE, compile, escape, sub
from unicodedata import category as unicode_category
from unicodedata import normalize as unicode_normalize


UNAVAILABLE_VALUES = {"", "n/a", "none", "not available", "null", "unknown"}


class QuerySpecificationError(ValueError):
    """A Maps query-file entry is malformed or duplicates another entry."""


def _normalized_query_component(value):
    """Return a stable Unicode-aware comparison form without transliteration."""
    return " ".join(unicode_normalize("NFKC", str(value or "")).split()).casefold()


def _validate_query_text(value, *, field, line_number=None, allow_empty=False):
    prefix = f"Line {line_number}: " if line_number is not None else ""
    if not value and not allow_empty:
        raise QuerySpecificationError(f"{prefix}{field} must not be empty")
    if any(unicode_category(character) == "Cc" for character in value):
        raise QuerySpecificationError(
            f"{prefix}{field} contains a prohibited control character"
        )


@dataclass(frozen=True, slots=True)
class MapsQuerySpec:
    """One exact Maps search plus optional operator-declared geography.

    ``search_text`` is the only value sent to Google Maps.  Explicit target
    values are metadata and deliberately accept arbitrary Unicode country and
    city names; they are not checked against ``COUNTRY_ALIASES``.
    """

    search_text: str
    target_city: str = ""
    target_country: str = ""
    geography_source: str = "UNDECLARED"

    @property
    def is_explicit(self):
        return self.geography_source == "EXPLICIT"

    @property
    def target_location(self):
        if self.target_city and self.target_country:
            return f"{self.target_city}, {self.target_country}"
        return self.target_country or self.target_city

    @property
    def canonical_text(self):
        """Return the durable query representation used for hashing/resume."""
        if self.is_explicit:
            return "\t".join((self.search_text, self.target_city, self.target_country))
        return self.search_text

    @property
    def canonical_key(self):
        """Return the Unicode-safe identity used for duplicate detection."""
        return tuple(_normalized_query_component(value) for value in (
            self.search_text, self.target_city, self.target_country,
        ))

# Long aliases are checked first so ``United Kingdom`` is consumed as one
# country rather than leaving a word behind that could be mistaken for a city.
COUNTRY_ALIASES = (
    ("United Kingdom", ("united kingdom", "royaume-uni", "royaume uni", "u.k.", "uk")),
    ("Switzerland", ("switzerland", "suisse")),
    ("Belgium", ("belgium", "belgique")),
    ("Tunisia", ("tunisia", "tunisie", "tn")),
    ("Ireland", ("ireland", "irlande")),
    ("Malta", ("malta",)),
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


def parse_maps_query_spec(line, *, line_number=None):
    """Parse one legacy or explicit Maps query-file line.

    Legacy lines contain only search text.  Explicit lines contain exactly
    ``search text<TAB>target city<TAB>target country``.  Target names are
    normalized for Unicode and whitespace but are never allowlist-validated.
    """
    raw = str(line).rstrip("\r\n")
    if not raw.strip():
        raise QuerySpecificationError(
            f"Line {line_number}: query must not be empty"
            if line_number is not None else "Query must not be empty"
        )

    if "\t" in raw:
        fields = raw.split("\t")
        prefix = f"Line {line_number}: " if line_number is not None else ""
        if len(fields) != 3:
            raise QuerySpecificationError(
                f"{prefix}explicit query specifications require exactly three "
                "tab-separated fields: search text, target city, target country"
            )
        search_text, target_city, target_country = fields
        for field_name, value in (
            ("search text", search_text),
            ("target country", target_country),
        ):
            if value != value.strip():
                raise QuerySpecificationError(
                    f"{prefix}{field_name} must not have leading or trailing whitespace"
                )
            _validate_query_text(
                value, field=field_name, line_number=line_number,
            )
        if target_city != target_city.strip():
            raise QuerySpecificationError(
                f"{prefix}target city must not have leading or trailing whitespace"
            )
        _validate_query_text(
            target_city, field="target city", line_number=line_number,
            allow_empty=True,
        )
        return MapsQuerySpec(
            search_text=search_text,
            target_city=" ".join(unicode_normalize("NFKC", target_city).split()),
            target_country=" ".join(
                unicode_normalize("NFKC", target_country).split()
            ),
            geography_source="EXPLICIT",
        )

    # Preserve historical behavior for one-column files by trimming only the
    # outer whitespace. Internal spacing remains part of the exact search.
    search_text = raw.strip()
    _validate_query_text(
        search_text, field="search text", line_number=line_number,
    )
    inferred = extract_query_geography(search_text)
    return MapsQuerySpec(
        search_text=search_text,
        target_city=inferred["city"],
        target_country=inferred["country"],
        geography_source=("LEGACY_INFERRED" if inferred["country"] else "UNDECLARED"),
    )


def load_maps_query_specs(file_name):
    """Load, validate, and de-duplicate a UTF-8 Maps query file."""
    path = Path(file_name)
    specs = []
    first_lines = {}
    with path.open("r", encoding="utf-8") as query_file:
        for line_number, line in enumerate(query_file, start=1):
            if not line.strip():
                continue
            spec = parse_maps_query_spec(line, line_number=line_number)
            first_line = first_lines.get(spec.canonical_key)
            if first_line is not None:
                raise QuerySpecificationError(
                    f"Line {line_number}: duplicate canonical query specification "
                    f"(first declared on line {first_line})"
                )
            first_lines[spec.canonical_key] = line_number
            specs.append(spec)
    return specs


def coerce_maps_query_spec(value):
    """Return a query specification for an API caller's string or spec."""
    if isinstance(value, MapsQuerySpec):
        return value
    return parse_maps_query_spec(value)


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
            candidate = sub(r"\s+[A-Z]{2,4}\s+\d{3,6}\s*$", "", candidate).strip()
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
