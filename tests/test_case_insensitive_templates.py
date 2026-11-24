"""Regression tests for case-insensitive /batch1renamed Template Set matching."""
import ast
import asyncio
import re
import unittest
from pathlib import Path
from types import SimpleNamespace

from helper.file_names import normalize_anime_title

ROOT = Path(__file__).resolve().parents[1]


class FakeCollection:
    def __init__(self, documents):
        self.documents = list(documents)

    async def find_one(self, query):
        for document in self.documents:
            matched = True
            for field, expected in query.items():
                actual = document.get(field)
                if isinstance(expected, dict) and "$regex" in expected:
                    flags = re.IGNORECASE if expected.get("$options") == "i" else 0
                    if actual is None or re.search(expected["$regex"], str(actual), flags) is None:
                        matched = False
                        break
                elif actual != expected:
                    matched = False
                    break
            if matched:
                return document
        return None


class FakeDatabase:
    def __init__(self, template_sets, routes):
        self.collections = {
            "anime_templates": FakeCollection(template_sets),
            "anime_routes": FakeCollection(routes),
        }

    def __getitem__(self, name):
        return self.collections[name]


def _load_resolver():
    source = (ROOT / "plugins" / "file_rename.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    node = next(
        item for item in tree.body
        if isinstance(item, ast.AsyncFunctionDef) and item.name == "_find_saved_anime_route"
    )
    namespace = {
        "re": re,
        "normalize_anime_title": normalize_anime_title,
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), "plugins/file_rename.py", "exec"), namespace)
    return namespace["_find_saved_anime_route"], namespace


class CaseInsensitiveTemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        resolver, cls.namespace = _load_resolver()
        cls.resolver = staticmethod(resolver)

    def _resolve(self, command_title, template_doc, route_doc):
        database = FakeDatabase([template_doc], [route_doc])
        self.namespace["codeflixbots"] = SimpleNamespace(codeflixbots=database)
        return asyncio.run(self.resolver(command_title))

    def test_upper_lower_and_mixed_case_titles_resolve_the_same_saved_template(self):
        # Simulate older records whose stored _id/title_key kept uppercase letters.
        template = {
            "_id": "NARUTO SHIPPUDEN",
            "title": "NARUTO SHIPPUDEN",
            "title_key": "NARUTO SHIPPUDEN",
            "setup_complete": True,
        }
        route = {
            "title": "NARUTO SHIPPUDEN",
            "title_key": "NARUTO SHIPPUDEN",
            "individual_channel": -1001234567890,
        }

        for command_title in ("NARUTO SHIPPUDEN", "naruto shippuden", "Naruto Shippuden"):
            with self.subTest(command_title=command_title):
                matched_route, matched_template = self._resolve(command_title, template, route)
                self.assertEqual(matched_route["individual_channel"], -1001234567890)
                self.assertTrue(matched_template["setup_complete"])

    def test_uppercase_command_matches_a_normalized_lowercase_template(self):
        template = {
            "_id": "naruto shippuden",
            "title": "naruto shippuden",
            "title_key": "naruto shippuden",
            "setup_complete": True,
        }
        route = {
            "title": "naruto shippuden",
            "title_key": "naruto shippuden",
            "individual_channel": -1009876543210,
        }
        matched_route, matched_template = self._resolve("NARUTO SHIPPUDEN", template, route)
        self.assertEqual(matched_route["individual_channel"], -1009876543210)
        self.assertEqual(matched_template["_id"], "naruto shippuden")

    def test_route_only_legacy_setup_still_resolves(self):
        database = FakeDatabase([], [{
            "title": "NARUTO SHIPPUDEN",
            "title_key": "NARUTO SHIPPUDEN",
            "individual_channel": -1001234567890,
        }])
        self.namespace["codeflixbots"] = SimpleNamespace(codeflixbots=database)
        matched_route, matched_template = asyncio.run(self.resolver("naruto shippuden"))
        self.assertEqual(matched_route["individual_channel"], -1001234567890)
        self.assertIsNone(matched_template)

    def test_unmatched_title_does_not_select_another_anime(self):
        route, template = self._resolve(
            "JUJUTSU KAISEN",
            {"_id": "naruto shippuden", "title_key": "naruto shippuden"},
            {"title": "naruto shippuden", "title_key": "naruto shippuden", "individual_channel": -100123},
        )
        self.assertIsNone(route)
        self.assertIsNone(template)


if __name__ == "__main__":
    unittest.main()
