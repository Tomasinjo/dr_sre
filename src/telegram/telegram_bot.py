"""Telegram interface for the SRE agent: reports out, instructions in.

Requires two env vars:

- TELEGRAM_BOT_TOKEN — token from @BotFather
- TELEGRAM_CHAT_ID — the chat that receives reports and may command the agent;
  messages from any other chat are ignored

Messages over Telegram's 4096-char cap are split at newline boundaries.
Importing this module is side-effect free; config is checked at first use.
"""

import os
from collections.abc import Awaitable, Callable

from telegram import Bot, Update
from telegram.constants import ChatAction, MessageLimit
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

HELP_TEXT = (
    "SRE monitoring agent at your service.\n\n"
    "/report — run the log sweep and post the analysis now\n"
    "Anything else is a prompt to the agent, with full report context:\n"
    "• Silence: `Silence error <regex pattern>`\n"
    "• Fix: `Fix <error name> in <file_path> with code: ...`"
)

_bot: Bot | None = None


def _require_config() -> tuple[str, str]:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError(
            "Set TELEGRAM_BOT_TOKEN (use BotFather) and TELEGRAM_CHAT_ID "
        )
    return token, chat_id


def split_message(text: str, limit: int = MessageLimit.MAX_TEXT_LENGTH) -> list[str]:
    """Split text into chunks of at most `limit` chars, cutting at newlines when possible."""
    chunks: list[str] = []
    while text:
        cut = min(len(text), limit)
        if cut < len(text):
            newline = text.rfind("\n", 0, cut)
            if newline > 0:
                cut = newline + 1
        chunks.append(text[:cut])
        text = text[cut:]
    return chunks


async def send(text: str) -> None:
    """Send text to the configured chat; used for scheduled and --once reports."""
    global _bot
    token, chat_id = _require_config()
    if _bot is None:
        _bot = Bot(token)
        await _bot.initialize()
    for chunk in split_message(text):
        await _bot.send_message(chat_id=chat_id, text=chunk)


async def close() -> None:
    global _bot
    if _bot is not None:
        await _bot.shutdown()
        _bot = None


def build_application(
    on_message: Callable[[str], Awaitable[str]],
    on_report: Callable[[], Awaitable[str]],
) -> Application:
    """Wire the polling bot: plain text goes to `on_message`, /report to `on_report`.

    Both callbacks return the reply text; exceptions are caught and reported
    back to the chat instead of killing the handler.
    """
    token, chat_id = _require_config()

    def from_owner(update: Update) -> bool:
        chat = update.effective_chat
        return chat is not None and str(chat.id) == chat_id

    async def answer(update: Update, text: str) -> None:
        for chunk in split_message(text):
            await update.effective_message.reply_text(chunk)

    async def typing(context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except Exception:
            pass  # cosmetic; don't let it kill the reply

    async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not from_owner(update):
            return
        text = (update.effective_message.text or "").strip()
        if not text:
            return
        await typing(context)
        try:
            reply = await on_message(text)
        except Exception as e:
            reply = f"Agent failed: {e}"
        await answer(update, reply)

    async def handle_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not from_owner(update):
            return
        await typing(context)
        try:
            reply = await on_report()
        except Exception as e:
            reply = f"Report failed: {e}"
        await answer(update, reply)

    async def handle_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if from_owner(update):
            await answer(update, HELP_TEXT)

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("report", handle_report))
    app.add_handler(CommandHandler(["start", "help"], handle_help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    return app


async def start_polling(
    on_message: Callable[[str], Awaitable[str]],
    on_report: Callable[[], Awaitable[str]],
) -> Application:
    """Start the polling bot on the current event loop; caller keeps it alive."""
    app = build_application(on_message, on_report)
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    return app


async def stop_polling(app: Application) -> None:
    if app.updater:
        await app.updater.stop()
    await app.stop()
    await app.shutdown()
