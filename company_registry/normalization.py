"""Deterministic identity normalization shared by the SQLite resolver."""

from __future__ import annotations

from hashlib import sha256
import json
import re
import unicodedata
from urllib.parse import urlsplit

from utils.maps_identity import normalize_place_id


UNAVAILABLE = {"", "n/a", "none", "null", "not available", "unknown"}
WEAK_DOMAINS = {
    "facebook.com", "instagram.com", "linkedin.com", "twitter.com", "x.com",
    "youtube.com", "wix.com", "wixsite.com", "wixpress.com",
    "sentry-next.wixpress.com", "o2switch.fr", "wordpress.com",
    "blogspot.com", "github.io", "google.com", "googleusercontent.com",
    "example.com", "example.org", "example.net", "exemple.com",
    "domaine.com", "domain.com", "mail.com", "email.com",
    "gmail.com", "outlook.com", "outlook.fr", "hotmail.com", "yahoo.com",
}


def clean_text(value: object) -> str:
    text = " ".join(str(value or "").split()).strip()
    return "" if text.casefold() in UNAVAILABLE else text


def normalize_name(value: object) -> str:
    return unicodedata.normalize("NFKC", clean_text(value)).casefold()


def normalize_domain(value: object) -> str:
    text = clean_text(value)
    if not text:
        return ""
    parsed = urlsplit(text if "://" in text else "//" + text)
    domain = (parsed.hostname or "").casefold().rstrip(".")
    if domain.startswith("www."):
        domain = domain[4:]
    try:
        return domain.encode("idna").decode("ascii")
    except UnicodeError:
        return ""


def is_trustworthy_domain(domain: str) -> bool:
    domain = normalize_domain(domain)
    if not domain or "." not in domain:
        return False
    return not any(domain == weak or domain.endswith("." + weak) for weak in WEAK_DOMAINS)


def normalize_phone(value: object) -> str:
    digits = "".join(character for character in clean_text(value) if character.isdigit())
    return digits if len(digits) >= 6 else ""


def normalize_address(value: object) -> str:
    text = unicodedata.normalize("NFKC", clean_text(value)).casefold()
    return " ".join(re.sub(r"[^\w]+", " ", text, flags=re.UNICODE).split())


def normalize_google_place_id(value: object) -> str:
    return normalize_place_id(clean_text(value))


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def payload_hash(value: object) -> str:
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()

