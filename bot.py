import asyncio
import logging
import re
from typing import Dict, List, Optional
from urllib.parse import urlparse
from pyrogram import Client, filters
from pyrogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery
)
from pyrogram.errors import (
    FloodWait,
    UserNotParticipant,
    MessageNotModified
)

import config
from helper.state_store import StateStore
from helper.link_codec import (
    decode_batch,
    decode_batch_manifest_id,
    decode_message_id,
    encode_batch_manifest_id,
    encode_message_id,
)
from helper.storage import (
    MAX_BATCH_MESSAGES,
    Storage,
    deliver_archived_batch,
    save_batch_manifest,
)
from helper.upload_order import ordered_archive_message_ids, sort_upload_messages

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("ANIFLIX-FILE-STORE-BOT")

# In-memory tracking for multi-file /getlink upload sessions
USER_UPLOAD_SESSIONS: Dict[int, Dict] = {}
# In-memory tracking for /batch forwarding sessions
USER_BATCH_SESSIONS: Dict[int, Dict] = {}
ADMIN_PENDING_ACTIONS: Dict[int, str] = {}
ADMIN_PENDING_DATA: Dict[int, Dict] = {}


def _get_archive_channel_id() -> int:
    return bot.settings.archive_channel_id


def _get_admins() -> List[int]:
    return list(bot.settings.admins)


class AniflixBot(Client):
    def __init__(self, settings=None):
        self.settings = settings or config.load_settings()
        super().__init__(
            name=self.settings.session_name,
            api_id=self.settings.api_id,
            api_hash=self.settings.api_hash,
            bot_token=self.settings.bot_token,
            workers=8
        )
        self.archive_channel_id = self.settings.archive_channel_id
        self.state_store = StateStore(self, self.archive_channel_id)
        # Keep Pyrogram Client.storage reserved for its internal MTProto session backend.
        self.file_storage = Storage(self, self.archive_channel_id)
        self.batch_reader = None
        if self.settings.batch_user_session_string:
            # Optional user authorization is isolated to /batch source reads.
            # Never log or persist the session string in project files.
            self.batch_reader = Client(
                name=f"{self.settings.session_name}_batch_reader",
                api_id=self.settings.api_id,
                api_hash=self.settings.api_hash,
                session_string=self.settings.batch_user_session_string,
                no_updates=True,
            )

    async def start(self):
        await super().start()
        try:
            if self.batch_reader:
                await self.batch_reader.start()
                logger.info("Optional MTProto batch reader started.")
            me = await self.get_me()
            self.username = me.username
            logger.info(f"Bot started as @{self.username}")
            # Initialize remote state safely without get_chat_history
            await self.state_store.load_state()
            logger.info("Bot state initialized successfully.")
        except Exception:
            if self.batch_reader and self.batch_reader.is_connected:
                await self.batch_reader.stop()
            await super().stop()
            raise

    async def stop(self, *args):
        await self.state_store.save_state()
        if self.batch_reader and self.batch_reader.is_connected:
            await self.batch_reader.stop()
        await super().stop(*args)
        logger.info("Bot stopped cleanly.")


bot = AniflixBot()


@bot.on_chat_member_updated(filters.channel)
async def record_fsub_membership_update(client: Client, update):
    """Count only Telegram membership updates, never button presses or requests."""
    try:
        channel_id = int(update.chat.id)
        old_member = getattr(update, "old_chat_member", None)
        new_member = getattr(update, "new_chat_member", None)
        user = getattr(new_member, "user", None)
        if not user:
            return
        old_status = _member_status_name(old_member) if old_member else "LEFT"
        new_status = _member_status_name(new_member)
        became_member = new_status in {"MEMBER", "ADMINISTRATOR", "OWNER"} and old_status not in {"MEMBER", "ADMINISTRATOR", "OWNER"}
        if not became_member:
            return
        for channel in bot.state_store.get_fsub_channels():
            if int(channel.get("id") or 0) != channel_id:
                continue
            is_request_channel = bool(channel.get("join_request") or channel.get("type") == "join_request")
            if is_request_channel:
                if bot.state_store.has_fsub_join_request(channel_id, int(user.id)):
                    await bot.state_store.record_fsub_member(channel_id, int(user.id), accepted=True)
            else:
                await bot.state_store.record_fsub_member(channel_id, int(user.id))
            return
    except Exception:
        logger.exception("Could not record FSUB membership update")


@bot.on_chat_join_request(filters.channel)
async def record_fsub_join_request(client: Client, request):
    """Persist a user's request only when it used our configured FSUB invite."""
    try:
        channel_id = int(request.chat.id)
        user_id = int(request.from_user.id)
        invite = getattr(request, "invite_link", None)
        received_link = getattr(invite, "invite_link", None) if invite else None
        for channel in bot.state_store.get_fsub_channels():
            configured_id = int(channel.get("id") or 0)
            is_request_channel = bool(
                channel.get("join_request") or channel.get("type") == "join_request"
            )
            if configured_id != channel_id or not is_request_channel:
                continue
            # Do not count requests made through unrelated channel invite links.
            if not received_link or received_link != channel.get("link"):
                logger.info(
                    "Ignoring join request not made through the configured FSUB link "
                    "(channel=%s, user=%s).", channel_id, user_id
                )
                return
            await bot.state_store.record_fsub_join_request(channel_id, user_id)
            logger.info(
                "Recorded FSUB join request (channel=%s, user=%s).",
                channel_id, user_id
            )
            return
    except Exception:
        logger.exception("Could not record incoming FSUB join request")


# ------------------- HELPER FUNCTIONS ------------------- #

def is_admin(user_id: int) -> bool:
    if user_id in _get_admins():
        return True
    admins = bot.state_store.get_admins()
    return user_id in admins


def is_owner(user_id: int) -> bool:
    return user_id == bot.settings.owner_id


def _member_status_name(member) -> str:
    """Normalize Pyrogram enum/string statuses without assuming enum formatting."""
    return str(getattr(member, "status", "")).upper().split(".")[-1]


def _is_joined_member(member) -> bool:
    # A pending join request is NOT membership and must never unlock a file.
    return _member_status_name(member) in {"MEMBER", "ADMINISTRATOR", "OWNER"}


def _forwarded_channel(message: Message):
    """Return the channel chat from either legacy or modern Telegram forward metadata."""
    chat = getattr(message, "forward_from_chat", None)
    if chat is not None:
        return chat
    origin = getattr(message, "forward_origin", None)
    return getattr(origin, "chat", None) if origin is not None else None


async def _verify_bot_can_manage_invites(client: Client, channel_id: int):
    """Require the bot to be an admin/owner with invite-user permission."""
    bot_identity = await client.get_me()
    bot_member = await client.get_chat_member(channel_id, bot_identity.id)
    status = _member_status_name(bot_member)
    if status not in {"ADMINISTRATOR", "OWNER"}:
        raise PermissionError("I am not an administrator in that channel. Promote me first.")
    if status != "OWNER":
        privileges = getattr(bot_member, "privileges", None)
        if not privileges or not getattr(privileges, "can_invite_users", False):
            raise PermissionError(
                "I am an administrator, but I need the 'Invite Users via Link' permission."
            )


async def check_fsub(client: Client, user_id: int) -> List[Dict]:
    """Return channels still required for FSUB verification.

    Normal-join channels require actual membership. For a join-request channel,
    a successfully received Telegram chat_join_request update for this user's
    request through this bot-created invite link is sufficient. The request is
    persisted, so a restart does not make the user submit it again.
    """
    # The persisted admin-managed list is authoritative. In particular, an
    # empty list means FSUB is disabled; never resurrect removed channels from
    # legacy config defaults.
    fsub_channels = bot.state_store.get_fsub_channels()

    unjoined = []
    for ch in fsub_channels:
        chat_identifier = ch.get("id") or ch.get("username")
        is_request_channel = bool(ch.get("join_request") or ch.get("type") == "join_request")
        has_request = is_request_channel and bot.state_store.has_fsub_join_request(int(ch.get("id") or 0), user_id)
        try:
            member = await client.get_chat_member(chat_identifier, user_id)
            if not _is_joined_member(member):
                if not has_request:
                    unjoined.append(ch)
                # A recorded request still satisfies FSUB while approval is pending.
            else:
                if is_request_channel and has_request:
                    await bot.state_store.record_fsub_member(int(ch.get("id") or 0), user_id, accepted=True)
                elif not is_request_channel:
                    await bot.state_store.record_fsub_member(int(ch.get("id") or 0), user_id)
        except UserNotParticipant:
            if not has_request:
                unjoined.append(ch)
        except Exception as e:
            logger.warning("FSUB membership check failed for %s: %s", chat_identifier, e)
            if not has_request:
                unjoined.append(ch)
    return unjoined


def get_fsub_keyboard(unjoined: List[Dict], redirect_data: Optional[str] = None) -> InlineKeyboardMarkup:
    buttons = []
    for ch in unjoined:
        name = ch.get("name", "Join Channel")
        link = ch.get("link") or f"https://t.me/{str(ch.get('username', '')).lstrip('@')}"
        buttons.append([InlineKeyboardButton(text=name, url=link)])

    try_again_data = f"try_again_{redirect_data}" if redirect_data else "try_again"
    buttons.append([InlineKeyboardButton(text="🔄 Try Again", callback_data=try_again_data)])
    return InlineKeyboardMarkup(buttons)


# ------------------- COMMAND: /START ------------------- #

async def _deliver_start_payload(client: Client, user_id: int, message: Message, param: str):
    """Deliver a single file, a new ordered manifest, or a legacy bounded range."""
    try:
        if param.startswith("batch_"):
            payload = param[6:]
            manifest_id = decode_batch_manifest_id(payload)
            if manifest_id is not None:
                sent_count, failed_count = await deliver_archived_batch(
                    client,
                    bot.archive_channel_id,
                    manifest_id,
                    user_id,
                    bot.settings.protect_content,
                )
                await bot.state_store.update_stats(total_views=1)
                if sent_count == 0:
                    return await message.reply_text("❌ No files in this batch are available.")
                if failed_count:
                    await message.reply_text(
                        f"⚠️ Batch delivered: `{sent_count}` files; `{failed_count}` unavailable."
                    )
                return

            start_id, end_id = decode_batch(payload)
            if end_id - start_id + 1 > MAX_BATCH_MESSAGES:
                return await message.reply_text(
                    f"❌ This legacy batch exceeds the {MAX_BATCH_MESSAGES}-message safety limit."
                )
            sent_count = failed_count = 0
            for message_id in range(start_id, end_id + 1):
                if await bot.file_storage.deliver_message(
                    user_id, message_id, bot.settings.protect_content
                ):
                    sent_count += 1
                else:
                    failed_count += 1
                await asyncio.sleep(0.1)
            await bot.state_store.update_stats(total_views=1)
            if sent_count == 0:
                return await message.reply_text("❌ No files in this batch are available.")
            if failed_count:
                await message.reply_text(
                    f"⚠️ Legacy batch delivered: `{sent_count}` files; `{failed_count}` unavailable."
                )
            return

        message_id = decode_message_id(param)
        if not await bot.file_storage.deliver_message(
            user_id, message_id, bot.settings.protect_content
        ):
            return await message.reply_text("❌ This file is no longer available in the archive.")
        await bot.state_store.update_stats(total_views=1)
    except Exception as exc:
        logger.warning("Start payload could not be delivered: %s", exc)
        await message.reply_text("❌ This link is corrupted, invalid, or expired.")


async def _reply_configured_message(
    message: Message,
    content,
    *,
    default_text: str,
    reply_markup=None,
    replacements: Optional[Dict[str, str]] = None,
):
    """Reply with a saved text or photo+caption message (legacy text is supported)."""
    if not content:
        content = default_text

    if isinstance(content, dict) and content.get("type") == "photo" and content.get("file_id"):
        caption = content.get("caption") or ""
        for key, value in (replacements or {}).items():
            caption = caption.replace("{" + key + "}", value)
        return await message.reply_photo(
            photo=content["file_id"],
            caption=caption or None,
            reply_markup=reply_markup,
        )

    text = content if isinstance(content, str) else default_text
    for key, value in (replacements or {}).items():
        text = text.replace("{" + key + "}", value)
    return await message.reply_text(text, reply_markup=reply_markup)


def _admin_message_payload(message: Message):
    """Capture text or a Telegram photo+caption using Pyrogram's Photo shape.

    Pyrogram/Pyrofork exposes ``message.photo`` as one Photo object with a
    ``file_id``. Some compatible wrappers expose a list of PhotoSize objects;
    accept both shapes so the admin flow does not silently lose the image.
    """
    photo = getattr(message, "photo", None)
    if photo:
        # Pyrogram/Pyrofork: one Photo object. Compatibility: list/tuple of sizes.
        if isinstance(photo, (list, tuple)):
            photo = next(
                (item for item in reversed(photo) if getattr(item, "file_id", None)),
                None,
            )
        file_id = getattr(photo, "file_id", None) if photo is not None else None
        if file_id:
            return {
                "type": "photo",
                "file_id": file_id,
                "caption": (getattr(message, "caption", None) or "").strip(),
            }

    text = (getattr(message, "text", None) or getattr(message, "caption", None) or "").strip()
    return text or None


async def _reply_welcome(message: Message, first_name: str = "there"):
    welcome = bot.state_store.get("welcome_message")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("ONGOING ANIME", url=bot.settings.ongoing_url)],
        [InlineKeyboardButton("ANIME REQUEST GROUP", url=bot.settings.support_url)],
    ])
    default_text = (
        f"👋 Hello, **{first_name}**!\n\n"
        "Welcome to **ANIFLIX FILE STORE BOT**.\n"
        "Send me media or use `/getlink` and `/batch` to store and generate sharing links."
    )
    return await _reply_configured_message(
        message,
        welcome,
        default_text=default_text,
        reply_markup=kb,
        replacements={"user": first_name},
    )


async def start_handler(client: Client, message: Message):
    user_id = message.from_user.id
    await bot.state_store.record_user(
        user_id,
        first_name=getattr(message.from_user, "first_name", "") or "",
        last_name=getattr(message.from_user, "last_name", "") or "",
        username=getattr(message.from_user, "username", "") or "",
    )
    args = getattr(message, "command", []) or []
    if len(args) > 1:
        param = args[1]
        unjoined = await check_fsub(client, user_id)
        if unjoined:
            fsub_text = bot.state_store.get("fsub_message")
            default_fsub_text = (
                "⚠️ **Access Denied!**\n\n"
                "You must join our official channels to access files stored here.\n"
                "Join each channel below and tap **Try Again**."
            )
            return await _reply_configured_message(
                message,
                fsub_text,
                default_text=default_fsub_text,
                reply_markup=get_fsub_keyboard(unjoined, redirect_data=param),
            )
        return await _deliver_start_payload(client, user_id, message, param)
    return await _reply_welcome(message, message.from_user.first_name)


bot.on_message(filters.command("start") & filters.private)(start_handler)


# ------------------- COMMAND: /GETLINK ------------------- #

@bot.on_message(filters.command("getlink") & filters.private)
async def getlink_handler(client: Client, message: Message):
    user_id = message.from_user.id
    buttons = [
        [InlineKeyboardButton("✅ GENERATE LINK", callback_data="gen_link_finish")],
        [InlineKeyboardButton("⏸ PAUSE", callback_data="gen_link_pause"),
         InlineKeyboardButton("❌ CANCEL", callback_data="gen_link_cancel")]
    ]
    control_message = await message.reply_text(
        "📥 **Multi-Message Upload Session Started**\n\n"
        "Forward all the messages/files together. I will keep their original order.\n"
        "Queued: **0**\n\n"
        "Tap **GENERATE LINK** after forwarding is complete.",
        reply_markup=InlineKeyboardMarkup(buttons)
    )
    USER_UPLOAD_SESSIONS[user_id] = {
        "status": "recording",
        "source_messages": {},
        "archived": {},
        "message_ids": [],
        "control_message": control_message,
        "status_lock": asyncio.Lock(),
        "finish_lock": asyncio.Lock(),
    }


# ------------------- COMMAND: /BATCH ------------------- #

@bot.on_message(filters.command("batch") & filters.private)
async def batch_cmd_handler(client: Client, message: Message):
    if not is_admin(message.from_user.id):
        return await message.reply_text("⛔ This command is restricted to administrators.")

    USER_BATCH_SESSIONS[message.from_user.id] = {"step": "await_first"}
    await message.reply_text(
        "📦 **Batch File Link Generator**\n\n"
        "1. Forward the **FIRST** message from the source channel or send its Telegram post link.\n"
        "Send /cancel to abort at any time."
    )


# ------------------- COMMAND: /ADMIN & ADMIN FEATURES ------------------- #

@bot.on_message(filters.command("admin") & filters.private)
async def admin_panel(client: Client, message: Message):
    if not is_admin(message.from_user.id):
        return await message.reply_text("⛔ Unauthorized.")

    stats = bot.state_store.get_stats()
    text = (
        "⚙️ **ANIFLIX Admin Dashboard**\n\n"
        f"👥 Total Users: `{stats.get('total_users', 0)}`\n"
        f"🔗 Generated Links: `{stats.get('total_links', 0)}`\n"
        f"📁 Stored Files: `{stats.get('total_files', 0)}`\n"
        f"👁️ Views/Retrievals: `{stats.get('total_views', 0)}`\n"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 FSUB Settings", callback_data="adm_fsub"),
         InlineKeyboardButton("📊 Bot Status", callback_data="adm_status")],
        [InlineKeyboardButton("📝 Edit Welcome", callback_data="adm_edit_welcome"),
         InlineKeyboardButton("📬 Broadcast", callback_data="adm_broadcast")]
    ])
    await message.reply_text(text, reply_markup=kb)


@bot.on_message(filters.command("status") & filters.private)
async def status_handler(client: Client, message: Message):
    if not is_admin(message.from_user.id):
        return
    stats = bot.state_store.get_stats()
    await message.reply_text(
        f"📊 **Bot Status & Health**\n\n"
        f"• Status: `Online & Running`\n"
        f"• Registered Users: `{stats.get('total_users', 0)}`\n"
        f"• Total Links Created: `{stats.get('total_links', 0)}`\n"
        f"• Total Files Copied: `{stats.get('total_files', 0)}`\n"
        f"• Total Link Views: `{stats.get('total_views', 0)}`"
    )


@bot.on_message(filters.command("users") & filters.private)
async def users_count_handler(client: Client, message: Message):
    if not is_admin(message.from_user.id):
        return
    users = sorted({int(uid) for uid in bot.state_store.get("users", [])})
    profiles = bot.state_store.get("user_profiles", {})
    if not users:
        return await message.reply_text("👥 No registered users yet.")
    await message.reply_text(f"👥 Registered Users: {len(users)}\nShowing saved user names:", parse_mode=None)
    lines = []
    for number, uid in enumerate(users, start=1):
        profile = profiles.get(str(uid), {}) if isinstance(profiles, dict) else {}
        name = " ".join(part for part in [profile.get("first_name", ""), profile.get("last_name", "")] if part).strip()
        username = profile.get("username", "")
        display = name or "Name unavailable (older registration)"
        if username:
            display += f" (@{username.lstrip('@')})"
        lines.append(f"{number}. {display} — ID: {uid}")
    for start in range(0, len(lines), 35):
        await message.reply_text("\n".join(lines[start:start + 35]), parse_mode=None)


@bot.on_message(filters.command("addadmin") & filters.private)
async def add_admin_handler(client: Client, message: Message):
    if not is_owner(message.from_user.id):
        return await message.reply_text("⛔ Only the configured owner can manage extra administrators.")
    args = getattr(message, "command", []) or []
    if len(args) != 2 or not args[1].isdigit() or int(args[1]) <= 0:
        return await message.reply_text("Usage: `/addadmin <numeric_user_id>`")
    await bot.state_store.add_admin(int(args[1]))
    await message.reply_text("✅ Administrator added.")


@bot.on_message(filters.command("deladmin") & filters.private)
async def remove_admin_handler(client: Client, message: Message):
    if not is_owner(message.from_user.id):
        return await message.reply_text("⛔ Only the configured owner can manage extra administrators.")
    args = getattr(message, "command", []) or []
    if len(args) != 2 or not args[1].isdigit() or int(args[1]) <= 0:
        return await message.reply_text("Usage: `/deladmin <numeric_user_id>`")
    admin_id = int(args[1])
    if admin_id == bot.settings.owner_id or admin_id in bot.settings.admins:
        return await message.reply_text("⚠️ The owner and environment-configured admins cannot be removed here.")
    await bot.state_store.remove_admin(admin_id)
    await message.reply_text("✅ Administrator removed if they were present.")


@bot.on_message(filters.command("broadcast") & filters.private)
async def broadcast_handler(client: Client, message: Message):
    if not is_admin(message.from_user.id):
        return

    if not message.reply_to_message:
        return await message.reply_text("⚠️ Reply to a message with `/broadcast` to send it to all users.")

    users = bot.state_store.get("users", [])
    status_msg = await message.reply_text(f"🚀 Broadcasting to `{len(users)}` users...")
    success = 0
    failed = 0

    for uid in users:
        try:
            await message.reply_to_message.copy(chat_id=uid)
            success += 1
            await asyncio.sleep(0.05)
        except FloodWait as f:
            await asyncio.sleep(f.value)
            try:
                await message.reply_to_message.copy(chat_id=uid)
                success += 1
            except Exception:
                failed += 1
        except Exception:
            failed += 1

    await status_msg.edit_text(
        f"✅ **Broadcast Completed**\n\n"
        f"• Successful: `{success}`\n"
        f"• Failed: `{failed}`"
    )


# ------------------- PRIVATE MESSAGE INTERCEPTOR ------------------- #

async def _batch_endpoint(client: Client, message: Message):
    """Resolve a forwarded channel post or Telegram post URL to (chat ID, msg ID)."""
    forwarded_chat = getattr(message, "forward_from_chat", None)
    forwarded_id = getattr(message, "forward_from_message_id", None)
    if forwarded_chat and forwarded_id:
        return int(forwarded_chat.id), int(forwarded_id)

    text = getattr(message, "text", None) or ""
    match = re.search(r"https?://(?:www\.)?(?:t\.me|telegram\.me)/[^\s]+", text)
    if not match:
        return None
    parsed = urlparse(match.group(0).rstrip(".,);]"))
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        return None
    try:
        message_id = int(parts[-1])
    except ValueError:
        return None

    if parts[0] == "c" and len(parts) >= 3 and parts[1].isdigit():
        channel_id = int(f"-100{parts[1]}")
    else:
        username = parts[0]
        if username in {"s", "share"} or username.startswith("+"):
            return None
        channel_id = username if not username.lstrip("-").isdigit() else int(username)
    try:
        resolver = bot.batch_reader or client
        chat = await resolver.get_chat(channel_id)
        return int(chat.id), message_id
    except Exception as exc:
        logger.info("Could not resolve batch source %s: %s", channel_id, exc)
        return None


def _normalize_messages(result):
    """Normalize Telegram results and preserve original channel message order."""
    if isinstance(result, (list, tuple)):
        messages = [item for item in result if item and not getattr(item, "empty", False)]
    elif result is None or getattr(result, "empty", False):
        messages = []
    else:
        messages = [result]
    return sorted(messages, key=lambda item: int(item.id))

@bot.on_message(filters.private & ~filters.command(["start", "admin", "getlink", "batch", "broadcast", "users", "status"]))
async def incoming_private_messages(client: Client, message: Message):
    user_id = message.from_user.id

    action = ADMIN_PENDING_ACTIONS.get(user_id)
    if action:
        if message.text and message.text.split(maxsplit=1)[0].split("@")[0] == "/cancel":
            ADMIN_PENDING_ACTIONS.pop(user_id, None)
            ADMIN_PENDING_DATA.pop(user_id, None)
            return await message.reply_text("❌ Admin edit cancelled.")
        if not is_admin(user_id):
            ADMIN_PENDING_ACTIONS.pop(user_id, None)
            ADMIN_PENDING_DATA.pop(user_id, None)
            return await message.reply_text("⛔ Unauthorized.")
        if action == "fsub_add_forward":
            channel = _forwarded_channel(message)
            if channel is None:
                return await message.reply_text(
                    "⚠️ I couldn't identify a forwarded channel. Please forward a message "
                    "directly from the channel after making me an administrator there."
                )
            channels = list(bot.state_store.get_fsub_channels())
            if len(channels) >= 10:
                ADMIN_PENDING_ACTIONS.pop(user_id, None)
                return await message.reply_text("⚠️ The maximum of 10 FSUB channels is already configured.")
            try:
                await _verify_bot_can_manage_invites(client, int(channel.id))
                chat = await client.get_chat(int(channel.id))
            except Exception as exc:
                logger.warning("FSUB channel validation failed for %s: %s", getattr(channel, "id", "?"), exc)
                return await message.reply_text(
                    f"❌ Channel verification failed: `{exc}`\n\n"
                    "Make sure I am an administrator and have permission to invite users."
                )
            if any(int(item.get("id", 0) or 0) == int(chat.id) for item in channels):
                ADMIN_PENDING_ACTIONS.pop(user_id, None)
                ADMIN_PENDING_DATA.pop(user_id, None)
                return await message.reply_text("⚠️ This channel is already in the FSUB list.")
            ADMIN_PENDING_DATA[user_id] = {
                "id": int(chat.id),
                "name": chat.title or "Channel",
                "username": getattr(chat, "username", None),
            }
            ADMIN_PENDING_ACTIONS[user_id] = "fsub_choose_method"
            return await message.reply_text(
                f"✅ Channel verified: **{chat.title or 'Channel'}**\n"
                f"🆔 `{chat.id}`\n\nChoose the force-subscription method:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📨 JOIN REQUEST", callback_data="adm_fsub_method_request")],
                    [InlineKeyboardButton("👥 NORMAL JOIN", callback_data="adm_fsub_method_normal")],
                    [InlineKeyboardButton("❌ CANCEL", callback_data="adm_fsub")]
                ])
            )

        if action == "fsub_choose_method":
            return await message.reply_text(
                "Please choose JOIN REQUEST or NORMAL JOIN using the buttons above, or /cancel."
            )

        value = _admin_message_payload(message)
        if value is None:
            return await message.reply_text(
                "⚠️ Send text, or send a photo with its caption, or /cancel."
            )

        if action == "welcome":
            await bot.state_store.set("welcome_message", value)
            ADMIN_PENDING_ACTIONS.pop(user_id, None)
            return await message.reply_text("✅ Welcome message updated (photo and caption are supported).")
        if action == "fsub_message":
            await bot.state_store.set("fsub_message", value)
            ADMIN_PENDING_ACTIONS.pop(user_id, None)
            return await message.reply_text("✅ Force-subscription message updated (photo and caption are supported).")
        if action == "fsub_clear_counts":
            if not value.isdigit():
                return await message.reply_text("Send the channel number from the list, or /cancel.")
            channels = list(bot.state_store.get_fsub_channels())
            index = int(value) - 1
            if index < 0 or index >= len(channels):
                return await message.reply_text("⚠️ No FSUB channel has that number.")
            channel = channels[index]
            ADMIN_PENDING_ACTIONS.pop(user_id, None)
            ADMIN_PENDING_DATA[user_id] = {"fsub_clear_channel_id": int(channel.get("id") or 0)}
            return await message.reply_text(
                f"⚠️ Clear statistics for **{channel.get('name', 'Channel')}**?\n"
                "This clears only the displayed counts. It does NOT remove saved join-request access records.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🧹 YES, CLEAR COUNTS", callback_data="adm_fsub_clear_confirm"),
                     InlineKeyboardButton("❌ CANCEL", callback_data="adm_fsub_clear_cancel")]
                ])
            )

        if action == "fsub_remove":
            if not value.isdigit():
                return await message.reply_text("Send the 1-based channel number to remove, or /cancel.")
            channels = list(bot.state_store.get_fsub_channels())
            index = int(value) - 1
            if index < 0 or index >= len(channels):
                return await message.reply_text("⚠️ No FSUB channel has that number.")
            removed_channel = channels[index]
            await bot.state_store.remove_fsub_channel(int(removed_channel.get("id") or 0))
            ADMIN_PENDING_ACTIONS.pop(user_id, None)
            ADMIN_PENDING_DATA.pop(user_id, None)
            return await message.reply_text("✅ FSUB channel removed.")

    if message.text and message.text.split(maxsplit=1)[0].split("@")[0] == "/cancel":
        if USER_BATCH_SESSIONS.pop(user_id, None) is not None:
            return await message.reply_text("❌ Batch process cancelled.")
        if USER_UPLOAD_SESSIONS.pop(user_id, None) is not None:
            return await message.reply_text("❌ Upload session cancelled.")

    # Handle /batch conversational input
    if user_id in USER_BATCH_SESSIONS:
        step = USER_BATCH_SESSIONS[user_id].get("step")
        if message.text and message.text == "/cancel":
            del USER_BATCH_SESSIONS[user_id]
            return await message.reply_text("❌ Batch process cancelled.")

        if step == "await_first":
            endpoint = await _batch_endpoint(client, message)
            if not endpoint:
                return await message.reply_text("⚠️ Please forward a message from a channel or supply a valid message link.")
            f_chat_id, f_msg_id = endpoint
            USER_BATCH_SESSIONS[user_id]["chat_id"] = f_chat_id
            USER_BATCH_SESSIONS[user_id]["first_id"] = f_msg_id
            USER_BATCH_SESSIONS[user_id]["step"] = "await_last"
            return await message.reply_text(
                f"✅ First message ID: `{f_msg_id}`\n\n"
                "Now forward the **LAST** message from the same channel."
            )

        elif step == "await_last":
            endpoint = await _batch_endpoint(client, message)
            if not endpoint:
                return await message.reply_text("⚠️ Please forward the last message.")
            l_chat_id, l_msg_id = endpoint

            first_id = USER_BATCH_SESSIONS[user_id]["first_id"]
            chat_id = USER_BATCH_SESSIONS[user_id]["chat_id"]
            if int(l_chat_id) != int(chat_id):
                return await message.reply_text("⚠️ The last message must come from the same channel as the first.")
            del USER_BATCH_SESSIONS[user_id]

            start_id = min(first_id, l_msg_id)
            end_id = max(first_id, l_msg_id)
            if end_id - start_id + 1 > MAX_BATCH_MESSAGES:
                return await message.reply_text(
                    f"⚠️ Batch ranges are limited to {MAX_BATCH_MESSAGES} message IDs. Start a new /batch session."
                )

            status_msg = await message.reply_text(f"⏳ Processing `{end_id - start_id + 1}` messages into Archive...")

            archived_ids = []
            stored_count = 0

            all_ids = list(range(start_id, end_id + 1))
            for i in range(0, len(all_ids), MAX_BATCH_MESSAGES):
                chunk = all_ids[i:i + MAX_BATCH_MESSAGES]
                try:
                    source_reader = bot.batch_reader or client
                    msgs = await source_reader.get_messages(chat_id, chunk)
                except Exception as e:
                    logger.error(f"Cannot get messages from {chat_id}: {e}")
                    if "BOT_METHOD_INVALID" in str(e).upper() and not bot.batch_reader:
                        return await status_msg.edit_text(
                            "❌ Telegram rejected source-history access for the bot account. "
                            "Set the optional `BATCH_USER_SESSION_STRING` to an authorized "
                            "MTProto user session that can access this channel, then restart. "
                            "No arbitrary chat-history scan is performed."
                        )
                    return await status_msg.edit_text(
                        f"❌ Failed to fetch messages from source: `{e}`.\n"
                        "Make sure the source channel is public or the bot is an administrator there."
                    )

                for src_msg in _normalize_messages(msgs):
                    if not src_msg or src_msg.empty:
                        continue
                    try:
                        copied = await bot.file_storage.store_message(src_msg)
                        if not copied:
                            continue
                        archived_ids.append(copied.id)
                        stored_count += 1
                        await asyncio.sleep(0.2)
                    except FloodWait as f:
                        await asyncio.sleep(f.value)
                        copied = await bot.file_storage.store_message(src_msg)
                        if not copied:
                            continue
                        archived_ids.append(copied.id)
                        stored_count += 1
                    except Exception as e:
                        logger.error(f"Failed to archive msg {src_msg.id}: {e}")

            if not stored_count or not archived_ids:
                return await status_msg.edit_text("❌ No messages were successfully copied.")

            try:
                manifest_id = await save_batch_manifest(bot, bot.archive_channel_id, archived_ids)
            except Exception as exc:
                logger.exception("Could not save batch manifest")
                return await status_msg.edit_text(
                    f"❌ Files were archived, but the batch link could not be saved: `{exc}`"
                )
            batch_hash = encode_batch_manifest_id(manifest_id)
            batch_url = f"https://t.me/{bot.username}?start=batch_{batch_hash}"

            await bot.state_store.update_stats(total_links=1, total_files=stored_count)
            return await status_msg.edit_text(
                f"🎉 **Batch Link Generated Successfully!**\n\n"
                f"• Total Files: `{stored_count}`\n"
                f"• Link: {batch_url}"
            )

    # Handle /getlink upload sessions. Buffer first, then archive in source-chat
    # message-ID order when the user generates the link. This prevents parallel
    # Pyrogram handlers from changing the order of a multi-message forward.
    if user_id in USER_UPLOAD_SESSIONS:
        session = USER_UPLOAD_SESSIONS[user_id]
        if session.get("status") == "paused":
            return await message.reply_text("⏸ This upload session is paused. Tap RESUME or CANCEL before sending more files.")
        if session.get("status") != "recording":
            return

        source_messages = session.setdefault("source_messages", {})
        source_message_id = int(getattr(message, "id", 0) or 0)
        if source_message_id <= 0:
            return await message.reply_text("❌ I could not identify this message. Please forward it again.")
        if source_message_id not in source_messages and len(source_messages) >= MAX_BATCH_MESSAGES:
            return await message.reply_text(
                f"⚠️ Upload sessions are limited to {MAX_BATCH_MESSAGES} messages. Generate this link or cancel the session."
            )

        source_messages[source_message_id] = message
        count = len(source_messages)
        buttons = [
            [InlineKeyboardButton("✅ GENERATE LINK", callback_data="gen_link_finish")],
            [InlineKeyboardButton("⏸ PAUSE", callback_data="gen_link_pause"),
             InlineKeyboardButton("❌ CANCEL", callback_data="gen_link_cancel")]
        ]
        control_message = session.get("control_message")
        status_lock = session.get("status_lock")
        if control_message and status_lock:
            try:
                async with status_lock:
                    if USER_UPLOAD_SESSIONS.get(user_id) is session and session.get("status") == "recording":
                        current_count = len(session.get("source_messages", {}))
                        await control_message.edit_text(
                            "📥 **Multi-Message Upload Session**\n\n"
                            "✅ Messages queued in original send order.\n"
                            f"Queued: **{current_count}**\n\n"
                            "Tap **GENERATE LINK** after forwarding is complete.",
                            reply_markup=InlineKeyboardMarkup(buttons)
                        )
            except MessageNotModified:
                pass
            except Exception as exc:
                logger.warning("Could not update /getlink queue status: %s", exc)
        return

    # Single-file direct upload
    if message.media or message.document:
        copied = await bot.file_storage.store_message(message)
        if not copied:
            return await message.reply_text("❌ File could not be verified in the archive. Please retry.")
        link_hash = encode_message_id(copied.id)
        file_url = f"https://t.me/{bot.username}?start={link_hash}"
        await bot.state_store.update_stats(total_links=1, total_files=1)
        return await message.reply_text(
            f"✅ **File Stored Permanently!**\n\n"
            f"🔗 Access Link:\n{file_url}"
        )


# ------------------- CALLBACK QUERY HANDLERS ------------------- #

@bot.on_callback_query()
async def callback_router(client: Client, query: CallbackQuery):
    data = query.data
    user_id = query.from_user.id

    if data.startswith("adm_") and not is_admin(user_id):
        return await query.answer("⛔ Unauthorized.", show_alert=True)

    if data.startswith("try_again"):
        param = data.split("try_again_", 1)[1] if "_" in data else None
        unjoined = await check_fsub(client, user_id)
        if unjoined:
            return await query.answer("⚠️ You still haven't joined all required channels!", show_alert=True)
        await query.answer("✅ Verification passed!")
        await query.message.delete()
        if param:
            return await _deliver_start_payload(client, user_id, query.message, param)
        return await _reply_welcome(query.message, query.from_user.first_name)

    if data == "gen_link_finish":
        session = USER_UPLOAD_SESSIONS.get(user_id)
        if not session or not session.get("source_messages"):
            return await query.answer("No messages received yet!", show_alert=True)

        finish_lock = session.setdefault("finish_lock", asyncio.Lock())
        async with finish_lock:
            if USER_UPLOAD_SESSIONS.get(user_id) is not session:
                return await query.answer("This upload session has already finished.", show_alert=True)

            session["status"] = "processing"
            source_messages = sort_upload_messages(session.get("source_messages", {}).values())
            archived_by_source_id = session.setdefault("archived", {})
            failed_ids = []

            # Archive sequentially in the original private-chat message order.
            for source_message in source_messages:
                source_id = int(source_message.id)
                if source_id in archived_by_source_id:
                    continue
                try:
                    copied = await bot.file_storage.store_message(source_message)
                    if not copied:
                        raise RuntimeError("Archive copy could not be verified")
                    archived_by_source_id[source_id] = int(copied.id)
                except Exception:
                    logger.exception("Could not archive /getlink source message %s", source_id)
                    failed_ids.append(source_id)

            if failed_ids:
                session["status"] = "recording"
                return await query.answer(
                    f"{len(failed_ids)} message(s) could not be saved. Please tap GENERATE LINK to retry.",
                    show_alert=True,
                )

            mids = ordered_archive_message_ids(archived_by_source_id)
            if not mids:
                session["status"] = "recording"
                return await query.answer("No files were successfully saved.", show_alert=True)

            try:
                if len(mids) == 1:
                    code = encode_message_id(mids[0])
                    url = f"https://t.me/{bot.username}?start={code}"
                else:
                    manifest_id = await save_batch_manifest(bot, bot.archive_channel_id, mids)
                    code = encode_batch_manifest_id(manifest_id)
                    url = f"https://t.me/{bot.username}?start=batch_{code}"
            except Exception:
                session["status"] = "recording"
                logger.exception("Could not create upload-session link")
                return await query.answer(
                    "Could not save the link manifest. Your queued messages are safe; tap GENERATE LINK to retry.",
                    show_alert=True,
                )

            session["message_ids"] = mids
            del USER_UPLOAD_SESSIONS[user_id]
            await bot.state_store.update_stats(total_links=1, total_files=len(mids))
            await query.answer("Ordered batch link generated successfully!")
            await query.message.edit_text(
                f"🎉 **Batch Generated Successfully!**\n\n"
                f"• Count: `{len(mids)}` messages\n"
                f"• Order: **Preserved**\n"
                f"• Shareable Link: {url}"
            )

    elif data == "gen_link_pause":
        session = USER_UPLOAD_SESSIONS.get(user_id)
        if session and session.get("status") == "processing":
            return await query.answer("The batch is being saved. Please wait for it to finish.", show_alert=True)
        if session:
            session["status"] = "paused"
            await query.answer("Upload session paused.")
            buttons = [
                [InlineKeyboardButton("▶️ RESUME", callback_data="gen_link_resume")],
                [InlineKeyboardButton("✅ GENERATE LINK", callback_data="gen_link_finish"),
                 InlineKeyboardButton("❌ CANCEL", callback_data="gen_link_cancel")]
            ]
            await query.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(buttons))

    elif data == "gen_link_resume":
        session = USER_UPLOAD_SESSIONS.get(user_id)
        if session and session.get("status") == "processing":
            return await query.answer("The batch is being saved. Please wait for it to finish.", show_alert=True)
        if session:
            session["status"] = "recording"
            await query.answer("Upload session resumed.")
            buttons = [
                [InlineKeyboardButton("✅ GENERATE LINK", callback_data="gen_link_finish")],
                [InlineKeyboardButton("⏸ PAUSE", callback_data="gen_link_pause"),
                 InlineKeyboardButton("❌ CANCEL", callback_data="gen_link_cancel")]
            ]
            await query.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(buttons))

    elif data == "gen_link_cancel":
        session = USER_UPLOAD_SESSIONS.get(user_id)
        if session and session.get("status") == "processing":
            return await query.answer("The batch is being saved. Please wait for it to finish.", show_alert=True)
        if session:
            del USER_UPLOAD_SESSIONS[user_id]
        await query.answer("Upload session cancelled.")
        await query.message.edit_text("❌ Upload session cancelled.")

    elif data == "adm_status":
        await query.answer()
        stats = bot.state_store.get_stats()
        text = (
            f"📊 **Current Statistics**\n\n"
            f"• Users: `{stats.get('total_users', 0)}`\n"
            f"• Links: `{stats.get('total_links', 0)}`\n"
            f"• Files: `{stats.get('total_files', 0)}`\n"
            f"• Views: `{stats.get('total_views', 0)}`"
        )
        try:
            await query.message.edit_text(
                text,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="adm_back")]])
            )
        except MessageNotModified:
            pass

    elif data == "adm_edit_welcome":
        ADMIN_PENDING_ACTIONS[user_id] = "welcome"
        await query.answer()
        await query.message.edit_text(
            "📝 Send welcome text, or send a photo with its caption. Use {user} to insert the user's first name. Send /cancel to abort.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="adm_back")]])
        )

    elif data == "adm_broadcast":
        await query.answer()
        await query.message.edit_text(
            "📬 Reply to the message you want to broadcast, then send `/broadcast` in that chat.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="adm_back")]])
        )

    elif data == "adm_back":
        ADMIN_PENDING_ACTIONS.pop(user_id, None)
        ADMIN_PENDING_DATA.pop(user_id, None)
        await query.answer()
        stats = bot.state_store.get_stats()
        text = (
            "⚙️ **ANIFLIX Admin Dashboard**\n\n"
            f"👥 Total Users: `{stats.get('total_users', 0)}`\n"
            f"🔗 Generated Links: `{stats.get('total_links', 0)}`\n"
            f"📁 Stored Files: `{stats.get('total_files', 0)}`\n"
            f"👁️ Views/Retrievals: `{stats.get('total_views', 0)}`\n"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 FSUB Settings", callback_data="adm_fsub"),
             InlineKeyboardButton("📊 Bot Status", callback_data="adm_status")],
            [InlineKeyboardButton("📝 Edit Welcome", callback_data="adm_edit_welcome"),
             InlineKeyboardButton("📬 Broadcast", callback_data="adm_broadcast")]
        ])
        await query.message.edit_text(text, reply_markup=kb)

    elif data == "adm_fsub":
        ADMIN_PENDING_ACTIONS.pop(user_id, None)
        ADMIN_PENDING_DATA.pop(user_id, None)
        await query.answer()
        channels = bot.state_store.get_fsub_channels()
        channel_lines = []
        for index, channel in enumerate(channels, start=1):
            channel_id = int(channel.get("id") or 0)
            counts = bot.state_store.get_fsub_counts(channel_id)
            is_request = bool(channel.get("join_request") or channel.get("type") == "join_request")
            if is_request:
                count_text = f"Requests: {counts['requests']} | Accepted: {counts['accepted']}"
                method = "JOIN REQUEST"
            else:
                count_text = f"Verified joined: {counts['joined']}"
                method = "NORMAL JOIN"
            channel_lines.append(
                f"{index}. **{channel.get('name', 'Channel')}** (`{channel.get('id') or channel.get('username')}`) — {method}\n   {count_text}"
            )
        ch_list = "\n\n".join(channel_lines) if channel_lines else "No force-subscription channels added yet."
        await query.message.edit_text(
            f"📢 **Force Subscribe Channels (Max 10)**:\n\n{ch_list}\n\n"
            "Counts include only unique users confirmed by Telegram updates or membership checks. "
            "Updates missed while the bot is not an admin cannot be reconstructed reliably.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Add Channel", callback_data="adm_fsub_add"),
                 InlineKeyboardButton("➖ Remove Channel", callback_data="adm_fsub_remove")],
                [InlineKeyboardButton("📝 Edit FSUB Message", callback_data="adm_fsub_message")],
                [InlineKeyboardButton("🧹 Clear Channel Counts", callback_data="adm_fsub_clear")],
                [InlineKeyboardButton("🔙 Back", callback_data="adm_back")],
            ])
        )

    elif data == "adm_fsub_clear":
        channels = bot.state_store.get_fsub_channels()
        if not channels:
            return await query.answer("No FSUB channels are configured.", show_alert=True)
        ADMIN_PENDING_ACTIONS[user_id] = "fsub_clear_counts"
        ADMIN_PENDING_DATA.pop(user_id, None)
        await query.answer()
        options = []
        for index, channel in enumerate(channels, start=1):
            method = "JOIN REQUEST" if channel.get("join_request") or channel.get("type") == "join_request" else "NORMAL JOIN"
            options.append(f"{index}. {channel.get('name', 'Channel')} — {method}")
        await query.message.reply_text(
            "Send the number of the channel whose counts you want to clear:\n\n" + "\n".join(options) + "\n\nSend /cancel to abort."
        )

    elif data == "adm_fsub_clear_confirm":
        pending = ADMIN_PENDING_DATA.pop(user_id, {})
        channel_id = pending.get("fsub_clear_channel_id")
        if not channel_id:
            return await query.answer("Clear action expired. Try again.", show_alert=True)
        await bot.state_store.clear_fsub_counts(int(channel_id))
        await query.answer("Channel counts cleared.", show_alert=True)
        await query.message.edit_text(
            "✅ FSUB statistics cleared. Saved join-request access records were preserved.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 FSUB Settings", callback_data="adm_fsub")]])
        )

    elif data == "adm_fsub_clear_cancel":
        ADMIN_PENDING_DATA.pop(user_id, None)
        ADMIN_PENDING_ACTIONS.pop(user_id, None)
        await query.answer("Cancelled.")
        await query.message.edit_text(
            "❌ Count clearing cancelled.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 FSUB Settings", callback_data="adm_fsub")]])
        )

    elif data == "adm_fsub_add":
        if len(bot.state_store.get_fsub_channels()) >= 10:
            return await query.answer("Maximum 10 FSUB channels are allowed.", show_alert=True)
        ADMIN_PENDING_ACTIONS[user_id] = "fsub_add_forward"
        ADMIN_PENDING_DATA.pop(user_id, None)
        await query.answer()
        await query.message.edit_text(
            "➕ **ADD FORCE-SUB CHANNEL**\n\n"
            "1. Add me to your channel as an administrator.\n"
            "2. Give me **Invite Users via Link** permission.\n"
            "3. Forward any message from that channel here.\n\n"
            "I will verify my admin permission, then ask you to choose JOIN REQUEST or NORMAL JOIN.\n"
            "Use /cancel to abort.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="adm_fsub")]])
        )

    elif data in {"adm_fsub_method_request", "adm_fsub_method_normal"}:
        pending = ADMIN_PENDING_DATA.get(user_id)
        if ADMIN_PENDING_ACTIONS.get(user_id) != "fsub_choose_method" or not pending:
            return await query.answer("The channel setup expired. Start Add Channel again.", show_alert=True)
        join_request = data == "adm_fsub_method_request"
        channel_id = int(pending["id"])
        try:
            await _verify_bot_can_manage_invites(client, channel_id)
            invite = await client.create_chat_invite_link(
                chat_id=channel_id,
                creates_join_request=join_request,
            )
            channels = list(bot.state_store.get_fsub_channels())
            if len(channels) >= 10:
                raise ValueError("The maximum of 10 FSUB channels is already configured.")
            if any(int(item.get("id", 0) or 0) == channel_id for item in channels):
                raise ValueError("This channel is already in the FSUB list.")
            channels.append({
                "id": channel_id,
                "name": pending.get("name") or "Channel",
                "username": pending.get("username"),
                "link": invite.invite_link,
                "type": "join_request" if join_request else "normal",
                "join_request": join_request,
            })
            await bot.state_store.set_fsub_channels(channels)
        except Exception as exc:
            logger.exception("Could not complete FSUB channel setup")
            return await query.answer(f"Could not add channel: {exc}", show_alert=True)

        ADMIN_PENDING_ACTIONS.pop(user_id, None)
        ADMIN_PENDING_DATA.pop(user_id, None)
        await query.answer("Force-subscription channel added successfully.")
        await query.message.edit_text(
            f"✅ **FORCE SUBSCRIPTION ADDED**\n\n"
            f"Channel: **{pending.get('name') or 'Channel'}**\n"
            f"Channel ID: `{channel_id}`\n"
            f"Method: **{'JOIN REQUEST' if join_request else 'NORMAL JOIN'}**\n"
            f"Invite link created automatically.\n\n"
            "For JOIN REQUEST, users can press the channel button and submit their request. "
            "Once Telegram sends the bot that request, TRY AGAIN will verify it and unlock "
            "the file even while approval is pending. The bot never auto-approves requests.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📋 View FSUB Channels", callback_data="adm_fsub")],
                [InlineKeyboardButton("🔙 Admin Panel", callback_data="adm_back")],
            ])
        )

    elif data == "adm_fsub_remove":
        ADMIN_PENDING_DATA.pop(user_id, None)
        if not bot.state_store.get_fsub_channels():
            return await query.answer("No FSUB channels are configured.", show_alert=True)
        ADMIN_PENDING_ACTIONS[user_id] = "fsub_remove"
        await query.answer()
        await query.message.edit_text(
            "Send the 1-based number of the channel to remove, or /cancel to abort.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="adm_fsub")]])
        )

    elif data == "adm_fsub_message":
        ADMIN_PENDING_ACTIONS[user_id] = "fsub_message"
        await query.answer()
        await query.message.edit_text(
            "📝 Send the FSUB prompt as text, or send a photo with its caption. Send /cancel to abort.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="adm_fsub")]])
        )


def register_handlers(app: Client):
    """Compatibility export for main.py."""
    pass


if __name__ == "__main__":
    bot.run()
