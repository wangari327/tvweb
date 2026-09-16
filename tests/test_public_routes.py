import os
import unittest
from datetime import datetime
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SITE_BASE_URL", "https://ibox-tv.com")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")

from tv_app.app import (
    _catalogue_exact_search_query, _detail_page_title, _popular_genres,
    _popularity_leaderboard_key, _search_phrases, app, get_trending_shows,
    refresh_genre_hub_cache,
)
from tv_app.models import Genre, TVShow, db


class FakeRedis:
    """Small in-memory Redis subset for popularity-route tests."""

    def __init__(self):
        self.values = {}
        self.sorted_sets = {}
        self.expirations = {}

    def get(self, key):
        return self.values.get(key)

    def setex(self, key, _ttl, value):
        self.values[key] = value

    def delete(self, *keys):
        removed = 0
        for key in keys:
            if key in self.values:
                del self.values[key]
                removed += 1
            if key in self.sorted_sets:
                del self.sorted_sets[key]
                removed += 1
            self.expirations.pop(key, None)
        return removed

    def zincrby(self, key, amount, member):
        values = self.sorted_sets.setdefault(key, {})
        member = str(member)
        values[member] = values.get(member, 0) + amount
        return values[member]

    def zrevrange(self, key, start, end):
        values = self.sorted_sets.get(key, {})
        ranked = [
            member for member, _score in sorted(
                values.items(), key=lambda item: (-item[1], int(item[0]))
            )
        ]
        if end == -1:
            return ranked[start:]
        return ranked[start:end + 1]

    def expire(self, key, seconds):
        self.expirations[key] = seconds
        return True


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
                        episode_title="#_TheArk",
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
            db.session.add_all(
                [
                    TVShow(
                        tmdb_id=505,
                        message_id=5005,
                        show_name="Deep Horizon",
                        episode_title="Season 1 Complete",
                        download_link="https://t.me/example?start=deep-horizon",
                        overview="Explorers cross a distant system after receiving a mysterious signal.",
                        poster_path="https://image.tmdb.org/t/p/w500/deep-horizon.jpg",
                        year=2024,
                        rating=7.7,
                        category="tv",
                        content_hash="tv-505",
                        slug="deep-horizon",
                    ),
                    TVShow(
                        tmdb_id=606,
                        message_id=6006,
                        show_name="Quiet Garden",
                        episode_title="Season 3",
                        download_link="https://t.me/example?start=quiet-garden",
                        overview="A family restores a garden and rebuilds their life together.",
                        poster_path="https://image.tmdb.org/t/p/w500/quiet-garden.jpg",
                        year=2024,
                        rating=9.2,
                        category="tv",
                        content_hash="tv-606",
                        slug="quiet-garden",
                    ),
                ]
            )
            db.session.flush()
            science_fiction = Genre(name="Science Fiction")
            drama = Genre(name="Drama")
            romance = Genre(name="Romance")
            action = Genre(name="Action")
            documentary = Genre(name="Documentary")
            db.session.add_all([science_fiction, drama, romance, action, documentary])
            db.session.flush()

            ark = TVShow.query.filter_by(tmdb_id=101, category="tv").first()
            ark.clicks = 10
            ark.availability_updated_at = datetime(2026, 8, 15)
            ark.tagline = "Humanity needs a second chance."
            ark.runtime_minutes = 45
            ark.number_of_seasons = 2
            ark.release_status = "Returning Series"
            ark.original_language = "en"
            ark.cast_data = [
                {
                    "id": 1,
                    "name": "Avery Stone",
                    "character": "Commander Lane",
                    "profile_url": "https://image.tmdb.org/t/p/w185/avery.jpg",
                }
            ]
            ark.official_trailer_key = "AbCdEf12345"
            ark.official_trailer_name = "The Ark Official Trailer"
            ark.official_trailer_published_at = "2023-01-03T12:00:00Z"
            ark.genres = [science_fiction, drama]
            TVShow.query.filter_by(tmdb_id=505).first().genres = [science_fiction]
            TVShow.query.filter_by(tmdb_id=606).first().genres = [romance]
            TVShow.query.filter_by(tmdb_id=202).first().genres = [action]
            TVShow.query.filter_by(tmdb_id=303).first().genres = [action, documentary]
            db.session.commit()

    def assert_contains(self, response, text):
        self.assertIn(text, response.get_data(as_text=True))

    def test_homepage_uses_clean_canonical_and_heading(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assert_contains(response, '<link rel="canonical" href="https://ibox-tv.com/">')
        self.assert_contains(response, '<section class="feature-hero feature-carousel shell google-auto-ads-ignore"')
        self.assert_contains(response, "Popular TV pick")
        self.assert_contains(response, "<h1>The Ark</h1>")
        self.assert_contains(response, "<h1>Deep Horizon</h1>")
        self.assert_contains(response, '<a class="feature-poster" href="/tv/101-the-ark"')
        self.assert_contains(response, "Most opened on iBOX TV")
        self.assert_contains(response, 'href="/static/catalogue.css?v=2"')
        self.assert_contains(response, 'class="media-grid latest-grid"')
        self.assertNotIn("#_TheArk", body)
        self.assertEqual(body.count('type="search"'), 1)
        self.assertNotIn("?search=&amp;page=1", body)

    def test_category_pages_live_on_one_host(self):
        self.assertEqual(self.client.get("/anime").status_code, 200)
        self.assertEqual(self.client.get("/movies").status_code, 200)
        self.assertEqual(self.client.get("/browse/tv").status_code, 200)
        self.assertEqual(self.client.get("/browse/anime").status_code, 200)
        self.assertEqual(self.client.get("/browse/movies").status_code, 200)
        self.assert_contains(self.client.get("/anime"), "https://ibox-tv.com/anime")
        self.assert_contains(self.client.get("/movies"), "https://ibox-tv.com/movies")

    def test_movie_home_and_full_catalogue_are_separate(self):
        home = self.client.get("/movies")
        home_body = home.get_data(as_text=True)
        self.assert_contains(home, "Popular movie pick")
        self.assert_contains(home, "Latest drops")
        self.assertNotIn("Refine results", home_body)
        self.assert_contains(home, 'href="/browse/movies"')

        catalogue = self.client.get("/browse/movies")
        self.assert_contains(catalogue, "Browse all movies.")
        self.assert_contains(catalogue, "Refine results")
        self.assert_contains(catalogue, '<form method="get" action="/browse/movies"')
        self.assert_contains(catalogue, 'class="mode-movies')

        # Old shared links retain their catalogue behaviour rather than
        # accidentally becoming movie-home pagination.
        legacy_filter = self.client.get("/movies?sort_by=rating_desc")
        self.assert_contains(legacy_filter, "Refine results")

    def test_worker_cache_exposes_real_genres_beyond_the_tmdb_fallback(self):
        fake_redis = FakeRedis()
        with patch("tv_app.app._redis", return_value=fake_redis):
            with app.app_context():
                refresh_genre_hub_cache()
                movie_genres = [genre.name for genre, _count in _popular_genres("movie", limit=100)]
                self.assertIn("Documentary", movie_genres)

            response = self.client.get("/browse/movies")
        self.assert_contains(response, "Documentary")

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

    def test_legacy_category_homepages_redirect_to_matching_paths(self):
        previous_testing = app.testing
        app.testing = False
        try:
            movies = self.client.get("/?q=moon", base_url="https://movies.ibox-tv.com")
            anime = self.client.get("/?search=blue", base_url="https://anime.ibox-tv.com")
        finally:
            app.testing = previous_testing
        self.assertEqual(movies.status_code, 301)
        self.assertEqual(movies.headers["Location"], "https://ibox-tv.com/movies?q=moon")
        self.assertEqual(anime.status_code, 301)
        self.assertEqual(anime.headers["Location"], "https://ibox-tv.com/anime?search=blue")

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

    def test_detail_title_including_brand_is_at_most_sixty_characters(self):
        show = TVShow(
            show_name="Magical Girl Lyrical Nanoha Exceeds Gun Blaze Vengeance",
            category="anime",
        )
        title = _detail_page_title(show)

        self.assertLessEqual(len(f"{title} | iBOX TV"), 60)

    def test_detail_renders_enrichment_and_contextual_internal_links(self):
        response = self.client.get("/tv/101-the-ark")
        body = response.get_data(as_text=True)
        self.assert_contains(response, "/tv/genre/science-fiction")
        self.assert_contains(response, "Official trailer")
        self.assert_contains(response, "youtube-nocookie.com/embed/AbCdEf12345")
        self.assert_contains(response, "Avery Stone")
        self.assert_contains(response, "Availability snapshot")
        self.assert_contains(response, '"@type": "BreadcrumbList"')
        # Genre links keep the page contextually connected without requiring
        # an expensive related-title catalogue query on every visitor request.
        self.assertNotIn("More like The Ark", body)

    def test_genre_hub_is_crawlable_indexable_and_in_core_sitemap(self):
        response = self.client.get("/tv/genre/science-fiction")
        self.assertEqual(response.status_code, 200)
        self.assert_contains(response, '<meta name="robots" content="index,follow">')
        self.assert_contains(response, '<link rel="canonical" href="https://ibox-tv.com/tv/genre/science-fiction">')
        self.assert_contains(response, "The Ark")
        self.assert_contains(response, "Deep Horizon")
        self.assert_contains(response, '"@type": "CollectionPage"')
        sitemap = self.client.get("/sitemaps/core.xml").get_data(as_text=True)
        self.assertIn("https://ibox-tv.com/tv/genre/science-fiction", sitemap)

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

    def test_search_keeps_cross_category_counts_and_separator_variants(self):
        with app.app_context():
            db.session.add(
                TVShow(
                    tmdb_id=909,
                    message_id=9009,
                    show_name="Spider-Man: New Dawn",
                    episode_title="Available now",
                    download_link="https://t.me/example?start=spider-man",
                    overview="A new Spider-Man adventure.",
                    poster_path="https://image.tmdb.org/t/p/w500/spider-man.jpg",
                    category="movie",
                    content_hash="movie-909",
                    slug="spider-man-new-dawn",
                )
            )
            db.session.commit()

        response = self.client.get('/?search=spider+man')
        body = response.get_data(as_text=True)
        self.assert_contains(response, 'TV <span>0</span>')
        self.assert_contains(response, 'Movies <span>1</span>')
        self.assert_contains(response, 'No results in TV shows')
        self.assert_contains(response, 'href="/movies?q=spider+man"')
        self.assert_contains(self.client.get('/movies?q=spider+man'), 'Spider-Man: New Dawn')
        self.assertEqual(
            _search_phrases('Spider Man'),
            ('Spider Man', 'spider man', 'spider-man', 'spiderman'),
        )

    def test_search_ranks_exact_title_before_newer_partial_matches(self):
        with app.app_context():
            db.session.add_all(
                [
                    TVShow(
                        tmdb_id=910, message_id=9010, show_name="Spider-Man",
                        download_link="https://t.me/example?start=spider-man-2002",
                        overview="The original Spider-Man movie.",
                        poster_path="https://image.tmdb.org/t/p/w500/spider-man-2002.jpg",
                        year=2002, rating=7.4, category="movie",
                        content_hash="movie-910", slug="spider-man-2002",
                    ),
                    TVShow(
                        tmdb_id=911, message_id=9011, show_name="Spider-Man: New Dawn",
                        download_link="https://t.me/example?start=spider-man-new-dawn",
                        overview="A newer Spider-Man adventure.",
                        poster_path="https://image.tmdb.org/t/p/w500/spider-man-new-dawn.jpg",
                        year=2026, rating=8.0, category="movie",
                        content_hash="movie-911", slug="spider-man-new-dawn",
                    ),
                    TVShow(
                        tmdb_id=912, message_id=9012, show_name="Across the Spider-Man Multiverse",
                        download_link="https://t.me/example?start=across-spider-man",
                        overview="A multiverse Spider-Man adventure.",
                        poster_path="https://image.tmdb.org/t/p/w500/across-spider-man.jpg",
                        year=2027, rating=9.0, category="movie",
                        content_hash="movie-912", slug="across-spider-man",
                    ),
                ]
            )
            db.session.commit()
            names = [show.show_name for show in _catalogue_exact_search_query("movie", "spider-man").all()]

        self.assertEqual(names[:3], [
            "Spider-Man",
            "Spider-Man: New Dawn",
            "Across the Spider-Man Multiverse",
        ])

    def test_clean_catalogue_pagination_is_indexable_and_self_canonical(self):
        with app.app_context():
            for index in range(17):
                db.session.add(
                    TVShow(
                        tmdb_id=1000 + index,
                        message_id=9000 + index,
                        show_name=f"Catalogue Show {index}",
                        episode_title="Season 1",
                        download_link=f"https://t.me/example?start=catalogue-{index}",
                        overview="A complete catalogue entry with a useful overview for visitors.",
                        poster_path=f"https://image.tmdb.org/t/p/w500/catalogue-{index}.jpg",
                        category="tv",
                        content_hash=f"catalogue-{index}",
                        slug=f"catalogue-show-{index}",
                    )
                )
            db.session.commit()

        response = self.client.get("/?page=2")
        self.assert_contains(response, '<meta name="robots" content="index,follow">')
        self.assert_contains(response, '<link rel="canonical" href="https://ibox-tv.com/?page=2">')

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
        self.assertEqual(self.client.get("/favicon.ico").status_code, 200)
        self.assertEqual(self.client.get("/static/style.min.css").status_code, 200)
        self.assertEqual(self.client.get("/static/script.min.js").status_code, 200)

    def test_google_auto_ads_is_restored_without_ezoic_page_code(self):
        body = self.client.get("/").get_data(as_text=True)
        self.assertIn("pagead2.googlesyndication.com/pagead/js/adsbygoogle.js", body)
        self.assertIn("ca-pub-3351229899410110", body)
        self.assertIn('class="site-footer google-auto-ads-ignore"', body)
        self.assertNotIn("gatekeeperconsent.com", body)
        self.assertNotIn("ezoic-pub-ad-placeholder", body)
        styles = self.client.get("/static/style.min.css").get_data(as_text=True)
        self.assertIn("google-auto-ads:ignore", styles)
        self.assertIn("ins.adsbygoogle[data-ad-status=unfilled]{display:none!important}", styles)
        ads_txt = self.client.get("/ads.txt")
        self.assertEqual(ads_txt.status_code, 301)
        self.assertEqual(ads_txt.headers["Location"], "https://srv.adstxtmanager.com/75094/ibox-tv.com")

    def test_manual_adsense_units_only_render_when_configured(self):
        original = {
            key: app.config[key]
            for key in ('ADSENSE_HOME_SLOT', 'ADSENSE_DETAIL_SLOT')
        }
        try:
            app.config.update(
                ADSENSE_HOME_SLOT='1234567890',
                ADSENSE_DETAIL_SLOT='0987654321',
            )
            home = self.client.get('/').get_data(as_text=True)
            detail = self.client.get('/tv/101-the-ark').get_data(as_text=True)
        finally:
            app.config.update(original)

        self.assertIn('data-ad-slot="1234567890"', home)
        self.assertIn('adsense-slot--home', home)
        self.assertIn('data-ad-slot="0987654321"', detail)
        self.assertIn('adsense-slot--detail', detail)
        self.assertNotIn(
            'detail-enrichment content-section google-auto-ads-ignore', detail
        )

    def test_robots_allows_search_pages_to_be_crawled_for_noindex(self):
        response = self.client.get("/robots.txt")
        body = response.get_data(as_text=True)
        self.assertNotIn("Disallow: /?search=", body)
        self.assertIn("Disallow: /nuke", body)

    def test_download_click_drives_trending_metric(self):
        with app.app_context():
            show = TVShow.query.filter_by(slug="the-ark").first()
            show_id = show.id
            starting_clicks = show.clicks
        response = self.client.get(f"/download/{show_id}")
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            self.assertEqual(TVShow.query.get(show_id).clicks, starting_clicks + 1)

    def test_download_updates_redis_popularity_leaderboard(self):
        with app.app_context():
            ark = TVShow.query.filter_by(slug="the-ark").first()
            quiet_garden = TVShow.query.filter_by(slug="quiet-garden").first()
            ark_id = ark.id
            quiet_garden_id = quiet_garden.id

        fake_redis = FakeRedis()
        with patch("tv_app.app._redis", return_value=fake_redis):
            self.assertEqual(self.client.get(f"/download/{quiet_garden_id}").status_code, 302)
            self.assertEqual(self.client.get(f"/download/{quiet_garden_id}").status_code, 302)
            self.assertEqual(self.client.get(f"/download/{ark_id}").status_code, 302)
            with app.app_context():
                trending = get_trending_shows(limit=2, category="tv")
            browse = self.client.get("/browse/tv?sort_by=popular")

        self.assertEqual([show.id for show in trending], [quiet_garden_id, ark_id])
        self.assertEqual(browse.status_code, 200)
        browse_body = browse.get_data(as_text=True)
        self.assertLess(browse_body.index("Quiet Garden"), browse_body.index("The Ark"))
        leaderboard_key = _popularity_leaderboard_key("tv")
        self.assertEqual(fake_redis.expirations[leaderboard_key], 43200)

    def test_security_headers_are_added(self):
        response = self.client.get("/")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["Referrer-Policy"], "strict-origin-when-cross-origin")
        self.assertEqual(
            response.headers["Strict-Transport-Security"],
            "max-age=31536000; includeSubDomains",
        )


if __name__ == "__main__":
    unittest.main()
