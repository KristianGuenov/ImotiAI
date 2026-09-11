import asyncio
import gzip
from io import BytesIO
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from scraper.runner import (
    ApiClient,
    HttpInventoryExtractor,
    OlxApiExtractor,
    PlaywrightExtractor,
    ScrapeTarget,
    SitemapExtractor,
    TargetsLoader,
    playwright_proxy_from_env,
)
from scraper.listing_health import HealthVerdict, ListingHealthValidator, classify_response
from scraper.detail_runner import (
    DomainCircuitBreaker,
    _docx_text,
    _document_extension,
    _is_boilerplate_text,
    _next_queue_offset,
    clean_extracted_media,
    has_meaningful_detail,
    load_rules,
    make_detail_payload,
)


class TargetsLoaderTests(unittest.TestCase):
    def _load(self, yaml_text):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "targets.yml"
            path.write_text(yaml_text, encoding="utf-8")
            return TargetsLoader(str(path)).load()

    def test_disabled_targets_are_skipped(self):
        targets = self._load(
            "- name: blocked\n  enabled: false\n  url: https://example.com/a\n"
            "- name: live\n  url: https://example.com/b\n"
        )
        self.assertEqual([target.name for target in targets], ["live"])

    def test_duplicate_url_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate source URL"):
            self._load(
                "- name: first\n  url: https://example.com/a\n"
                "- name: second\n  url: https://example.com/a\n"
            )

    def test_authoritative_inventory_supersedes_domain_partitions(self):
        targets = self._load(
            "- name: old\n  url: https://example.com/city\n"
            "- name: inventory\n  url: https://example.com/sitemap.xml\n"
            "  mode: sitemap\n  authoritative_domain_inventory: true\n"
        )
        self.assertEqual([target.name for target in targets], ["inventory"])

    def test_unknown_mode_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unsupported mode"):
            self._load(
                "- name: broken\n  url: https://example.com/a\n  mode: magic\n"
            )

    def test_olx_api_requires_category_id(self):
        with self.assertRaisesRegex(ValueError, "requires api_category_id"):
            self._load(
                "- name: olx\n  url: https://www.olx.bg/x\n  mode: olx_api\n"
            )

    def test_unknown_validation_mode_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unsupported validation_mode"):
            self._load(
                "- name: broken\n  url: https://example.com/a\n"
                "  validation_mode: wishful-thinking\n"
            )

    def test_proxy_url_is_translated_without_embedding_credentials(self):
        with patch.dict(
            "os.environ",
            {"SCRAPER_PROXY_URL": "http://alice:secret@proxy.example:8080"},
        ):
            self.assertEqual(
                playwright_proxy_from_env(),
                {
                    "server": "http://proxy.example:8080",
                    "username": "alice",
                    "password": "secret",
                },
            )


class ListingHealthTests(unittest.TestCase):
    def classify(self, **overrides):
        values = {
            "original_url": "https://example.com/listing/123",
            "final_url": "https://example.com/listing/123",
            "status_code": 200,
            "body": b"<html><title>Live property 123</title><body>Details</body></html>",
            "content_type": "text/html; charset=utf-8",
            "listing_url_regex": r"^https://example\.com/listing/[0-9]+$",
            "body_expected": True,
        }
        values.update(overrides)
        return classify_response(**values)

    def test_accepts_live_listing(self):
        self.assertEqual(self.classify().state, "valid")

    def test_rejects_hard_404(self):
        verdict = self.classify(status_code=404, body=b"")
        self.assertEqual((verdict.state, verdict.reason), ("dead", "http_404"))

    def test_rejects_listing_redirected_to_category(self):
        verdict = self.classify(final_url="https://example.com/listings")
        self.assertEqual(
            (verdict.state, verdict.reason), ("dead", "redirected_off_listing")
        )

    def test_rejects_cross_domain_redirect(self):
        verdict = self.classify(final_url="https://ads.example.net/listing/123")
        self.assertEqual(
            (verdict.state, verdict.reason), ("dead", "cross_domain_redirect")
        )

    def test_rejects_soft_inactive_page(self):
        verdict = self.classify(
            body=(
                "<html><title>Обявата вече не е активна</title>"
                "<body>Няма налични данни.</body></html>"
            ).encode()
        )
        self.assertEqual((verdict.state, verdict.reason), ("dead", "soft_404_title"))

    def test_empty_and_block_pages_are_unverifiable_not_dead(self):
        self.assertEqual(self.classify(body=b"").state, "unverifiable")
        blocked = self.classify(
            body=b"<html><title>Just a moment...</title>" + b"x" * 200
        )
        self.assertEqual((blocked.state, blocked.reason), ("unverifiable", "block_page"))

    def test_accepts_www_only_canonicalization(self):
        verdict = self.classify(
            original_url="https://www.example.com/listing/123",
            final_url="https://example.com/listing/123",
            listing_url_regex=r"^https://www\.example\.com/listing/[0-9]+$",
        )
        self.assertEqual(verdict.state, "valid")

    def test_accepts_yavlena_localized_canonical_redirect(self):
        verdict = classify_response(
            original_url="https://www.yavlena.com/20062/rent",
            final_url="https://www.yavlena.com/bg/20062/rent",
            status_code=200,
            listing_url_regex=(
                r"^https://www\.yavlena\.com/(?:bg/)?[0-9]+(?:/rent)?/?$"
            ),
            body_expected=False,
        )
        self.assertEqual(verdict.state, "valid")


class DetailRunnerSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_publisher_block_opens_domain_circuit_at_threshold(self):
        circuit = DomainCircuitBreaker(blocked_threshold=2, incomplete_threshold=5)
        self.assertFalse(
            await circuit.failure("olx.bg", "publisher_blocked", "http_403")
        )
        self.assertTrue(
            await circuit.failure("olx.bg", "publisher_blocked", "http_403")
        )
        self.assertIn("http_403", await circuit.reason("olx.bg"))
        self.assertIsNone(await circuit.reason("homes.bg"))

    async def test_success_resets_failure_streak(self):
        circuit = DomainCircuitBreaker(blocked_threshold=2)
        await circuit.failure("example.bg", "publisher_blocked", "http_429")
        await circuit.success("example.bg")
        self.assertFalse(
            await circuit.failure("example.bg", "publisher_blocked", "http_429")
        )
        self.assertIsNone(await circuit.reason("example.bg"))

    async def test_domain_rule_loads_bounded_request_delay(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rules.yml"
            path.write_text(
                "defaults:\n  request_delay_ms: 250\n"
                "domains:\n- domain: slow.bg\n  request_delay_ms: 2500\n"
                "- domain: bounded.bg\n  request_delay_ms: 999999\n",
                encoding="utf-8",
            )
            default, rules = load_rules(str(path))
        self.assertEqual(default.request_delay_ms, 250)
        self.assertEqual(rules[0].request_delay_ms, 2500)
        self.assertEqual(rules[1].request_delay_ms, 60_000)

class ListingHealthConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_domain_cannot_starve_another_of_global_slots(self):
        validator = ListingHealthValidator(
            global_concurrency=2,
            per_domain_concurrency=1,
            retries=1,
        )
        started = []
        two_domains_started = asyncio.Event()
        release = asyncio.Event()

        async def fake_read(method, url, listing_url_regex):
            started.append(url)
            if len({entry.split("/")[2] for entry in started}) == 2:
                two_domains_started.set()
            await release.wait()
            return HealthVerdict("valid", "test", 200, url)

        validator._read_response = fake_read
        tasks = [
            asyncio.create_task(
                validator.check(
                    "https://a.example/listing/1",
                    mode="head",
                    listing_url_regex=None,
                )
            ),
            asyncio.create_task(
                validator.check(
                    "https://a.example/listing/2",
                    mode="head",
                    listing_url_regex=None,
                )
            ),
            asyncio.create_task(
                validator.check(
                    "https://b.example/listing/1",
                    mode="head",
                    listing_url_regex=None,
                )
            ),
        ]
        try:
            await asyncio.wait_for(two_domains_started.wait(), timeout=0.25)
        finally:
            release.set()
            await asyncio.gather(*tasks)
            await validator.close()


class HttpInventoryExtractorTests(unittest.TestCase):
    def test_parses_address_offers_object_and_builds_item(self):
        embedded = (
            '{&quot;current_page&quot;:2,&quot;last_page&quot;:3,'
            '&quot;total&quot;:41,&quot;data&quot;:[{&quot;id&quot;:707931,'
            '&quot;is_active&quot;:1,&quot;url&quot;:'
            '&quot;https:\\/\\/address.bg\\/sofia-offer707931&quot;,'
            '&quot;estateTypeLabel&quot;:&quot;Двустаен апартамент&quot;,'
            '&quot;imageUrl370&quot;:'
            '&quot;https:\\/\\/address.bg\\/img\\/1.jpg&quot;,'
            '&quot;location&quot;:{&quot;translated&quot;:{&quot;name&quot;:'
            '&quot;София&quot;}},&quot;quarter&quot;:{&quot;translated&quot;:'
            '{&quot;name&quot;:&quot;Център&quot;}}}]}'
        )
        page = HttpInventoryExtractor.parse_page(
            f'<catalog :offers-object="{embedded}"></catalog>'.encode()
        )
        item = HttpInventoryExtractor.offer_item(page["data"][0])

        self.assertEqual(page["current_page"], 2)
        self.assertEqual(item["url"], "https://address.bg/sofia-offer707931")
        self.assertIn("София", item["title"])
        self.assertEqual(item["images"], ["https://address.bg/img/1.jpg"])

    def test_inactive_offer_is_not_emitted(self):
        self.assertIsNone(
            HttpInventoryExtractor.offer_item(
                {"url": "https://address.bg/x-offer1", "is_active": 0}
            )
        )

    def test_page_url_preserves_existing_query(self):
        self.assertEqual(
            HttpInventoryExtractor.page_url(
                "https://example.com/sale?kind=flat", 7
            ),
            "https://example.com/sale?kind=flat&page=7",
        )


class OlxApiExtractorTests(unittest.TestCase):
    def test_emits_only_active_property_offers(self):
        item = OlxApiExtractor.offer_item(
            {
                "status": "active",
                "offer_type": "offer",
                "title": "Апартамент",
                "url": "https://www.olx.bg/d/ad/a-CID368-IDabc.html",
                "photos": [
                    {
                        "link": (
                            "https://img.example/image;s={width}x{height}"
                        )
                    }
                ],
            }
        )
        self.assertEqual(item["title"], "Апартамент")
        self.assertEqual(item["images"], ["https://img.example/image;s=800x600"])
        self.assertIsNone(
            OlxApiExtractor.offer_item(
                {
                    "status": "removed",
                    "offer_type": "offer",
                    "url": "https://www.olx.bg/d/ad/a-CID368-IDabc.html",
                }
            )
        )

    def test_price_partition_has_no_gap_at_boundary(self):
        self.assertEqual(OlxApiExtractor._split_pivot(None, None), 100000)
        self.assertEqual(
            OlxApiExtractor._price_params(None, 100000),
            {"filter_float_price:to": "100000"},
        )
        self.assertEqual(
            OlxApiExtractor._price_params(100000, None),
            {"filter_float_price:from": "100000"},
        )


class SitemapExtractorTests(unittest.TestCase):
    def test_parses_urlset_images_and_lastmod(self):
        xml = b"""<?xml version='1.0'?>
        <urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'
                xmlns:image='http://www.google.com/schemas/sitemap-image/1.1'>
          <url><loc>https://example.com/listing/1</loc><lastmod>2026-09-01</lastmod>
            <image:image><image:loc>https://img.example/1.jpg</image:loc>
              <image:title>Home one</image:title></image:image></url>
        </urlset>"""
        kind, children, items = SitemapExtractor()._parse_document(xml)
        self.assertEqual(kind, "urlset")
        self.assertEqual(children, [])
        self.assertEqual(items[0]["url"], "https://example.com/listing/1")
        self.assertEqual(items[0]["title"], "Home one")
        self.assertEqual(items[0]["images"], ["https://img.example/1.jpg"])
        self.assertEqual(items[0]["texts"], ["2026-09-01"])

    def test_parses_sitemap_index(self):
        xml = b"""<sitemapindex xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>
          <sitemap><loc>https://example.com/one.xml</loc></sitemap>
          <sitemap><loc>https://example.com/two.xml</loc></sitemap>
        </sitemapindex>"""
        kind, children, items = SitemapExtractor()._parse_document(xml)
        self.assertEqual(kind, "sitemapindex")
        self.assertEqual(children, ["https://example.com/one.xml", "https://example.com/two.xml"])
        self.assertEqual(items, [])

    def test_parses_gzipped_sitemap(self):
        xml = b"""<?xml version='1.0' encoding='UTF-8'?>
        <urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>
          <url><loc>https://example.com/listing/1</loc></url>
        </urlset>"""
        kind, children, items = SitemapExtractor()._parse_document(
            gzip.compress(xml)
        )
        self.assertEqual(kind, "urlset")
        self.assertEqual(children, [])
        self.assertEqual(items[0]["url"], "https://example.com/listing/1")


class SitemapFailureRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_overlapping_sitemap_shards(self):
        xml = b"""<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>
          <url><loc>https://example.com/listing/1</loc></url>
          <url><loc>https://example.com/listing/1</loc></url>
        </urlset>"""
        extractor = SitemapExtractor()
        extractor._fetch_xml = lambda _client, _url: asyncio.sleep(0, result=xml)

        class FakeApi:
            async def post_extraction(self, _payload):
                return {"id": 1}

        target = ScrapeTarget(
            name="inventory",
            url="https://example.com/index.xml",
            mode="sitemap",
            listing_url_regex=r"^https://example\.com/listing/[0-9]+$",
            sitemap_min_unique_ratio=0.9,
        )
        with self.assertRaisesRegex(RuntimeError, "sitemap shard overlap"):
            await extractor.scrape_target(
                FakeApi(), target, seen_item_keys=set()
            )

    async def test_skip_existing_validates_only_missing_urls(self):
        xml = b"""<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>
          <url><loc>https://example.com/listing/1</loc></url>
          <url><loc>https://example.com/listing/2</loc></url>
        </urlset>"""
        extractor = SitemapExtractor()

        async def fake_fetch(_client, _url):
            return xml

        extractor._fetch_xml = fake_fetch

        class FakeApi:
            def __init__(self):
                self.posts = []

            async def existing_active_urls(self, _urls):
                return {"https://example.com/listing/1"}

            async def post_extraction(self, payload):
                self.posts.append(payload)
                return {"id": 1}

        api = FakeApi()
        target = ScrapeTarget(
            name="inventory",
            url="https://example.com/index.xml",
            mode="sitemap",
            listing_url_regex=r"^https://example\.com/listing/[0-9]+$",
        )
        seen = set()
        await extractor.scrape_target(
            api,
            target,
            seen_item_keys=seen,
            skip_existing=True,
        )

        self.assertEqual(len(seen), 2)
        self.assertEqual(
            [item["url"] for item in api.posts[0]["items"]],
            ["https://example.com/listing/2"],
        )

    async def test_resume_checkpoint_posts_only_unhandled_candidates(self):
        xml = b"""<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>
          <url><loc>https://example.com/listing/1</loc></url>
          <url><loc>https://example.com/listing/2</loc></url>
          <url><loc>https://example.com/listing/3</loc></url>
        </urlset>"""
        extractor = SitemapExtractor()

        async def fake_fetch(_client, _url):
            return xml

        extractor._fetch_xml = fake_fetch

        class FakeApi:
            def __init__(self):
                self.posts = []

            async def post_extraction(self, payload):
                self.posts.append(payload)
                return {"id": len(self.posts)}

        api = FakeApi()
        target = ScrapeTarget(
            name="inventory",
            url="https://example.com/index.xml",
            mode="sitemap",
            listing_url_regex=r"^https://example\.com/listing/[0-9]+$",
        )

        await extractor.scrape_target(
            api,
            target,
            seen_item_keys=set(),
            resume_after_items=2,
        )

        self.assertEqual(
            [item["url"] for item in api.posts[0]["items"]],
            ["https://example.com/listing/3"],
        )

    async def test_failed_child_does_not_discard_reachable_children(self):
        root = b"""<sitemapindex xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>
          <sitemap><loc>https://example.com/live.xml</loc></sitemap>
          <sitemap><loc>https://example.com/stale.xml</loc></sitemap>
        </sitemapindex>"""
        live = b"""<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>
          <url><loc>https://example.com/listing/1</loc></url>
        </urlset>"""

        extractor = SitemapExtractor()

        async def fake_fetch(_client, url):
            if url == "https://example.com/index.xml":
                return root
            if url == "https://example.com/live.xml":
                return live
            raise RuntimeError("HTTP 404")

        extractor._fetch_xml = fake_fetch

        class FakeApi:
            def __init__(self):
                self.posts = []

            async def post_extraction(self, payload):
                self.posts.append(payload)
                return {"id": len(self.posts)}

        api = FakeApi()
        target = ScrapeTarget(
            name="inventory",
            url="https://example.com/index.xml",
            mode="sitemap",
            listing_url_regex=r"^https://example\.com/listing/[0-9]+$",
        )
        with self.assertRaisesRegex(RuntimeError, "1 sitemap children failed"):
            await extractor.scrape_target(api, target, seen_item_keys=set())

        self.assertEqual(len(api.posts), 1)
        self.assertEqual(
            api.posts[0]["items"][0]["url"],
            "https://example.com/listing/1",
        )
        self.assertEqual(
            api.posts[0]["meta"]["sitemapFailures"][0]["url"],
            "https://example.com/stale.xml",
        )


class PaginationTests(unittest.TestCase):
    def setUp(self):
        self.extractor = PlaywrightExtractor("extension/content.js")

    def test_query_page_urls_preserve_existing_filters(self):
        result = self.extractor._manual_page_url(
            "https://www.alo.bg/obiavi/imoti-prodajbi/apartamenti-stai/?foo=bar",
            3,
        )
        self.assertEqual(
            result,
            "https://www.alo.bg/obiavi/imoti-prodajbi/apartamenti-stai/?foo=bar&page=3",
        )

    def test_bcpea_uses_p_query_parameter(self):
        result = self.extractor._manual_page_url(
            "https://sales.bcpea.org/properties?perpage=36",
            3,
        )
        self.assertEqual(
            result,
            "https://sales.bcpea.org/properties?perpage=36&p=3",
        )

    def test_bulgarian_properties_uses_index_number_pages(self):
        result = self.extractor._manual_page_url(
            "https://www.bulgarianproperties.com/sale_properties/index.html",
            84,
        )
        self.assertEqual(
            result,
            "https://www.bulgarianproperties.com/sale_properties/index84.html",
        )

    def test_property_selection_uses_path_pages(self):
        result = self.extractor._propertybg_page_url(
            "https://www.property.bg/rentals/bulgaria/selection/",
            3,
        )
        self.assertEqual(
            result,
            "https://www.property.bg/rentals/bulgaria/selection/page/3/",
        )

    def test_listing_url_filter_rejects_navigation_links(self):
        target = ScrapeTarget(
            name="bp",
            url="https://www.bulgarianproperties.com/sale_properties/index.html",
            listing_url_regex=r"/AD[0-9]+BG_.*\.html$",
        )
        payload = {
            "items": [
                {
                    "url": "https://www.bulgarianproperties.com/Houses_in_Bulgaria/AD123BG_House.html"
                },
                {
                    "url": "https://www.bulgarianproperties.com/Burgas_property/index.html"
                },
            ]
        }
        filtered = self.extractor._filter_payload_items(payload, target)
        self.assertEqual(len(filtered["items"]), 1)
        self.assertIn("AD123BG", filtered["items"][0]["url"])

    def test_listing_filter_rejects_external_non_http_and_mixed_auction_ads(self):
        target = ScrapeTarget(
            name="municipal",
            url="https://example.bg/tenders",
            listing_url_regex=r"/notice/[0-9]+$",
            listing_title_regex=r"(имот|сграда)",
            listing_title_exclude_regex=r"движими",
        )
        payload = {
            "items": [
                {"url": "https://example.bg/notice/1", "title": "Продажба на имот"},
                {"url": "https://example.bg/notice/2", "title": "Продажба на движими вещи"},
                {"url": "https://facebook.com/example", "title": "Facebook"},
                {"url": "javascript:;", "title": "Разширено търсене"},
            ]
        }
        filtered = self.extractor._filter_payload_items(payload, target)
        self.assertEqual(filtered["items"], [payload["items"][0]])

    def test_domaza_search_context_suffix_is_canonicalized(self):
        target = ScrapeTarget(
            name="domaza",
            url="https://www.domaza.bg/listings/",
            listing_url_regex=r"^https://www\.domaza\.bg/.+-[0-9]+-[0-9]+-p/$",
        )
        payload = {
            "items": [
                {
                    "url": (
                        "https://www.domaza.bg/apartment-sofia-16-8356211-p/"
                        "_hasSearch/1/"
                    )
                }
            ]
        }
        filtered = self.extractor._filter_payload_items(payload, target)
        self.assertEqual(
            filtered["items"][0]["url"],
            "https://www.domaza.bg/apartment-sofia-16-8356211-p/",
        )

    def test_ubb_uses_stable_path_pagination(self):
        result = self.extractor._manual_page_url(
            "https://estates.ubb.bg/?listing_type_id=1", 4
        )
        self.assertEqual(
            result,
            "https://estates.ubb.bg/list/page:4?listing_type_id=1",
        )

    def test_site_override_json_is_injectable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            path.write_text(
                '{"siteOverrides":{"example.com":{"selectorCards":["article"]}}}',
                encoding="utf-8",
            )
            old = __import__("os").environ.get("PROFILE_SINK_OUT")
            __import__("os").environ["PROFILE_SINK_OUT"] = str(path)
            try:
                script = self.extractor._load_site_overrides_json_script()
            finally:
                if old is None:
                    __import__("os").environ.pop("PROFILE_SINK_OUT", None)
                else:
                    __import__("os").environ["PROFILE_SINK_OUT"] = old
            self.assertIn("__imotiRunnerSiteOverrides", script)
            self.assertIn("example.com", script)


class DomazaFallbackExtractionTests(unittest.IsolatedAsyncioTestCase):
    async def test_domaza_property_anchors_are_merged_into_an_empty_result(self):
        extractor = PlaywrightExtractor("extension/content.js")

        async def extractor_is_ready(_page):
            return None

        extractor._ensure_extractor = extractor_is_ready

        class FakePage:
            url = "https://www.domaza.bg/properties/_page/1/"

            async def evaluate(self, script):
                if "__imotiExtractor.run" in script:
                    return {"ok": True, "result": {"items": []}}
                return [
                    {
                        "url": (
                            "https://www.domaza.bg/apartment-sofia-16-8356211-p/"
                            "_hasSearch/1/"
                        ),
                        "title": "Apartment Sofia",
                    }
                ]

        result = await extractor._extract(FakePage())
        self.assertEqual(len(result["items"]), 1)
        self.assertIn("8356211", result["items"][0]["url"])


class DomazaFallbackMergeTests(unittest.IsolatedAsyncioTestCase):
    async def test_domaza_property_anchors_are_merged_when_generic_result_is_empty(self):
        extractor = PlaywrightExtractor("extension/content.js")

        async def no_op(_page):
            return None

        extractor._ensure_extractor = no_op

        class FakePage:
            url = "https://www.domaza.bg/properties/_page/1/"

            async def evaluate(self, script):
                if "__imotiExtractor.run" in script:
                    return {"ok": True, "result": {"items": []}}
                return [
                    {
                        "url": "https://www.domaza.bg/apartment-sofia-16-8356211-p/_hasSearch/1/",
                        "title": "Apartment",
                    }
                ]

        payload = await extractor._extract(FakePage())
        self.assertEqual(len(payload["items"]), 1)
        self.assertIn("8356211", payload["items"][0]["url"])


class DetailPayloadTests(unittest.TestCase):
    def test_detail_drain_offset_defers_only_failed_rows(self):
        self.assertEqual(_next_queue_offset(7, seen=20, posted=16), 11)
        self.assertEqual(_next_queue_offset(11, seen=20, posted=20), 11)

    def test_cookie_consent_is_not_meaningful_detail(self):
        consent = (
            "Ние и нашите партньори използваме бисквитки и подобни технологии "
            "за обработване на лични данни. Настройки на бисквитките."
        )
        self.assertTrue(_is_boilerplate_text(consent))
        self.assertEqual(
            has_meaningful_detail(
                {"description": consent, "raw_text_blocks": [{"text": consent}]},
                60,
            )[0],
            False,
        )

    def test_known_publisher_chrome_is_removed_from_canonical_media(self):
        cleaned = clean_extracted_media(
            "https://estates.ubb.bg/sales/1",
            {
                "image": "https://estates.ubb.bg/images/og_image.png",
                "images": [
                    "https://estates.ubb.bg/images/og_image.png",
                    "https://estates.ubb.bg/attachments/Listing/1/main/home.jpg",
                    "https://estates.ubb.bg/images/arrow-top.png",
                ],
            },
        )
        self.assertEqual(
            cleaned["image"],
            "https://estates.ubb.bg/attachments/Listing/1/main/home.jpg",
        )
        self.assertEqual(cleaned["images"], [cleaned["image"]])

    def test_imot_media_keeps_only_current_listing_gallery(self):
        own = "https://cdn.focus.bg/imot/photosimotbg/1/595/big/1b175023352321595_a.jpg"
        cleaned = clean_extracted_media(
            "https://www.imot.bg/obiava-1b175023352321595-prodava-apartament",
            {
                "image": own,
                "images": [
                    own,
                    "https://www.imot.bg/images/logos/big/agency.pic",
                    "https://cdn.focus.bg/imot/photosimotbg/1/999/1k178463761589836_b.jpg",
                ],
            },
        )
        self.assertEqual(cleaned["image"], own)
        self.assertEqual(cleaned["images"], [own])

    def test_focus_gallery_anchors_to_first_listing_photo(self):
        own = "https://cdn.focus.bg/imot/photosimotbg/1/020/big/1a173764051952020_A.jpg"
        duplicate_size = "https://cdn.focus.bg/imot/photosimotbg/1/020/med/1a173764051952020_A.jpg"
        related = "https://cdn.focus.bg/imot/photosimotbg/1/999/1a178557742959772_B.jpg"
        cleaned = clean_extracted_media(
            "https://www.holmes.bg/obiava/99839073/property",
            {"image": own, "images": [own, duplicate_size, related]},
        )
        self.assertEqual(cleaned["images"], [own])

    def test_address_media_excludes_related_offers_and_size_duplicates(self):
        cover = "https://address.bg/storage/uploads/offers/419/681419/764x510/1.jpg"
        cleaned = clean_extracted_media(
            "https://address.bg/sofia-office-offer681419",
            {
                "image": cover,
                "images": [
                    cover,
                    "https://address.bg/storage/uploads/offers/419/681419/370x200/1.webp",
                    "https://address.bg/storage/uploads/offers/419/681419/370x200/2.jpg",
                    "https://address.bg/storage/uploads/offers/421/681421/370x200/1.jpg",
                    "https://maps.googleapis.com/map.png",
                ],
            },
        )
        self.assertEqual(cleaned["image"], cover)
        self.assertEqual(
            cleaned["images"],
            [cover, "https://address.bg/storage/uploads/offers/419/681419/370x200/2.jpg"],
        )

    def test_olx_media_deduplicates_responsive_variants(self):
        large = "https://cdn.olx.com/v1/files/photo-one/image;s=1200x0"
        cleaned = clean_extracted_media(
            "https://www.olx.bg/d/ad/property-ID1.html",
            {
                "image": large,
                "images": [
                    large,
                    "https://cdn.olx.com/v1/files/photo-one/image;s=640x480",
                    "https://img-resizer.prd.01.eu-west-1.eu.olx.org/related.jpg",
                ],
            },
        )
        self.assertEqual(cleaned["images"], [large])

    def test_arcoreal_media_excludes_site_chrome(self):
        own = "https://arcoreal.bg/image?id=604102&w=255&h=254&crop=1"
        cleaned = clean_extracted_media(
            "https://arcoreal.bg/offers/example-91418",
            {
                "image": "https://www.arcoreal.bg/img/logo-bg.png",
                "images": [
                    "https://www.arcoreal.bg/img/logo-bg.png",
                    "https://arcoreal.bg/image-main?id=2&type=logo-head-bg",
                    own,
                ],
            },
        )
        self.assertEqual(cleaned["image"], own)
        self.assertEqual(cleaned["images"], [own])

    def test_buildingbox_media_excludes_logo_and_chat_assets(self):
        own = "https://buildingbox.bg/wp-content/uploads/2026/08/property.jpg"
        cleaned = clean_extracted_media(
            "https://buildingbox.bg/properties/example/",
            {
                "image": own,
                "images": [
                    own,
                    "https://buildingbox.bg/wp-content/uploads/2025/03/logo.png",
                    "https://salesiq-eu.nimbuspop.com/brands/sticker.png",
                ],
            },
        )
        self.assertEqual(cleaned["image"], own)
        self.assertEqual(cleaned["images"], [own])

    def test_homes_media_stays_in_current_listing_upload_directory(self):
        cover = "https://g1.homes.bg/2021-02-14_2/60190955o.jpg"
        own_second = "https://g1.homes.bg/2021-02-14_2/60190956s.jpg"
        cleaned = clean_extracted_media(
            "https://www.homes.bg/offer/apartament-za-prodazhba/example/as1",
            {
                "image": cover,
                "images": [
                    cover,
                    "https://g1.homes.bg/2021-02-14_2/60190955s.jpg",
                    own_second,
                    "https://g1.homes.bg/2026-09-11_1/99999999o.jpg",
                    "https://www.homes.bg/images/users_logos/4391.png",
                ],
            },
        )
        self.assertEqual(cleaned["image"], cover)
        self.assertEqual(cleaned["images"], [cover, own_second])

    def test_home2u_media_deduplicates_wordpress_sizes(self):
        full = "https://home2u.skyholding.media/2026/09/house-main-1.jpg"
        cleaned = clean_extracted_media(
            "https://home2u.bg/property/example/",
            {
                "image": full,
                "images": [
                    full,
                    "https://home2u.skyholding.media/2026/09/house-main-1-1536x864.jpg",
                ],
            },
        )
        self.assertEqual(cleaned["images"], [full])

    def test_imoti_net_media_deduplicates_thumbnail_sizes(self):
        cover = (
            "https://www.imoti.net/web/files/obiavi/6302516/main_image/"
            "thumb_1200x630_wm_main_image_6302516_1.jpg?ver=1"
        )
        gallery = (
            "https://www.imoti.net/web/files/obiavi/6302516/images/"
            "thumb_1200x630_wm_images_6302516_2.jpg?ver=1"
        )
        cleaned = clean_extracted_media(
            "https://www.imoti.net/bg/obiava/6302516",
            {
                "image": cover,
                "images": [
                    cover,
                    cover.replace("1200x630", "880x0"),
                    cover.replace("thumb_1200x630_wm_", "thumb_160x90_"),
                    gallery,
                    gallery.replace("1200x630", "880x0"),
                ],
            },
        )
        self.assertEqual(cleaned["images"], [cover, gallery])

    def test_novite_sgradi_media_excludes_logo_maps_and_related_chrome(self):
        own = "https://novitesgradi.bg/wp-content/uploads/2020/06/192.1.jpg"
        cleaned = clean_extracted_media(
            "https://novitesgradi.bg/sgrada/example/",
            {
                "image": own,
                "images": [
                    own,
                    "https://novitesgradi.bg/wp-content/uploads/2020/06/192.1-1240x720.jpg.webp",
                    "https://novitesgradi.bg/wp-content/uploads/2020/06/Novite_180x60.png.webp",
                    "https://a.basemaps.cartocdn.com/light_all/17/1/2.png",
                    "https://novitesgradi.bg/wp-content/plugins/a3-lazy-load/assets/images/lazy_placeholder.gif",
                ],
            },
        )
        self.assertEqual(cleaned["image"], own)
        self.assertEqual(cleaned["images"], [own])

    def test_shared_property_template_media_uses_listing_reference(self):
        own = "https://static.superimoti.bg/property-images/big/123T142119_1.jpg"
        cleaned = clean_extracted_media(
            "https://www.suprimmo.bg/imot-142119-property/",
            {
                "image": own,
                "images": [
                    own,
                    "https://static.superimoti.bg/property-images/medium/123T142119_1.jpg",
                    "https://www.suprimmo.bg/img/logo-bg.png",
                ],
            },
        )
        self.assertEqual(cleaned["images"], [own])

    def test_revolution_media_anchors_to_cover_estate(self):
        own = "https://revolution-estate.bg/images/estate/69605/image_0.jpg"
        cleaned = clean_extracted_media(
            "https://revolution-estate.bg/imot/property-1",
            {
                "image": own,
                "images": [
                    own,
                    "https://revolution-estate.bg/images/estate/69605/medium_image_0.jpg",
                    "https://revolution-estate.bg/images/logo.png",
                    "https://revolution-estate.bg/images/estate/70000/image_0.jpg",
                ],
            },
        )
        self.assertEqual(cleaned["images"], [own])

    def test_ues_media_deduplicates_next_image_variants(self):
        proxied = (
            "https://ues.bg/_next/image?url=https%3A%2F%2Fcdn.example%2Foffers%2F"
            "offer_3_28076_1920_one.webp"
            "&w=1920&q=85"
        )
        cleaned = clean_extracted_media(
            "https://ues.bg/bg/imot/example-123",
            {
                "image": proxied,
                "images": [
                    proxied,
                    "https://cdn.example/offers/offer_3_28076_1920_one.webp",
                    "https://ues.bg/_next/image?url=https%3A%2F%2Fcdn.example%2Foffers%2Foffer_3_28076_1920_two.webp&w=640&q=85",
                    "https://cdn.example/offers/offer_3_99999_1920_related.webp",
                    "https://ues.bg/img/ues-og-image.webp",
                ],
            },
        )
        self.assertEqual(len(cleaned["images"]), 2)

    def test_mirela_media_keeps_only_current_offer_gallery(self):
        own = "https://www.mirela.bg/dynamic/i/offers/php/761/462761/3_2.jpg?v=1"
        cleaned = clean_extracted_media(
            "https://www.mirela.bg/naemi/property-462761",
            {
                "image": own,
                "images": [
                    own,
                    "https://www.mirela.bg/dynamic/i/brokers/php/45/1045/1_2.jpg",
                    "https://tile.openstreetmap.org/17/1/1.png",
                ],
            },
        )
        self.assertEqual(cleaned["images"], [own])

    def test_auction_media_keeps_only_current_property_uploads(self):
        own = "https://sales.bcpea.org/upload/91143/455118/photo.png"
        cleaned = clean_extracted_media(
            "https://sales.bcpea.org/properties/91143",
            {
                "image": own,
                "images": [
                    own,
                    "https://sales.bcpea.org/assets/images/photo-placeholder.png",
                    "https://sales.bcpea.org/upload/90476/other.png",
                ],
            },
        )
        self.assertEqual(cleaned["images"], [own])

    def test_domaza_media_keeps_only_current_listing_directory(self):
        own = "https://static.domaza.bg/images/8614408/property.jpg"
        cleaned = clean_extracted_media(
            "https://www.domaza.bg/property-sofia-16-8614408-p/",
            {
                "image": own,
                "images": [own, "https://static.domaza.bg/images/8613842/related.jpg"],
            },
        )
        self.assertEqual(cleaned["images"], [own])

    def test_imotno_discards_expiring_signed_gallery_urls(self):
        stable = "https://imotno.bg/property/9b136a0d-4174-444a-8a12-3da52153dfa6/share-image"
        cleaned = clean_extracted_media(
            "https://imotno.bg/property/9b136a0d-4174-444a-8a12-3da52153dfa6",
            {
                "image": stable,
                "images": [
                    stable,
                    "https://storage.example/object.jpg?X-Amz-Expires=900&X-Amz-Signature=x",
                ],
            },
        )
        self.assertEqual(cleaned["images"], [stable])

    def test_ubb_media_keeps_only_current_listing_attachments(self):
        own = "https://estates.ubb.bg/attachments/Listing/2257/main/home.jpg"
        cleaned = clean_extracted_media(
            "https://estates.ubb.bg/sales/2257",
            {
                "image": own,
                "images": [
                    own,
                    "https://estates.ubb.bg/attachments/Listing/2200/main/related.jpg",
                ],
            },
        )
        self.assertEqual(cleaned["images"], [own])

    def test_document_extension_ignores_query_strings(self):
        self.assertEqual(
            _document_extension("https://example.bg/notice.PDF?download=1"), ".pdf"
        )

    def test_docx_text_extracts_paragraphs(self):
        stream = BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr(
                "word/document.xml",
                """<?xml version="1.0" encoding="UTF-8"?>
                <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
                  <w:body><w:p><w:r><w:t>First auction</w:t></w:r></w:p>
                  <w:p><w:r><w:t>Second paragraph</w:t></w:r></w:p></w:body>
                </w:document>""",
            )
        self.assertEqual(_docx_text(stream.getvalue()), "First auction\nSecond paragraph")

    def test_nested_jsonld_arrays_are_flattened_to_schema_objects(self):
        payload = make_detail_payload(
            "https://example.com/listing/1",
            {
                "title": "Listing",
                "description": "Description",
                "raw_jsonld": [[{"@type": "Offer"}], {"@type": "Place"}],
            },
        )
        self.assertEqual(
            payload["items"][0]["raw_jsonld"],
            [{"@type": "Offer"}, {"@type": "Place"}],
        )


class ApiClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_dry_run_never_posts(self):
        api = ApiClient("http://127.0.0.1:1/api/v1/extractions", "", dry_run=True)
        try:
            cycle = await api.start_inventory_cycle("example.com", 1)
            result = await api.post_extraction(
                {"sourceUrl": "https://example.com", "items": [{"url": "https://example.com/1"}]}
            )
            self.assertEqual(cycle["status"], "dry_run")
            self.assertTrue(result["dryRun"])
        finally:
            await api.close()


if __name__ == "__main__":
    unittest.main()
