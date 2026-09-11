"""Canonical CSV schema for the company-enrichment to outreach boundary."""


COMPANY_ENRICHMENT_FIELDS = (
    "description",
    "industry",
    "services",
    "website_title",
    "website_meta_description",
    "hero_text",
    "about_text",
    "linkedin_url",
)

SOURCE_CONTEXT_FIELDS = (
    "services",
    "website_title",
    "website_meta_description",
    "hero_text",
    "about_text",
)

PROVENANCE_FIELDS = (
    "source",
    "source_queries",
)

CANONICAL_ENRICHMENT_FIELDS = (
    "company_name",
    "email",
    "website",
    "added_at",
    "description",
    "industry",
    "services",
    "website_title",
    "website_meta_description",
    "hero_text",
    "about_text",
    "country",
    "city",
    "location",
    "linkedin_url",
    "phone",
    "source",
    "source_queries",
    "enrichment_status",
    "enrichment_attempts",
)


def normalize_enrichment_row(row, preserve_extra=False):
    """Fill absent canonical columns with blanks without inventing values."""
    normalized = {
        field: (row.get(field) if row.get(field) is not None else "")
        for field in CANONICAL_ENRICHMENT_FIELDS
    }
    if preserve_extra:
        for field, value in row.items():
            if field is not None and field not in normalized:
                normalized[field] = value
    return normalized
