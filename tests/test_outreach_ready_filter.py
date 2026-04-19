"""Tests for strict ICP outreach-ready filter."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from lead_extraction.outreach_ready_filter import (
    dedupe_rows,
    outreach_ready_reject_reason,
    passes_outreach_ready,
)


def _good_row(**overrides: str) -> dict[str, str]:
    base: dict[str, str] = {
        "full_name": "Alex Founder",
        "linkedin_url": "https://www.linkedin.com/in/alexfounder",
        "email": "alex@acmecorp.io",
        "title": "Co-Founder & CEO",
        "company_name": "Acme Corp",
        "company_size": "15",
        "industry": "computer software",
        "location": "Singapore, Singapore",
        "organization_keywords": "defi, blockchain, b2b, api",
        "organization_short_description": "",
        "organization_seo_description": "",
        "web3_keyword_hits": "defi",
        "email_status": "",
    }
    base.update(overrides)
    return base


class OutreachReadyFilterTests(unittest.TestCase):
    def test_passes_minimal_icp_row(self) -> None:
        self.assertIsNone(outreach_ready_reject_reason(_good_row()))

    def test_passes_helper(self) -> None:
        self.assertTrue(passes_outreach_ready(_good_row()))

    def test_reject_missing_linkedin(self) -> None:
        r = _good_row(linkedin_url="")
        self.assertEqual(outreach_ready_reject_reason(r), "missing_or_invalid_linkedin")

    def test_reject_missing_email(self) -> None:
        r = _good_row(email="")
        self.assertEqual(outreach_ready_reject_reason(r), "missing_email")

    def test_reject_generic_email_localpart(self) -> None:
        r = _good_row(email="info@acmedefi.io")
        self.assertEqual(outreach_ready_reject_reason(r), "generic_or_personal_email")

    def test_reject_personal_email_domain(self) -> None:
        r = _good_row(email="alex@gmail.com")
        self.assertEqual(outreach_ready_reject_reason(r), "generic_or_personal_email")

    def test_reject_agency_company_name(self) -> None:
        r = _good_row(company_name="Visual Agency LLC")
        self.assertEqual(outreach_ready_reject_reason(r), "agency_or_consulting_name_pattern")

    def test_reject_solutions_token_in_company_name(self) -> None:
        r = _good_row(company_name="Prime Data Solutions Inc")
        self.assertEqual(outreach_ready_reject_reason(r), "agency_or_consulting_name_pattern")

    def test_allow_software_consulting_in_company_name(self) -> None:
        r = _good_row(company_name="Software Consulting Partners")
        self.assertIsNone(outreach_ready_reject_reason(r))

    def test_reject_missing_product_signal_when_required(self) -> None:
        r = _good_row(
            company_name="Onchain Labs",
            industry="financial services",
            organization_keywords="defi, blockchain, token",
            web3_keyword_hits="defi",
        )
        with patch.dict(os.environ, {"ICP_OUTREACH_REQUIRE_PRODUCT_SIGNAL": "true"}, clear=False):
            self.assertEqual(outreach_ready_reject_reason(r), "missing_product_signal")

    def test_pass_product_signal_when_required(self) -> None:
        r = _good_row(organization_keywords="defi, blockchain, b2b, api")
        with patch.dict(os.environ, {"ICP_OUTREACH_REQUIRE_PRODUCT_SIGNAL": "true"}, clear=False):
            self.assertIsNone(outreach_ready_reject_reason(r))

    def test_reject_financial_services_without_web3(self) -> None:
        r = _good_row(
            company_name="Traditional Bank Corp",
            industry="financial services",
            organization_keywords="payments, banking, retail branches",
            web3_keyword_hits="",
        )
        self.assertEqual(outreach_ready_reject_reason(r), "industry_not_allowed")

    def test_reject_software_without_web3_signal(self) -> None:
        r = _good_row(
            company_name="Boring SaaS Inc",
            organization_keywords="enterprise, productivity",
            web3_keyword_hits="",
        )
        self.assertEqual(outreach_ready_reject_reason(r), "missing_web3_signal")

    def test_pass_financial_services_with_web3(self) -> None:
        r = _good_row(
            company_name="Onchain Capital Ltd",
            industry="financial services",
            organization_keywords="defi, crypto lending",
            web3_keyword_hits="defi",
        )
        self.assertIsNone(outreach_ready_reject_reason(r))

    def test_reject_industry_not_allowed(self) -> None:
        r = _good_row(
            company_name="Motor Parts Inc",
            industry="automotive",
            organization_keywords="defi, manufacturing",
            web3_keyword_hits="defi",
        )
        self.assertEqual(outreach_ready_reject_reason(r), "industry_not_allowed")

    def test_reject_title_president_only(self) -> None:
        r = _good_row(title="President")
        self.assertEqual(outreach_ready_reject_reason(r), "title_not_founder_ceo")

    def test_reject_headcount_over_cap(self) -> None:
        r = _good_row(company_size="75")
        self.assertEqual(outreach_ready_reject_reason(r), "headcount_unknown_or_over_cap")

    def test_reject_unknown_headcount(self) -> None:
        r = _good_row(company_size="")
        self.assertEqual(outreach_ready_reject_reason(r), "headcount_unknown_or_over_cap")

    def test_geo_strict_rejects_wrong_region(self) -> None:
        r = _good_row(location="Berlin, Germany")
        self.assertEqual(
            outreach_ready_reject_reason(r, geo_strict=True),
            "geo_not_in_allowlist",
        )

    def test_geo_strict_off_allows_any_location(self) -> None:
        r = _good_row(location="Berlin, Germany")
        self.assertIsNone(outreach_ready_reject_reason(r, geo_strict=False))

    def test_apollo_email_status_required(self) -> None:
        r = _good_row(email_status="unavailable")
        with patch.dict(os.environ, {"ICP_REQUIRE_APOLLO_EMAIL_STATUS": "true"}, clear=False):
            self.assertEqual(outreach_ready_reject_reason(r), "apollo_email_status")

    def test_apollo_email_status_verified_when_required(self) -> None:
        r = _good_row(email_status="verified")
        with patch.dict(os.environ, {"ICP_REQUIRE_APOLLO_EMAIL_STATUS": "true"}, clear=False):
            self.assertIsNone(outreach_ready_reject_reason(r))

    def test_dedupe_by_linkedin(self) -> None:
        a = _good_row(email="a@acme.io", linkedin_url="https://www.linkedin.com/in/same")
        b = _good_row(email="b@acme.io", linkedin_url="https://www.linkedin.com/in/same")
        out = dedupe_rows([a, b])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["email"], "a@acme.io")

    def test_dedupe_fallback_email_when_no_linkedin(self) -> None:
        a = {**_good_row(), "linkedin_url": "", "email": "only@acme.io"}
        b = {**_good_row(), "linkedin_url": "", "email": "only@acme.io"}
        out = dedupe_rows([a, b])
        self.assertEqual(len(out), 1)


if __name__ == "__main__":
    unittest.main()
