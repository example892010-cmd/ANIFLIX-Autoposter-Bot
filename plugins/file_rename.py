"""File download, rename, optional metadata, thumbnail and upload handlers."""

import asyncio
import logging
import os
import re
import shutil
import tempfile
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from PIL import Image
from pyrogram import Client, enums, filters, StopPropagation
from pyrogram.errors import FloodWait
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from helper.database import codeflixbots
from config import Config
from helper.file_names import (
    extract_quality,
    extract_season_episode,
    format_caption,
    metadata_enabled,
    render_filename,
    source_basename,
    sanitize_filename,
    normalize_anime_title,
)
from helper.utils import progress_for_pyrogram
from plugins.antinsfw import check_anti_nsfw
from plugins.episode_publisher import (
    _batch_id, store_file_and_process, _safe_template,
    DEFAULT_INDIVIDUAL_TEMPLATE, _send_ongoing_bot_api,
)
from plugins.channel_routing import resolve_ongoing_targets as _resolve_ongoing_targets_from_settings
from plugins.ongoing_posts import post_ongoing_anime as _post_ongoing_anime

async def _resolve_ongoing_targets(route: dict) -> list:
    """Resolve anime-specific ongoing targets with global-channel fallback."""
    return await _resolve_ongoing_targets_from_settings(
        route or {}, codeflixbots.codeflixbots
    )

logger = logging.getLogger(__name__)

_PROGRESS_KEYBOARD = InlineKeyboardMarkup(
    [[InlineKeyboardButton("🔄 Refresh Progress", callback_data="refresh_file_progress")]]
)


@Client.on_callback_query(filters.regex(r"^refresh_file_progress$"))
async def refresh_file_progress(client: Client, query: CallbackQuery):
    """Refresh the visible download/upload progress message on demand."""
    try:
        if query.message:
            current_text = query.message.text or query.message.caption or "⏳ Progress is updating..."
            await query.message.edit_text(
                current_text,
                reply_markup=_PROGRESS_KEYBOARD,
                disable_web_page_preview=True,
            )
        await query.answer("Progress refreshed")
    except Exception as exc:
        # Telegram raises MESSAGE_NOT_MODIFIED if the visible text is already current.
        if "MESSAGE_NOT_MODIFIED" in str(exc).upper() or "message is not modified" in str(exc).lower():
            await query.answer("Progress is already up to date")
        else:
            logger.debug("Could not refresh progress message: %s", exc)
            try:
                await query.answer("Progress is updating automatically", show_alert=False)
            except Exception:
                pass

# Prevent accidentally handling the same Telegram file twice in a short interval.
renaming_operations: dict[str, datetime] = {}

VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v", ".ts", ".mpeg",
    ".mpg", ".wmv", ".flv", ".3gp", ".ogv", ".vob", ".mts", ".m2ts",
}
AUDIO_EXTENSIONS = {
    ".mp3", ".m4a", ".aac", ".flac", ".wav", ".ogg", ".opus", ".wma",
    ".alac", ".aiff",
}
METADATA_EXTENSIONS = VIDEO_EXTENSIONS | AUDIO_EXTENSIONS
# Telegram video/audio media messages are most reliable with these formats.
SEND_VIDEO_EXTENSIONS = {".mp4"}
SEND_AUDIO_EXTENSIONS = {".mp3", ".m4a"}


def extract_quality_compat(filename):
    """Compatibility wrapper kept for any local extensions importing this helper."""
    return extract_quality(filename)


async def _detect_video_quality(path: str, media=None) -> str | None:
    """Detect quality from the actual video dimensions, not just its filename."""
    width = height = 0

    # Telegram provides dimensions for normal video messages, but not always for
    # videos uploaded as documents, so probe the downloaded original file too.
    try:
        probe = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0", path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(probe.communicate(), timeout=12)
        if probe.returncode == 0:
            match = re.search(rb"(\d+)x(\d+)", stdout)
            if match:
                width, height = int(match.group(1)), int(match.group(2))
    except (FileNotFoundError, asyncio.TimeoutError, OSError):
        pass

    if not height:
        try:
            width = int(getattr(media, "width", 0) or 0)
            height = int(getattr(media, "height", 0) or 0)
        except (TypeError, ValueError):
            width = height = 0

    if height <= 0:
        return None
    # Standard release labels follow the video's vertical resolution.
    if height >= 2000:
        return "2160p"
    if height >= 1300:
        return "1440p"
    if height >= 1000:
        return "1080p"
    if height >= 650:
        return "720p"
    if height >= 430:
        return "480p"
    if height >= 300:
        return "360p"
    return f"{height}p"



# Each uploaded file waits for its own three-line details before processing. Per-user
# queues ensure a later upload is not renamed/stored/published until the prior one ends.
_MEDIA_QUEUES = defaultdict(list)
_MEDIA_WAITING_DETAILS = set()
_MEDIA_PROCESSING = set()


def _parse_upload_details(text: str) -> dict | None:
    raw = (text or "").strip()
    if "|" in raw and len(raw.splitlines()) == 1:
        parts = [part.strip() for part in raw.split("|", 2)]
    else:
        parts = [part.strip() for part in raw.splitlines() if part.strip()]
    if len(parts) < 3:
        return None
    # The last two non-empty lines are Season and Episode. Everything before them
    # is the title, allowing long anime titles to be wrapped across multiple lines.
    title_parts, raw_season, raw_episode = parts[:-2], parts[-2], parts[-1]
    title = " ".join(part for part in title_parts if part).strip()
    if not title:
        return None
    season_match = re.search(r"(?i)\bseason\s*(\d{1,2})\b|^\s*(\d{1,2})\s*$", raw_season)
    if not season_match:
        return None
    season = season_match.group(1) or season_match.group(2)
    episode_match = re.fullmatch(r"\s*(?:Episode\s*)?(\d{1,6})(?:\s*[-–]\s*(\d{1,6}))?\s*", raw_episode, re.I)
    if not episode_match:
        return None
    first = episode_match.group(1)
    last = episode_match.group(2) if episode_match.group(2) else None
    if last and int(last) < int(first):
        return None
    episode_range = f"{first}-{last}" if last and int(last) != int(first) else first
    return {"title": title, "season": str(int(season)), "episode_range": episode_range}


def _render_filename_for_details(original_filename: str, template: str, details: dict, quality_override: str | None = None) -> str:
    """Render a filename using the user's supplied anime/season/episode details."""
    original = source_basename(original_filename)
    extension = Path(original).suffix
    quality = quality_override or extract_quality(original)
    # Prefer verified source-video resolution when available; otherwise retain filename detection.
    values = {
        "title": str(details["title"]).strip(),
        "season": str(details["season"]).strip(),
        "episode": str(details["episode_range"]).strip(),
        "quality": quality,
    }
    text = str(template or "{title} S{season} Ep{episode} [{quality}]").strip()
    text = re.sub(r"(?i)\bSseason\b", "S{season}", text)
    text = re.sub(r"(?i)\bEepisode\b", "E{episode}", text)
    text = re.sub(r"(?i)(?<!\{)\bquality\b(?!\})", "{quality}", text)
    text = re.sub(
        r"\{\s*(title|season|episode|quality)\s*\}",
        lambda match: values[match.group(1).lower()],
        text,
        flags=re.I,
    )
    text = re.sub(r"\{[^{}]*\}", "", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    text = re.sub(r"\[\s*\]|\(\s*\)", "", text)
    if extension and not text.lower().endswith(extension.lower()):
        text += extension
    return sanitize_filename(text)


# Manual batch workflow: /batch1 through /batch5 starts a separate task.
# The task metadata and source message IDs are stored in MongoDB so the collecting
# state survives a routine worker restart; processing remains sequential per task.
BATCH_TASKS_COLLECTION = "manual_batch_tasks"
_BATCH_TASK_LOCKS = defaultdict(asyncio.Lock)
_BATCH_WORKING_USERS: set[int] = set()
_BATCH_FILE_LIMIT = 3


def _batch_task_id(user_id: int, batch_number: int) -> str:
    return f"{int(user_id)}:batch{int(batch_number)}"


def _is_batch_admin(user) -> bool:
    """Check configured owner/admin IDs without depending on optional UI modules."""
    try:
        user_id = int(getattr(user, "id", user))
    except (TypeError, ValueError):
        return False
    allowed = {int(value) for value in (getattr(Config, "ADMIN", []) or [])}
    for name in ("OWNER_ID", "OWNERID", "ADMIN_ID", "ADMIN"):
        raw = os.environ.get(name, "").strip()
        for item in raw.replace(",", " ").split() if raw else []:
            try:
                allowed.add(int(item))
            except ValueError:
                continue
    return user_id in allowed


def _batch_tasks_collection():
    return codeflixbots.codeflixbots[BATCH_TASKS_COLLECTION]


def _parse_batch_command(message_text: str) -> tuple[int, dict] | None:
    """Parse /batchN details, including /batch1renamed's optional stored URL."""
    raw_lines = (message_text or "").splitlines()
    if not raw_lines:
        return None
    command_match = re.match(r"^\s*/batch(1renamed|[1-5])(?:@\w+)?(?:\s+(.*))?\s*$", raw_lines[0], re.I)
    if not command_match:
        return None
    command_mode = command_match.group(1).lower()
    batch_number = 1 if command_mode == "1renamed" else int(command_mode)
    title_on_command = (command_match.group(2) or "").strip()
    detail_lines = ([title_on_command] if title_on_command else []) + [line.strip() for line in raw_lines[1:] if line.strip()]

    stored_link = None
    if command_mode == "1renamed" and len(detail_lines) >= 4:
        candidate = detail_lines[-1].strip()
        parsed_url = urlparse(candidate)
        if parsed_url.scheme in {"https", "http"} and parsed_url.netloc:
            stored_link = candidate
            detail_lines = detail_lines[:-1]

    details = _parse_upload_details("\n".join(detail_lines))
    if not details:
        raise ValueError(
            "Send the batch command with anime details in this format:\n\n"
            "/batch1\nNaruto\nSeason 01\n04\n\n"
            "For already-stored links, add the URL on the line after the episode number:\n"
            "/batch1renamed\nNaruto\nSeason 01\n590\nhttps://t.me/your_stored_link\n\n"
            "The anime title must match a saved Anime Post channel."
            " Use /batch2, /batch3, /batch4, or /batch5 for separate tasks."
        )
    if command_mode == "1renamed":
        details["skip_rename"] = True
        if stored_link:
            details["link_only"] = True
            details["stored_link"] = stored_link
    return batch_number, details


DEFAULT_BATCH1RENAMED_BUTTON_TEMPLATE = "#g EPISODE {episode} - {link}"


def _build_batch1renamed_button_rows(template: str, details: dict, stored_link: str) -> list[list[dict]]:
    """Build customizable single-link buttons. Each URL must resolve to the supplied stored link."""
    parsed_url = urlparse(str(stored_link or "").strip())
    if parsed_url.scheme not in {"https", "http"} or not parsed_url.netloc:
        raise ValueError("The stored link must be a complete http:// or https:// URL.")

    values = {
        "title": str(details.get("title") or "").strip(),
        "anime": str(details.get("title") or "").strip(),
        "season": str(details.get("season") or "").strip(),
        "episode": str(details.get("episode_range") or "").strip(),
        "episode_range": str(details.get("episode_range") or "").strip(),
    }
    style_map = {"b": "primary", "r": "danger", "g": "success"}
    rows: list[list[dict]] = []
    text = str(template or DEFAULT_BATCH1RENAMED_BUTTON_TEMPLATE).strip()
    for line_number, line in enumerate((line.strip() for line in text.splitlines() if line.strip()), start=1):
        row = []
        for item in line.split(" | "):
            match = re.match(r"^#([bgr])\s+(.+?)\s+-\s*(.*?)\s*$", item.strip(), re.IGNORECASE)
            if not match:
                raise ValueError(f"Button line {line_number} is invalid. Use #g EPISODE {{episode}} - {{link}}.")
            color, raw_label, raw_url = match.groups()
            if raw_url.strip().casefold() not in {"{link}", "{url}", "{stored_link}"}:
                raise ValueError(f"Button line {line_number} must use {{link}} as its URL so the supplied stored link is published.")
            label = raw_label.strip()
            for key, value in values.items():
                label = re.sub(r"\{" + re.escape(key) + r"\}", lambda _m, v=value: v, label, flags=re.IGNORECASE)
            if not label or len(label) > 64:
                raise ValueError(f"Button line {line_number} label must be 1–64 characters after placeholders are filled.")
            row.append({"text": label, "url": str(stored_link).strip(), "style": style_map[color.lower()]})
        if row:
            rows.append(row)
    if not rows:
        raise ValueError("The link-only button template needs at least one button.")
    return rows


async def _publish_batch1renamed_link(client, message, details: dict) -> None:
    """Publish a supplied stored link to Anime Post and Ongoing without media handling."""
    title = str(details.get("title") or "").strip()
    title_key = normalize_anime_title(title)
    link = str(details.get("stored_link") or "").strip()
    routes = codeflixbots.codeflixbots["anime_routes"]
    route = await routes.find_one({"title_key": title_key}) if title_key else None
    if not route and title:
        route = await routes.find_one({"title": {"$regex": "^" + re.escape(title) + "$", "$options": "i"}})
    if not route:
        await message.reply_text(
            f"❌ No saved channel settings were found for {title}. Add it in Admin Panel → Anime Channels first."
        )
        return

    episode = str(details.get("episode_range") or "").strip()
    season = str(details.get("season") or "").strip()
    anime_result = "❌ Anime Post was not published."
    anime_post_published = False
    anime_post_skipped = bool(route.get("anime_post_skipped"))

    # Publish destinations independently. A skipped or broken Anime Post target must
    # never prevent the matching Ongoing post from being attempted.
    if anime_post_skipped:
        anime_result = (
            "⏭️ Skipped during Template Set creation (/skip). "
            "The submitted stored-file link will be used for the Ongoing Download button."
        )
    else:
        try:
            individual_target = route.get("individual_channel")
            if not individual_target:
                raise RuntimeError("No Anime Post channel is assigned to this anime")

            settings = await codeflixbots.codeflixbots["post_template_settings"].find_one({"_id": "global"}) or {}
            template = settings.get("batch1renamed_button_template") or DEFAULT_BATCH1RENAMED_BUTTON_TEMPLATE
            button_rows = _build_batch1renamed_button_rows(template, details, link)
            caption_template = settings.get("anime_post_template") or DEFAULT_INDIVIDUAL_TEMPLATE
            caption_values = {
                "title": title, "anime": title,
                "season": season,
                "episode": episode, "episode_range": episode,
                "qualities": "Stored link", "status": "ON", "audio": "Tamil Audio",
                "quality_480p": "", "quality_720p": "", "quality_1080p": "",
                "link_480p": "", "link_720p": "", "link_1080p": "",
            }
            caption = _safe_template(caption_template, caption_values)

            # Send directly through Bot API. Calling Pyrogram get_chat() with a private
            # numeric channel ID may raise PEER_ID_INVALID after a Render restart.
            api_target = (
                int(individual_target)
                if isinstance(individual_target, str) and individual_target.lstrip("-").isdigit()
                else individual_target
            )
            sent_id = await _send_ongoing_bot_api(client, api_target, None, caption, button_rows)
            anime_post_published = True

            # Resolve a public username only for a nicer history record; this lookup is
            # deliberately non-fatal because the Bot API send has already succeeded.
            chat = None
            if not isinstance(api_target, int):
                try:
                    chat = await client.get_chat(api_target)
                except Exception:
                    logger.debug("Could not resolve Anime Post username after successful send: %s", api_target, exc_info=True)
            recorded_chat_id = int(chat.id) if chat is not None else api_target
            try:
                await codeflixbots.codeflixbots["renamed_link_posts"].insert_one({
                    "user_id": int(message.from_user.id), "title": title, "title_key": title_key,
                    "season": season, "episode": episode,
                    "url": link, "channel_id": recorded_chat_id, "message_id": int(sent_id),
                    "created_at": datetime.utcnow(),
                })
            except Exception:
                logger.exception("Anime Post sent but link-only history could not be saved")

            display_target = getattr(chat, "title", None) if chat is not None else str(individual_target)
            anime_result = (
                f"✅ Published to {display_target}\n"
                f"🔘 Button: {button_rows[0][0]['text']}"
            )
        except Exception as exc:
            logger.exception("Link-only /batch1renamed Anime Post failed")
            anime_result = f"❌ Anime Post failed: {type(exc).__name__}: {exc}"

    # If Anime Post was skipped or Telegram rejected the post (for example, the bot
    # is not an admin), use this episode's stored-file URL for the Ongoing Download
    # button. Only a successfully published Anime Post uses the saved anime invite.
    use_stored_link = link if not anime_post_published else None
    try:
        ongoing_result = await _post_ongoing_anime(
            client,
            str(route.get("title") or title),
            season=season,
            episode_range=episode,
            fallback_download_url=use_stored_link,
        )
    except Exception as exc:
        logger.exception("Link-only /batch1renamed Ongoing publication failed")
        ongoing_result = f"❌ Ongoing publication failed: {type(exc).__name__}: {exc}"

    report_text = (
        f"🎬 {title} • Season {season} • Episode {episode}\n\n"
        f"📺 Anime Post:\n{anime_result}\n\n"
        f"📡 Ongoing:\n{ongoing_result}\n\n"
        "No media was downloaded or stored by this command."
    )
    await message.reply_text(
        report_text,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🗑 DELETE POST", callback_data="delete_batch1renamed_report")]
        ]),
        disable_web_page_preview=True,
        parse_mode=enums.ParseMode.DISABLED,
    )


@Client.on_callback_query(filters.regex(r"^delete_batch1renamed_report$"))
async def delete_batch1renamed_report(client: Client, query: CallbackQuery):
    """Delete only the /batch1renamed result/report message, never channel posts."""
    report_message = query.message
    if report_message is None:
        return await query.answer("This report message is no longer available.", show_alert=True)

    if not _is_batch_admin(query.from_user):
        return await query.answer("Only the bot owner/admin can delete this report.", show_alert=True)

    try:
        await report_message.delete()
        await query.answer("Report deleted.")
    except Exception as exc:
        logger.exception("Could not delete /batch1renamed report message")
        try:
            await query.answer(
                f"Could not delete this report: {type(exc).__name__}",
                show_alert=True,
            )
        except Exception:
            pass


async def _active_batch_task(user_id: int) -> dict | None:
    cursor = _batch_tasks_collection().find({
        "user_id": int(user_id),
        "status": {"$in": ["choosing_channel", "waiting_files", "processing"]},
    }).sort("updated_at", -1).limit(1)
    rows = await cursor.to_list(length=1)
    return rows[0] if rows else None


async def _batch_routes_keyboard(user_id: int, batch_number: int, routes: list[dict]):
    rows = []
    route_choices = []
    for route in routes[:50]:
        route_key = str(route.get("title_key") or "").strip()
        channel = route.get("individual_channel")
        if not route_key or not channel:
            continue
        route_choices.append(route_key)
        label = f"{str(route.get('title') or route_key)[:38]} | {str(channel)[:20]}"[:64]
        rows.append([InlineKeyboardButton(label, callback_data=f"batchch:{batch_number}:{len(route_choices)-1}")])
    return route_choices, InlineKeyboardMarkup(rows) if rows else None


@Client.on_message(filters.private & filters.command(["batch1", "batch2", "batch3", "batch4", "batch5", "batch1renamed"]), group=-7)
async def start_manual_batch(client: Client, message):
    user = getattr(message, "from_user", None)
    if not user:
        return
    if not _is_batch_admin(user):
        return await message.reply_text("⛔ Owner/admin only.")

    try:
        parsed = _parse_batch_command(message.text or "")
    except ValueError as exc:
        return await message.reply_text(f"❌ {exc}", parse_mode=enums.ParseMode.DISABLED)
    if not parsed:
        return
    batch_number, details = parsed

    # An extra URL line selects link-only publication: no batch task, file download,
    # rename, or File Store operation is created for this path.
    if details.get("link_only"):
        await _publish_batch1renamed_link(client, message, details)
        return

    active = await _active_batch_task(user.id)
    if active:
        active_no = active.get("batch_number", "?")
        if active.get("status") == "processing" and int(user.id) not in _BATCH_WORKING_USERS:
            if int(active_no) != batch_number:
                return await message.reply_text(
                    f"♻️ Batch{active_no} has unfinished processing. Send /batch{active_no} with its original details to resume it before starting Batch{batch_number}."
                )
            # A Render restart can leave a task marked as processing. Reuse its stored
            # source message IDs and per-file progress rather than leaving it stuck.
            status_message = await message.reply_text(f"♻️ Resuming unfinished Batch{active_no}…")
            _BATCH_WORKING_USERS.add(int(user.id))
            try:
                await _complete_manual_batch(client, active, status_message)
            finally:
                _BATCH_WORKING_USERS.discard(int(user.id))
            return
        return await message.reply_text(
            f"⚠️ Batch{active_no} is currently {active.get('status', 'active').replace('_', ' ')}.\n"
            "Finish or cancel that task before starting another batch, so uploaded files cannot go to the wrong anime."
        )

    # A completed Template Set is selected automatically by the normalized anime title.
    # Legacy routes without a completed set keep the original manual channel picker.
    anime_key = normalize_anime_title(str(details.get("title") or ""))
    template_set = await codeflixbots.codeflixbots["anime_templates"].find_one({"_id": anime_key}) if anime_key else None
    if template_set and template_set.get("setup_complete"):
        route_key = str(template_set.get("title_key") or anime_key)
        route = await codeflixbots.codeflixbots["anime_routes"].find_one({"title_key": route_key})
        if not route or not route.get("individual_channel"):
            return await message.reply_text(
                f"❌ Template Set {details['title']!r} is missing its Anime Post channel. "
                "Open Admin Panel → Templates and create/finish that Template Set again."
            )
        invite_doc = await codeflixbots.codeflixbots["anime_invite_links"].find_one({"_id": anime_key}) or {}
        if not invite_doc.get("links"):
            return await message.reply_text(
                f"❌ Template Set {details['title']!r} has no ongoing invite link. "
                "Open Admin Panel → Anime Invite Links and save one before starting this batch."
            )
        if not template_set.get("poster_file_id"):
            return await message.reply_text(
                f"❌ Template Set {details['title']!r} has no ongoing poster. "
                "Open Admin Panel → Templates and finish its poster step."
            )
        now = datetime.utcnow()
        task = {
            "_id": _batch_task_id(user.id, batch_number),
            "user_id": int(user.id),
            "batch_number": batch_number,
            "details": details,
            "route_choices": [route_key],
            "selected_route_key": route_key,
            "template_set_key": anime_key,
            "files": [],
            "status": "waiting_files",
            "created_at": now,
            "updated_at": now,
        }
        await _batch_tasks_collection().replace_one({"_id": task["_id"]}, task, upsert=True)
        await message.reply_text(
            f"📦 Batch{batch_number} — Template Set detected\n"
            f"🎬 Anime: {details['title']}\nSeason: {details['season']} • Episode: {details['episode_range']}\n"
            f"📺 Anime channel: {route.get('individual_channel_title') or route.get('individual_channel')}\n"
            "🔗 Ongoing invite: Saved • 🖼 Ongoing poster: Saved\n\n"
            "📥 Send exactly 3 files (480p, 720p, and 1080p).\n"
            "The anime post uses your saved Anime Post text template; the saved poster and invite button are used only for ongoing-channel posts."
        )
        return

    routes_cursor = codeflixbots.codeflixbots["anime_routes"].find(
        {"individual_channel": {"$exists": True, "$nin": [None, ""]}}
    ).sort("title", 1).limit(50)
    routes = await routes_cursor.to_list(length=50)
    # Only show routes with a saved title and destination, because a selection must
    # resolve to an actual configured Anime Post channel.
    routes = [route for route in routes if route.get("title_key") and route.get("individual_channel")]
    route_choices, keyboard = await _batch_routes_keyboard(user.id, batch_number, routes)
    if not route_choices or not keyboard:
        return await message.reply_text(
            "❌ No saved Anime Post channels are available yet. Add them first in Admin Panel → Anime Channels."
        )

    now = datetime.utcnow()
    task = {
        "_id": _batch_task_id(user.id, batch_number),
        "user_id": int(user.id),
        "batch_number": batch_number,
        "details": details,
        "route_choices": route_choices,
        "selected_route_key": None,
        "template_set_key": None,
        "files": [],
        "status": "choosing_channel",
        "created_at": now,
        "updated_at": now,
    }
    await _batch_tasks_collection().replace_one({"_id": task["_id"]}, task, upsert=True)
    await message.reply_text(
        f"📦 **Batch{batch_number}**\n"
        f"Anime: {details['title']}\nSeason: {details['season']}\nEpisode: {details['episode_range']}\n\n"
        "No completed Template Set matched this title, so select the saved anime channel for this post:",
        reply_markup=keyboard,
    )


@Client.on_callback_query(filters.regex(r"^batchch:[1-5]:\d+$"))
async def select_manual_batch_channel(client: Client, query: CallbackQuery):
    if not _is_batch_admin(query.from_user):
        return await query.answer("Owner/admin only.", show_alert=True)
    try:
        _, batch_raw, index_raw = (query.data or "").split(":", 2)
        batch_number, index = int(batch_raw), int(index_raw)
    except (ValueError, TypeError):
        return await query.answer("Invalid batch selection. Start the batch again.", show_alert=True)

    task_id = _batch_task_id(query.from_user.id, batch_number)
    task = await _batch_tasks_collection().find_one({"_id": task_id, "user_id": int(query.from_user.id)})
    if not task or task.get("status") != "choosing_channel":
        return await query.answer("This batch selection has expired. Start the batch again.", show_alert=True)
    choices = task.get("route_choices") or []
    if index < 0 or index >= len(choices):
        return await query.answer("That channel option is no longer available.", show_alert=True)
    route_key = choices[index]
    route = await codeflixbots.codeflixbots["anime_routes"].find_one({"title_key": route_key})
    if not route or not route.get("individual_channel"):
        return await query.answer("That saved channel could not be found. Please restart the batch.", show_alert=True)

    await _batch_tasks_collection().update_one(
        {"_id": task_id},
        {"$set": {"selected_route_key": route_key, "status": "waiting_files", "files": [], "updated_at": datetime.utcnow()}},
    )
    details = task.get("details") or {}
    try:
        await query.message.edit_text(
            f"✅ Channel selected: {route.get('title') or route_key}\n"
            f"📺 Anime: {details.get('title')}\nSeason: {details.get('season')} | Episode: {details.get('episode_range')}\n\n"
            "📥 Send exactly 3 files now.\nFiles received: 0/3\n\n"
            "I will begin renaming only after all 3 files arrive, then process them one by one, generate links, and publish automatically.",
        )
    except Exception:
        await query.message.reply_text(
            f"✅ Channel selected: {route.get('title') or route_key}\n"
            "📥 Send exactly 3 files now.\nFiles received: 0/3"
        )
    await query.answer("Channel selected")


@Client.on_message(filters.private & filters.command("cancel"), group=-7)
async def cancel_manual_batch(client: Client, message):
    user = getattr(message, "from_user", None)
    if not user:
        return
    task = await _active_batch_task(user.id)
    if not task:
        return
    if task.get("status") == "processing":
        return await message.reply_text("⏳ This batch is already processing; it cannot be cancelled midway.")
    await _batch_tasks_collection().update_one(
        {"_id": task["_id"]},
        {"$set": {"status": "cancelled", "updated_at": datetime.utcnow()}},
    )
    await message.reply_text(f"✅ Batch{task.get('batch_number')} cancelled. No more files will be collected for it.")
    raise StopPropagation



async def _recorded_post_exists(client: Client, record: dict | None) -> bool:
    """Return true only when a saved channel post can still be fetched from Telegram."""
    if not isinstance(record, dict):
        return False
    chat_id = record.get("chat_id")
    message_id = record.get("message_id")
    if chat_id is None or message_id is None:
        return False
    try:
        target = int(chat_id) if str(chat_id).lstrip("-").isdigit() else str(chat_id)
        posted = await client.get_messages(target, int(message_id))
        if isinstance(posted, (list, tuple)):
            posted = posted[0] if posted else None
        return bool(posted and not getattr(posted, "empty", False))
    except Exception as exc:
        # A fetch/permission error is not proof that the post exists; never show a
        # success badge solely because MongoDB has an old published_targets entry.
        logger.warning("Could not verify recorded channel post %s: %s", message_id, exc)
        return False


async def _complete_manual_batch(client: Client, task: dict, status_message):
    user_id = int(task["user_id"])
    batch_number = int(task["batch_number"])
    details = dict(task.get("details") or {})
    details["route_title_key"] = task.get("selected_route_key")
    source_files = list(task.get("files") or [])
    completed_ids = {int(value) for value in (task.get("processed_message_ids") or [])}
    processed = int(task.get("processed_count", len(completed_ids)) or 0)
    failures = list(task.get("processing_failures") or [])
    await _batch_tasks_collection().update_one(
        {"_id": task["_id"]},
        {"$set": {"status": "processing", "updated_at": datetime.utcnow()}},
    )
    for index, source in enumerate(source_files, start=1):
        source_message_id = int(source.get("message_id", 0))
        if source_message_id in completed_ids:
            continue
        file_error = None
        file_completed = False
        try:
            file_message = await client.get_messages(int(source["chat_id"]), source_message_id)
            if not file_message or getattr(file_message, "empty", False):
                raise RuntimeError("Telegram source message could not be loaded")
            await _safe_edit(status_message, f"⚙️ Batch{batch_number}: processing file {index}/3…\nRenaming → File Store → link generation → publishing")
            operation_result = await auto_rename_files(client, file_message, _details=details)
            if isinstance(operation_result, str):
                result_text = operation_result.strip()
            else:
                result_text = str(getattr(operation_result, "text", "") or getattr(operation_result, "caption", "") or "").strip()

            # The rename handler used to swallow failures and return None, causing this
            # loop to count failed work as completed. Count only a real storage result.
            if result_text.startswith("✅"):
                file_completed = True
            elif result_text.startswith("⚠️") and any(marker in result_text for marker in (
                "Files are stored and links are generated",
                "Not published:",
                "All 3 files are stored and verified",
                "verified, but its access link could not be saved",
            )):
                # Storage completed, but a later publishing/link step needs attention.
                file_completed = True
                failures.append(f"File {index}: {result_text[:220]}")
            else:
                raise RuntimeError(result_text or "File processing returned no success result")
            processed += 1
        except Exception as exc:
            logger.exception("Batch%s failed while processing source file %s", batch_number, index)
            file_error = f"File {index}: {type(exc).__name__}: {exc}"
            failures.append(file_error)
        # Only successful inputs are marked complete. Unfinished ones remain resumable.
        if file_completed:
            completed_ids.add(source_message_id)
        else:
            completed_ids.discard(source_message_id)
        await _batch_tasks_collection().update_one(
            {"_id": task["_id"]},
            {"$set": {
                "processed_message_ids": sorted(completed_ids),
                "processed_count": processed,
                "processing_failures": failures,
                "updated_at": datetime.utcnow(),
            }},
        )

    title_key = normalize_anime_title(str(details.get("title") or ""))
    # Canonicalize the same way episode_publisher does (e.g. Season 01 / Episode 04
    # are saved as season "1" / episode "4"). The previous query used raw form input
    # and could miss the exact batch or select an unrelated historical record.
    season_value = str(details.get("season") or "").strip()
    episode_value = str(details.get("episode_range") or "").strip()
    try:
        season_value = str(int(season_value)) if season_value else ""
    except ValueError:
        pass
    episode_match = re.fullmatch(r"\s*(\d{1,6})(?:\s*[-–]\s*(\d{1,6}))?\s*", episode_value)
    if episode_match:
        first_episode = str(int(episode_match.group(1)))
        last_episode = str(int(episode_match.group(2))) if episode_match.group(2) else None
        episode_value = f"{first_episode}-{last_episode}" if last_episode and last_episode != first_episode else first_episode

    episode_batches = codeflixbots.codeflixbots["episode_batches"]
    published_batch = None
    if title_key and season_value and episode_value:
        exact_id = _batch_id(title_key, season_value, episode_value)
        published_batch = await episode_batches.find_one({"_id": exact_id, "state": {"$ne": "merged"}})
    if not published_batch:
        query = {
            "season": season_value,
            "episode": episode_value,
            "state": {"$ne": "merged"},
        }
        if task.get("selected_route_key"):
            # Manual batches are explicitly bound to the selected route; use that
            # stable key first rather than the user-entered title spelling.
            query["route_title_key"] = task.get("selected_route_key")
        else:
            query["title_key"] = title_key
        cursor = episode_batches.find(query).sort("updated_at", -1).limit(1)
        rows = await cursor.to_list(length=1)
        published_batch = rows[0] if rows else None
    published_targets = (published_batch or {}).get("published_targets") or {}
    route_key = task.get("selected_route_key")
    selected_route = await codeflixbots.codeflixbots["anime_routes"].find_one({"title_key": route_key}) if route_key else None
    individual_target = (selected_route or {}).get("individual_channel")
    ongoing_targets = await _resolve_ongoing_targets(selected_route or {}) if selected_route else []
    individual_record = published_targets.get(f"individual:{individual_target}") if individual_target else None
    individual_done = await _recorded_post_exists(client, individual_record)
    ongoing_done = 0
    for target in ongoing_targets:
        if target and await _recorded_post_exists(client, published_targets.get(f"ongoing:{target}")):
            ongoing_done += 1
    links_ready = bool(published_batch and all((published_batch.get("files") or {}).get("q" + quality, {}).get("url") for quality in ("480p", "720p", "1080p")))

    summary_lines = [
        f"📦 Batch{batch_number} processing finished.",
        f"📺 Anime: {details.get('title')}",
        f"Season {details.get('season')} • Episode {details.get('episode_range')}",
        f"📁 Files processed: {processed}/3",
        f"🔗 Access links: {'Ready' if links_ready else 'Not ready — check that filenames contain 480p, 720p, and 1080p.'}",
        f"📣 Anime channel: {'Confirmed posted' if individual_done else 'Not confirmed'}",
        f"📡 Ongoing: {ongoing_done}/{len([target for target in ongoing_targets if target])} channels published",
    ]
    batch_error = str((published_batch or {}).get("last_error") or "").strip()
    if batch_error:
        failures.append("Publisher: " + batch_error[:500])
    if failures:
        summary_lines.append("\n⚠️ Issues:\n" + "\n".join(failures[:4]))

    retry_required = any(int(source.get("message_id", 0)) not in completed_ids for source in source_files)
    if retry_required:
        final_status = "processing"
        summary_lines.append(f"\n♻️ One or more files did not finish. Send /batch{batch_number} again to retry the unfinished file(s).")
    elif processed == 3 and published_batch and published_batch.get("state") == "published" and individual_done and ongoing_done == len([target for target in ongoing_targets if target]):
        final_status = "completed"
        summary_lines.append("\n✅ Telegram confirms the post messages exist in the configured channels.")
    elif processed == 3 and individual_done:
        final_status = "completed_with_warnings"
        summary_lines.append("\n⚠️ Anime post is confirmed, but check the ongoing status or publisher issue above.")
    else:
        final_status = "completed_with_errors"
        summary_lines.append("\n❌ Posting was not fully confirmed. Check the publisher issue above and Render logs.")

    await _batch_tasks_collection().update_one(
        {"_id": task["_id"]},
        {"$set": {"status": final_status, "processed_count": processed, "finished_at": datetime.utcnow(), "updated_at": datetime.utcnow()}},
    )
    await _safe_edit(status_message, "\n".join(summary_lines))


async def _prompt_for_next_file(client, user_id: int):
    queue = _MEDIA_QUEUES.get(user_id) or []
    if queue and user_id not in _MEDIA_WAITING_DETAILS and user_id not in _MEDIA_PROCESSING:
        _MEDIA_WAITING_DETAILS.add(user_id)
        await queue[0].reply_text(
            "📝 Send the details for this file in 3 lines:\n"
            "Anime Name\nSeason 17\n335-336\n\n"
            "The bot will start renaming only after you send these details."
        )


@Client.on_message(filters.private & filters.text, group=-8)
async def collect_upload_details(client: Client, message):
    user = getattr(message, "from_user", None)
    if not user or user.id not in _MEDIA_WAITING_DETAILS:
        return
    text = (message.text or "").strip()
    if re.match(r"^/batch[1-5](?:@\w+)?(?:\s|$)", text, re.I):
        return
    if re.match(r"^/cancel(?:@\w+)?(?:\s|$)", text, re.I):
        # Let the manual-batch cancel handler own /cancel when a batch is active.
        if await _active_batch_task(user.id):
            return
        _MEDIA_WAITING_DETAILS.discard(user.id)
        _MEDIA_QUEUES.pop(user.id, None)
        await message.reply_text("✅ Waiting file queue cancelled. Your original files were not renamed.")
        raise StopPropagation
    details = _parse_upload_details(text)
    if not details:
        await message.reply_text(
            "❌ I couldn't read those details. Send exactly 3 lines, for example:\n"
            "Naruto Shippuden\nSeason 17\n335-336"
        )
        raise StopPropagation
    queue = _MEDIA_QUEUES.get(user.id) or []
    if not queue:
        _MEDIA_WAITING_DETAILS.discard(user.id)
        return
    current = queue[0]
    _MEDIA_WAITING_DETAILS.discard(user.id)
    _MEDIA_PROCESSING.add(user.id)
    try:
        await auto_rename_files(client, current, _details=details)
    finally:
        queue = _MEDIA_QUEUES.get(user.id) or []
        if queue:
            queue.pop(0)
        _MEDIA_PROCESSING.discard(user.id)
        if queue:
            await _prompt_for_next_file(client, user.id)
        else:
            _MEDIA_QUEUES.pop(user.id, None)
            _MEDIA_WAITING_DETAILS.discard(user.id)
    raise StopPropagation

async def process_thumbnail(thumb_path: str | None) -> str | None:
    """Create a Telegram-friendly JPEG thumbnail (maximum 320x320, under 200 KB)."""
    if not thumb_path or not os.path.isfile(thumb_path):
        return None

    try:
        with Image.open(thumb_path) as source:
            image = source.convert("RGB")
            image.thumbnail((320, 320))
            # Reduce JPEG quality as needed to stay under Telegram's thumbnail size limit.
            for quality in (88, 80, 72, 64, 56, 48):
                image.save(thumb_path, format="JPEG", quality=quality, optimize=True)
                if os.path.getsize(thumb_path) <= 190 * 1024:
                    return thumb_path
        logger.warning("Thumbnail was too large after compression; skipping it")
    except Exception:
        logger.exception("Thumbnail processing failed")
    return None


async def add_metadata(input_path: str, output_path: str, user_id: int) -> None:
    """Copy streams and set common metadata using FFmpeg, without re-encoding."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpeg is not installed or not on PATH")

    metadata = {
        "title": await codeflixbots.get_title(user_id),
        "artist": await codeflixbots.get_artist(user_id),
        "author": await codeflixbots.get_author(user_id),
        "video_title": await codeflixbots.get_video(user_id),
        "audio_title": await codeflixbots.get_audio(user_id),
        "subtitle": await codeflixbots.get_subtitle(user_id),
    }

    command = [
        ffmpeg,
        "-hide_banner",
        "-y",
        "-i", input_path,
        "-map", "0",
        "-c", "copy",
        "-metadata", f"title={metadata['title']}",
        "-metadata", f"artist={metadata['artist']}",
        "-metadata", f"author={metadata['author']}",
        "-metadata:s:v", f"title={metadata['video_title']}",
        "-metadata:s:a", f"title={metadata['audio_title']}",
        "-metadata:s:s", f"title={metadata['subtitle']}",
        "-loglevel", "error",
        output_path,
    ]

    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    if process.returncode != 0:
        try:
            error_text = stderr.decode("utf-8", errors="replace").strip()
        except AttributeError:
            error_text = str(stderr)
        raise RuntimeError(f"FFmpeg metadata operation failed: {error_text or 'unknown error'}")


async def _safe_edit(message, text: str) -> None:
    try:
        await message.edit_text(text, parse_mode=enums.ParseMode.DISABLED)
    except Exception:
        logger.debug("Could not update progress message", exc_info=True)


async def _get_thumbnail_path(client, message, user_id: int, work_dir: str) -> str | None:
    """Download the user's saved thumbnail, falling back to a video thumbnail."""
    path = os.path.join(work_dir, "thumbnail.jpg")
    try:
        saved_thumb = await codeflixbots.get_thumbnail(user_id)
        if saved_thumb:
            downloaded = await client.download_media(saved_thumb, file_name=path)
            return await process_thumbnail(downloaded)

        if message.video and getattr(message.video, "thumbs", None):
            thumbs = message.video.thumbs
            if thumbs:
                downloaded = await client.download_media(thumbs[0].file_id, file_name=path)
                return await process_thumbnail(downloaded)
    except Exception:
        logger.exception("Could not retrieve thumbnail; uploading without one")
    return None


@Client.on_message(filters.private & (filters.document | filters.video | filters.audio))
async def auto_rename_files(client, message, _details: dict | None = None):
    """Queue an upload, then rename only after the user provides anime details."""
    user = getattr(message, "from_user", None)
    if user is None:
        return
    user_id = user.id
    if _details is None:
        batch_to_process = None
        batch_status_message = None
        async with _BATCH_TASK_LOCKS[user_id]:
            batch_task = await _active_batch_task(user_id)
            if batch_task and batch_task.get("status") == "choosing_channel":
                await message.reply_text("⚠️ Select an anime channel from the buttons first. Then send the 3 files.")
                raise StopPropagation
            if batch_task and batch_task.get("status") == "processing":
                await message.reply_text(f"⏳ Batch{batch_task.get('batch_number')} is processing its queued files. Please wait for it to finish.")
                raise StopPropagation
            if batch_task and batch_task.get("status") == "waiting_files":
                incoming = batch_task.get("files") or []
                message_id = int(getattr(message, "id", 0) or 0)
                if any(int(item.get("message_id", -1)) == message_id for item in incoming):
                    raise StopPropagation
                if len(incoming) >= _BATCH_FILE_LIMIT:
                    await message.reply_text("✅ This batch already has 3/3 files. Please wait while they are processed.")
                    raise StopPropagation
                source_info = {
                    "chat_id": int(message.chat.id),
                    "message_id": message_id,
                    "file_name": getattr(getattr(message, "document", None), "file_name", None)
                        or getattr(getattr(message, "video", None), "file_name", None)
                        or getattr(getattr(message, "audio", None), "file_name", None)
                        or f"file_{message_id}",
                    "file_id": getattr(getattr(message, "document", None), "file_id", None)
                        or getattr(getattr(message, "video", None), "file_id", None)
                        or getattr(getattr(message, "audio", None), "file_id", None),
                }
                incoming.append(source_info)
                incoming.sort(key=lambda item: int(item.get("message_id", 0)))
                new_count = len(incoming)
                new_status = "processing" if new_count == _BATCH_FILE_LIMIT else "waiting_files"
                await _batch_tasks_collection().update_one(
                    {"_id": batch_task["_id"], "status": "waiting_files"},
                    {"$set": {"files": incoming, "status": new_status, "updated_at": datetime.utcnow()}},
                )
                if new_count < _BATCH_FILE_LIMIT:
                    await message.reply_text(
                        f"📥 Batch{batch_task.get('batch_number')} files received: {new_count}/3\n"
                        f"Send {3-new_count} more file(s). Processing starts only after all 3 arrive."
                    )
                    raise StopPropagation
                batch_status_message = await message.reply_text(
                    f"✅ Batch{batch_task.get('batch_number')}: files received 3/3.\n"
                    "⏳ Starting sequential processing now…"
                )
                batch_to_process = {**batch_task, "files": incoming, "status": "processing"}

        if batch_to_process is not None:
            _BATCH_WORKING_USERS.add(int(user_id))
            try:
                await _complete_manual_batch(client, batch_to_process, batch_status_message)
            finally:
                _BATCH_WORKING_USERS.discard(int(user_id))
            raise StopPropagation

        queue = _MEDIA_QUEUES[user_id]
        if not any(getattr(item, "id", None) == getattr(message, "id", None) for item in queue):
            queue.append(message)
        if user_id not in _MEDIA_WAITING_DETAILS and user_id not in _MEDIA_PROCESSING:
            await _prompt_for_next_file(client, user_id)
        return

    try:
        format_template = await codeflixbots.get_format_template(user_id)
    except Exception:
        logger.exception("Could not read rename format for user %s", user_id)
        return await message.reply_text("❌ Couldn't read your settings. Please try /start again.")

    skip_rename = bool((_details or {}).get("skip_rename"))
    if not format_template and not skip_rename:
        return await message.reply_text("Please set a rename format using /autorename first.")

    if message.document:
        media = message.document
        media_type = "document"
    elif message.video:
        media = message.video
        media_type = "video"
    elif message.audio:
        media = message.audio
        media_type = "audio"
    else:
        return await message.reply_text("❌ Unsupported file type.")

    file_id = getattr(media, "file_id", None)
    if not file_id:
        return await message.reply_text("❌ Telegram did not provide a file ID for this message.")

    default_extension = ".mp4" if media_type == "video" else ".mp3" if media_type == "audio" else ".bin"
    original_name = source_basename(
        getattr(media, "file_name", None),
        fallback=f"file_{getattr(message, 'id', 'upload')}{default_extension}",
    )
    # Some Telegram documents have no filename extension. Add a safe fallback
    # so downstream upload/metadata tools can infer the file type where possible.
    if not Path(original_name).suffix:
        original_name = original_name.rstrip(" .") + default_extension

    # The checker sends its own reply when a blocked name is detected.
    try:
        if await check_anti_nsfw(original_name, message):
            return
    except Exception:
        logger.exception("Filename safety check failed; refusing this file")
        return await message.reply_text("❌ Filename validation failed. Please try another filename.")

    now = datetime.now()
    previous = renaming_operations.get(file_id)
    if previous and (now - previous).total_seconds() < 10:
        return
    renaming_operations[file_id] = now

    status = None
    sent_message = None
    storage_result = None
    started_at = time.perf_counter()
    timings = {}
    try:
        new_filename = original_name if skip_rename else _render_filename_for_details(original_name, format_template, _details)
        extension = Path(original_name).suffix.lower()
        if not re.fullmatch(r"\.[a-z0-9]{1,10}", extension):
            extension = ""
        file_size = int(getattr(media, "file_size", 0) or 0)
        duration = getattr(media, "duration", None)
        status = await message.reply_text("⏬ Downloading file...", reply_markup=_PROGRESS_KEYBOARD)

        # Unique temp directory prevents collisions across concurrent uploads and keeps
        # user-provided filenames from escaping the temporary work area.
        with tempfile.TemporaryDirectory(prefix="autorename_") as work_dir:
            source_path = os.path.join(work_dir, f"source{extension}")
            stage_started = time.perf_counter()
            downloaded_path = await client.download_media(
                message,
                file_name=source_path,
                progress=progress_for_pyrogram,
                progress_args=("Downloading...", status, time.time()),
            )
            timings["download"] = time.perf_counter() - stage_started
            if not downloaded_path or not os.path.isfile(downloaded_path):
                raise RuntimeError("Telegram download did not produce a file")

            # Inspect the original uploaded file before choosing the final filename.
            # This fixes files named without a quality marker being renamed [Unknown].
            detected_quality = None
            # If the source filename already contains a recognized quality, preserve it and
            # skip ffprobe. This avoids a subprocess delay for most uploads without changing
            # their names. Probe only when quality is absent/unknown.
            source_quality = extract_quality(original_name)
            if (extension in VIDEO_EXTENSIONS or media_type == "video") and source_quality.strip().casefold() == "unknown":
                detected_quality = await _detect_video_quality(downloaded_path, media)
            if not skip_rename:
                new_filename = _render_filename_for_details(
                    original_name, format_template, _details, quality_override=detected_quality
                )

            current_path = downloaded_path
            stage_started = time.perf_counter()
            meta_state = await codeflixbots.get_metadata(user_id)
            if not skip_rename and metadata_enabled(meta_state) and extension in METADATA_EXTENSIONS:
                await _safe_edit(status, "🧩 Applying metadata (this may take extra time)...")
                metadata_path = os.path.join(work_dir, f"metadata{extension}")
                try:
                    await add_metadata(current_path, metadata_path, user_id)
                    if os.path.isfile(metadata_path) and os.path.getsize(metadata_path) > 0:
                        current_path = metadata_path
                except Exception:
                    # Metadata is optional: a metadata problem must not prevent a rename.
                    logger.exception("Metadata failed for %s; continuing with original file", original_name)
                    await _safe_edit(status, "⚠️ Metadata skipped; continuing with rename...")
            elif not skip_rename and metadata_enabled(meta_state):
                logger.info("Metadata skipped for unsupported file extension %s", extension)
            timings["metadata"] = time.perf_counter() - stage_started

            output_path = os.path.join(work_dir, new_filename)
            if os.path.abspath(current_path) != os.path.abspath(output_path):
                # Both paths are in the same temporary directory, so an atomic rename
                # avoids a second full-size copy (important for large episodes).
                os.replace(current_path, output_path)

            caption_template = await codeflixbots.get_caption(user_id)
            caption = format_caption(caption_template, new_filename, file_size, duration)
            if len(caption) > 1024:
                caption = caption[:1021] + "..."
            thumbnail_path = await _get_thumbnail_path(client, message, user_id, work_dir)

            # Retain the incoming media type unless the user's /setmedia preference is
            # compatible with the actual file extension.
            preferred_type = (await codeflixbots.get_media_preference(user_id) or "").lower()
            upload_type = media_type
            if preferred_type == "document":
                upload_type = "document"
            elif preferred_type == "video" and extension in SEND_VIDEO_EXTENSIONS:
                upload_type = "video"
            elif preferred_type == "audio" and extension in SEND_AUDIO_EXTENSIONS:
                upload_type = "audio"

            # If an incoming message was mislabeled or has an unsupported media
            # extension, send it as a document rather than failing the upload.
            if upload_type == "video" and extension not in SEND_VIDEO_EXTENSIONS:
                upload_type = "document"
            elif upload_type == "audio" and extension not in SEND_AUDIO_EXTENSIONS:
                upload_type = "document"

            await _safe_edit(status, "⏫ Uploading file without renaming..." if skip_rename else "⏫ Uploading renamed file...")
            try:
                await status.edit_reply_markup(_PROGRESS_KEYBOARD)
            except Exception:
                pass
            upload_params = {
                "chat_id": message.chat.id,
                "caption": caption,
                "parse_mode": None,
                "progress": progress_for_pyrogram,
                "progress_args": ("Uploading...", status, time.time()),
            }
            if thumbnail_path:
                upload_params["thumb"] = thumbnail_path

            stage_started = time.perf_counter()
            if upload_type == "video":
                sent_message = await client.send_video(
                    video=output_path,
                    supports_streaming=extension == ".mp4",
                    **upload_params,
                )
            elif upload_type == "audio":
                sent_message = await client.send_audio(audio=output_path, **upload_params)
            else:
                sent_message = await client.send_document(document=output_path, **upload_params)
            timings["upload"] = time.perf_counter() - stage_started

        # Save a separate copy in the configured Telegram storage channel, then group
        # the stored file by anime/season/episode/quality. Publication is gated on all
        # three required qualities being present and independently verified.
        if sent_message is not None:
            try:
                storage_result = await store_file_and_process(
                    client=client,
                    sent_message=sent_message,
                    new_filename=new_filename,
                    user_id=user_id,
                    file_size=file_size,
                    details=_details,
                )
            except Exception as storage_exc:
                logger.exception("Renamed file could not be stored/processed for user %s", user_id)
                storage_result = (
                    "⚠️ File renamed and returned, but File Store failed. Check STORAGE_CHANNEL_ID, "
                    "bot channel permissions, and MongoDB access.\n\n"
                    f"{type(storage_exc).__name__}: {storage_exc}"
                )

        timings["total"] = time.perf_counter() - started_at
        logger.info(
            "Rename timings for user %s | size=%s bytes | download=%.2fs | metadata=%.2fs | thumbnail=%.2fs | upload=%.2fs | total=%.2fs | metadata_enabled=%s | filename=%s",
            user_id, file_size, timings.get("download", 0.0), timings.get("metadata", 0.0),
            timings.get("thumbnail", 0.0), timings.get("upload", 0.0), timings["total"],
            metadata_enabled(meta_state), new_filename,
        )
        final_result = storage_result or "✅ Done! File renamed successfully."
        await _safe_edit(status, final_result)
        logger.info("%s %s -> %s for user %s", "Passed through" if skip_rename else "Renamed", original_name, new_filename, user_id)
        return final_result

    except FloodWait as exc:
        wait_seconds = int(getattr(exc, "value", 1))
        logger.warning("Flood wait while processing for user %s: %s seconds", user_id, wait_seconds)
        error_text = f"⏳ Telegram rate limit. Please try again in {wait_seconds} seconds."
        if status:
            await _safe_edit(status, error_text)
        else:
            await message.reply_text(error_text)
        return error_text
    except Exception as exc:
        logger.exception("File processing failed for user %s (%s)", user_id, original_name)
        error_text = f"❌ File processing failed: {type(exc).__name__}: {exc}"
        if status:
            await _safe_edit(status, error_text[:3900])
        else:
            await message.reply_text(error_text[:3900], parse_mode=enums.ParseMode.DISABLED)
        return error_text
    finally:
        renaming_operations.pop(file_id, None)
