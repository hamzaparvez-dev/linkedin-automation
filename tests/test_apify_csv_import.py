"""Apify CSV column mapping for dashboard import."""

from __future__ import annotations

import unittest

from dashboard_app.apify_csv_import import map_apify_csv_row


class TestApifyCsvImport(unittest.TestCase):
    def test_typical_apify_headers(self) -> None:
        row = {
            "url": "https://www.linkedin.com/in/example-person/",
            "firstName": "Ada",
            "lastName": "Lovelace",
            "currentJobTitle": "Engineer",
            "companyName": "Analytical Engines Inc",
        }
        m = map_apify_csv_row(row)
        self.assertIsNotNone(m)
        assert m is not None
        self.assertEqual(m["linkedin_url"], "https://linkedin.com/in/example-person")
        self.assertEqual(m["first_name"], "Ada")
        self.assertEqual(m["last_name"], "Lovelace")
        self.assertEqual(m["full_name"], "Ada Lovelace")
        self.assertEqual(m["title"], "Engineer")
        self.assertEqual(m["company_name"], "Analytical Engines Inc")

    def test_missing_linkedin_returns_none(self) -> None:
        self.assertIsNone(map_apify_csv_row({"firstName": "X", "lastName": "Y"}))

    def test_explicit_lead_id_hex(self) -> None:
        lid = "a" * 24
        row = {"lead_id": lid, "url": "https://linkedin.com/in/z"}
        m = map_apify_csv_row(row)
        self.assertIsNotNone(m)
        assert m is not None
        self.assertEqual(m["lead_id"], lid)

    def test_google_sheet_post_text_headers(self) -> None:
        row = {
            "Name": "Jane Doe",
            "Headline": "Building in Web3",
            "occupation": "Founder",
            "Profile Url": "https://www.linkedin.com/in/jane-doe/",
            "Post url": "https://www.linkedin.com/feed/update/urn:li:activity:123",
            "Post text": "Excited to share our new protocol launch.",
        }
        m = map_apify_csv_row(row)
        self.assertIsNotNone(m)
        assert m is not None
        self.assertEqual(m["full_name"], "Jane Doe")
        self.assertEqual(m["linkedin_headline"], "Building in Web3")
        self.assertEqual(m["title"], "Founder")
        self.assertEqual(m["post_url"], "https://www.linkedin.com/feed/update/urn:li:activity:123")
        self.assertEqual(m["post_text"], "Excited to share our new protocol launch.")
        self.assertNotEqual(m["title"], m["linkedin_headline"])


if __name__ == "__main__":
    unittest.main()
