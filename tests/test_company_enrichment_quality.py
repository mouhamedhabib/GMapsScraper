"""Dependency-free fixtures for company enrichment quality rules."""

from ast import Assign, FunctionDef, Module, Name, parse
from builtins import compile as compile_code
from pathlib import Path
from re import IGNORECASE, compile as re_compile, findall, split
from unittest import TestCase, main
from unittest.mock import patch
from urllib.parse import urljoin, urlsplit, urlunsplit

from utils.enrich_leads import enrichment_status


SOURCE_PATH = Path(__file__).parents[1] / "utils" / "company_enrichment.py"
ASSIGNMENTS = {
    "DESCRIPTION_FIELDS",
    "INDUSTRY_FIELD_WEIGHTS",
    "PRIMARY_INDUSTRY_FIELDS",
    "ERP_PRIMARY_EVIDENCE",
    "ERP_SECONDARY_FEATURE_CATEGORIES",
    "RELATED_TECHNOLOGY_CATEGORIES",
    "MARKETING_SENTENCE",
    "ERROR_PAGE_MARKER",
    "INTERSTITIAL_PAGE_MARKER",
    "CONTACT_LEGAL_BLOCK",
    "CONTACT_VALUE_BLOCK",
    "ABOUT_LINK_MARKER",
    "SERVICE_LINK_MARKER",
    "INDUSTRY_RULES",
    "BOILERPLATE_PHRASES",
    "PAGE_PATHS",
}
FUNCTIONS = {
    "normalize_whitespace",
    "trim_text",
    "unique_text",
    "is_boilerplate",
    "is_error_text",
    "is_contact_or_legal_text",
    "is_content_noise",
    "_description_tokens",
    "_description_candidates",
    "_repeats_description",
    "build_company_description",
    "classify_company_industry",
    "_candidate_urls",
    "_normalized_domain",
    "_deduplicate_candidates",
    "_discover_internal_pages",
    "_secondary_candidates",
    "_is_error_page",
    "_has_structured_homepage_evidence",
    "_has_useful_page_text",
    "_load_company_pages",
    "_meaningful_raw_text",
    "_meaningful_text",
    "_extract_title",
    "_extract_meta_description",
    "_block_text",
    "_normalize_linkedin_url",
}


def load_quality_functions():
    tree = parse(SOURCE_PATH.read_text(encoding="utf-8"), filename=str(SOURCE_PATH))
    nodes = []
    for node in tree.body:
        if isinstance(node, Assign):
            names = {
                target.id for target in node.targets
                if isinstance(target, Name)
            }
            if names & ASSIGNMENTS:
                nodes.append(node)
        elif isinstance(node, FunctionDef) and node.name in FUNCTIONS:
            nodes.append(node)

    namespace = {
        "IGNORECASE": IGNORECASE,
        "compile": re_compile,
        "findall": findall,
        "split": split,
        "urljoin": urljoin,
        "urlsplit": urlsplit,
        "urlunsplit": urlunsplit,
    }
    code = compile_code(Module(body=nodes, type_ignores=[]), str(SOURCE_PATH), "exec")
    exec(code, namespace)
    return namespace


QUALITY = load_quality_functions()
classify = QUALITY["classify_company_industry"]
describe = QUALITY["build_company_description"]


def evidence(meta="", about="", hero="", services="", title=""):
    return {
        "website_meta_description": meta,
        "about_text": about,
        "hero_text": hero,
        "services": services,
        "website_title": title,
    }


class FakeElement:
    def __init__(self, text, name="p"):
        self.text = text
        self.name = name

    def get_text(self, separator=" ", strip=False):
        return self.text.strip() if strip else self.text


class FakeMeta:
    def __init__(self, **attributes):
        self.attributes = attributes

    def get(self, key, default=""):
        return self.attributes.get(key, default)


class FakeAnchor(FakeMeta):
    def __init__(self, href, text):
        super().__init__(href=href)
        self.text = text

    def get_text(self, separator=" ", strip=False):
        return self.text.strip() if strip else self.text


class FakeSoup:
    def __init__(self, title="", metas=None, heading=None, anchors=None):
        self.title = FakeElement(title, "title")
        self.metas = metas or []
        self.heading = heading
        self.anchors = anchors or []

    def find_all(self, name):
        return self.metas if name == "meta" else []

    def find(self, name):
        return self.heading if name == "h1" else None

    def select(self, selector):
        return self.anchors if selector == "a[href]" else []


class FakeContainer:
    def __init__(self, children):
        self.children = children

    def find_all(self, names):
        return self.children


class CompanyEnrichmentQualityTests(TestCase):
    def test_short_homepage_with_useful_meta_is_accepted(self):
        soup = FakeSoup(
            title="Home",
            metas=[FakeMeta(
                name="description",
                content="A collaborative SaaS platform built for distributed teams.",
            )],
        )
        source = "<html><head>structured metadata</head></html>"
        with patch.dict(QUALITY, {"_soup": lambda page_source: soup}):
            self.assertTrue(
                QUALITY["_has_useful_page_text"](
                    source,
                    "Short text",
                    is_homepage=True,
                )
            )

    def test_good_homepage_is_preserved_when_about_load_fails(self):
        homepage = {
            "kind": "homepage",
            "url": "https://example.test/",
            "source": "<html>good homepage</html>",
        }
        calls = []

        def load_pages(driver, candidates, timeout, verbose):
            calls.append(candidates)
            return [homepage] if candidates[0][0] == "homepage" else []

        replacements = {
            "_load_pages": load_pages,
            "_secondary_candidates": lambda website_url, page: [
                ("about", "https://example.test/company/about-us")
            ],
        }
        with patch.dict(QUALITY, replacements):
            pages = QUALITY["_load_company_pages"](
                object(),
                "https://example.test",
                15,
                False,
            )
        self.assertEqual(pages, [homepage])
        self.assertEqual(len(calls), 2)

    def test_discovers_real_nested_about_link(self):
        soup = FakeSoup(anchors=[
            FakeAnchor("/company/about-us", "About us"),
            FakeAnchor("/company/solutions", "Solutions"),
            FakeAnchor("https://external.test/about", "About partner"),
        ])
        with patch.dict(QUALITY, {"_soup": lambda page_source: soup}):
            discovered = QUALITY["_discover_internal_pages"](
                "<html>links</html>",
                "https://example.test/",
            )
        self.assertEqual(
            discovered,
            [
                ("about", "https://example.test/company/about-us"),
                ("services", "https://example.test/company/solutions"),
            ],
        )

    def test_503_page_is_rejected_as_content(self):
        visible = (
            "Service Unavailable. The server is temporarily unable to service "
            "your request due to maintenance downtime or capacity problems."
        )
        source = "<html><title>503 Service Unavailable</title>" + ("x" * 300)
        with patch.dict(
            QUALITY,
            {"_soup": lambda page_source: FakeSoup("503 Service Unavailable")},
        ):
            self.assertFalse(QUALITY["_has_useful_page_text"](source, visible))
        error_evidence = evidence(title="503 Service Unavailable", meta=visible)
        self.assertEqual(describe(error_evidence), "")
        self.assertEqual(classify(error_evidence), "")

    def test_cynoia_like_evidence_is_saas(self):
        extracted = evidence(
            meta="An all-in-one platform designed for African teams.",
            about="A SaaS collaboration platform bringing team tools together.",
            services="Projects; Messaging; Calls",
        )
        self.assertEqual(classify(extracted), "SaaS")

    def test_projects_in_production_is_not_manufacturing(self):
        extracted = evidence(
            meta="Software teams move digital projects into production safely.",
        )
        self.assertNotEqual(classify(extracted), "Manufacturing")

    def test_physical_factory_evidence_is_manufacturing(self):
        extracted = evidence(
            meta=(
                "A manufacturing plant operating production lines and "
                "industrial equipment for physical goods."
            ),
        )
        self.assertEqual(classify(extracted), "Manufacturing")

    def test_steps_like_evidence_is_technology_not_manufacturing(self):
        extracted = evidence(
            meta=(
                "Technology partner building digital platforms, AI solutions, "
                "data products and custom web development."
            ),
            about=(
                "Artificial intelligence, machine learning and LLM applications "
                "for MVP and POC delivery."
            ),
        )
        self.assertEqual(classify(extracted), "AI / Automation")

    def test_steps_like_services_resolve_related_technology_tie(self):
        extracted = evidence(services=(
            "Digital Platforms & AI Engineering; Custom web development; "
            "MVP & POC; Artificial intelligence; Data products and reporting; "
            "Machine learning; LLM applications"
        ))
        self.assertEqual(classify(extracted), "AI / Automation")

    def test_unrelated_strong_categories_remain_other(self):
        extracted = evidence(meta=(
            "Cybersecurity services for a manufacturing plant and factory."
        ))
        self.assertEqual(classify(extracted), "Other")

    def test_cta_only_about_block_is_blank(self):
        container = FakeContainer([
            FakeElement(
                "If you're passionate about what you do, we'd love to connect. "
                "Then reach out to us."
            ),
            FakeElement("Entreprise", "h3"),
            FakeElement("Suivez-Nous", "h3"),
        ])
        self.assertEqual(QUALITY["_block_text"](container), "")

    def test_about_block_keeps_description_and_removes_footer_noise(self):
        container = FakeContainer([
            FakeElement(
                "We build digital platforms and AI solutions for growing businesses. "
                "We'd love to connect."
            ),
            FakeElement("Phone", "h3"),
            FakeElement("+216 71 000 000"),
            FakeElement("Email", "h3"),
            FakeElement("hello@example.test"),
            FakeElement("Office address", "h3"),
            FakeElement("Les Berges du Lac, Tunis"),
            FakeElement("Conditions générales", "h3"),
            FakeElement("© 2026 All rights reserved"),
        ])
        about = QUALITY["_block_text"](container)
        self.assertEqual(
            about,
            "We build digital platforms and AI solutions for growing businesses.",
        )

    def test_linkedin_only_is_partial(self):
        record = {
            "website": "https://example.test",
            "linkedin_url": "https://www.linkedin.com/company/example",
            "enrichment_attempts": "1",
        }
        self.assertEqual(enrichment_status(record), "PARTIAL")

    def test_error_page_only_is_incomplete_after_attempt(self):
        record = {
            "website": "https://example.test",
            "website_title": "",
            "website_meta_description": "",
            "description": "",
            "industry": "",
            "enrichment_attempts": "1",
        }
        self.assertEqual(enrichment_status(record), "INCOMPLETE")


if __name__ == "__main__":
    main()
