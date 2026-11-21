"""Store renamed files, group 480p/720p/1080p per episode, and publish safely.

Channel routing is configured by the bot owner with /setanimechannels. No episode
post is sent until all three qualities have been stored and checked in Telegram.
"""

import asyncio
import hashlib
import logging
import re
from difflib import SequenceMatcher
import os
import secrets
from urllib.parse import urlparse
from datetime import datetime
from typing import Any

from pyrogram import Client, enums, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import Config
from helper.database import codeflixbots
from helper.file_names import (
    canonical_quality,
    extract_quality,
    extract_season_episode,
    extract_title,
    normalize_anime_title,
)
from plugins.channel_routing import resolve_ongoing_targets as _resolve_ongoing_targets_from_settings
from plugins.ongoing_posts import (
    _build_button_rows as _build_ongoing_button_rows,
    _send_bot_api as _send_ongoing_bot_api,
)


async def _resolve_ongoing_targets(route: dict) -> list[Any]:
    """Resolve anime-specific ongoing targets, falling back to global settings."""
    return await _resolve_ongoing_targets_from_settings(
        route or {}, codeflixbots.codeflixbots
    )

logger = logging.getLogger(__name__)
EXPECTED_QUALITIES = ("480p", "720p", "1080p")
_BATCH_LOCKS: dict[str, asyncio.Lock] = {}
# The rename handler may process multiple uploads concurrently. Serialize store + group
# mutations so two simultaneous qualities cannot race into separate episode batches.
_STORE_PROCESS_LOCK = asyncio.Lock()

DEFAULT_INDIVIDUAL_TEMPLATE = (
    "⌬ {anime} | Official Tamil Dub\n\n"
    "⌬ Quality : {qualities}\n\n"
    "⌬ Season : {season} | ⌬ Episode : {episode_range}👇"
)
DEFAULT_ONGOING_TEMPLATE = (
    "✦ {title}╭━━━━━━━━━━━━━━━━━━━━━━\n"
    "‣ Season : {season}\n"
    "‣ Episode : {episode_range}\n"
    "‣ Status : {status}\n"
    "‣ Audio : [Tamil Audio]\n"
    "‣ Quality : {qualities}╰━━━━━━━━━━━━━━━━━━━━━━\n"
    "✦ Powered By : @ANIFLIXANIMETAMIL"
)
DEFAULT_ANIME_BUTTONS_TEMPLATE = (
    "#b 480P - {quality_480p} | #r 720P - {quality_720p} | #g 1080P - {quality_1080p}"
)


def _now():
    return datetime.utcnow()


def _valid_http_url(value: str | None) -> bool:
    """Accept only complete HTTP(S) URLs for per-batch Download button overrides."""
    try:
        parsed = urlparse(str(value or "").strip())
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    except Exception:
        return False


def _title_key(title: str) -> str:
    return normalize_anime_title(title)


_TITLE_NOISE_SUFFIXES = {
    "official", "tamil", "audio", "dub", "dubbed", "sub", "subbed",
    "version", "eng", "english", "japanese", "dual", "multi", "uncensored",
    "hindi", "telugu", "malayalam", "kannada", "bengali", "jpn",
    "hd", "sd", "fhd", "hdrip", "hdtv", "webrip", "web", "dl",
    "bluray", "brrip", "x264", "x265", "hevc", "av1",
}


def _comparison_title(value: str) -> str:
    """Normalize harmless naming variants without merging distinct short titles."""
    text = _title_key(value)
    text = re.sub(r"[^a-z0-9]+", " ", text).strip()
    tokens = text.split()
    # Release labels sometimes remain in the title after filenames are renamed.
    # Strip only trailing labels; never remove words in the middle of a real title.
    while tokens and tokens[-1] in _TITLE_NOISE_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def _title_similarity(left: str, right: str) -> float:
    a = _comparison_title(left)
    b = _comparison_title(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    # Prevent short titles such as "Naruto" from matching "Naruto Shippuden".
    if min(len(a), len(b)) < 10:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _titles_match(left: str, right: str) -> bool:
    return _title_similarity(left, right) >= 0.92


def _batch_id(title_key: str, season: str, episode: str) -> str:
    raw = f"{title_key}|S{season}|E{episode}".casefold()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _quality_field(quality: str) -> str:
    return "q" + quality


def _safe_template(template: str, values: dict[str, Any]) -> str:
    """Interpolate only named values. Unknown placeholders become empty strings."""
    class SafeValues(dict):
        def __missing__(self, key):
            return ""

    try:
        rendered = str(template or DEFAULT_INDIVIDUAL_TEMPLATE).format_map(SafeValues(values))
    except (ValueError, TypeError, KeyError):
        rendered = DEFAULT_INDIVIDUAL_TEMPLATE.format_map(SafeValues(values))
    return rendered[:1024]


def _private_message_link(chat_id: int, message_id: int) -> str:
    # Telegram's /c/ form works for members of a private channel.
    internal_id = str(abs(int(chat_id)))
    if internal_id.startswith("100"):
        internal_id = internal_id[3:]
    return f"https://t.me/c/{internal_id}/{int(message_id)}"


def _storage_post_url(storage_chat, message_id: int) -> str:
    username = (getattr(storage_chat, "username", None) or "").strip().lstrip("@")
    if username:
        return f"https://t.me/{username}/{int(message_id)}"
    return _private_message_link(storage_chat.id, message_id)


def _media_from_message(message):
    if not message:
        return None
    return (
        getattr(message, "document", None)
        or getattr(message, "video", None)
        or getattr(message, "audio", None)
        or getattr(message, "animation", None)
        or getattr(message, "photo", None)
    )


def _link_summary(files: dict[str, Any]) -> str:
    return "\n".join(
        f"{q}: {files[_quality_field(q)]['url']}"
        for q in EXPECTED_QUALITIES
    )


def _buttons(files: dict[str, Any]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(q, url=files[_quality_field(q)]["url"])
        for q in EXPECTED_QUALITIES
    ]])


def _build_anime_button_rows(template: str, files: dict[str, Any], title: str = "") -> list[list[dict[str, str]]]:
    """Render the editable Anime Post button template against verified File Store links.

    Each non-empty line creates one row. Use `` | `` between button definitions to
    keep multiple quality buttons on the same row. #b/#r/#g map to Bot API button styles.
    """
    style_map = {"b": "primary", "r": "danger", "g": "success"}
    aliases = {
        "{quality_480p}": "480p", "{link_480p}": "480p", "{480p}": "480p",
        "{quality_720p}": "720p", "{link_720p}": "720p", "{720p}": "720p",
        "{quality_1080p}": "1080p", "{link_1080p}": "1080p", "{1080p}": "1080p",
    }
    rows: list[list[dict[str, str]]] = []
    found: list[str] = []
    text = str(template or DEFAULT_ANIME_BUTTONS_TEMPLATE).strip()
    for line_number, line in enumerate((line.strip() for line in text.splitlines() if line.strip()), start=1):
        row: list[dict[str, str]] = []
        for item in line.split(" | "):
            match = re.match(r"^#([bgr])\s+(.+?)\s+-\s*(.*?)\s*$", item.strip(), re.IGNORECASE)
            if not match:
                raise ValueError(f"Anime button line {line_number} is invalid. Use #b/#r/#g Button Name - {{quality_480p}}.")
            color_code, label, raw_url = match.groups()
            if not label.strip() or len(label.strip()) > 64:
                raise ValueError(f"Anime button line {line_number}: names must be 1–64 characters.")
            quality = aliases.get(raw_url.strip().casefold())
            if not quality:
                raise ValueError(f"Anime button line {line_number}: link must be {{quality_480p}}, {{quality_720p}}, or {{quality_1080p}}.")
            if quality in found:
                raise ValueError(f"Anime quality {quality} appears more than once in the button template.")
            found.append(quality)
            entry = files.get(_quality_field(quality)) or {}
            url = str(entry.get("url") or "").strip()
            if not (url.startswith("https://") or url.startswith("http://")):
                raise ValueError(f"The stored {quality} File Store URL is missing; refusing to create a broken button.")
            final_label = label.replace("{title}", title).replace("{anime}", title).strip()
            row.append({"text": final_label, "url": url, "style": style_map[color_code.lower()]})
        if row:
            rows.append(row)
    if set(found) != set(EXPECTED_QUALITIES) or len(found) != 3:
        raise ValueError("Anime Buttons must contain exactly one button each for 480p, 720p, and 1080p.")
    return rows


def _render_post(route: dict, mode: str, title: str, season: str, episode: str,
                 files: dict[str, Any]) -> str:
    links = {q: files[_quality_field(q)]["url"] for q in EXPECTED_QUALITIES}
    details = route.get("ongoing_details") or {}
    values = {
        "title": title,
        "anime": title,
        "season": season,
        "episode": episode,
        "episode_range": episode,
        "tag": str(details.get("tag") or ""),
        "qualities": ", ".join(EXPECTED_QUALITIES),
        "quality_480p": links["480p"],
        "quality_720p": links["720p"],
        "quality_1080p": links["1080p"],
        "link_480p": links["480p"],
        "link_720p": links["720p"],
        "link_1080p": links["1080p"],
        "audio": "Tamil Audio",
        "status": str(details.get("status") or "ONGOING"),
    }
    key = "individual_template" if mode == "individual" else "ongoing_template"
    fallback = DEFAULT_INDIVIDUAL_TEMPLATE if mode == "individual" else DEFAULT_ONGOING_TEMPLATE
    return _safe_template(route.get(key) or fallback, values)


async def _verify_batch_files(client, batch: dict) -> tuple[bool, str]:
    files = batch.get("files") or {}
    for quality in EXPECTED_QUALITIES:
        entry = files.get(_quality_field(quality))
        if not entry or not entry.get("storage_message_id") or not entry.get("storage_chat_id"):
            return False, f"Missing stored {quality} file details."
        if canonical_quality(entry.get("filename", "")) != quality:
            return False, f"The filename for the {quality} slot does not match that quality."
        entry_season, entry_episode = extract_season_episode(entry.get("filename", ""))
        expected_episode_start = str(batch.get("episode", "")).split("-", 1)[0].strip()
        try:
            episode_matches = bool(entry_episode) and int(entry_episode) == int(expected_episode_start)
            season_matches = bool(entry_season) and int(entry_season) == int(batch.get("season"))
        except (TypeError, ValueError):
            episode_matches = season_matches = False
        if not (season_matches and episode_matches):
            return False, f"The {quality} filename does not match the saved season/episode range."
        if not _titles_match(extract_title(entry.get("filename", "")), batch.get("title", batch.get("title_key", ""))):
            return False, f"The {quality} filename does not match the saved anime title."
        try:
            stored_username = (entry.get("storage_chat_username") or "").strip()
            if stored_username:
                storage_target = stored_username if stored_username.startswith("@") else "@" + stored_username
                storage_chat = await client.get_chat(storage_target)
            else:
                storage_chat = await client.get_chat(int(entry["storage_chat_id"]))
            stored = await client.get_messages(
                chat_id=int(storage_chat.id),
                message_ids=int(entry["storage_message_id"]),
            )
        except Exception as exc:
            logger.exception("Unable to verify stored %s file", quality)
            return False, f"Could not verify the {quality} stored message ({type(exc).__name__})."
        stored_media = _media_from_message(stored)
        if not stored_media:
            return False, f"The stored {quality} message no longer contains a file."
        expected_unique_id = entry.get("file_unique_id")
        actual_unique_id = getattr(stored_media, "file_unique_id", None)
        if expected_unique_id and actual_unique_id and expected_unique_id != actual_unique_id:
            return False, f"The stored {quality} message points to a different Telegram file."
        # Store the resolved Telegram chat ID after peer resolution for retrieval across restarts.
        entry["storage_chat_id"] = int(storage_chat.id)
    unique_ids = [files[_quality_field(q)].get("file_unique_id") for q in EXPECTED_QUALITIES]
    if not all(unique_ids):
        return False, "Telegram did not provide a unique file ID for every quality; nothing was published."
    if len(set(unique_ids)) != len(EXPECTED_QUALITIES):
        return False, "Two or more quality slots point to the same Telegram file; nothing was published."
    return True, ""


async def _send_target(client, route: dict, batch: dict, mode: str, target_override=None) -> tuple[bool, str]:
    target_field = "individual_channel" if mode == "individual" else "ongoing_channel"
    target = target_override if target_override is not None else route.get(target_field)
    if not target:
        return False, "not configured"

    target_key = f"{mode}:{target}"
    published = batch.get("published_targets") or {}
    if published.get(target_key):
        return True, "already published"

    chat = None
    if mode == "individual":
        chat = await client.get_chat(target)
    else:
        # Public usernames can be resolved for a real chat ID; private numeric IDs
        # are intentionally sent through Bot API and do not require Pyrogram's peer cache.
        try:
            chat = await client.get_chat(target)
        except Exception:
            if not (isinstance(target, int) or (isinstance(target, str) and target.lstrip("-").isdigit())):
                raise
    title = batch["title"]
    season = batch["season"]
    episode = batch.get("episode_display") or batch["episode"]
    files = batch["files"]
    global_templates = await codeflixbots.codeflixbots["post_template_settings"].find_one({"_id": "global"}) or {}
    route = dict(route)
    if mode == "individual":
        route["individual_template"] = global_templates.get("anime_post_template") or DEFAULT_INDIVIDUAL_TEMPLATE
    else:
        route["ongoing_template"] = global_templates.get("ongoing_template") or DEFAULT_ONGOING_TEMPLATE
    caption = _render_post(route, mode, title, season, episode, files)
    if mode == "individual":
        button_template = global_templates.get("anime_buttons_template") or DEFAULT_ANIME_BUTTONS_TEMPLATE
        keyboard_rows = _build_anime_button_rows(button_template, files, title)
        sent_id = await _send_ongoing_bot_api(client, int(chat.id), None, caption, keyboard_rows)
        await codeflixbots.codeflixbots["episode_batches"].update_one(
            {"_id": batch["_id"]},
            {"$set": {
                f"published_targets.{target_key}": {
                    "chat_id": int(chat.id), "message_id": int(sent_id), "published_at": _now(),
                },
                "updated_at": _now(),
            }},
        )
        logger.info("Published %s S%sE%s to Anime Post %s as message %s", title, season, episode, target, sent_id)
        return True, "published"

    keyboard = _buttons(files)
    poster_file_id = None
    if mode == "ongoing":
        named_template = await codeflixbots.codeflixbots["anime_templates"].find_one({"_id": _title_key(title)}) or {}
        poster_file_id = named_template.get("poster_file_id") or route.get("poster_file_id")
        invite_doc = await codeflixbots.codeflixbots["anime_invite_links"].find_one({"_id": route.get("title_key") or _title_key(title)}) or {}
        invite_links = list(invite_doc.get("links") or [])
        post_settings = await codeflixbots.codeflixbots["post_template_settings"].find_one({"_id": "global"}) or {}
        button_template = post_settings.get("ongoing_buttons_template") or "#b ✦ 𝗗𝗢𝗪𝗡𝗟𝗢𝗔𝗗 𝗡𝗢𝗪 ✦ - {anime_invite}\n#r 𖤓𝗔𝗡𝗜𝗠𝗘 𝗜𝗡𝗗𝗘𝗫 𝗧𝗔𝗠𝗜𝗟𖤓 - https://t.me/+qGiH-Zw3ydJjZjU1"
        keyboard = _build_ongoing_button_rows(
            button_template,
            invite_links,
            fallback_download_url=batch.get("fallback_download_url"),
        )
        api_target = int(target) if isinstance(target, str) and target.lstrip("-").isdigit() else target
        sent_id = await _send_ongoing_bot_api(client, api_target, poster_file_id, caption, keyboard)
        stored_chat_id = int(chat.id) if chat is not None else (int(target) if isinstance(target, int) or (isinstance(target, str) and target.lstrip("-").isdigit()) else str(target))
        await codeflixbots.codeflixbots["episode_batches"].update_one(
            {"_id": batch["_id"]},
            {"$set": {
                f"published_targets.{target_key}": {"chat_id": stored_chat_id, "message_id": int(sent_id), "published_at": _now()},
                "updated_at": _now(),
            }},
        )
        logger.info("Published %s S%sE%s to ongoing %s as message %s", title, season, episode, target, sent_id)
        return True, "published"

    try:
        if poster_file_id:
            sent = await client.send_photo(
                chat_id=chat.id,
                photo=poster_file_id,
                caption=caption,
                reply_markup=keyboard,
                parse_mode=enums.ParseMode.HTML,
            )
        else:
            sent = await client.send_message(
                chat_id=chat.id,
                text=caption,
                reply_markup=keyboard,
                parse_mode=enums.ParseMode.HTML,
                disable_web_page_preview=True,
            )
    except Exception as exc:
        # A malformed user-provided HTML tag should not prevent a valid post from being sent.
        error_text = str(exc).casefold()
        if not ("parse entities" in error_text or "can't parse" in error_text or "unsupported start tag" in error_text):
            raise
        logger.warning("Template HTML could not be parsed; retrying as plain text: %s", exc)
        if poster_file_id:
            sent = await client.send_photo(
                chat_id=chat.id,
                photo=poster_file_id,
                caption=caption,
                reply_markup=keyboard,
                parse_mode=enums.ParseMode.DISABLED,
            )
        else:
            sent = await client.send_message(
                chat_id=chat.id,
                text=caption,
                reply_markup=keyboard,
                parse_mode=enums.ParseMode.DISABLED,
                disable_web_page_preview=True,
            )

    # Write per-target success immediately so a later target failing cannot cause a duplicate.
    await codeflixbots.codeflixbots["episode_batches"].update_one(
        {"_id": batch["_id"]},
        {"$set": {
            f"published_targets.{target_key}": {
                "chat_id": int(chat.id),
                "message_id": int(sent.id),
                "published_at": _now(),
            },
            "updated_at": _now(),
        }},
    )
    logger.info("Published %s S%sE%s to %s as message %s", title, season, episode, target, sent.id)
    return True, "published"


async def _publish_ready_batch_locked(client, batch: dict) -> str:
    """Verify and publish a complete batch; caller holds its per-episode lock."""
    ok, why = await _verify_batch_files(client, batch)
    if not ok:
        await codeflixbots.codeflixbots["episode_batches"].update_one(
            {"_id": batch["_id"]},
            {"$set": {"state": "verification_failed", "last_error": why, "updated_at": _now()}},
        )
        return f"⚠️ Not published: {why}"

    # Only now, after every stored message has been re-read and checked, create the
    # File-Store style deep links. A click starts this bot and returns the selected file.
    bot_username = getattr(client, "username", None)
    if not bot_username:
        bot_username = getattr(await client.get_me(), "username", None)
    if not bot_username:
        return "⚠️ Files are stored and verified, but the bot username could not be resolved; no links were generated."
    bot_username = str(bot_username).lstrip("@")
    files = batch.get("files") or {}
    for quality in EXPECTED_QUALITIES:
        entry = files[_quality_field(quality)]
        # A 24-byte URL-safe token provides stronger resistance to guessing than
        # the old short hex token while remaining valid in Telegram deep-link payloads.
        token = entry.get("access_token") or secrets.token_urlsafe(24)
        access_url = f"https://t.me/{bot_username}?start=file_{token}"
        entry["access_token"] = token
        entry["url"] = access_url
        entry["storage_post_url"] = _storage_post_url(
            type("StoredChat", (), {"id": entry["storage_chat_id"], "username": entry.get("storage_chat_username")})(),
            int(entry["storage_message_id"]),
        )
        entry["verified_at"] = _now()
        await codeflixbots.codeflixbots["episode_batches"].update_one(
            {"_id": batch["_id"]},
            {"$set": {f"files.{_quality_field(quality)}": entry, "updated_at": _now()}},
        )
        await codeflixbots.codeflixbots["file_store"].update_one(
            {"storage_chat_id": int(entry["storage_chat_id"]),
             "storage_message_id": int(entry["storage_message_id"])},
            {"$set": {
                "access_token": token,
                "url": access_url,
                "storage_post_url": entry["storage_post_url"],
                "verified_at": _now(),
            }},
        )
        token_record = await codeflixbots.codeflixbots["file_store"].find_one({
            "access_token": token,
            "storage_chat_id": int(entry["storage_chat_id"]),
            "storage_message_id": int(entry["storage_message_id"]),
        })
        if not token_record or token_record.get("url") != access_url:
            return f"⚠️ {quality} was verified, but its access link could not be saved. Nothing was published."

    route = await _find_route_for_batch(batch)
    if not route:
        await codeflixbots.codeflixbots["episode_batches"].update_one(
            {"_id": batch["_id"]},
            {"$set": {"state": "ready_waiting_for_channel", "updated_at": _now()}},
        )
        return (
            "✅ All 3 files are stored and verified. Links are ready, but no channel mapping is configured for this anime yet.\n"
            "Choose a saved anime channel in the batch workflow, or configure it in Admin Panel → Anime Channels.\n\n"
            + _link_summary(files)
        )

    individual_target = route.get("individual_channel")
    ongoing_targets = await _resolve_ongoing_targets(route)
    # Preserve order and avoid sending twice if the same channel was assigned in both fields.
    unique_ongoing = []
    seen_ongoing = set()
    for candidate in ongoing_targets:
        if candidate is None or str(candidate).strip() == "":
            continue
        identity = str(candidate).casefold()
        if identity not in seen_ongoing:
            seen_ongoing.add(identity)
            unique_ongoing.append(candidate)

    failures = []
    if individual_target:
        try:
            await _send_target(client, route, batch, "individual", target_override=individual_target)
        except Exception as exc:
            logger.exception("Could not publish individual post for batch %s", batch["_id"])
            failures.append(f"Anime channel {individual_target}: {type(exc).__name__}: {exc}")
    else:
        failures.append("No individual Anime Post channel is configured for the selected route")

    # Ongoing posts are now automatic too; no /ongoing command is required.
    for ongoing_target in unique_ongoing:
        try:
            await _send_target(client, route, batch, "ongoing", target_override=ongoing_target)
        except Exception as exc:
            logger.exception("Could not publish ongoing post to %s for batch %s", ongoing_target, batch["_id"])
            failures.append(f"Ongoing {ongoing_target}: {type(exc).__name__}: {exc}")

    refreshed = await codeflixbots.codeflixbots["episode_batches"].find_one({"_id": batch["_id"]}) or batch
    published_targets = refreshed.get("published_targets") or {}
    expected_target_keys = []
    if individual_target:
        expected_target_keys.append(f"individual:{individual_target}")
    expected_target_keys.extend(f"ongoing:{target}" for target in unique_ongoing)
    all_done = bool(expected_target_keys) and all(published_targets.get(key) for key in expected_target_keys)
    state = "published" if all_done and not failures else "publish_partial"
    await codeflixbots.codeflixbots["episode_batches"].update_one(
        {"_id": batch["_id"]},
        {"$set": {"state": state, "last_error": "\n".join(failures) if failures else None, "updated_at": _now()}},
    )
    if failures:
        return (
            "⚠️ Files are stored and links are generated, but one or more posts were not confirmed.\n"
            + "\n".join(failures[:4])
        )
    ongoing_summary = f" Ongoing published to {len(unique_ongoing)} channel(s)." if unique_ongoing else " No ongoing destinations are configured for this anime."
    return "✅ All 3 files stored and verified. Anime Post published with inline quality buttons." + ongoing_summary


async def _find_route_for_batch(batch: dict):
    """Use the route explicitly selected for a manual batch, then title matching."""
    routes = codeflixbots.codeflixbots["anime_routes"]
    selected_key = batch.get("route_title_key")
    if selected_key:
        selected = await routes.find_one({"title_key": selected_key})
        if selected:
            return selected
    exact = await routes.find_one({"title_key": batch.get("title_key")})
    if exact:
        return exact
    cursor = routes.find({})
    candidates = []
    async for route in cursor:
        score = _title_similarity(
            route.get("title_key") or route.get("title", ""),
            batch.get("title_key") or batch.get("title", ""),
        )
        if score >= 0.92:
            candidates.append((score, route))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    # If two different saved routes are equally plausible, do not guess a channel.
    if len(candidates) > 1 and candidates[0][0] - candidates[1][0] < 0.03:
        return None
    return candidates[0][1]


async def _find_or_merge_episode_batch(batches, group_id: str, title: str,
                                       title_key: str, season: str, episode: str):
    """Reuse an episode group when harmless filename/title variants split qualities.

    Search by the same season and episode even when the incoming file's exact hash
    already exists. This repairs the specific case where 720p and 1080p were saved in
    separate pending batches before the grouping fix was deployed.
    """
    exact = await batches.find_one({"_id": group_id})
    if exact and exact.get("state") == "published":
        # An admin may intentionally upload the same episode/quality again.
        # Reuse its batch, but allow a fresh publication instead of suppressing it.
        return exact, "republish"

    cursor = batches.find({
        "season": str(season),
        "episode": str(episode),
        "state": {"$ne": "merged"},
    })
    all_candidates = await cursor.to_list(length=100)
    matches = []
    seen_ids = set()
    for candidate in all_candidates:
        candidate_id = str(candidate.get("_id", ""))
        if candidate_id in seen_ids:
            continue
        seen_ids.add(candidate_id)
        score = _title_similarity(
            candidate.get("title_key") or candidate.get("title", ""),
            title_key or title,
        )
        if score >= 0.92 or candidate_id == str(group_id):
            matches.append((score, candidate))

    # Reuse a matching published batch so the new upload can refresh its quality
    # slot and intentionally publish the episode again. Do not treat prior
    # publication as a permanent duplicate lock.
    published = [(score, candidate) for score, candidate in matches
                 if candidate.get("state") == "published"]
    if published:
        published.sort(key=lambda item: item[0], reverse=True)
        return published[0][1], "republish"

    pending = [(score, candidate) for score, candidate in matches
               if candidate.get("state") not in {"published", "merged"}]
    if not pending:
        return None, None

    # Prefer the exact ID if it exists; otherwise keep the best-matching / fullest
    # batch as canonical so links and publish history remain stable.
    exact_entry = next((item for item in pending if str(item[1].get("_id")) == str(group_id)), None)
    if exact_entry:
        canonical = exact_entry[1]
    else:
        pending.sort(key=lambda item: (item[0], len(item[1].get("files") or {})), reverse=True)
        canonical = pending[0][1]
    canonical_id = str(canonical["_id"])
    current_files = canonical.get("files") or {}

    for _score, other in pending:
        other_id = str(other.get("_id", ""))
        if other_id == canonical_id:
            continue
        other_files = other.get("files") or {}
        conflict = False
        for quality in EXPECTED_QUALITIES:
            field = _quality_field(quality)
            left = current_files.get(field)
            right = other_files.get(field)
            if left and right:
                left_uid = left.get("file_unique_id")
                right_uid = right.get("file_unique_id")
                if left_uid and right_uid and left_uid == right_uid:
                    continue
                # Never silently choose between two different files of one quality.
                conflict = True
                break
        if conflict:
            logger.warning(
                "Not merging episode batches %s and %s: conflicting quality slots",
                canonical_id, other_id,
            )
            continue

        updates = {}
        for quality in EXPECTED_QUALITIES:
            field = _quality_field(quality)
            if not current_files.get(field) and other_files.get(field):
                current_files[field] = other_files[field]
                updates[f"files.{field}"] = other_files[field]
        if updates:
            updates["updated_at"] = _now()
            await batches.update_one({"_id": canonical_id}, {"$set": updates})
        await batches.update_one(
            {"_id": other_id},
            {"$set": {"state": "merged", "merged_into": canonical_id, "updated_at": _now()}},
        )

    refreshed = await batches.find_one({"_id": canonical_id})
    return refreshed or canonical, None


async def publish_ready_batch(client, batch: dict) -> str:
    """Serialize publication per episode and reload current DB state before sending."""
    batch_id = str(batch.get("_id", ""))
    lock = _BATCH_LOCKS.setdefault(batch_id, asyncio.Lock())
    async with lock:
        current = await codeflixbots.codeflixbots["episode_batches"].find_one({"_id": batch_id})
        if not current:
            return "⚠️ Episode batch disappeared from MongoDB; nothing was published."
        if current.get("state") == "published":
            current_route = await _find_route_for_batch(current) or {}
            published_targets = current.get("published_targets") or {}
            expected = []
            if current_route.get("individual_channel"):
                expected.append(f"individual:{current_route['individual_channel']}")
            # Resolve the same destination list used by the publisher. This includes
            # globally configured ongoing channels when the anime has no per-route
            # ongoing assignment. Without this, a batch could be marked "already
            # published" after only the individual channel succeeded, even if a new
            # global ongoing destination had never received the post.
            ongoing = await _resolve_ongoing_targets(current_route)
            expected.extend(f"ongoing:{target}" for target in ongoing if target)
            if expected and all(published_targets.get(key) for key in expected):
                return "✅ This episode has already been published to all configured channels; duplicate post prevented."
        current_files = current.get("files") or {}
        if not all(current_files.get(_quality_field(q)) for q in EXPECTED_QUALITIES):
            return "⏳ Waiting until 480p, 720p, and 1080p are all stored."
        return await _publish_ready_batch_locked(client, current)


async def _store_file_and_process_locked(client, sent_message, new_filename: str, user_id: int, file_size: int, details: dict | None = None) -> str:
    """Implementation protected by the global store/grouping lock."""
    # Keep compatibility with deployments that already used DUMP_CHANNEL, while
    # preferring the explicit STORAGE_CHANNEL_ID setting for the file store.
    storage_setting = (
        os.environ.get("STORAGE_CHANNEL_ID", "").strip()
        or os.environ.get("FILE_STORE_CHANNEL_ID", "").strip()
        or os.environ.get("DUMP_CHANNEL", "").strip()
        or (str(Config.DUMP_CHANNEL) if Config.DUMP_CHANNEL is not None else "")
    )
    fallback_username = os.environ.get("STORAGE_CHANNEL_USERNAME", "").strip()
    if not storage_setting:
        return "⚠️ Renaming succeeded, but File Store is not configured. Set STORAGE_CHANNEL_ID (or DUMP_CHANNEL) to the dump channel ID."

    storage_target: Any = storage_setting
    if storage_setting.lstrip("-").isdigit():
        storage_target = int(storage_setting)
    try:
        storage_chat = await client.get_chat(storage_target)
    except Exception:
        if fallback_username:
            storage_chat = await client.get_chat(fallback_username if fallback_username.startswith("@") else "@" + fallback_username)
        else:
            raise RuntimeError(
                "Cannot resolve STORAGE_CHANNEL_ID. For a public channel, set it to @ChannelUsername; "
                "optionally set STORAGE_CHANNEL_USERNAME as well."
            )

    stored_message = await client.copy_message(
        chat_id=storage_chat.id,
        from_chat_id=sent_message.chat.id,
        message_id=sent_message.id,
    )
    stored_media = _media_from_message(stored_message)
    if not stored_media:
        raise RuntimeError("Telegram copied the message, but the copied post contains no file")

    season, episode = extract_season_episode(new_filename)
    quality = canonical_quality(new_filename) or "Unknown"
    title = extract_title(new_filename)
    if details:
        title = str(details.get("title") or title).strip()
        season = str(details.get("season") or season or "").strip()
        episode = str(details.get("episode_range") or episode or "").strip()
    try:
        season = str(int(str(season).strip())) if season else None
    except (ValueError, TypeError):
        season = None
    episode_display = str(episode).strip() if episode else None
    if episode:
        range_match = re.fullmatch(r"\s*(\d{1,6})(?:\s*[-–]\s*(\d{1,6}))?\s*", str(episode))
        if range_match:
            first_ep = range_match.group(1)
            last_ep = range_match.group(2) if range_match.group(2) else None
            if last_ep and int(last_ep) < int(first_ep):
                episode = None
                episode_display = None
            else:
                # Internal IDs and grouping use canonical values, while the post
                # retains exactly the digits/range the admin entered.
                episode = f"{int(first_ep)}-{int(last_ep)}" if last_ep else str(int(first_ep))
        else:
            episode = None
            episode_display = None
    title_key = _title_key(title)
    created_at = _now()
    logger.info(
        "Stored episode parse: filename=%r title=%r title_key=%r season=%r episode=%r quality=%r",
        new_filename, title, title_key, season, episode, quality,
    )
    record = {
        "user_id": int(user_id),
        "storage_chat_id": int(storage_chat.id),
        "storage_chat_username": getattr(storage_chat, "username", None),
        "storage_message_id": int(stored_message.id),
        "filename": str(new_filename),
        "title": str(title),
        "title_key": title_key,
        "season": season,
        "episode": episode,
        "episode_display": episode_display,
        "quality": quality,
        "url": None,
        "access_token": None,
        "storage_post_url": None,
        "file_id": getattr(stored_media, "file_id", None),
        "file_unique_id": getattr(stored_media, "file_unique_id", None),
        "file_size": int(getattr(stored_media, "file_size", file_size) or file_size),
        "created_at": created_at,
    }
    await codeflixbots.codeflixbots["file_store"].update_one(
        {"storage_chat_id": int(storage_chat.id), "storage_message_id": int(stored_message.id)},
        {"$set": record},
        upsert=True,
    )

    # An unidentifiable file stays safely stored but never gets attached to an episode.
    if not title_key or not season or not episode or quality not in EXPECTED_QUALITIES:
        missing = []
        if not title_key:
            missing.append("anime title")
        if not season:
            missing.append("season number")
        if not episode:
            missing.append("episode number")
        if quality not in EXPECTED_QUALITIES:
            missing.append("quality (480p/720p/1080p)")
        return (
            "✅ Renamed file saved to File Store.\n\n"
            "⚠️ No access link was generated and nothing was published because I could not reliably detect: "
            + ", ".join(missing) + "."
        )

    group_id = _batch_id(title_key, str(season), str(episode))
    quality_key = _quality_field(quality)
    group_file = {
        "quality": quality,
        "filename": str(new_filename),
        "url": None,
        "access_token": None,
        "storage_chat_id": int(storage_chat.id),
        "storage_chat_username": getattr(storage_chat, "username", None),
        "storage_message_id": int(stored_message.id),
        "file_id": getattr(stored_media, "file_id", None),
        "file_unique_id": getattr(stored_media, "file_unique_id", None),
        "file_size": record["file_size"],
        "verified_at": None,
    }
    batches = codeflixbots.codeflixbots["episode_batches"]
    existing, match_status = await _find_or_merge_episode_batch(
        batches,
        group_id,
        title,
        title_key,
        str(season),
        str(episode),
    )
    republishing = match_status == "republish" or bool(existing and existing.get("state") == "published")
    if existing:
        group_id = str(existing["_id"])
        # Keep the main batch's saved title key for its route and verification history.
        title_key = existing.get("title_key") or title_key
        title = existing.get("title") or title

    await batches.update_one(
        {"_id": group_id},
        {
            "$setOnInsert": {
                "title": title,
                "title_key": title_key,
                "season": str(season),
                "episode": str(episode),
                "episode_display": episode_display or str(episode),
                "created_at": created_at,
                "published_targets": {},
            },
            "$set": {
                f"files.{quality_key}": group_file,
                "state": "collecting",
                "updated_at": created_at,
                "last_error": None,
                **({"published_targets": {}} if republishing else {}),
                **({"route_title_key": details.get("route_title_key")} if details and details.get("route_title_key") else {}),
                "fallback_download_url": (
                    str(details.get("fallback_download_url")).strip()
                    if details and _valid_http_url(details.get("fallback_download_url"))
                    else None
                ),
            },
        },
        upsert=True,
    )
    batch = await batches.find_one({"_id": group_id})
    files = batch.get("files") or {}
    missing_quality = [q for q in EXPECTED_QUALITIES if not files.get(_quality_field(q))]
    if missing_quality:
        return (
            f"✅ {quality} renamed and stored.\n\n"
            f"📺 {title} — Season {season}, Episode {episode}\n"
            f"Stored qualities: {3 - len(missing_quality)}/3\n"
            f"Waiting for: {', '.join(missing_quality)}"
        )

    # Re-read media from Telegram to ensure all three stored messages are present.
    result = await publish_ready_batch(client, batch)
    return result


async def store_file_and_process(client, sent_message, new_filename: str, user_id: int, file_size: int, details: dict | None = None) -> str:
    """Serialize stored-file writes and episode grouping across concurrent uploads."""
    async with _STORE_PROCESS_LOCK:
        return await _store_file_and_process_locked(
            client=client,
            sent_message=sent_message,
            new_filename=new_filename,
            user_id=user_id,
            file_size=file_size,
            details=details,
        )


# ---------- Owner-only channel and template configuration commands ----------

def _is_admin(message) -> bool:
    user = getattr(message, "from_user", None)
    if not user:
        return False
    allowed = set(Config.ADMIN or [])
    for name in ("OWNER_ID", "OWNERID", "ADMIN_ID"):
        raw = os.environ.get(name, "").strip()
        for item in raw.replace(",", " ").split() if raw else []:
            if item.isdigit():
                allowed.add(int(item))
    return int(user.id) in allowed


def _split_pipe_args(message, command_name: str, count: int):
    raw = (message.text or "").split(maxsplit=1)
    if len(raw) < 2:
        return None
    parts = [part.strip() for part in raw[1].split("|", count - 1)]
    if len(parts) != count or not parts[0]:
        return None
    return parts


@Client.on_message(filters.private & filters.command("setanimechannels"))
async def set_anime_channels(client, message):
    if not _is_admin(message):
        return await message.reply_text("⛔ Owner/admin only.")
    parts = _split_pipe_args(message, "setanimechannels", 3)
    if not parts:
        return await message.reply_text(
            "Usage:\n/setanimechannels Anime Title | @individual_channel | @ongoing_channel\n"
            "Use - for a channel you don't want to configure.\nExample:\n"
            "/setanimechannels Naruto Shippuden | @NarutoTamil | @NarutoOngoing"
        )
    title, individual, ongoing = parts
    individual = None if individual == "-" else individual
    ongoing = None if ongoing == "-" else ongoing
    if not individual and not ongoing:
        return await message.reply_text("❌ Set at least one channel.")

    # Validate destinations before saving. Invalid or inaccessible destinations are rejected.
    resolved = {"individual_channel": None, "ongoing_channel": None}
    try:
        for field, target in (("individual_channel", individual), ("ongoing_channel", ongoing)):
            if target:
                lookup_target = int(target) if target.lstrip("-").isdigit() else target
                if isinstance(lookup_target, str) and not lookup_target.startswith(("@", "https://t.me/", "http://t.me/")):
                    lookup_target = "@" + lookup_target
                chat = await client.get_chat(lookup_target)
                resolved[field] = ("@" + chat.username) if getattr(chat, "username", None) else int(chat.id)
                me = await client.get_me()
                bot_member = await client.get_chat_member(chat.id, me.id)
                bot_status = str(getattr(bot_member, "status", "")).casefold().split(".")[-1]
                if bot_status not in {"administrator", "creator", "owner"}:
                    return await message.reply_text(f"❌ The bot must be an administrator in {getattr(chat, 'title', target)} before it can publish there.")
                if field == "individual_channel" and getattr(bot_member, "privileges", None) is not None:
                    if not getattr(bot_member.privileges, "can_post_messages", True) and bot_status not in {"creator", "owner"}:
                        return await message.reply_text("❌ The bot admin role does not have permission to post messages in the Anime Post channel.")
    except Exception as exc:
        return await message.reply_text(
            f"❌ Could not access one of those channels: {type(exc).__name__}: {exc}\n"
            "Add the bot as an administrator with permission to post, then try again."
        )

    key = _title_key(title)
    route_fields = {"title": title, "title_key": key, **resolved, "updated_at": _now()}
    route_update = {"$set": route_fields, "$setOnInsert": {"created_at": _now()}}
    if ongoing:
        route_update["$set"]["ongoing_channels"] = [resolved["ongoing_channel"]]
    else:
        route_update["$unset"] = {"ongoing_channels": "", "ongoing_channel_titles": ""}
    await codeflixbots.codeflixbots["anime_routes"].update_one(
        {"title_key": key}, route_update, upsert=True,
    )
    await message.reply_text(
        f"✅ Channel route saved for {title}.\n"
        f"Individual: {individual or 'Not set'}\nOngoing: {ongoing or 'Not set'}\n\n"
        "I will publish only after 480p, 720p, and 1080p are stored and verified."
    )
    # Publish batches that were already complete while no route was configured.
    cursor = codeflixbots.codeflixbots["episode_batches"].find({"state": {"$ne": "merged"}})
    async for batch in cursor:
        if not _titles_match(batch.get("title_key") or batch.get("title", ""), key):
            continue
        files = batch.get("files") or {}
        if all(files.get(_quality_field(q)) for q in EXPECTED_QUALITIES):
            result = await publish_ready_batch(client, batch)
            await message.reply_text(f"{batch.get('title')} S{batch.get('season')}E{batch.get('episode')}:\n{result}")


@Client.on_message(filters.private & filters.command("setepisodetemplate"))
async def set_episode_template(client, message):
    if not _is_admin(message):
        return await message.reply_text("⛔ Owner/admin only.")
    parts = _split_pipe_args(message, "setepisodetemplate", 2)
    if not parts:
        return await message.reply_text(
            "Usage:\n/setepisodetemplate Anime Title | Your post text\n\n"
            "Placeholders: {title}, {season}, {episode}, {qualities}, {link_480p}, {link_720p}, {link_1080p}"
        )
    title, template = parts
    if len(template) > 950:
        return await message.reply_text("❌ Template must be 950 characters or shorter.")
    key = _title_key(title)
    result = await codeflixbots.codeflixbots["anime_routes"].update_one(
        {"title_key": key},
        {"$set": {"title": title, "title_key": key, "individual_template": template, "updated_at": _now()},
         "$setOnInsert": {"created_at": _now()}}, upsert=True,
    )
    await message.reply_text("✅ Individual episode template saved. Configure channels with /setanimechannels.")


@Client.on_message(filters.private & filters.command("setongoingtemplate"))
async def set_ongoing_template(client, message):
    if not _is_admin(message):
        return await message.reply_text("⛔ Owner/admin only.")
    parts = _split_pipe_args(message, "setongoingtemplate", 2)
    if not parts:
        return await message.reply_text(
            "Usage:\n/setongoingtemplate Anime Title | Your ongoing post text\n\n"
            "Placeholders: {title}, {season}, {episode}, {qualities}, {link_480p}, {link_720p}, {link_1080p}"
        )
    title, template = parts
    if len(template) > 950:
        return await message.reply_text("❌ Template must be 950 characters or shorter.")
    key = _title_key(title)
    await codeflixbots.codeflixbots["anime_routes"].update_one(
        {"title_key": key},
        {"$set": {"title": title, "title_key": key, "ongoing_template": template, "updated_at": _now()},
         "$setOnInsert": {"created_at": _now()}}, upsert=True,
    )
    await message.reply_text("✅ Ongoing post template saved.")


@Client.on_message(filters.private & filters.command("setanimeposter"))
async def set_anime_poster(client, message):
    if not _is_admin(message):
        return await message.reply_text("⛔ Owner/admin only.")
    raw = (message.text or "").split(maxsplit=1)
    if len(raw) < 2 or not message.reply_to_message or not message.reply_to_message.photo:
        return await message.reply_text(
            "Reply to an anime poster photo with:\n/setanimeposter Anime Title"
        )
    title = raw[1].strip()
    key = _title_key(title)
    photo = message.reply_to_message.photo
    await codeflixbots.codeflixbots["anime_routes"].update_one(
        {"title_key": key},
        {"$set": {"title": title, "title_key": key, "poster_file_id": photo.file_id, "updated_at": _now()},
         "$setOnInsert": {"created_at": _now()}}, upsert=True,
    )
    await message.reply_text(f"✅ Ongoing poster saved for {title}.")


@Client.on_message(filters.private & filters.command("listanimechannels"))
async def list_anime_channels(client, message):
    if not _is_admin(message):
        return await message.reply_text("⛔ Owner/admin only.")
    cursor = codeflixbots.codeflixbots["anime_routes"].find({}).sort("title", 1)
    lines = []
    async for route in cursor:
        ongoing = route.get("ongoing_channels") or ([route.get("ongoing_channel")] if route.get("ongoing_channel") else [])
        ongoing_text = ", ".join(str(target) for target in ongoing) if ongoing else "Global fallback / Not assigned"
        lines.append(
            f"• {route.get('title', '(unknown)')}\n"
            f"  Individual: {route.get('individual_channel') or 'Not set'}\n"
            f"  Ongoing: {ongoing_text}"
        )
    await message.reply_text("\n\n".join(lines) if lines else "No anime channel routes saved yet.")


@Client.on_message(filters.private & filters.command("fixepisodegroups"))
async def fix_episode_groups(client, message):
    """Owner-only repair for split pending batches created by the previous grouping bug."""
    if not _is_admin(message):
        return await message.reply_text("⛔ Owner/admin only.")

    batches_col = codeflixbots.codeflixbots["episode_batches"]
    cursor = batches_col.find({"state": {"$nin": ["published", "merged"]}}).sort("updated_at", 1).limit(500)
    pending = await cursor.to_list(length=500)
    if not pending:
        return await message.reply_text("✅ No pending episode groups need repair.")

    merged_count = 0
    published_count = 0
    waiting = []
    visited = set()
    async with _STORE_PROCESS_LOCK:
        for old_batch in pending:
            if old_batch.get("state") in {"published", "merged"}:
                continue
            batch, match_status = await _find_or_merge_episode_batch(
                batches_col,
                str(old_batch.get("_id", "")),
                old_batch.get("title", ""),
                old_batch.get("title_key", ""),
                str(old_batch.get("season", "")),
                str(old_batch.get("episode", "")),
            )
            if match_status == "published" or not batch:
                continue
            canonical_id = str(batch.get("_id", ""))
            if not canonical_id or canonical_id in visited:
                continue
            visited.add(canonical_id)
            files = batch.get("files") or {}
            present = [q for q in EXPECTED_QUALITIES if files.get(_quality_field(q))]
            if len(present) == 3:
                result = await publish_ready_batch(client, batch)
                if "published" in result.lower() or "links are ready" in result.lower():
                    published_count += 1
                waiting.append(f"{batch.get('title')} S{batch.get('season')}E{batch.get('episode')}: {result}")
            else:
                waiting.append(
                    f"{batch.get('title')} S{batch.get('season')}E{batch.get('episode')}: "
                    f"{len(present)}/3 stored; waiting for "
                    f"{', '.join(q for q in EXPECTED_QUALITIES if q not in present)}"
                )
            merged_count += 1

    header = f"🛠 Repair scan finished. Checked {merged_count} episode group(s); {published_count} complete group(s) processed."
    details = "\n\n".join(waiting[:15])
    await message.reply_text((header + ("\n\n" + details if details else ""))[:3900])


@Client.on_message(filters.private & filters.command("episodequeue"))
async def show_episode_queue(client, message):
    if not _is_admin(message):
        return await message.reply_text("⛔ Owner/admin only.")
    cursor = codeflixbots.codeflixbots["episode_batches"].find(
        {"state": {"$ne": "published"}}
    ).sort("updated_at", -1).limit(20)
    lines = []
    async for batch in cursor:
        files = batch.get("files") or {}
        qualities = [q for q in EXPECTED_QUALITIES if files.get(_quality_field(q))]
        lines.append(
            f"• {batch.get('title')} S{batch.get('season')}E{batch.get('episode')} — "
            f"{len(qualities)}/3 ({', '.join(qualities) or 'none'}) — {batch.get('state', 'collecting')}"
        )
    await message.reply_text("\n".join(lines) if lines else "✅ No pending episode batches.")
