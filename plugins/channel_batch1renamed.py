
"""Handle link-only /batch1renamed commands posted directly in Telegram channels.

This is intentionally separate from the private batch/file workflow. It never
starts a file task, downloads media, renames files, or stores media.
"""

import logging
import re
from datetime import datetime
from urllib.parse import urlparse

from pyrogram import Client, filters, enums

from helper.database import codeflixbots
from helper.file_names import normalize_anime_title
from plugins.file_rename import (
    DEFAULT_BATCH1RENAMED_BUTTON_TEMPLATE,
    _build_batch1renamed_button_rows,
    _parse_batch_command,
)
from plugins.episode_publisher import (
    DEFAULT_INDIVIDUAL_TEMPLATE,
    _safe_template,
    _send_ongoing_bot_api,
)
from plugins.ongoing_posts import post_ongoing_anime

logger = logging.getLogger(__name__)

_COMMAND_RE = re.compile(r"(?is)^\s*/batch1renamed(?:@\w+)?(?:\s|$)")


def _valid_http_url(value: str) -> bool:
    try:
        parsed = urlparse(str(value or "").strip())
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    except Exception:
        return False


def _admin_status(member) -> bool:
    """Handle Pyrogram enum instances and string-like status values."""
    status = getattr(member, "status", None)
    status = getattr(status, "value", status)
    return str(status).casefold().rsplit(".", 1)[-1] in {
        "administrator", "owner"
    }


async def _bot_is_channel_admin(client: Client, chat_id: int) -> bool:
    me = await client.get_me()
    member = await client.get_chat_member(chat_id, me.id)

    if not _admin_status(member):
        return False

    can_post = getattr(member, "can_post_messages", None)
    return can_post is not False


async def _sender_is_channel_admin(client: Client, message) -> bool:
    """Verify a visible sender; anonymous channel posts are channel-authenticated."""
    sender = getattr(message, "from_user", None)

    if sender is None:
        sender_chat = getattr(message, "sender_chat", None)
        return (
            sender_chat is None
            or int(sender_chat.id) == int(message.chat.id)
        )

    member = await client.get_chat_member(message.chat.id, sender.id)
    return _admin_status(member)


async def _find_saved_anime_route(title: str) -> dict | None:
    """Find an exact saved anime title regardless of uppercase/lowercase."""
    title = str(title or "").strip()
    key = normalize_anime_title(title)

    if not key:
        return None

    db = codeflixbots.codeflixbots
    routes = db["anime_routes"]

    # First try the normalized database key.
    route = await routes.find_one({"title_key": key})

    if not route:
        # Case-insensitive exact title lookup for older saved records.
        route = await routes.find_one({
            "title": {
                "$regex": "^" + re.escape(title) + "$",
                "$options": "i",
            }
        })

    if not route:
        # Last-resort normalized scan for legacy title_key values.
        cursor = routes.find({})
        candidates = await cursor.to_list(length=5000)

        for candidate in candidates:
            saved_title_key = normalize_anime_title(
                str(candidate.get("title_key") or "")
            )
            saved_title = normalize_anime_title(
                str(candidate.get("title") or "")
            )

            if key == saved_title_key or key == saved_title:
                route = candidate
                break

    if not route:
        return None

    # Only allow completed Template Sets, not incomplete channel-route records.
    templates = db["anime_templates"]

    template = await templates.find_one({"_id": key})

    if not template:
        template = await templates.find_one({"title_key": key})

    if not template:
        template_cursor = templates.find({})

        for candidate in await template_cursor.to_list(length=5000):
            candidate_key = normalize_anime_title(
                str(
                    candidate.get("title_key")
                    or candidate.get("title")
                    or candidate.get("_id")
                    or ""
                )
            )

            if candidate_key == key:
                template = candidate
                break

    is_complete = bool(
        template
        and (
            template.get("setup_complete") is True
            or template.get("template_set") is True
        )
    )

    return route if is_complete else None


async def _send_status(client: Client, message, text: str) -> None:
    """Send the result into the source channel, with a reply fallback."""
    try:
        await message.reply_text(
            text,
            disable_web_page_preview=True,
            parse_mode=enums.ParseMode.DISABLED,
        )
    except Exception:
        try:
            await client.send_message(
                chat_id=message.chat.id,
                text=text,
                disable_web_page_preview=True,
                parse_mode=enums.ParseMode.DISABLED,
            )
        except Exception:
            logger.exception(
                "Could not send /batch1renamed result to channel %s",
                message.chat.id,
            )


async def _publish_channel_link(
    client: Client,
    message,
    details: dict,
    route: dict,
) -> str:
    """Publish the supplied URL to Anime Post and Ongoing independently."""

    title = str(
        route.get("title") or details.get("title") or ""
    ).strip()

    title_key = normalize_anime_title(title)
    season = str(details.get("season") or "").strip()
    episode = str(details.get("episode_range") or "").strip()
    link = str(details.get("stored_link") or "").strip()

    if not _valid_http_url(link):
        raise ValueError(
            "The last line must be a complete http:// or https:// download URL."
        )

    db = codeflixbots.codeflixbots

    anime_result = "❌ Anime Post was not published."
    anime_post_published = False

    if route.get("anime_post_skipped"):
        anime_result = (
            "⏭️ Anime Post skipped in Template Set. "
            "The supplied download URL will be used in the Ongoing button."
        )

    else:
        try:
            individual_target = route.get("individual_channel")

            if not individual_target:
                raise RuntimeError(
                    "No individual Anime Post channel is configured for this anime."
                )

            settings = await db[
                "post_template_settings"
            ].find_one({"_id": "global"}) or {}

            button_template = (
                settings.get("batch1renamed_button_template")
                or DEFAULT_BATCH1RENAMED_BUTTON_TEMPLATE
            )

            button_rows = _build_batch1renamed_button_rows(
                button_template,
                details,
                link,
            )

            caption_template = (
                settings.get("anime_post_template")
                or DEFAULT_INDIVIDUAL_TEMPLATE
            )

            caption_values = {
                "title": title,
                "anime": title,
                "season": season,
                "episode": episode,
                "episode_range": episode,
                "qualities": "Stored link",
                "status": "ON",
                "audio": "Tamil Audio",
                "quality_480p": "",
                "quality_720p": "",
                "quality_1080p": "",
                "link_480p": "",
                "link_720p": "",
                "link_1080p": "",
            }

            caption = _safe_template(
                caption_template,
                caption_values,
            )

            api_target = (
                int(individual_target)
                if (
                    isinstance(individual_target, str)
                    and individual_target.lstrip("-").isdigit()
                )
                else individual_target
            )

            sent_message_id = await _send_ongoing_bot_api(
                client,
                api_target,
                None,
                caption,
                button_rows,
            )

            anime_post_published = True

            # Do not invent a user ID for anonymous channel posts.
            history = {
                "title": title,
                "title_key": title_key,
                "season": season,
                "episode": episode,
                "url": link,
                "channel_id": (
                    int(api_target)
                    if str(api_target).lstrip("-").isdigit()
                    else str(api_target)
                ),
                "message_id": int(sent_message_id),
                "source_chat_id": int(message.chat.id),
                "source_message_id": int(message.id),
                "created_at": datetime.utcnow(),
            }

            sender = getattr(message, "from_user", None)

            if sender is not None:
                history["user_id"] = int(sender.id)

            try:
                await db["renamed_link_posts"].insert_one(history)
            except Exception:
                logger.exception(
                    "Anime Post was sent but link history could not be saved"
                )

            anime_result = (
                f"✅ Published to Anime Post channel {individual_target}\n"
                f"🔘 Button: {button_rows[0][0]['text']}"
            )

        except Exception as exc:
            logger.exception(
                "Channel /batch1renamed Anime Post failed for %s",
                title,
            )

            anime_result = (
                f"❌ Anime Post failed: {type(exc).__name__}: {exc}"
            )

    # If Anime Post failed or was skipped, use this episode's supplied URL.
    # Otherwise Ongoing uses the anime's saved invite link.
    fallback_url = None if anime_post_published else link

    try:
        ongoing_result = await post_ongoing_anime(
            client,
            title,
            season=season,
            episode_range=episode,
            fallback_download_url=fallback_url,
        )

    except Exception as exc:
        logger.exception(
            "Channel /batch1renamed Ongoing publication failed for %s",
            title,
        )

        ongoing_result = (
            f"❌ Ongoing publication failed: {type(exc).__name__}: {exc}"
        )

    return (
        f"🎬 {title} • Season {season} • Episode {episode}\n\n"
        f"📺 Anime Post:\n{anime_result}\n\n"
        f"📡 Ongoing:\n{ongoing_result}\n\n"
        "No media was downloaded, renamed, uploaded, or stored by this command."
    )


@Client.on_message(
    filters.channel & filters.regex(_COMMAND_RE),
    group=-8,
)
async def batch1renamed_from_channel(client: Client, message):
    """Process /batch1renamed sent as a post in a Telegram channel."""

    text = message.text or message.caption or ""

    if not _COMMAND_RE.match(text):
        return

    try:
        # The bot must itself be an administrator in the source channel.
        if not await _bot_is_channel_admin(
            client,
            int(message.chat.id),
        ):
            return await _send_status(
                client,
                message,
                "⛔ Please make this bot an administrator in this channel before using /batch1renamed here.",
            )

        if not await _sender_is_channel_admin(client, message):
            return await _send_status(
                client,
                message,
                "⛔ Only this channel's administrators can run this command.",
            )

        parsed = _parse_batch_command(text)

        if not parsed:
            return

        _batch_number, details = parsed

        # Channel mode is only for manually supplied URLs.
        if not details.get("link_only"):
            return await _send_status(
                client,
                message,
                "❌ For channel publishing, include the existing download URL on the last line:\n\n"
                "/batch1renamed\n"
                "Anime Title\n"
                "Season 03\n"
                "04\n"
                "https://your-download-link",
            )

        title = str(details.get("title") or "").strip()

        route = await _find_saved_anime_route(title)

        if not route:
            return await _send_status(
                client,
                message,
                f"❌ No completed saved Anime Template Set matched '{title}'. "
                "Check the spelling and make sure it exists in Admin Panel → Templates.",
            )

        # Use the canonical saved title for all subsequent lookups.
        # This makes uppercase/lowercase user input resolve identically.
        details["title"] = str(route.get("title") or title)

        # Atomic duplicate protection for this exact channel post.
        job_id = f"{int(message.chat.id)}:{int(message.id)}"

        jobs = codeflixbots.codeflixbots[
            "channel_batch1renamed_jobs"
        ]

        reservation = await jobs.update_one(
            {"_id": job_id},
            {
                "$setOnInsert": {
                    "status": "processing",
                    "source_chat_id": int(message.chat.id),
                    "source_message_id": int(message.id),
                    "title_key": normalize_anime_title(details["title"]),
                    "created_at": datetime.utcnow(),
                }
            },
            upsert=True,
        )

        if reservation.upserted_id is None:
            return await _send_status(
                client,
                message,
                "ℹ️ This /batch1renamed channel post has already been processed. "
                "Send a new command post if you need to publish again.",
            )

        result_text = await _publish_channel_link(
            client,
            message,
            details,
            route,
        )

        await jobs.update_one(
            {"_id": job_id},
            {
                "$set": {
                    "status": "completed",
                    "result": result_text,
                    "completed_at": datetime.utcnow(),
                }
            },
        )

        await _send_status(client, message, result_text)

    except ValueError as exc:
        await _send_status(client, message, f"❌ {exc}")

    except Exception as exc:
        logger.exception(
            "Unexpected channel /batch1renamed error in %s",
            message.chat.id,
        )

        await _send_status(
            client,
            message,
            f"❌ /batch1renamed failed: {type(exc).__name__}: {exc}",
        )

        try:
            jobs = codeflixbots.codeflixbots[
                "channel_batch1renamed_jobs"
            ]

            job_id = f"{int(message.chat.id)}:{int(message.id)}"

            await jobs.update_one(
                {"_id": job_id},
                {
                    "$set": {
                        "status": "failed",
                        "error": str(exc),
                        "failed_at": datetime.utcnow(),
                    }
                },
            )

        except Exception:
            logger.exception(
                "Could not record failed channel /batch1renamed job"
            )
