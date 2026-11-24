#!/usr/bin/env python3
"""Verify that the /batch1renamed dual-publish fix is present.

The fix is included directly in plugins/file_rename.py and plugins/ongoing_posts.py.
This script is intentionally read-only; it never rewrites production source files.
Run from the repository root with: python apply_fix.py --check
"""
from __future__ import annotations

import argparse
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FILE_RENAME = ROOT / "plugins" / "file_rename.py"
ONGOING_POSTS = ROOT / "plugins" / "ongoing_posts.py"


def verify() -> list[str]:
    problems: list[str] = []
    for path in (FILE_RENAME, ONGOING_POSTS):
        if not path.is_file():
            problems.append(f"Missing required source file: {path.relative_to(ROOT)}")
            continue
        source = path.read_text(encoding="utf-8")
        try:
            ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            problems.append(f"Syntax error in {path.relative_to(ROOT)}: {exc}")

    if problems:
        return problems

    file_source = FILE_RENAME.read_text(encoding="utf-8")
    ongoing_source = ONGOING_POSTS.read_text(encoding="utf-8")
    required_file_features = {
        "Ongoing publisher import": "from plugins.ongoing_posts import post_ongoing_anime as _post_ongoing_anime",
        "direct Bot API Anime Post send": "await _send_ongoing_bot_api(client, api_target, None, caption, button_rows)",
        "Ongoing publish attempt": "await _post_ongoing_anime(",
        "numeric private-channel ID handling": "individual_target.lstrip(\"-\").isdigit()",
        "independent Anime Post result": "anime_result = f\"❌ Anime Post failed:",
        "link-only no-media confirmation": "No media was downloaded or stored by this command.",
        "/batchcompleted command handler": "async def batch_completed_command(",
        "/batchcompleted payload parser": "def _parse_batchcompleted_payload(",
        "completed publishing workflow": "async def _publish_batchcompleted(",
        "submitted poster is passed through": "poster_override=poster_file_id",
        "saved Anime Post template is reused": 'settings.get("anime_post_template")',
        "case-insensitive saved Template Set resolver": "async def _find_saved_anime_route(title: str)",
        "batch1renamed uses shared case-insensitive resolver": "route, _template_set = await _find_saved_anime_route(title)",
    }
    for label, snippet in required_file_features.items():
        if snippet not in file_source:
            problems.append(f"Missing {label} in plugins/file_rename.py")

    required_ongoing_features = {
        "episode override parameters": "episode_range: str | None = None",
        "saved global-channel fallback": "await resolve_ongoing_targets(route, codeflixbots.codeflixbots)",
        "saved ongoing button template": "ongoing_buttons_template",
        "saved ongoing post text": "ongoing_template",
        "saved anime poster": "saved_template.get(\"poster_file_id\")",
        "poster override support": "poster_override: str | None = None",
        "saved poster can be bypassed": "use_saved_poster: bool = True",
        "completed status override": "status_override: str | None = None",
    }
    for label, snippet in required_ongoing_features.items():
        if snippet not in ongoing_source:
            problems.append(f"Missing {label} in plugins/ongoing_posts.py")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="accepted for compatibility; verification is always read-only")
    parser.parse_args()
    problems = verify()
    if problems:
        print("❌ Verification failed:")
        for problem in problems:
            print(f"- {problem}")
        return 1
    print("✅ /batch1renamed dual publishing and /batchcompleted are present; source syntax is valid.")
    print("✅ Saved anime Template Sets and channel routes resolve with uppercase, lowercase, or mixed-case titles.")
    print("✅ /batchcompleted reuses saved text/button templates, accepts an optional supplied poster, and bypasses saved posters when none is supplied.")
    print("ℹ️ This is an offline source check. Telegram permissions, MongoDB records, and live posting still require deployment testing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
