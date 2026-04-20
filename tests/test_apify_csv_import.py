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


if __name__ == "__main__":
    unittest.main()
