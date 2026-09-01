"""Thread-safe, in-memory identities for incremental company discovery."""

from csv import DictReader
from pathlib import Path
from threading import Lock
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit
import re

try:
    from utils.build_leads import clean_value, normalize_name, normalize_phone, website_domain
except ModuleNotFoundError:  # Support direct execution from the utils directory.
    from build_leads import clean_value, normalize_name, normalize_phone, website_domain


PLACE_DATA_ID = re.compile(r"!1s([^!/?&#]+)", re.IGNORECASE)


def normalize_place_id(value):
    """Extract a stable Google place/cid identity when one is present."""
    value = clean_value(value)
    if not value:
        return ""
    parsed = urlsplit(value if "://" in value else "//" + value)
    query = parse_qs(parsed.query)
    for key in ("place_id", "query_place_id", "cid"):
        candidate = clean_value((query.get(key) or [""])[0])
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
    value = clean_value(value)
    if not value:
        return ""
    parsed = urlsplit(value if "://" in value else "//" + value)
    host = (parsed.hostname or "").casefold()
    lowered_path = parsed.path.casefold()
    if (
        "google." not in host
        or "/maps/" not in lowered_path
        or ("/place/" not in lowered_path)
        or ("/@" not in lowered_path and "/data=" not in lowered_path)
    ):
        return ""
    # Query parameters and fragments are commonly tracking/session state. Keep
    # the full path, which includes Maps' stable data token when supplied.
    path = unquote(parsed.path).rstrip("/")
    return urlunsplit(("https", "google.com", path, "", "")) if path else ""


def identities_for(row):
    """Return conservative identities found in either raw or master schemas."""
    maps_url = clean_value(
        row.get("map_link") or row.get("place_url") or row.get("maps_url")
    )
    explicit_place_id = clean_value(row.get("place_id"))
    place_id = (
        ("place_id:" + explicit_place_id.casefold())
        if explicit_place_id
        else normalize_place_id(maps_url)
    )
    domain = website_domain(
        row.get("webpage") or row.get("website") or row.get("source_url")
    )
    name = normalize_name(row.get("title") or row.get("company_name") or row.get("name"))
    phone = normalize_phone(row.get("phone_number") or row.get("phone"))
    return {
        "place_id": place_id.casefold(),
        "place_url": normalize_place_url(maps_url),
        "domain": domain,
        "name_phone": (name, phone) if name and phone else None,
    }


class KnownCompanies:
    """Load and update known identities with O(1), thread-safe lookups."""

    def __init__(self):
        self.known_domains = set()
        self.known_place_urls = set()
        self.known_place_ids = set()
        self.known_name_phones = set()
        self._startup_identities = set()
        self._lock = Lock()

    @classmethod
    def from_directory(cls, directory):
        registry = cls()
        root = Path(directory)
        for filename in (
            "leads_master.csv",
            "google_maps_data.csv",
            "google_search_companies.csv",
        ):
            registry.load_csv(root / filename)
        registry._startup_identities = registry._identity_tokens()
        return registry

    def _identity_tokens(self):
        return (
            {("place_id", value) for value in self.known_place_ids}
            | {("place_url", value) for value in self.known_place_urls}
            | {("domain", value) for value in self.known_domains}
            | {("name_phone", value) for value in self.known_name_phones}
        )

    @staticmethod
    def _tokens(identities):
        return {
            (kind, value)
            for kind, value in identities.items()
            if value
        }

    def load_csv(self, path):
        """Load any supported columns; old/sparse CSV schemas are harmless."""
        path = Path(path)
        if not path.exists():
            return 0
        count = 0
        try:
            with path.open("r", newline="", encoding="utf-8-sig") as handle:
                for row in DictReader(handle):
                    self.add(row)
                    count += 1
        except (OSError, UnicodeError):
            return count
        return count

    def _matches_unlocked(self, identities):
        return bool(
            (identities["place_id"] and identities["place_id"] in self.known_place_ids)
            or (identities["place_url"] and identities["place_url"] in self.known_place_urls)
            or (identities["domain"] and identities["domain"] in self.known_domains)
            or (
                identities["name_phone"]
                and identities["name_phone"] in self.known_name_phones
            )
        )

    def contains(self, row):
        identities = identities_for(row)
        with self._lock:
            return self._matches_unlocked(identities)

    def duplicate_kind(self, row):
        """Return ``known``, ``same_run``, or an empty string."""
        identities = identities_for(row)
        with self._lock:
            matched = self._tokens(identities) & self._identity_tokens()
            if not matched:
                return ""
            return "known" if matched & self._startup_identities else "same_run"

    def check_and_add(self, row):
        """Atomically reject a duplicate or reserve a new same-run identity."""
        identities = identities_for(row)
        with self._lock:
            if self._matches_unlocked(identities):
                return False
            self._add_unlocked(identities)
            return True

    def add(self, row):
        identities = identities_for(row)
        with self._lock:
            self._add_unlocked(identities)

    def discard(self, row):
        """Release identities reserved for a result that failed before writing."""
        identities = identities_for(row)
        with self._lock:
            if identities["place_id"]:
                self.known_place_ids.discard(identities["place_id"])
            if identities["place_url"]:
                self.known_place_urls.discard(identities["place_url"])
            if identities["domain"]:
                self.known_domains.discard(identities["domain"])
            if identities["name_phone"]:
                self.known_name_phones.discard(identities["name_phone"])

    def _add_unlocked(self, identities):
        if identities["place_id"]:
            self.known_place_ids.add(identities["place_id"])
        if identities["place_url"]:
            self.known_place_urls.add(identities["place_url"])
        if identities["domain"]:
            self.known_domains.add(identities["domain"])
        if identities["name_phone"]:
            self.known_name_phones.add(identities["name_phone"])

    def domain_is_known(self, value):
        domain = website_domain(value)
        with self._lock:
            return bool(domain and domain in self.known_domains)
