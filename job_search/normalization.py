"""Conservative URL cleanup and normalization for job identity."""

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


TRACKING_KEYS = {
    "fbclid", "gclid", "msclkid", "ref", "referrer", "source", "src",
    "campaign", "campaignid", "trk", "trackingid",
}


def normalize_job_url(url):
    """Remove tracking noise while retaining unknown identity-bearing parameters."""
    value = (url or "").strip()
    if not value:
        return ""
    initial = urlsplit(value)
    if initial.scheme and initial.scheme.casefold() not in {"http", "https"}:
        return ""
    parsed = urlsplit(value if initial.scheme else "https://" + value)
    scheme = parsed.scheme.casefold()
    if scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    hostname = parsed.hostname.casefold().rstrip(".")
    port = parsed.port
    netloc = hostname
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{hostname}:{port}"
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")
    query = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        folded = key.casefold()
        if folded.startswith("utm_") or folded in TRACKING_KEYS:
            continue
        query.append((key, value))
    query.sort(key=lambda pair: (pair[0].casefold(), pair[1]))
    return urlunsplit((scheme, netloc, path, urlencode(query, doseq=True), ""))
