import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class SourceVerificationTests(unittest.TestCase):
    def test_read_only_verifier_accepts_repository_sources(self):
        source = (ROOT / "apply_fix.py").read_text(encoding="utf-8")
        namespace = {"__name__": "apply_fix_test", "__file__": str(ROOT / "apply_fix.py")}
        exec(compile(source, str(ROOT / "apply_fix.py"), "exec"), namespace)
        self.assertEqual(namespace["verify"](), [])

    def test_production_modules_are_syntactically_valid(self):
        for relative in ("plugins/file_rename.py", "plugins/ongoing_posts.py"):
            source = (ROOT / relative).read_text(encoding="utf-8")
            ast.parse(source, filename=relative)

    def test_verifier_remains_read_only(self):
        source = (ROOT / "apply_fix.py").read_text(encoding="utf-8")
        self.assertIn("intentionally read-only", source)
        self.assertNotIn("write_text(", source)
        self.assertNotIn("transform_file_rename", source)


if __name__ == "__main__":
    unittest.main()
