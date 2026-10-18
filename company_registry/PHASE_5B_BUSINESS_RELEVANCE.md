# Phase 5B — Business Relevance Classification

## Scope and safety

Phase 5B is an offline policy simulation over the 105 `NEW` rows exported by
`maps-20261010-first-local-01` (5 rows) and `maps-20261011-global-01` (100
rows). It does not reassess either run in SQLite, replace historical exports,
claim a current job opening or freelance contract, or start Batch 02.

The policy version is `maps-dual-purpose-v3-business-relevance`. The existing
result, persistence, eligibility, and export contracts are unchanged. Business
relevance remains separate for employment and missions. Opportunity evidence
is recorded in purpose-specific reasons. Website, contact, address, and
conflict evidence continue to determine readiness without changing relevance.

## Rules

- Exact technical Maps categories are strong relevance evidence: `Software
  company`, `Software development company`, `Website designer`, `Web
  designer`, `Computer consultant`, `IT consultant`, `Computer security
  service`, `Cybersecurity service`, `Computer networking service`, and `Web
  hosting company`.
- Category and description policies remain independent for employment and
  missions even where their current exact-category lists overlap.
- Bounded technical phrases cover application, backend/frontend, API, web,
  mobile, cybersecurity, cloud/infrastructure, managed IT, ERP/business
  software, automation, and system integration services.
- A web/digital/marketing/advertising/design agency is not a target from broad
  agency wording alone. Its description must explicitly demonstrate a listed
  technical service; otherwise it remains `POSSIBLE` or `NOISE` according to
  the existing purpose policy.
- Training institutions, universities, councils, and government organizations
  are unrelated-category `NOISE`; generic/ambiguous categories do not become
  targets from a technical-looking name or discovery query.
- Phrase matching uses normalized whole phrases instead of unrestricted
  substrings.
- Reasons explicitly distinguish `explicit_hiring_opportunity_evidence` from
  `current_hiring_not_established`, and
  `explicit_mission_opportunity_evidence` from
  `active_mission_not_established`. These reasons do not change relevance or
  readiness by themselves.

## Aggregate before/after simulation

Counts below are computed from the exported rows with the Phase 5A policy as
the before state and the Phase 5B policy as the after state.

| Population | Measure | Before | After |
| --- | --- | --- | --- |
| All 105 | Employment relevance (TARGET / POSSIBLE / NOISE) | 22 / 10 / 73 | 70 / 8 / 27 |
| All 105 | Mission relevance (TARGET / POSSIBLE / NOISE) | 0 / 104 / 1 | 70 / 30 / 5 |
| All 105 | Employment readiness (ELIGIBLE / REVIEW / EXCLUDED) | 18 / 14 / 73 | 62 / 16 / 27 |
| All 105 | Mission readiness (ELIGIBLE / REVIEW / EXCLUDED) | 0 / 104 / 1 | 60 / 40 / 5 |
| First-local 5 | Employment relevance | 1 / 0 / 4 | 4 / 0 / 1 |
| First-local 5 | Mission relevance | 0 / 5 / 0 | 4 / 1 / 0 |
| First-local 5 | Employment readiness | 1 / 0 / 4 | 4 / 0 / 1 |
| First-local 5 | Mission readiness | 0 / 5 / 0 | 4 / 1 / 0 |
| Global 100 | Employment relevance | 21 / 10 / 69 | 66 / 8 / 26 |
| Global 100 | Mission relevance | 0 / 99 / 1 | 66 / 29 / 5 |
| Global 100 | Employment readiness | 17 / 14 / 69 | 58 / 16 / 26 |
| Global 100 | Mission readiness | 0 / 99 / 1 | 56 / 39 / 5 |

No row in these exports contains an explicit hiring or mission-availability
phrase recognized by the new policy. All 70 targets therefore retain the
appropriate `*_not_established` opportunity reason.

## Every changed classification and reason

The following exhaustive groups cover all 74 changed companies. Companies in
the same group changed for the same deterministic reason.

### Software company category — 22 companies

Employment stays `TARGET`; mission changes `POSSIBLE` → `TARGET` because the
exact category establishes technical-service/collaboration relevance. Mission
readiness changes `REVIEW` → `ELIGIBLE` for the 18 rows with a usable website,
direct contact, address, and no conflict. It remains `REVIEW` for BnB Tunisie,
Mobilys, Numerys, and APPSFACTOR because no usable website is present.

NETWORKS AND SYSTEMS ENGINEERING; Your Next Agency; BnB Tunisie; Mobilys;
Systematic; Carthago Digital; Numerys; Elinyom; SupplyzPro; APPSFACTOR; SSC
Security System Consulting; Dynamix Services; Zulvancy Engineering; Abdeljalil
Web; Sonicware; SmartClinic.tn — Logiciel dentaire en Tunisie; Tinkerview; WEB
— GAME — APP Ontwikkelaar / DTT Amsterdam; Software engineering; Innotech
Consulting; TSI — EDITEUR D'ERP; Pixelium.

### Website designer category — 41 companies

Employment changes to `TARGET` from `NOISE` for 39 rows and from `POSSIBLE`
for The Savvy Few and Mars. Mission changes `POSSIBLE` → `TARGET` for all 41
because the exact category is direct web-design business evidence. Employment
readiness becomes `ELIGIBLE` for 39 and `REVIEW` for Root4Pro and Full Time Code
because their websites are missing. Mission readiness becomes `ELIGIBLE` for
37; Root4Pro and Full Time Code lack websites, while Ranking partner and Upside
lack accepted direct contact evidence.

Alphasys Tunisia; Web2code; CrawlWise — Agence WEB Tunisie, SEO; Ranking
partner; Rickid webdesign; Heymedia; NOVALABS WEB 3.0; Root4Pro; Ryhane
dalhoum; Website Design Company; CodeShift; Upside; The Brink Agency; Van Ons;
Mediabirds; Webaware; Website Laten Maken Amsterdam; Klein Media; Nebula
Studio; Eweb2pro; Boogarneen; Rcode Vision; clickovate; IMAR LAB; The Savvy Few
— Creative Digital Agency; Agence Pixelia; Flatline Agency; CSI Business;
Développeur web freelance Tunisie; Full Time Code; team lead software
solutions; Responsive Web Systems; PIXIHIVE; Klyra; Webfluencer; De Web
Developer; EpceDev; The Yellow Web; Simple Concept; WeBloom; Mars — Brand &
Digital Agency.

### Computer security service category — 4 companies

Employment changes `NOISE / EXCLUDED` → `TARGET / ELIGIBLE`; mission changes
`POSSIBLE / REVIEW` → `TARGET / ELIGIBLE`. The exact cybersecurity-service
category supplies relevance and all four rows have the existing readiness
evidence. Active hiring and active missions remain unestablished.

Gérance Informatique; DATASERV; LOGIQE; Security Accent.

### Computer consultant category — 2 companies

Employment changes `NOISE / EXCLUDED` → `TARGET / REVIEW`; mission relevance
changes `POSSIBLE` → `TARGET` while readiness stays `REVIEW`. The exact
consulting category supplies relevance, but Erp sap and MACSI Centre have no
usable website.

### Computer networking service category — 1 company

PWN & PATCH changes from `NOISE / EXCLUDED` to `TARGET / ELIGIBLE` for
employment and from `POSSIBLE / REVIEW` to `TARGET / ELIGIBLE` for missions.
The exact infrastructure-service category supplies relevance and the row has
the existing readiness evidence.

### Unrelated organization categories — 4 companies

Mission changes `POSSIBLE / REVIEW` → `NOISE / EXCLUDED` based on exact
unrelated organization categories: Softy Skills (`Training center`), Horizon
University (`University`), Axians Cybersecurity (`Council`), and Gate Training
(`Software training institute`). Employment was already `NOISE / EXCLUDED`.

## Newly included by the simulated ELIGIBLE-only exports

- Employment: 44 newly eligible — 39 website designers, four computer-security
  services, and one computer-networking service.
- Mission: 60 newly eligible — 18 software companies, 37 website designers,
  four computer-security services, and one computer-networking service.

These are simulated qualified prospect/collaborator inclusions only. They are
not 44 job openings or 60 available contracts.

## False-positive and false-negative risks

- A Maps category can be wrong or stale. `Website designer` may describe a
  solo freelancer or branding-heavy studio, so Développeur web freelance
  Tunisie, Ryhane dalhoum, IMAR LAB, Mars, The Savvy Few, Mediabirds, Klein
  Media, and Webfluencer deserve review before employment outreach.
- `Software company` proves sector relevance, not hiring, outsourcing demand,
  budget, or company size. The 22 category matches are therefore prospect
  targets only.
- `Computer networking service` can include hardware-only work; PWN & PATCH is
  retained because infrastructure is in scope, but its exact offer should be
  reviewed.
- Axians Cybersecurity is a likely false negative because Maps labels it
  `Council`; deterministic policy correctly refuses to override that category
  from its name alone.
- Cybertech Security (`Security service`), BrainStack (`Corporate office`),
  REDSYS (`Corporate campus`), Cloud Temple/CloudShift (`Computer support and
  services`), and marketing-category rows may be technical false negatives.
  Their names or source queries alone are intentionally insufficient.

## Recommended next step

Perform a read-only human review of the 44 newly employment-eligible and 60
newly mission-eligible rows, beginning with the named risk cases. If accepted,
run an explicitly approved reassessment/export step under policy v3 on a copy
or via the normal versioned lifecycle. Do not overwrite v1/v2 assessments or
historical exports, and do not start Batch 02.
