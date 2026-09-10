import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from backend.app.services import (
    _index_fingerprint,
    _lock_listing_urls,
    _unverifiable_urls_from_meta,
)


class IndexFingerprintTests(unittest.TestCase):
    def test_sitemap_lastmod_change_triggers_a_new_fingerprint(self):
        first = SimpleNamespace(title=None, image=None, texts=["2026-09-08"])
        second = SimpleNamespace(title=None, image=None, texts=["2026-09-09"])

        self.assertNotEqual(_index_fingerprint(first), _index_fingerprint(second))

    def test_empty_index_evidence_has_no_fingerprint(self):
        item = SimpleNamespace(title=None, image=None, texts=[])
        self.assertIsNone(_index_fingerprint(item))


class ListingLockTests(unittest.TestCase):
    def test_postgres_locks_sorted_unique_urls(self):
        session = Mock()
        session.get_bind.return_value.dialect.name = "postgresql"
        result = Mock()
        session.execute.return_value = result

        _lock_listing_urls(session, ["https://b", "https://a", "https://b"])

        params = session.execute.call_args.args[1]
        self.assertEqual(params["urls"], ["https://a", "https://b"])
        self.assertIn("pg_advisory_xact_lock", str(session.execute.call_args.args[0]))
        result.all.assert_called_once_with()

    def test_non_postgres_is_noop(self):
        session = Mock()
        session.get_bind.return_value.dialect.name = "sqlite"
        _lock_listing_urls(session, ["https://a"])
        session.execute.assert_not_called()


class UnverifiableHeartbeatTests(unittest.TestCase):
    def test_only_same_domain_http_urls_are_accepted(self):
        meta = {
            "listingValidation": {
                "unverifiableUrls": [
                    "https://www.example.com/listing/1",
                    "https://example.com/listing/1",
                    "https://ads.example.net/tracker",
                    "javascript:alert(1)",
                    None,
                ]
            }
        }
        self.assertEqual(
            _unverifiable_urls_from_meta(
                meta, source_url="https://example.com/search"
            ),
            [
                "https://www.example.com/listing/1",
                "https://example.com/listing/1",
            ],
        )


if __name__ == "__main__":
    unittest.main()
