"""Apify CSV column mapping for dashboard import."""

from __future__ import annotations

import sqlite3
import unittest

from core.db import init_schema
from core.repository import insert_lead_csv_import
from dashboard_app.apify_csv_import import import_apify_csv, map_apify_csv_row


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
            "Profile Url": "https://www.linkedin.com/in/jane-doe/",
            "Post text": "Excited to share our new protocol launch.",
            "Post url": "https://www.linkedin.com/feed/update/urn:li:activity:123",
            "occupation": "Founder",
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

    def test_company_profile_url_skipped(self) -> None:
        row = {
            "Name": "Acme Corp",
            "Headline": "29 followers",
            "Profile Url": "https://www.linkedin.com/company/acme/posts",
            "Post text": "Hello world",
            "Post url": "https://www.linkedin.com/feed/update/1",
            "occupation": "",
        }
        self.assertIsNone(map_apify_csv_row(row))

    def test_csv_import_inserts_without_column_mismatch(self) -> None:
        csv_body = (
            "Name,Headline,Profile Url,Post text,Post url,occupation\n"
            "Test User,Web3 builder,https://www.linkedin.com/in/test-user-xyz/,"
            "Shipped v2 today.,https://www.linkedin.com/feed/update/1,Founder\n"
        ).encode("utf-8")
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_schema(conn)
        try:
            imported, skipped, errors = import_apify_csv(
                conn, csv_body, "test-campaign", max_errors=5
            )
            self.assertEqual(errors, [])
            self.assertEqual(imported, 1)
            self.assertEqual(skipped, 0)
            row = conn.execute(
                "SELECT full_name, post_text, linkedin_headline, title FROM leads"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["full_name"], "Test User")
            self.assertIn("Shipped", row["post_text"])
        finally:
            conn.close()

    def test_insert_lead_csv_import_explicit_columns(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_schema(conn)
        try:
            ok = insert_lead_csv_import(
                conn,
                lead_id="b" * 24,
                linkedin_url="https://www.linkedin.com/in/abc-unique/",
                campaign_id="c1",
                full_name="A",
                linkedin_headline="H",
                post_url="https://linkedin.com/feed/1",
                post_text="Post body",
                title="CEO",
            )
            self.assertTrue(ok)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
