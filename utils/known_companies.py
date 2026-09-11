"""Thread-safe, in-memory identities for incremental company discovery."""

from csv import DictReader
from pathlib import Path
from threading import Lock
try:
    from utils.build_leads import clean_value, normalize_name, normalize_phone, website_domain
    from utils.maps_identity import maps_identities_for, normalize_place_id, normalize_place_url
except ModuleNotFoundError:  # Support direct execution from the utils directory.
    from build_leads import clean_value, normalize_name, normalize_phone, website_domain
    from maps_identity import maps_identities_for, normalize_place_id, normalize_place_url


def identities_for(row):
    """Return conservative identities found in either raw or master schemas."""
    maps_identities = maps_identities_for(row)
    domain = website_domain(
        row.get("webpage") or row.get("website") or row.get("source_url")
    )
    name = normalize_name(row.get("title") or row.get("company_name") or row.get("name"))
    phone = normalize_phone(row.get("phone_number") or row.get("phone"))
    return {
        "place_id": maps_identities["place_id"],
        "place_url": maps_identities["place_url"],
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
