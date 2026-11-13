"""Application entry point for the Telegram auto-rename bot."""

import logging
import time
from datetime import timedelta

from aiohttp import web
from pyrogram import Client, __version__
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import Config
from route import web_server

logger = logging.getLogger("autorename")

PORT = Config.PORT


class Bot(Client):
    def __init__(self):
        super().__init__(
            name="aniflix_auto_rename",
            api_id=Config.API_ID,
            api_hash=Config.API_HASH,
            bot_token=Config.BOT_TOKEN,
            workers=32,
            plugins={"root": "plugins"},
            sleep_threshold=15,
            max_concurrent_transmissions=2,
        )
        self.start_time = time.time()

    async def start(self, *args, **kwargs):
        await super().start(*args, **kwargs)
        me = await self.get_me()
        self.mention = me.mention
        self.username = me.username
        self.uptime = Config.BOT_UPTIME

        if Config.WEBHOOK:
            app = web.AppRunner(await web_server())
            await app.setup()
            await web.TCPSite(app, "0.0.0.0", PORT).start()
            self._web_runner = app

        logger.info("%s started successfully (Pyrogram %s)", me.first_name, __version__)

        elapsed = str(timedelta(seconds=max(0, int(time.time() - self.start_time))))
        targets = [chat_id for chat_id in (Config.LOG_CHANNEL, Config.SUPPORT_CHAT) if chat_id is not None]
        for chat_id in dict.fromkeys(targets):
            try:
                if Config.START_PIC:
                    await self.send_photo(
                        chat_id=chat_id,
                        photo=Config.START_PIC,
                        caption=f"**ANIFLIX Auto-Rename Bot is online**\n\nUptime: `{elapsed}`",
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton("Updates", url="https://t.me/ANIFLIXANIMETAMIL")
                        ]]),
                    )
                else:
                    await self.send_message(
                        chat_id=chat_id,
                        text=f"**ANIFLIX Auto-Rename Bot is online**\n\nUptime: `{elapsed}`",
                    )
            except Exception:
                logger.exception("Could not send startup notification to chat %s", chat_id)

    async def stop(self, *args, **kwargs):
        try:
            runner = getattr(self, "_web_runner", None)
            if runner is not None:
                await runner.cleanup()
        finally:
            await super().stop(*args, **kwargs)


def validate_config() -> None:
    missing = []
    if not Config.BOT_TOKEN:
        missing.append("BOT_TOKEN")
    if Config.API_ID <= 0:
        missing.append("API_ID (must be a positive integer)")
    if not Config.API_HASH:
        missing.append("API_HASH")
    if not Config.DB_URL.startswith(("mongodb://", "mongodb+srv://")):
        missing.append("DB_URL (must start with mongodb:// or mongodb+srv://)")

    if missing:
        raise RuntimeError(
            "Missing required environment variable(s): " + ", ".join(missing)
            + ". Set them in Render Dashboard > Environment."
        )


def main() -> None:
    validate_config()
    Bot().run()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    main()
