# Phase 2A identity resolver

Phase 2A is a standalone SQLite resolver. It is deliberately not called by the
Maps scraper, Google Search discovery, exporters, or outreach code.

## Evidence policy

Resolution is deterministic and exact-only:

- An exact normalized Google Maps Place ID identifies a branch unless company
  ownership evidence conflicts. A conflicting company name plus domain, or a
  conflicting name plus phone and address, requires review.
- A new Place ID is accepted as an alias only when one existing branch has the
  exact normalized name, phone, and address. Otherwise overlap is ambiguous.
- Exact company name plus a trustworthy website domain identifies a company.
  A new Place ID under that company creates an `UPDATED` branch.
- Without a Place ID, exact name plus phone or exact name plus address may
  identify one unique branch.
- A shared domain alone never merges a company or branch. Social, hosting,
  infrastructure, placeholder, and free-mail domains are weak evidence.
- Missing names on unmatched records and records with no Place ID, trustworthy
  domain, phone, or address are quarantined.
- Fuzzy matching and probabilistic/AI identity decisions are intentionally out
  of scope.

Before returning `NEW`, the policy searches unresolved historical provenance.
An exact historical payload fingerprint preserves its historical `AMBIGUOUS`
or `QUARANTINED` classification. The same unresolved Place ID is ambiguous.
Without a new Place ID, exact historical name plus phone, address, or a
trustworthy domain is ambiguous. Domain-only overlap does not blanket-
quarantine an otherwise distinct observation. A genuinely new Place ID can be
decisive unless it conflicts with existing registry evidence.

Entity origin and observation classification are separate. Existing historical
companies and branches stay `LEGACY_UNKNOWN` with null `first_seen_at`; live
observations are recorded separately in `discovery_observations`.

## Transaction and worker contract

`IdentityResolver.preview()` evaluates without recording a resolution.
`IdentityResolver.resolve()` uses one connection and one `BEGIN IMMEDIATE`
transaction per observation. The source system, source record key, and payload
hash form the persistent idempotency key. SQLite foreign keys and a bounded
busy timeout are enabled on every connection. Worker threads must not share
connections.
