"""Regression tests for safe in-place inline-button URL replacement."""
import ast
import re
import unittest
from datetime import datetime, timezone
from typing import Any
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "plugins" / "admin_panel.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)
WANTED = {
    "_flatten_inline_keyboard",
    "_parse_button_selection",
    "_validate_replacement_url",
    "_build_updated_keyboard",
    "_button_keyboard_snapshot",
    "_saved_post_button_styles",
}
DEFAULT_NAMES = {"DEFAULT_ONGOING_BUTTONS_TEMPLATE", "DEFAULT_ANIME_BUTTONS_TEMPLATE"}
NODES = [
    node for node in TREE.body
    if (
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in WANTED
    ) or (
        isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id in DEFAULT_NAMES for target in node.targets)
    )
]
NAMESPACE = {"re": re, "urlparse": urlparse, "Any": Any}
exec(compile(ast.Module(body=NODES, type_ignores=[]), "plugins/admin_panel.py", "exec"), NAMESPACE)

FINDER_WANTED = {"_channel_match_values", "_same_published_channel", "_find_recorded_anime_post"}
FINDER_NODES = [
    node for node in TREE.body
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in FINDER_WANTED
]


class _FakeCursor:
    def __init__(self, values):
        self.values = values

    def sort(self, *_args):
        return self

    def limit(self, *_args):
        return self

    async def to_list(self, length=None):
        return self.values[:length] if length is not None else self.values


class _FakeCollection:
    def __init__(self, rows=None, direct=None):
        self.rows = rows or []
        self.direct = direct

    def find(self, _query):
        return _FakeCursor(self.rows)

    async def find_one(self, _query):
        return self.direct


class _FakeDatabase:
    def __init__(self, collections):
        self.collections = collections

    def __getitem__(self, key):
        return self.collections.setdefault(key, _FakeCollection())


class _FakeDatabaseClient:
    def __init__(self, collections):
        self.codeflixbots = _FakeDatabase(collections)


FINDER_NAMESPACE = {"Any": Any, "codeflixbots": None}
exec(compile(ast.Module(body=FINDER_NODES, type_ignores=[]), "plugins/admin_panel.py", "exec"), FINDER_NAMESPACE)

ONGOING_SOURCE = (ROOT / "plugins" / "ongoing_posts.py").read_text(encoding="utf-8")
ONGOING_TREE = ast.parse(ONGOING_SOURCE)
RECORD_NODE = next(
    node for node in ONGOING_TREE.body
    if isinstance(node, ast.AsyncFunctionDef) and node.name == "_record_bot_published_ongoing_post"
)


class PublishedPostButtonEditTests(unittest.TestCase):
    def _markup(self):
        return SimpleNamespace(inline_keyboard=[
            [
                SimpleNamespace(text="DOWNLOAD NOW", url="https://old-download.example/file", style=None),
                SimpleNamespace(text="ANIME INDEX", url="https://old-index.example", style=None),
            ],
            [SimpleNamespace(text="SUPPORT", url="https://old-support.example", style=None)],
        ])

    def test_button_selection_supports_single_multiple_and_all(self):
        parse = NAMESPACE["_parse_button_selection"]
        self.assertEqual(parse("2", 3), [1])
        self.assertEqual(parse("1, 3", 3), [0, 2])
        self.assertEqual(parse("ALL", 3), [0, 1, 2])
        self.assertEqual(parse("*", 3), [0, 1, 2])

    def test_button_selection_rejects_bad_values_and_out_of_range(self):
        parse = NAMESPACE["_parse_button_selection"]
        for value in ("", "0", "4", "one", "1,a"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse(value, 3)

    def test_replacement_url_requires_full_http_or_https_url(self):
        validate = NAMESPACE["_validate_replacement_url"]
        self.assertEqual(validate(" https://t.me/example?start=abc "), "https://t.me/example?start=abc")
        self.assertEqual(validate("http://example.com/file"), "http://example.com/file")
        for value in ("", "t.me/example", "ftp://example.com", "https:///missing-host", "https://example.com/a b"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate(value)

    def test_selected_link_changes_and_other_buttons_are_preserved(self):
        build = NAMESPACE["_build_updated_keyboard"]
        updated = build(self._markup(), [0], "https://new.example/download", ["success", "danger", "primary"])
        self.assertEqual(len(updated), 2)
        self.assertEqual([len(row) for row in updated], [2, 1])
        self.assertEqual(updated[0][0]["url"], "https://new.example/download")
        self.assertEqual(updated[0][1]["url"], "https://old-index.example")
        self.assertEqual(updated[1][0]["url"], "https://old-support.example")
        self.assertEqual(updated[0][0]["text"], "DOWNLOAD NOW")
        self.assertEqual(updated[0][1]["text"], "ANIME INDEX")
        self.assertEqual(updated[0][0]["style"], "success")

    def test_keyboard_snapshot_preserves_row_layout_and_detects_changed_links(self):
        snapshot = NAMESPACE["_button_keyboard_snapshot"](self._markup())
        self.assertEqual([len(row) for row in snapshot], [2, 1])
        self.assertEqual(snapshot[0][0]["url"], "https://old-download.example/file")
        changed = SimpleNamespace(inline_keyboard=[[
            SimpleNamespace(text="DOWNLOAD NOW", url="https://changed.example", style=None),
            SimpleNamespace(text="ANIME INDEX", url="https://old-index.example", style=None),
        ], [SimpleNamespace(text="SUPPORT", url="https://old-support.example", style=None)]])
        self.assertNotEqual(snapshot, NAMESPACE["_button_keyboard_snapshot"](changed))

    def test_all_selection_updates_all_links_without_changing_layout(self):
        build = NAMESPACE["_build_updated_keyboard"]
        updated = build(self._markup(), [0, 1, 2], "https://new.example/replacement")
        self.assertEqual([len(row) for row in updated], [2, 1])
        self.assertTrue(all(button["url"] == "https://new.example/replacement" for row in updated for button in row))

    def test_non_url_buttons_are_rejected_without_a_partial_keyboard(self):
        markup = SimpleNamespace(inline_keyboard=[[
            SimpleNamespace(text="OPEN", url=None, callback_data="open")
        ]])
        with self.assertRaises(ValueError):
            NAMESPACE["_build_updated_keyboard"](markup, [0], "https://new.example")

    def test_saved_style_parser_handles_ongoing_and_anime_templates(self):
        parse = NAMESPACE["_saved_post_button_styles"]
        settings = {
            "ongoing_buttons_template": "#b DOWNLOAD NOW - {anime_invite}\n#r ANIME INDEX - https://t.me/index",
            "anime_buttons_template": "#g 480P - {quality_480p} | #b 720P - {quality_720p} | #r 1080P - {quality_1080p}",
        }
        self.assertEqual(parse(settings, "ongoing"), ["primary", "danger"])
        self.assertEqual(parse(settings, "individual"), ["success", "primary", "danger"])

    def test_forward_flow_accepts_all_publication_records_and_ongoing_history(self):
        self.assertIn('"episode_batches"', SOURCE)
        self.assertIn('"renamed_link_posts"', SOURCE)
        self.assertIn('"bot_published_messages"', SOURCE)
        self.assertIn('"anime_replace_button_selection"', SOURCE)
        self.assertNotIn("Ongoing-channel posts aren't supported", SOURCE)


class PublishedPostHistoryLookupTests(unittest.IsolatedAsyncioTestCase):
    async def test_lookup_verifies_ongoing_episode_batch_posts(self):
        database = _FakeDatabaseClient({
            "episode_batches": _FakeCollection(rows=[{
                "_id": "naruto_s1_e1",
                "title": "Naruto",
                "published_targets": {
                    "ongoing:-100123": {"chat_id": -100123, "message_id": 77}
                },
            }]),
            "renamed_link_posts": _FakeCollection(),
            "bot_published_messages": _FakeCollection(),
        })
        FINDER_NAMESPACE["codeflixbots"] = database
        match, verified = await FINDER_NAMESPACE["_find_recorded_anime_post"](-100123, 77, "narutoongoing")
        self.assertTrue(verified)
        self.assertEqual(match["post_type"], "ongoing")
        self.assertEqual(match["title"], "Naruto")

    async def test_lookup_verifies_direct_ongoing_history_records(self):
        database = _FakeDatabaseClient({
            "episode_batches": _FakeCollection(),
            "renamed_link_posts": _FakeCollection(),
            "bot_published_messages": _FakeCollection(direct={
                "channel_id": -100321, "message_id": 88,
                "title": "One Piece", "post_type": "ongoing",
            }),
        })
        FINDER_NAMESPACE["codeflixbots"] = database
        match, verified = await FINDER_NAMESPACE["_find_recorded_anime_post"](-100321, 88)
        self.assertTrue(verified)
        self.assertEqual(match["post_type"], "ongoing")
        self.assertEqual(match["title"], "One Piece")

    async def test_unrecorded_message_is_not_accepted(self):
        database = _FakeDatabaseClient({
            "episode_batches": _FakeCollection(),
            "renamed_link_posts": _FakeCollection(),
            "bot_published_messages": _FakeCollection(),
        })
        FINDER_NAMESPACE["codeflixbots"] = database
        match, verified = await FINDER_NAMESPACE["_find_recorded_anime_post"](-100321, 999)
        self.assertIsNone(match)
        self.assertFalse(verified)

    async def test_direct_ongoing_send_history_is_saved(self):
        ongoing_namespace = {
            "Client": object,
            "Any": Any,
            "datetime": datetime,
            "timezone": timezone,
            "logger": SimpleNamespace(debug=lambda *args, **kwargs: None, exception=lambda *args, **kwargs: None),
        }
        exec(compile(ast.Module(body=[RECORD_NODE], type_ignores=[]), "plugins/ongoing_posts.py", "exec"), ongoing_namespace)

        class Collection:
            def __init__(self):
                self.args = None

            async def update_one(self, *args, **kwargs):
                self.args = (args, kwargs)

        collection = Collection()
        ongoing_namespace["codeflixbots"] = _FakeDatabaseClient({"bot_published_messages": collection})

        class ClientStub:
            async def get_chat(self, _target):
                return SimpleNamespace(id=-100777)

        await ongoing_namespace["_record_bot_published_ongoing_post"](ClientStub(), "@ongoing", 123, "Bleach")
        self.assertIsNotNone(collection.args)
        self.assertEqual(collection.args[0][0]["_id"], "-100777:123")
        saved = collection.args[0][1]["$set"]
        self.assertEqual(saved["channel_id"], -100777)
        self.assertEqual(saved["message_id"], 123)
        self.assertEqual(saved["title"], "Bleach")


if __name__ == "__main__":
    unittest.main()
