"""Regression tests for the new /batch1: files first, details + link second flow."""

import ast
import asyncio
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

from helper.file_names import normalize_anime_title

ROOT = Path(__file__).resolve().parents[1]
FILE_RENAME = ROOT / "plugins" / "file_rename.py"
PUBLISHER = ROOT / "plugins" / "episode_publisher.py"
ONGOING = ROOT / "plugins" / "ongoing_posts.py"


def _load_functions(path, wanted, extra=None):
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    nodes = [
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in wanted
    ]
    missing = set(wanted) - {node.name for node in nodes}
    if missing:
        raise AssertionError(f"Missing functions in {path.name}: {sorted(missing)}")
    namespace = {
        "re": re,
        "urlparse": urlparse,
        "normalize_anime_title": normalize_anime_title,
    }
    if extra:
        namespace.update(extra)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), namespace)
    return namespace, source, tree


class Batch1AfterFilesFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.helpers, cls.file_source, cls.file_tree = _load_functions(
            FILE_RENAME,
            {
                "_parse_upload_details",
                "_parse_batch_command",
                "_valid_batch_download_url",
                "_parse_batch1_post_details",
                "_linked_url_from_message",
            },
        )
        cls.publisher_source = PUBLISHER.read_text(encoding="utf-8")
        cls.ongoing_source = ONGOING.read_text(encoding="utf-8")

    def test_bare_batch1_starts_files_first_but_legacy_batches_keep_details_first(self):
        parse = self.helpers["_parse_batch_command"]
        self.assertEqual(parse("/batch1"), (1, {"await_details_after_files": True}))
        self.assertEqual(
            parse("/batch2\nNaruto Shippuden\nSeason 01\n355"),
            (2, {"title": "Naruto Shippuden", "season": "1", "episode_range": "355"}),
        )

    def test_batch1_multiline_details_accepts_plain_https_url(self):
        parsed = self.helpers["_parse_batch1_post_details"](
            "naruto shippuden\nSeason 01\n355\nhttps://t.me/ANIFLIX_FILE_STORE_BOT?start=batch_x"
        )
        self.assertEqual(parsed["title"], "naruto shippuden")
        self.assertEqual(parsed["season"], "1")
        self.assertEqual(parsed["episode_range"], "355")
        self.assertEqual(
            parsed["fallback_download_url"],
            "https://t.me/ANIFLIX_FILE_STORE_BOT?start=batch_x",
        )

    def test_batch1_accepts_telegram_text_link_label(self):
        message = SimpleNamespace(
            text="NARUTO SHIPPUDEN\nSeason 01\n355\nLink here",
            entities=[SimpleNamespace(type="text_link", url="https://t.me/store?start=abc")],
        )
        link = self.helpers["_linked_url_from_message"](message)
        parsed = self.helpers["_parse_batch1_post_details"](message.text, link)
        self.assertEqual(link, "https://t.me/store?start=abc")
        self.assertEqual(parsed["title"], "NARUTO SHIPPUDEN")
        self.assertEqual(parsed["fallback_download_url"], link)

    def test_batch1_accepts_pipe_separated_details_and_link(self):
        parsed = self.helpers["_parse_batch1_post_details"](
            "Naruto Shippuden | Season 01 | 355 | https://t.me/store?start=abc"
        )
        self.assertEqual(parsed["title"], "Naruto Shippuden")
        self.assertEqual(parsed["episode_range"], "355")
        self.assertEqual(parsed["fallback_download_url"], "https://t.me/store?start=abc")

    def test_invalid_or_missing_download_urls_are_rejected(self):
        parse_details = self.helpers["_parse_batch1_post_details"]
        self.assertIsNone(parse_details("Naruto\nSeason 01\n355"))
        self.assertIsNone(parse_details("Naruto\nSeason 01\n355\nftp://example.com/file"))
        self.assertIsNone(parse_details("Naruto\nSeason 01\n355\n/link"))
        self.assertFalse(self.helpers["_valid_batch_download_url"]("javascript:alert(1)"))

    def test_title_normalization_matches_case_variants(self):
        expected = normalize_anime_title("Naruto Shippuden")
        self.assertEqual(normalize_anime_title("naruto shippuden"), expected)
        self.assertEqual(normalize_anime_title("NARUTO SHIPPUDEN"), expected)
        route_func = next(
            node for node in self.file_tree.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_find_batch1_template_route"
        )
        route_body = ast.get_source_segment(self.file_source, route_func)
        self.assertIn('templates.find_one({"_id": title_key})', route_body)
        self.assertIn('templates.find_one({"title_key": title_key})', route_body)
        self.assertIn('"$options": "i"', route_body)
        self.assertIn('route.get("individual_channel")', route_body)
        self.assertIn('route.get("title_key") or route_key', route_body)


    def test_template_route_lookup_returns_canonical_route_key_case_insensitively(self):
        class FakeCollection:
            def __init__(self, rows):
                self.rows = rows

            async def find_one(self, query):
                if "_id" in query:
                    return next((row for row in self.rows if row.get("_id") == query["_id"]), None)
                if "title_key" in query:
                    return next((row for row in self.rows if row.get("title_key") == query["title_key"]), None)
                regex = query.get("title", {}).get("$regex")
                if regex is not None:
                    wanted = re.compile(regex, re.I)
                    return next((row for row in self.rows if wanted.fullmatch(str(row.get("title") or ""))), None)
                return None

        templates = FakeCollection([{
            "_id": "template-naruto",
            "title_key": "naruto shippuden",
            "title": "Naruto Shippuden",
            "setup_complete": True,
        }])
        routes = FakeCollection([{
            "title_key": "naruto-shippuden",
            "title": "Naruto Shippuden",
            "individual_channel": "-1001234567890",
        }])
        database = SimpleNamespace(codeflixbots={"anime_templates": templates, "anime_routes": routes})
        namespace, _, _ = _load_functions(
            FILE_RENAME,
            {"_find_batch1_template_route"},
            extra={"codeflixbots": database},
        )
        template_set, route_key, route, error = asyncio.run(
            namespace["_find_batch1_template_route"]("NARUTO SHIPPUDEN")
        )
        self.assertIsNone(error)
        self.assertEqual(template_set["title"], "Naruto Shippuden")
        self.assertEqual(route_key, "naruto-shippuden")
        self.assertEqual(route["individual_channel"], "-1001234567890")

    def test_three_uploaded_files_transition_to_waiting_for_details(self):
        self.assertIn('"waiting_details" if new_count == _BATCH_FILE_LIMIT and needs_details', self.file_source)
        self.assertIn('"status": "waiting_details"', self.file_source)
        self.assertIn("Now send the publishing details", self.file_source)
        self.assertIn('"await_details_after_files": True', self.file_source)

    def test_details_handler_resolves_template_route_and_resumes_batch(self):
        handler = next(
            node for node in self.file_tree.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "collect_upload_details"
        )
        body = ast.get_source_segment(self.file_source, handler)
        self.assertIn('_parse_batch1_post_details(text, _linked_url_from_message(message))', body)
        self.assertIn('_find_batch1_template_route(details["title"])', body)
        self.assertIn('"selected_route_key": route_key', body)
        self.assertIn('"status": "processing"', body)
        self.assertIn('await _complete_manual_batch(client, refreshed_task, status_message)', body)
        # Parsed fields (including fallback_download_url) are retained in details
        # and passed as the task's detail payload without rebuilding/dropping keys.
        self.assertIn('"details": details', body)

    def test_publisher_saves_and_uses_per_run_url_only_for_ongoing_buttons(self):
        self.assertIn('"fallback_download_url": (', self.publisher_source)
        self.assertIn('_valid_http_url(details.get("fallback_download_url"))', self.publisher_source)
        self.assertIn('fallback_download_url=batch.get("fallback_download_url")', self.publisher_source)
        send_target = next(
            node for node in ast.parse(self.publisher_source).body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_send_target"
        )
        body = ast.get_source_segment(self.publisher_source, send_target)
        individual_branch = body.split('if mode == "individual":', 1)[1].split('if mode == "ongoing":', 1)[0]
        self.assertNotIn("fallback_download_url", individual_branch)
        self.assertIn("fallback_download_url=batch.get(\"fallback_download_url\")", body)

    def test_ongoing_button_builder_replaces_download_link_with_supplied_url(self):
        wanted = {
            "_plain_label", "_valid_http_url", "_select_anime_invite",
            "_is_dynamic_invite", "_build_button_rows",
        }
        tree = ast.parse(self.ongoing_source)
        nodes = [
            node for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in wanted
        ]
        style = next(
            node for node in tree.body
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "_BUTTON_STYLES"
                for target in node.targets
            )
        )
        namespace = {
            "re": re,
            "unicodedata": __import__("unicodedata"),
            "urlparse": urlparse,
            "normalize_anime_title": normalize_anime_title,
        }
        exec(compile(ast.Module(body=[style, *nodes], type_ignores=[]), "ongoing_posts.py", "exec"), namespace)
        rows = namespace["_build_button_rows"](
            "#g DOWNLOAD NOW - {anime_invite}\n#b ANIME INDEX - https://t.me/index",
            [{"label": "Naruto download", "url": "https://t.me/+old_invite"}],
            fallback_download_url="https://t.me/store?start=episode355",
        )
        self.assertEqual(rows[0][0]["url"], "https://t.me/store?start=episode355")
        self.assertEqual(rows[1][0]["url"], "https://t.me/index")

    def test_video_quality_prefers_dimensions_and_handles_portrait_media(self):
        namespace, _, _ = _load_functions(
            FILE_RENAME,
            {"_detect_video_quality"},
            extra={"asyncio": asyncio},
        )
        detect = namespace["_detect_video_quality"]
        self.assertEqual(
            asyncio.run(detect("not-probed.mkv", SimpleNamespace(width=1920, height=1080))),
            "1080p",
        )
        self.assertEqual(
            asyncio.run(detect("not-probed.mkv", SimpleNamespace(width=1080, height=1920))),
            "1080p",
        )
        self.assertEqual(
            asyncio.run(detect("not-probed.mkv", SimpleNamespace(width=1280, height=720))),
            "720p",
        )

    def test_file_caption_is_html_escaped_and_bold(self):
        self.assertIn('caption = f"<b>{html.escape(caption, quote=False)}</b>"', self.file_source)
        self.assertIn('"parse_mode": enums.ParseMode.HTML', self.file_source)


if __name__ == "__main__":
    unittest.main()
