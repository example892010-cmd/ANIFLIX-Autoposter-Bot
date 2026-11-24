import ast
import asyncio
import logging
import re
import unittest
import unicodedata
from datetime import datetime
from types import SimpleNamespace
from urllib.parse import urlparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILE_RENAME_SOURCE = (ROOT / "plugins" / "file_rename.py").read_text(encoding="utf-8")
ONGOING_SOURCE = (ROOT / "plugins" / "ongoing_posts.py").read_text(encoding="utf-8")


def _nodes(source: str, names: set[str]):
    tree = ast.parse(source)
    return [
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names
    ]


def _load_file_rename_helpers():
    tree = ast.parse(FILE_RENAME_SOURCE)
    wanted = {"_parse_batchcompleted_payload", "_build_batch1renamed_button_rows"}
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in wanted]
    assignments = [
        node for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "DEFAULT_BATCH1RENAMED_BUTTON_TEMPLATE" for t in node.targets)
    ]
    namespace = {"re": re, "urlparse": urlparse}
    exec(compile(ast.Module(body=assignments + nodes, type_ignores=[]), "plugins/file_rename.py", "exec"), namespace)
    return namespace


class BatchCompletedParserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.helpers = _load_file_rename_helpers()
        cls.parse_payload = staticmethod(cls.helpers["_parse_batchcompleted_payload"])

    def test_parses_zero_padded_season_and_episode_ranges(self):
        result = self.parse_payload(
            "/batchcompleted\nJUJUTSU KAISEN\nSeason 01 - 03\n01 - 12\n"
            "https://example.com/completed"
        )
        self.assertEqual(result["title"], "JUJUTSU KAISEN")
        self.assertEqual(result["season"], "01 - 03")
        self.assertEqual(result["episode_range"], "01 - 12")
        self.assertEqual(result["stored_link"], "https://example.com/completed")

    def test_parses_comma_separated_seasons(self):
        result = self.parse_payload(
            "/batchCompleted\nARIFURETA: FROM COMMONPLACE TO WORLD’S STRONGEST\n"
            "season 01, 02, 03\n42\nhttps://t.me/example?start=abc"
        )
        self.assertEqual(result["season"], "01, 02, 03")
        self.assertEqual(result["episode_range"], "42")

    def test_parses_single_season_and_episode(self):
        result = self.parse_payload(
            "/batchcompleted@aniflixbot\nTitle: JUJUTSU KAISEN\nSeason 1\nEpisode 01\n"
            "https://example.com/episode"
        )
        self.assertEqual(result["season"], "1")
        self.assertEqual(result["episode_range"], "01")

    def test_accepts_episode_suffix_and_link_prefix(self):
        result = self.parse_payload(
            "/batchcompleted\nTitle: ARIFURETA\nSeason 01 - 03\n01 - 42-epi\n"
            "Download Link: https://example.com/full"
        )
        self.assertEqual(result["episode_range"], "01 - 42")
        self.assertEqual(result["stored_link"], "https://example.com/full")

    def test_rejects_missing_or_multiple_urls(self):
        with self.assertRaises(ValueError):
            self.parse_payload("/batchcompleted\nJUJUTSU KAISEN\n01\n01 - 12")
        with self.assertRaises(ValueError):
            self.parse_payload(
                "/batchcompleted\nJUJUTSU KAISEN\n01\n01 - 12\n"
                "https://one.example\nhttps://two.example"
            )

    def test_rejects_descending_or_repeated_seasons_and_descending_episodes(self):
        for season in ("Season 03 - 01", "Season 01, 03, 02", "Season 01, 01"):
            with self.subTest(season=season), self.assertRaises(ValueError):
                self.parse_payload(
                    f"/batchcompleted\nJUJUTSU KAISEN\n{season}\n01 - 12\nhttps://example.com/full"
                )
        with self.assertRaises(ValueError):
            self.parse_payload(
                "/batchcompleted\nJUJUTSU KAISEN\nSeason 01\n12 - 01\nhttps://example.com/full"
            )


class _FakeCollection:
    def __init__(self, value=None):
        self.value = value
        self.inserted = []

    async def find_one(self, query):
        return self.value

    async def insert_one(self, document):
        self.inserted.append(document)
        return SimpleNamespace(inserted_id=len(self.inserted))


class _FakeDatabase:
    def __init__(self):
        self.settings = _FakeCollection({
            "anime_post_template": "<b>{title}</b>\nSeason {season}\nEpisode {episode_range}\n{status}",
            "batch1renamed_button_template": "#g EPISODE {episode} - {link}",
        })

    def __getitem__(self, name):
        if name == "post_template_settings":
            return self.settings
        raise AssertionError(f"Unexpected collection: {name}")


class _FakeMessage:
    def __init__(self):
        self.from_user = SimpleNamespace(id=12345)
        self.reports = []

    async def reply_text(self, text, **kwargs):
        self.reports.append((text, kwargs))


class BatchCompletedPublishingTests(unittest.TestCase):
    def _load_publisher(self):
        tree = ast.parse(FILE_RENAME_SOURCE)
        publisher = next(
            node for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_publish_batchcompleted"
        )
        sent_anime = []
        ongoing_calls = []
        database = _FakeDatabase()

        async def find_route(title):
            self.assertEqual(title.casefold(), "jujutsu kaisen")
            return ({"title": "Jujutsu Kaisen", "individual_channel": "@anime_posts"}, None)

        async def send_anime(client, target, poster, caption, buttons):
            sent_anime.append({"target": target, "poster": poster, "caption": caption, "buttons": buttons})
            return 987

        async def ongoing(client, title, **kwargs):
            ongoing_calls.append((title, kwargs))
            return "✅ Ongoing post published to 2 channel(s)."

        class FakeClient:
            async def get_chat(self, target):
                return SimpleNamespace(id=-1001234567890, title="Anime Posts")

        namespace = {
            "codeflixbots": SimpleNamespace(codeflixbots=database),
            "_find_saved_anime_route": find_route,
            "DEFAULT_INDIVIDUAL_TEMPLATE": "{title} | S{season} | E{episode_range}",
            "_safe_template": lambda template, values: template.format_map(values),
            "DEFAULT_BATCH1RENAMED_BUTTON_TEMPLATE": "#g EPISODE {episode} - {link}",
            "_build_batch1renamed_button_rows": self._load_publisher_helpers()["_build_batch1renamed_button_rows"],
            "_send_ongoing_bot_api": send_anime,
            "_post_ongoing_anime": ongoing,
            "normalize_anime_title": lambda value: re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip(),
            "logger": logging.getLogger("batchcompleted-tests"),
            "datetime": datetime,
            "enums": SimpleNamespace(ParseMode=SimpleNamespace(DISABLED="disabled")),
        }
        exec(compile(ast.Module(body=[publisher], type_ignores=[]), "plugins/file_rename.py", "exec"), namespace)
        return namespace["_publish_batchcompleted"], sent_anime, ongoing_calls, database, FakeClient()

    def _load_publisher_helpers(self):
        return _load_file_rename_helpers()

    def test_publishes_anime_post_and_ongoing_post_with_supplied_photo(self):
        publish, anime_sends, ongoing_calls, db, client = self._load_publisher()
        message = _FakeMessage()
        details = {
            "title": "JUJUTSU KAISEN",
            "season": "01, 02, 03",
            "episode_range": "01 - 12",
            "stored_link": "https://t.me/storebot?start=episode-set",
        }
        asyncio.run(publish(client, message, details, poster_file_id="photo-file-id"))

        self.assertEqual(len(anime_sends), 1)
        anime = anime_sends[0]
        self.assertEqual(anime["target"], "@anime_posts")
        self.assertEqual(anime["poster"], "photo-file-id")
        self.assertIn("Jujutsu Kaisen", anime["caption"])
        self.assertIn("Season 01, 02, 03", anime["caption"])
        self.assertIn("Episode 01 - 12", anime["caption"])
        self.assertIn("COMPLETED", anime["caption"])
        self.assertEqual(anime["buttons"], [])
        self.assertNotIn(details["stored_link"], anime["caption"])
        self.assertEqual(len(ongoing_calls), 1)
        title, kwargs = ongoing_calls[0]
        self.assertEqual(title, "Jujutsu Kaisen")
        self.assertEqual(kwargs["fallback_download_url"], details["stored_link"])
        self.assertEqual(kwargs["status_override"], "COMPLETED")
        self.assertTrue(kwargs["global_targets_only"])
        self.assertFalse(kwargs["use_saved_poster"])
        self.assertIsNone(kwargs.get("poster_override"))
        self.assertIn("Anime Post", message.reports[0][0])
        self.assertIn("Ongoing Channels", message.reports[0][0])

    def test_publishes_without_poster_when_no_photo_is_supplied(self):
        publish, anime_sends, ongoing_calls, _db, client = self._load_publisher()
        message = _FakeMessage()
        details = {
            "title": "JUJUTSU KAISEN",
            "season": "1",
            "episode_range": "12",
            "stored_link": "https://example.com/download",
        }
        asyncio.run(publish(client, message, details, poster_file_id=None))
        self.assertEqual(anime_sends[0]["poster"], None)
        self.assertEqual(len(ongoing_calls), 1)


if __name__ == "__main__":
    unittest.main()
