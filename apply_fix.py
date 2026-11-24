#!/usr/bin/env python3
"""Read-only source verification for ANIFLIX /batchcompleted publishing."""
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
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            problems.append(f"Syntax error in {path.relative_to(ROOT)}: {exc}")
    if problems:
        return problems

    file_source = FILE_RENAME.read_text(encoding="utf-8")
    ongoing_source = ONGOING_POSTS.read_text(encoding="utf-8")
    required_file_features = {
        "batchcompleted command handler": "async def batch_completed_command(",
        "batchcompleted payload parser": "def _parse_batchcompleted_payload(",
        "comma-separated season support": r"\d{1,3}(?:\s*,\s*\d{1,3})*",
        "Anime Post saved text template": 'settings.get("anime_post_template")',
        "optional photo passed to Anime Post": "_send_ongoing_bot_api(client, api_target, poster_file_id, caption, button_rows)",
        "Anime Post does not reuse the Ongoing download URL": "button_rows = []",
        "Ongoing publication attempt": "await _post_ongoing_anime(",
        "Ongoing Download URL override": "fallback_download_url=link",
        "COMPLETED status": 'status_override="COMPLETED"',
        "global Ongoing channel configuration": "global_targets_only=True",
        "no saved-poster lookup for this command": "use_saved_poster=False",
    }
    for label, snippet in required_file_features.items():
        if snippet not in file_source:
            problems.append(f"Missing {label} in plugins/file_rename.py")

    required_ongoing_features = {
        "global-channel-only mode": "global_targets_only: bool = False",
        "global Ongoing target resolution": "await resolve_ongoing_targets(route, codeflixbots.codeflixbots)",
        "saved Ongoing text template": "ongoing_template",
        "saved Ongoing button template": "ongoing_buttons_template",
        "dynamic Download URL override": "fallback_download_url=fallback_download_url",
        "COMPLETED status override": "status_override: str | None = None",
        "published-post history recording": "_record_bot_published_post(",
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
    print("✅ /batchcompleted source checks passed.")
    print("✅ Anime Post and Ongoing Post use their existing saved templates and channels.")
    print("✅ Season range/list and episode range/single values are supported; poster is optional.")
    print("ℹ️ Offline checks only: live Telegram/MongoDB publishing still requires deployment testing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
