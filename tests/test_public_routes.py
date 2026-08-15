import os
import unittest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SITE_BASE_URL", "https://ibox-tv.com")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")

from tv_app.app import app
from tv_app.models import TVShow, db


class PublicRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.config.update(TESTING=True)
        cls.client = app.test_client()

    def setUp(self):
        with app.app_context():
            db.drop_all()
            db.create_all()
            db.session.add_all(
                [
                    TVShow(
                        tmdb_id=101,
                        message_id=1001,
                        show_name="The Ark",
                        episode_title="Season 3 Episode 1-3",
                        download_link="https://t.me/example?start=the-ark",
                        overview="A crew fights to keep humanity alive on a deep-space mission.",
                        poster_path="https://image.tmdb.org/t/p/w500/the-ark.jpg",
                        year=2023,
                        rating=6.4,
                        category="tv",
                        content_hash="tv-101",
                        slug="the-ark",
                    ),
                    TVShow(
                        tmdb_id=202,
                        message_id=2002,
                        show_name="Blue Lock",
                        episode_title="Season 2 Complete",
                        download_link="https://t.me/example?start=blue-lock",
                        overview="Young strikers compete in an elite football program.",
                        poster_path="https://image.tmdb.org/t/p/w500/blue-lock.jpg",
                        year=2022,
                        rating=8.1,
                        category="anime",
                        content_hash="anime-202",
                        slug="blue-lock",
                    ),
                    TVShow(
                        tmdb_id=303,
                        message_id=3003,
                        show_name="Moonlight Run",
                        download_link="https://t.me/example?start=moonlight-run",
                        overview="A night drive becomes an unexpected race home.",
                        poster_path="https://image.tmdb.org/t/p/w500/moonlight-run.jpg",
                        year=2025,
                        rating=7.3,
                        category="movie",
                        content_hash="movie-303",
                        slug="moonlight-run",
                    ),
                    TVShow(
                        tmdb_id=404,
                        message_id=4004,
                        show_name="Incomplete Listing",
                        episode_title="Episode 1",
                        download_link="https://t.me/example?start=incomplete",
                        overview=None,
                        poster_path=None,
                        category="tv",
                        content_hash="tv-404",
                        slug="incomplete-listing",
                    ),
                ]
            )
            db.session.commit()

    def assert_contains(self, response, text):
        self.assertIn(text, response.get_data(as_text=True))

    def test_homepage_uses_clean_canonical_and_heading(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assert_contains(response, '<link rel="canonical" href="https://ibox-tv.com/">')
        self.assert_contains(response, "<h1>")
        self.assertNotIn("?search=&amp;page=1", response.get_data(as_text=True))

    def test_category_pages_live_on_one_host(self):
        self.assertEqual(self.client.get("/anime").status_code, 200)
        self.assertEqual(self.client.get("/movies").status_code, 200)
        self.assertEqual(self.client.get("/browse/tv").status_code, 200)
        self.assertEqual(self.client.get("/browse/anime").status_code, 200)
        self.assert_contains(self.client.get("/anime"), "https://ibox-tv.com/anime")
        self.assert_contains(self.client.get("/movies"), "https://ibox-tv.com/movies")

    def test_legacy_subdomain_redirects_to_primary_host(self):
        previous_testing = app.testing
        app.testing = False
        try:
            response = self.client.get(
                "/show/the-ark?ref=legacy",
                base_url="https://anime.ibox-tv.com",
            )
        finally:
            app.testing = previous_testing
        self.assertEqual(response.status_code, 301)
        self.assertEqual(
            response.headers["Location"],
            "https://ibox-tv.com/show/the-ark?ref=legacy",
        )

    def test_legacy_and_wrong_category_detail_routes_redirect(self):
        legacy = self.client.get("/show/the-ark")
        wrong_category = self.client.get("/anime/the-ark")
        self.assertEqual(legacy.status_code, 301)
        self.assertEqual(legacy.headers["Location"], "/tv/101-the-ark")
        self.assertEqual(wrong_category.status_code, 301)
        self.assertEqual(wrong_category.headers["Location"], "/tv/101-the-ark")

    def test_detail_canonical_strips_query_and_renders_social_metadata(self):
        response = self.client.get("/tv/101-the-ark?utm_source=test")
        self.assertEqual(response.status_code, 200)
        self.assert_contains(response, '<link rel="canonical" href="https://ibox-tv.com/tv/101-the-ark">')
        self.assert_contains(response, '<meta property="og:title"')
        self.assert_contains(response, '<meta name="twitter:card"')

    def test_incomplete_detail_is_noindex_and_absent_from_sitemap(self):
        response = self.client.get("/tv/404-incomplete-listing")
        self.assert_contains(response, '<meta name="robots" content="noindex,follow">')
        sitemap = self.client.get("/sitemaps/tv-1.xml")
        self.assertEqual(sitemap.status_code, 200)
        xml = sitemap.get_data(as_text=True)
        self.assertIn("https://ibox-tv.com/tv/101-the-ark", xml)
        self.assertNotIn("incomplete-listing", xml)

    def test_search_and_filter_pages_are_noindex_but_crawlable(self):
        search = self.client.get("/?search=ark")
        filtered = self.client.get("/browse/tv?year=2023")
        self.assert_contains(search, '<meta name="robots" content="noindex,follow">')
        self.assert_contains(filtered, '<meta name="robots" content="noindex,follow">')

    def test_sitemap_index_is_segmented_by_category(self):
        response = self.client.get("/sitemap.xml")
        self.assertEqual(response.status_code, 200)
        xml = response.get_data(as_text=True)
        self.assertIn("https://ibox-tv.com/sitemaps/core.xml", xml)
        self.assertIn("https://ibox-tv.com/sitemaps/tv-1.xml", xml)
        self.assertIn("https://ibox-tv.com/sitemaps/anime-1.xml", xml)
        self.assertIn("https://ibox-tv.com/sitemaps/movies-1.xml", xml)

    def test_trust_pages_and_static_manifest_exist(self):
        self.assertEqual(self.client.get("/privacy-policy").status_code, 200)
        self.assertEqual(self.client.get("/about").status_code, 200)
        self.assertEqual(self.client.get("/static/site.webmanifest").status_code, 200)

    def test_robots_allows_search_pages_to_be_crawled_for_noindex(self):
        response = self.client.get("/robots.txt")
        body = response.get_data(as_text=True)
        self.assertNotIn("Disallow: /?search=", body)
        self.assertIn("Disallow: /nuke", body)

    def test_download_click_drives_trending_metric(self):
        with app.app_context():
            show = TVShow.query.filter_by(slug="the-ark").first()
            show_id = show.id
            self.assertEqual(show.clicks, 0)
        response = self.client.get(f"/download/{show_id}")
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            self.assertEqual(TVShow.query.get(show_id).clicks, 1)

    def test_security_headers_are_added(self):
        response = self.client.get("/")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["Referrer-Policy"], "strict-origin-when-cross-origin")


if __name__ == "__main__":
    unittest.main()
