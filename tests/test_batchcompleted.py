import ast
import re
import unittest
import unicodedata
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
FILE_RENAME_SOURCE = (ROOT / "plugins" / "file_rename.py").read_text(encoding="utf-8")
ONGOING_SOURCE = (ROOT / "plugins" / "ongoing_posts.py").read_text(encoding="utf-8")


def _load_parser():
    tree = ast.parse(FILE_RENAME_SOURCE)
    parser_node = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_parse_batchcompleted_payload"
    )
    namespace = {"re": re, "urlparse": urlparse}
    exec(compile(ast.Module(body=[parser_node], type_ignores=[]), "plugins/file_rename.py", "exec"), namespace)
    return namespace["_parse_batchcompleted_payload"]


class BatchCompletedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.parse_payload = staticmethod(_load_parser())
        cls.file_tree = ast.parse(FILE_RENAME_SOURCE)
        cls.ongoing_tree = ast.parse(ONGOING_SOURCE)

    def test_parses_season_and_episode_ranges_preserving_zero_padding(self):
        result = self.parse_payload(
            "/batchcompleted\nJUJUTSU KAISEN\nSeason 01 - 03\n01 - 12\n"
            "https://example.com/completed"
        )
        self.assertEqual(result["title"], "JUJUTSU KAISEN")
        self.assertEqual(result["season"], "01 - 03")
        self.assertEqual(result["episode_range"], "01 - 12")
        self.assertEqual(result["stored_link"], "https://example.com/completed")

    def test_parses_single_season_and_single_episode(self):
        result = self.parse_payload(
            "/batchcompleted JUJUTSU KAISEN\nSeason 01\nEpisode 01\n"
            "https://example.com/episode"
        )
        self.assertEqual(result["season"], "01")
        self.assertEqual(result["episode_range"], "01")

    def test_accepts_link_prefix_and_bot_username(self):
        result = self.parse_payload(
            "/batchcompleted@aniflixbot\nTitle: JUJUTSU KAISEN\n01 - 03\n01 - 12\n"
            "Download Link: https://example.com/full"
        )
        self.assertEqual(result["title"], "JUJUTSU KAISEN")
        self.assertEqual(result["stored_link"], "https://example.com/full")

    def test_rejects_missing_or_multiple_links(self):
        with self.assertRaises(ValueError):
            self.parse_payload("/batchcompleted\nJUJUTSU KAISEN\n01\n01 - 12")
        with self.assertRaises(ValueError):
            self.parse_payload(
                "/batchcompleted\nJUJUTSU KAISEN\n01\n01 - 12\n"
                "https://one.example\nhttps://two.example"
            )

    def test_rejects_descending_ranges(self):
        with self.assertRaises(ValueError):
            self.parse_payload(
                "/batchcompleted\nJUJUTSU KAISEN\nSeason 03 - 01\n01 - 12\n"
                "https://example.com/full"
            )
        with self.assertRaises(ValueError):
            self.parse_payload(
                "/batchcompleted\nJUJUTSU KAISEN\nSeason 01\n12 - 01\n"
                "https://example.com/full"
            )

    def test_command_reuses_saved_templates_and_supplied_link_and_poster(self):
        handler = next(
            node for node in self.file_tree.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_publish_batchcompleted"
        )
        body = ast.get_source_segment(FILE_RENAME_SOURCE, handler)
        self.assertIn('settings.get("anime_post_template")', body)
        self.assertIn('settings.get("batch1renamed_button_template")', body)
        self.assertIn("fallback_download_url=link", body)
        self.assertIn("poster_override=poster_file_id", body)
        self.assertIn("use_saved_poster=False", body)
        self.assertIn('status_override="COMPLETED"', body)

    def test_completed_download_link_replaces_the_saved_dynamic_download_button(self):
        wanted = {"_plain_label", "_valid_http_url", "_select_anime_invite", "_is_dynamic_invite", "_build_button_rows"}
        nodes = []
        for node in self.ongoing_tree.body:
            if isinstance(node, ast.FunctionDef) and node.name in wanted:
                nodes.append(node)
            elif isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "_BUTTON_STYLES"
                for target in node.targets
            ):
                nodes.append(node)
        namespace = {
            "re": re,
            "unicodedata": unicodedata,
            "urlparse": urlparse,
            "normalize_anime_title": lambda value: re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip(),
        }
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "plugins/ongoing_posts.py", "exec"), namespace)
        rows = namespace["_build_button_rows"](
            "#b DOWNLOAD NOW - {anime_invite}\n#r ANIME INDEX - https://t.me/aniflixindex",
            [{"label": "Saved Download", "url": "https://t.me/+old-invite"}],
            fallback_download_url="https://example.com/completed-anime",
        )
        self.assertEqual(rows[0][0]["url"], "https://example.com/completed-anime")
        self.assertEqual(rows[1][0]["url"], "https://t.me/aniflixindex")

    def test_ongoing_publisher_keeps_legacy_defaults_and_accepts_overrides(self):
        publisher = next(
            node for node in self.ongoing_tree.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "post_ongoing_anime"
        )
        args = [arg.arg for arg in publisher.args.args]
        self.assertIn("poster_override", args)
        self.assertIn("use_saved_poster", args)
        self.assertIn("status_override", args)
        defaults = publisher.args.defaults
        self.assertEqual(ast.unparse(defaults[-2]), "True")


if __name__ == "__main__":
    unittest.main()
