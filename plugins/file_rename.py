
@Client.on_message(filters.private & (filters.text | filters.caption), group=-7)
async def batch_completed_command(client: Client, message):
    """Publish both saved post types; supports a photo attached to the command."""

    # A photo command stores its command text in caption, not text.
    message_text = message.text or message.caption or ""

    if not re.match(
        r"^\s*/batchcompleted(?:@\w+)?(?:\s|$)",
        message_text,
        re.IGNORECASE,
    ):
        return

    user = getattr(message, "from_user", None)
    if not user or not _is_batch_admin(user):
        return await message.reply_text("⛔ Owner/admin only.")

    try:
        details = _parse_batchcompleted_payload(message_text)
    except ValueError as exc:
        return await message.reply_text(
            f"❌ {exc}",
            parse_mode=enums.ParseMode.DISABLED,
        )

    # Pyrogram Message.photo is a list of PhotoSize objects.
    # Select the largest available photo size.
    poster = None
    photo_sizes = getattr(message, "photo", None)

    if photo_sizes:
        try:
            poster = photo_sizes[-1].file_id
        except (AttributeError, IndexError, TypeError):
            poster = None

    # Also support a photo attached to a replied-to message.
    if not poster:
        reply_photo = getattr(message, "reply_to_message", None)
        reply_photo_sizes = getattr(reply_photo, "photo", None)

        if reply_photo_sizes:
            try:
                poster = reply_photo_sizes[-1].file_id
            except (AttributeError, IndexError, TypeError):
                poster = None

    logger.info(
        "/batchcompleted received for %s; poster attached: %s",
        details.get("title"),
        bool(poster),
    )

    await _publish_batchcompleted(
        client,
        message,
        details,
        poster_file_id=poster,
    )
