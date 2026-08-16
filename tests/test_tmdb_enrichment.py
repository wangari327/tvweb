import asyncio
import unittest

from tv_app.tmdb_enrichment import extract_enrichment, fetch_tmdb_details


class FakeResponse:
    def __init__(self, status, payload=None):
        self.status = status
        self.payload = payload or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def json(self):
        return self.payload


class FakeSession:
    def __init__(self, status, payload=None):
        self.response = FakeResponse(status, payload)

    def get(self, *args, **kwargs):
        return self.response


class TMDBEnrichmentTests(unittest.TestCase):
    def test_movie_enrichment_keeps_only_official_youtube_trailer(self):
        data = extract_enrichment(
            {
                "title": "Moonlight Run",
                "overview": "A night drive becomes a race home.",
                "release_date": "2025-04-02",
                "runtime": 112,
                "status": "Released",
                "original_language": "en",
                "genres": [{"id": 28, "name": "Action"}],
                "credits": {
                    "cast": [
                        {
                            "id": 9,
                            "name": "Jordan Vale",
                            "character": "Mara",
                            "profile_path": "/jordan.jpg",
                        }
                    ]
                },
                "videos": {
                    "results": [
                        {
                            "site": "YouTube",
                            "type": "Trailer",
                            "official": False,
                            "key": "Unofficial1",
                            "name": "Fan trailer",
                        },
                        {
                            "site": "YouTube",
                            "type": "Trailer",
                            "official": True,
                            "key": "Official123",
                            "name": "Official trailer",
                            "published_at": "2025-03-01T12:00:00Z",
                            "iso_639_1": "en",
                            "size": 1080,
                        },
                    ]
                },
            },
            "movie",
        )
        self.assertEqual(data["genres"], ["Action"])
        self.assertEqual(data["runtime_minutes"], 112)
        self.assertEqual(data["official_trailer_key"], "Official123")
        self.assertEqual(data["cast_data"][0]["character"], "Mara")
        self.assertTrue(data["cast_data"][0]["profile_url"].endswith("/jordan.jpg"))

    def test_tv_enrichment_uses_aggregate_cast_roles(self):
        data = extract_enrichment(
            {
                "name": "The Ark",
                "first_air_date": "2023-02-01",
                "episode_run_time": [45],
                "number_of_seasons": 2,
                "aggregate_credits": {
                    "cast": [
                        {
                            "id": 1,
                            "name": "Avery Stone",
                            "roles": [
                                {"character": "Commander Lane", "episode_count": 18}
                            ],
                        }
                    ]
                },
                "videos": {"results": []},
            },
            "tv",
        )
        self.assertEqual(data["runtime_minutes"], 45)
        self.assertEqual(data["number_of_seasons"], 2)
        self.assertEqual(data["cast_data"][0]["character"], "Commander Lane")
        self.assertIsNone(data["official_trailer_key"])

    def test_status_mode_distinguishes_not_found_from_temporary_error(self):
        not_found = asyncio.run(
            fetch_tmdb_details(
                FakeSession(404), 123, "movie", "token", return_status=True
            )
        )
        temporary_error = asyncio.run(
            fetch_tmdb_details(
                FakeSession(403), 123, "movie", "token", return_status=True
            )
        )

        self.assertEqual(not_found, ("not_found", None))
        self.assertEqual(temporary_error, ("error", None))

    def test_default_fetch_result_remains_backward_compatible(self):
        payload = {"id": 123, "title": "Example"}
        details = asyncio.run(
            fetch_tmdb_details(FakeSession(200, payload), 123, "movie", "token")
        )

        self.assertEqual(details, payload)


if __name__ == "__main__":
    unittest.main()
