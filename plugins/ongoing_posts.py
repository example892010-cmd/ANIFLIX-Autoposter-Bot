"""Publish ongoing posts with saved anime-specific links and editable colored buttons."""

import logging
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import aiohttp
from pyrogram import Client, enums, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import Config
from helper.auth import is_admin_user
from helper.database import codeflixbots
from helper.file_names import normalize_anime_title
from plugins.channel_routing import resolve_ongoing_targets

logger = logging.getLogger(__name__)
DEFAULT_ONGOING_TEMPLATE = (
    "✦ {title}╭━━━━━━━━━━━━━━━━━━━━━━\n"
    "‣ Season : {season}\n"
    "‣ Episode : {episode_range}\n"
    "‣ Status : {status}\n"
    "‣ Audio : [Tamil Audio]\n"
    "‣ Quality : {qualities}╰━━━━━━━━━━━━━━━━━━━━━━\n"
    "✦ Powered By : @ANIFLIXANIMETAMIL"
)
DEFAULT_ONGOING_BUTTONS_TEMPLATE = (
    "#b ✦ 𝗗𝗢𝗪𝗡𝗟𝗢𝗔𝗗 𝗡𝗢𝗪 ✦ - {anime_invite}\n"
    "#r 𖤓𝗔𝗡𝗜𝗠𝗘 𝗜𝗡𝗗𝗘𝗫 𝗧𝗔𝗠𝗜𝗟𖤓 - https://t.me/+qGiH-Zw3ydJjZjU1"
)
_BUTTON_STYLES = {"b": "primary", "r": "danger", "g": "success"}


def _title_key(title: str) -> str:
    return normalize_anime_title((title or "").strip())


class _SafeValues(dict):
    def __missing__(self, key):
        return ""


def _render_template(template: str, values: dict[str, Any], limit: int = 4096) -> str:
    text = (template or DEFAULT_ONGOING_TEMPLATE).strip()
    try:
        text = text.format_map(_SafeValues(values))
    except (ValueError, TypeError, KeyError):
        logger.exception("Invalid ongoing post template; falling back to default")
        text = DEFAULT_ONGOING_TEMPLATE.format_map(_SafeValues(values))
    return text[:limit]


def _plain_label(value: str) -> str:
    """Normalize decorative Unicode lettering before matching labels such as DOWNLOAD NOW."""
    return unicodedata.normalize("NFKC", str(value or "")).casefold()


def _valid_http_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    except Exception:
        return False


def _select_anime_invite(links: list[dict], button_label: str) -> str | None:
    """Prefer an invite labelled for downloads; otherwise use the first saved invite."""
    usable = [row for row in links if _valid_http_url(str(row.get("url") or "").strip())]
    if not usable:
        return None
    target = normalize_anime_title(_plain_label(button_label))
    # When the label is Download Now, look for a similarly named saved invite first.
    if "download" in _plain_label(button_label):
        for row in usable:
            if "download" in _plain_label(str(row.get("label") or "")):
                return str(row.get("url")).strip()
    for row in usable:
        label = normalize_anime_title(_plain_label(str(row.get("label") or "")))
        if label and (label == target or label in target or target in label):
            return str(row.get("url")).strip()
    return str(usable[0].get("url")).strip()


def _is_dynamic_invite(url: str, label: str) -> bool:
    low = (url or "").casefold()
    placeholder = any(marker in low for marker in (
        "{anime_invite}", "<anime_invite>", "your saved anime invite", "anime invite link here",
        "your_invite_link", "your-invite-link", "invite_link_here", "saved_invite_link",
    ))
    # The Download Now button is always anime-specific, even if an old example URL was pasted.
    return placeholder or "download" in _plain_label(label)


def _build_button_rows(
    template: str,
    links: list[dict],
    fallback_download_url: str | None = None,
) -> list[list[dict]]:
    """Parse ongoing buttons; ` | ` separates buttons within the same row."""
    rows: list[list[dict]] = []
    lines = [line.strip() for line in (template or DEFAULT_ONGOING_BUTTONS_TEMPLATE).splitlines() if line.strip()]
    for line_number, line in enumerate(lines, start=1):
        row: list[dict] = []
        # A newline starts a new row; a pipe separates buttons in that row.
        for button_number, button_spec in enumerate(line.split("|"), start=1):
            button_spec = button_spec.strip()
            match = re.match(r"^#([brg])\s+(.+?)\s+-\s*(.*?)\s*$", button_spec, re.IGNORECASE)
            if not match:
                raise ValueError(
                    f"Invalid ongoing button on line {line_number}, position {button_number}. "
                    "Use #b/#r/#g Button Name - URL; separate same-row buttons with |."
                )
            color_code, label, raw_url = match.group(1).lower(), match.group(2).strip(), match.group(3).strip()
            if not label or len(label) > 64:
                raise ValueError(f"Button name on line {line_number}, position {button_number} must be 1–64 characters.")
            url = raw_url
            if _is_dynamic_invite(raw_url, label):
                # In /batch1renamed fallback mode, the submitted episode's file URL
                # takes priority over the saved invite for the anime.
                url = (
                    str(fallback_download_url).strip()
                    if fallback_download_url and _valid_http_url(str(fallback_download_url).strip())
                    else (_select_anime_invite(links, label) or "")
                )
            if not _valid_http_url(url):
                raise ValueError(
                    f"Button link on line {line_number}, position {button_number} is invalid. "
                    "Use a full https:// URL or {anime_invite}."
                )
            row.append({"text": label, "url": url, "style": _BUTTON_STYLES[color_code]})
        if row:
            rows.append(row)
    if not rows:
        raise ValueError("No ongoing buttons were configured. Open Admin Panel → Ongoing Buttons.")
    return rows


def _plain_keyboard(rows: list[list[dict]]) -> InlineKeyboardMarkup:
    """Compatibility keyboard for installations where Telegram rejects the style field."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(item["text"], url=item["url"]) for item in row]
        for row in rows
    ])


async def _send_bot_api(client: Client, chat_id: int, poster: str | None, caption: str,
                        button_rows: list[list[dict]]) -> int:
    """Send through Bot API so new inline-button colors work on supported Telegram clients."""
    token = getattr(Config, "BOT_TOKEN", "")
    if not token:
        raise RuntimeError("BOT_TOKEN is not configured")
    method = "sendPhoto" if poster else "sendMessage"
    payload: dict[str, Any] = {"chat_id": chat_id}
    if button_rows:
        payload["reply_markup"] = {"inline_keyboard": button_rows}
    if poster:
        payload.update({"photo": poster, "caption": caption, "parse_mode": "HTML"})
    else:
        payload.update({"text": caption, "parse_mode": "HTML", "disable_web_page_preview": True})

    endpoint = f"https://api.telegram.org/bot{token}/{method}"
    timeout = aiohttp.ClientTimeout(total=35, connect=10, sock_read=25)
    # A rejected style is retried without styles; malformed HTML is retried as plain text.
    styled = True
    html_mode = True
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for _ in range(3):
            outgoing = dict(payload)
            if not html_mode:
                outgoing.pop("parse_mode", None)
            if not styled:
                outgoing["reply_markup"] = {
                    "inline_keyboard": [[{"text": b["text"], "url": b["url"]} for b in row] for row in button_rows]
                }
            async with session.post(endpoint, json=outgoing) as response:
                data = await response.json(content_type=None)
            if data.get("ok"):
                return int(data["result"]["message_id"])
            description = str(data.get("description") or "Telegram Bot API request failed")
            folded = description.casefold()
            if styled and ("style" in folded or "keyboardbuttonstyle" in folded or "inlinekeyboardbutton" in folded):
                styled = False
                logger.warning("Telegram rejected colored button styles; retrying with standard button styling: %s", description)
                continue
            if html_mode and any(term in folded for term in ("parse entities", "can't parse", "unsupported start tag", "entity")):
                html_mode = False
                logger.warning("Ongoing caption HTML was rejected; retrying as plain text")
                continue
            raise RuntimeError(description)
    raise RuntimeError("Telegram did not accept the ongoing post")


async def _record_bot_published_post(
    client: Client, target: Any, message_id: int, title: str, post_type: str = "ongoing"
) -> None:
    """Record the exact channel/message pair after a successful bot publication.

    The record powers the admin panel's safe in-place button editor. History failures
    are logged but never turn a successful Telegram send into a failed publication.
    """
    channel_id: Any = target
    try:
        chat = await client.get_chat(target)
        channel_id = int(chat.id)
    except Exception:
        if isinstance(target, str) and target.lstrip("-").isdigit():
            channel_id = int(target)
        logger.debug(
            "Could not resolve published destination %s after successful send",
            target, exc_info=True,
        )

    record_id = f"{channel_id}:{int(message_id)}"
    try:
        await codeflixbots.codeflixbots["bot_published_messages"].update_one(
            {"_id": record_id},
            {"$set": {
                "channel_id": channel_id,
                "message_id": int(message_id),
                "title": str(title or "Anime"),
                "post_type": str(post_type or "ongoing"),
                "published_at": datetime.now(timezone.utc),
            }},
            upsert=True,
        )
    except Exception:
        logger.exception(
            "Post was sent but its editable-post history record could not be saved"
        )


async def post_ongoing_anime(
    client: Client,
    title: str,
    season: str | None = None,
    episode_range: str | None = None,
    fallback_download_url: str | None = None,
    poster_override: str | None = None,
    use_saved_poster: bool = True,
    status_override: str | None = None,
    global_targets_only: bool = False,
) -> str:
    """Publish an ongoing post; optionally use only globally configured ongoing channels."""
    key = _title_key(title)
    if not key:
        return "❌ Send an anime title, e.g. /ongoing Naruto Shippuden."

    routes = codeflixbots.codeflixbots["anime_routes"]
    route = None
    if not global_targets_only:
        route = await routes.find_one({"title_key": key})
        if not route:
            route = await routes.find_one({"title": {"$regex": "^" + re.escape(title.strip()) + "$", "$options": "i"}})
        if not route:
            return f"❌ No saved channel/template settings were found for {title}. Add it in Admin Panel → Anime Channels."
    else:
        # Completed batch posts intentionally bypass anime-specific Anime Channel
        # routing and publish to the globally configured Ongoing Channels only.
        route = {"title": title, "title_key": key, "ongoing_details": {}}

    invite_collection = codeflixbots.codeflixbots["anime_invite_links"]
    route_key = str(route.get("title_key") or key).strip()
    invite_doc = await invite_collection.find_one({"_id": key})
    if not invite_doc and route_key != key:
        invite_doc = await invite_collection.find_one({"_id": route_key})
    if not invite_doc:
        invite_doc = await invite_collection.find_one({"title_key": key})
    links = list((invite_doc or {}).get("links") or [])

    post_settings = await codeflixbots.codeflixbots["post_template_settings"].find_one({"_id": "global"}) or {}
    button_template = post_settings.get("ongoing_buttons_template") or DEFAULT_ONGOING_BUTTONS_TEMPLATE
    if fallback_download_url and not _valid_http_url(str(fallback_download_url).strip()):
        return "❌ The stored-file fallback URL is invalid. Send a complete http:// or https:// link with /batch1renamed."
    try:
        button_rows = _build_button_rows(
            button_template,
            links,
            fallback_download_url=fallback_download_url,
        )
    except ValueError as exc:
        return f"❌ Ongoing button template error: {exc}"
    if fallback_download_url:
        expected_download_url = str(fallback_download_url).strip()
        if not any(
            str(button.get("url") or "").strip() == expected_download_url
            for row in button_rows for button in row
        ):
            return (
                "❌ The saved Ongoing Buttons template has no dynamic Download button. "
                "Add {anime_invite} or use a button label containing DOWNLOAD so the supplied link can replace it."
            )

    targets = await resolve_ongoing_targets(route, codeflixbots.codeflixbots)
    if not targets:
        return f"❌ No ongoing channel is assigned to {route.get('title', title)}. Configure it in Admin Panel → Anime Channels."

    details = route.get("ongoing_details") or {}
    qualities = details.get("qualities") or "480p, 720p, 1080p"
    templates_collection = codeflixbots.codeflixbots["anime_templates"]
    saved_template = await templates_collection.find_one({"_id": key}) or {}
    if not saved_template and route_key != key:
        saved_template = await templates_collection.find_one({"_id": route_key}) or {}
    if not saved_template:
        saved_template = await templates_collection.find_one({"title_key": key}) or {}
    if not saved_template:
        # Older saved sets may have a differently formatted _id but an exact title.
        escaped_title = re.escape(str(title or "").strip())
        if escaped_title:
            saved_template = await templates_collection.find_one({"title": {"$regex": f"^{escaped_title}$", "$options": "i"}}) or {}
    season_value = str(season).strip() if season is not None and str(season).strip() else (details.get("season") or "—")
    episode_value = str(episode_range).strip() if episode_range is not None and str(episode_range).strip() else (details.get("episode_range") or "—")
    values = {
        "anime": route.get("title") or title,
        "title": route.get("title") or title,
        "season": season_value,
        "episode_range": episode_value,
        "episode": episode_value,
        "tag": details.get("tag") or "",
        "status": str(status_override).strip() if status_override else (details.get("status") or "ONGOING"),
        "qualities": qualities,
        "audio": "Tamil Audio",
    }
    template = post_settings.get("ongoing_template") or DEFAULT_ONGOING_TEMPLATE
    if poster_override:
        poster = poster_override
    elif use_saved_poster:
        poster = saved_template.get("poster_file_id") or route.get("poster_file_id")
    else:
        poster = None
    caption = _render_template(template, values, limit=1024 if poster else 4096)

    success, failures = [], []
    for target in targets:
        try:
            # Admin setup already normalizes destinations to an @username or numeric ID.
            # Pass it directly to Bot API to avoid Pyrogram's local-peer-cache failures on
            # Render restarts (especially for private numeric channel IDs).
            api_target = int(target) if isinstance(target, str) and target.lstrip("-").isdigit() else target
            message_id = await _send_bot_api(client, api_target, poster, caption, button_rows)
            await _record_bot_published_post(
                client, api_target, message_id, str(route.get("title") or title), "ongoing"
            )
            success.append(f"{target} (message {message_id})")
        except Exception as exc:
            logger.exception("Ongoing post failed for %s", target)
            failures.append(f"{target}: {type(exc).__name__}: {exc}")

    result = []
    if success:
        result.append(f"✅ {route.get('title', title)} ongoing post published to {len(success)} channel(s):\n" + "\n".join(success))
    if failures:
        result.append("⚠️ Failed destinations:\n" + "\n".join(failures))
    return "\n\n".join(result) if result else "❌ No ongoing post was published. Check the bot's channel admin permissions."


@Client.on_message(filters.private & filters.command(["ongoing", "postongoing"]))
async def ongoing_command(client: Client, message: Message):
    if not is_admin_user(getattr(message, "from_user", None)):
        return await message.reply_text("⛔ Admin only.")
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2 or not parts[1].strip():
        return await message.reply_text(
            "Usage: `/ongoing Anime Title`\n\n"
            "The bot uses that anime's saved poster and invite links, plus the shared Main Ongoing Post Text and Ongoing Buttons template."
        )
    try:
        result = await post_ongoing_anime(client, parts[1].strip())
    except Exception as exc:
        logger.exception("Unexpected failure while publishing ongoing post")
        result = f"⚠️ Ongoing post failed ({type(exc).__name__}). Check the bot logs and channel permissions."
    await message.reply_text(result[:3900], disable_web_page_preview=True, parse_mode=enums.ParseMode.DISABLED)
