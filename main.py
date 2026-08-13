"""
Telegram Bot — Bulk Google One Link Checker (Customer Edition)
===============================================================
Professional customer-facing bot that checks Google One promo links.
Requires users to join a Telegram channel before use.
"""

import os
import re
import json
import asyncio
import logging
import threading

import telebot
from telebot import types
from flask import Flask
from playwright.async_api import async_playwright

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    logger.critical("BOT_TOKEN environment variable is not set. Exiting.")
    raise SystemExit("BOT_TOKEN is missing.")

# Channel that users must join before using the bot
REQUIRED_CHANNEL = "@samsshopofficial"

bot = telebot.TeleBot(BOT_TOKEN)

# ---------------------------------------------------------------------------
# Active task tracking (cancel support)
# ---------------------------------------------------------------------------
active_tasks: dict[int, dict] = {}

# ---------------------------------------------------------------------------
# Flask health-check server (Render requirement)
# ---------------------------------------------------------------------------
app = Flask(__name__)


@app.route("/")
def health_check():
    return "Bot is active and running!", 200


def keep_alive():
    port = int(os.environ.get("PORT", 10000))
    logger.info("Flask health-check server starting on port %s", port)
    app.run(host="0.0.0.0", port=port)


# ---------------------------------------------------------------------------
# Channel membership check
# ---------------------------------------------------------------------------
def is_channel_member(user_id: int) -> bool:
    """Check if user is a member of the required channel."""
    try:
        member = bot.get_chat_member(REQUIRED_CHANNEL, user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception as e:
        logger.warning("Channel check failed for %s: %s", user_id, e)
        return False


def send_join_message(message: telebot.types.Message):
    """Send a styled message asking user to join the channel."""
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton(
            "📢 Join Channel", url=f"https://t.me/{REQUIRED_CHANNEL.lstrip('@')}"
        ),
        types.InlineKeyboardButton(
            "✅ I've Joined", callback_data="check_membership"
        ),
    )
    bot.reply_to(
        message,
        "🔒 *Access Required*\n\n"
        f"To use this bot, you need to join our channel first:\n"
        f"👉 {REQUIRED_CHANNEL}\n\n"
        "After joining, tap the button below to verify.",
        parse_mode="Markdown",
        reply_markup=markup,
    )


# ---------------------------------------------------------------------------
# URL extraction helper
# ---------------------------------------------------------------------------
URL_REGEX = re.compile(r"https?://[^\s<>\"']+")


def extract_urls(text: str) -> list[str]:
    """Return de-duplicated URLs found in *text*, preserving first-seen order."""
    seen = set()
    urls = []
    for match in URL_REGEX.findall(text):
        if match not in seen:
            seen.add(match)
            urls.append(match)
    return urls


# ---------------------------------------------------------------------------
# Playwright link-checking engine (async, parallel)
# ---------------------------------------------------------------------------

USED_PHRASES = [
    "الاشتراك قيد الاستخدام",
    "سبق أن تم استخدام",
    "This promotion has already been redeemed",
    "Subscription already in use",
    "已兌換此促銷活動",
    "已有人使用這個促銷優惠",
    "已有人兌換這個促銷",
    "此促销活动已被兑换",
]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

SAME_SITE_MAP = {
    "no_restriction": "None",
    "lax": "Lax",
    "strict": "Strict",
    "unspecified": "Lax",
}

CONCURRENCY = 5
LINK_TIMEOUT = 50
PROGRESS_INTERVAL = 10


def convert_cookies_to_playwright(cookie_editor_path: str) -> dict:
    """Convert a Cookie-Editor JSON export to Playwright storage_state format."""
    with open(cookie_editor_path, "r", encoding="utf-8") as fh:
        raw_cookies = json.load(fh)

    pw_cookies = []
    for c in raw_cookies:
        expires = c.get("expirationDate", -1)
        if c.get("session", False) and not expires:
            expires = -1
        pw_cookies.append({
            "name": c["name"],
            "value": c["value"],
            "domain": c["domain"],
            "path": c.get("path", "/"),
            "expires": expires,
            "httpOnly": c.get("httpOnly", False),
            "secure": c.get("secure", False),
            "sameSite": SAME_SITE_MAP.get(
                (c.get("sameSite") or "").lower(), "Lax"
            ),
        })
    return {"cookies": pw_cookies, "origins": []}


async def _process_single_link(
    context, url: str, idx: int, total: int,
) -> tuple[str, str]:
    """Check one link. Returns (url, 'FRESH'|'USED'|'ERROR')."""
    page = await context.new_page()
    try:
        logger.info("[%d/%d] Checking: %s", idx, total, url)
        await page.goto(url, timeout=45000)
        await page.wait_for_timeout(2000)

        # Login wall detection (URL-based)
        if "accounts.google.com" in page.url:
            logger.warning("  → LOGIN WALL")
            return (url, "USED")

        # Content-based detection
        content = await page.content()
        for phrase in USED_PHRASES:
            if phrase in content:
                logger.info("  → USED (matched: %s)", phrase)
                return (url, "USED")

        logger.info("  → FRESH ✓")
        return (url, "FRESH")

    except Exception as exc:
        logger.error("  → ERROR: %s", exc)
        return (url, "ERROR")
    finally:
        try:
            await page.close()
        except Exception:
            pass


async def _check_links_async(
    urls: list[str],
    storage: dict,
    message: telebot.types.Message,
) -> dict:
    """Async engine: checks links in parallel."""
    chat_id = message.chat.id
    fresh, used, skipped = [], [], []
    completed = 0
    total = len(urls)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            storage_state=storage,
            locale="ar-EG",
            user_agent=USER_AGENT,
        )

        semaphore = asyncio.Semaphore(CONCURRENCY)

        async def process_with_guard(url: str, idx: int):
            nonlocal completed

            task_info = active_tasks.get(chat_id)
            if task_info and task_info["cancelled"]:
                return

            async with semaphore:
                task_info = active_tasks.get(chat_id)
                if task_info and task_info["cancelled"]:
                    return

                try:
                    result_url, status = await asyncio.wait_for(
                        _process_single_link(context, url, idx, total),
                        timeout=LINK_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    logger.error("[%d/%d] TIMEOUT: %s", idx, total, url)
                    result_url, status = url, "ERROR"

                if status == "FRESH":
                    fresh.append(result_url)
                elif status == "USED":
                    used.append(result_url)
                else:
                    skipped.append(result_url)

                completed += 1

                if (
                    total > PROGRESS_INTERVAL
                    and completed % PROGRESS_INTERVAL == 0
                    and completed < total
                ):
                    pct = int(completed / total * 100)
                    bar_filled = int(pct / 5)
                    bar = "▓" * bar_filled + "░" * (20 - bar_filled)
                    try:
                        bot.send_message(
                            chat_id,
                            f"⏳ *Checking in progress...*\n"
                            f"`{bar}` {pct}%\n"
                            f"📊 {completed}/{total} links\n"
                            f"✅ {len(fresh)} fresh  •  ❌ {len(used)} used",
                            parse_mode="Markdown",
                        )
                    except Exception:
                        pass

        tasks = [process_with_guard(url, idx) for idx, url in enumerate(urls, 1)]
        await asyncio.gather(*tasks)

        await context.close()
        await browser.close()

    return {"fresh": fresh, "used": used, "skipped": skipped}


def check_links(urls: list[str], message: telebot.types.Message) -> dict:
    """Synchronous wrapper for the async engine."""
    chat_id = message.chat.id

    if not os.path.exists("cookies.json"):
        bot.reply_to(message, "⚠️ Service temporarily unavailable. Contact admin.")
        return {"fresh": [], "used": [], "skipped": []}

    try:
        storage = convert_cookies_to_playwright("cookies.json")
    except Exception:
        bot.reply_to(message, "⚠️ Service temporarily unavailable. Contact admin.")
        return {"fresh": [], "used": [], "skipped": []}

    active_tasks[chat_id] = {"cancelled": False}

    # Send cancel button with styled message
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton(
        "🛑 Cancel", callback_data=f"cancel_{chat_id}"
    ))
    bot.send_message(
        chat_id,
        f"🔍 *Checking {len(urls)} links...*\n"
        f"⚡ Processing {CONCURRENCY} links simultaneously\n\n"
        f"_You can cancel anytime using the button below._",
        parse_mode="Markdown",
        reply_markup=markup,
    )

    try:
        results = asyncio.run(_check_links_async(urls, storage, message))
    finally:
        active_tasks.pop(chat_id, None)

    return results


# ---------------------------------------------------------------------------
# Result reporting
# ---------------------------------------------------------------------------
def send_results(message: telebot.types.Message, results: dict):
    """Send formatted results to user."""
    fresh = results["fresh"]
    used = results["used"]
    skipped = results.get("skipped", [])
    total = len(fresh) + len(used) + len(skipped)

    if total == 0:
        bot.reply_to(message, "⚠️ No links were checked.")
        return

    # Build styled summary
    fresh_pct = int(len(fresh) / total * 100) if total else 0
    summary = (
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"  📊  *CHECK COMPLETE*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"  🔗  Total links: *{total}*\n"
        f"  ✅  Fresh: *{len(fresh)}* ({fresh_pct}%)\n"
        f"  ❌  Used: *{len(used)}*\n"
    )
    if skipped:
        summary += f"  ⚠️  Skipped: *{len(skipped)}*\n"

    summary += f"\n━━━━━━━━━━━━━━━━━━━━"

    bot.reply_to(message, summary, parse_mode="Markdown")
    logger.info("Results — Fresh: %d | Used: %d | Skipped: %d", len(fresh), len(used), len(skipped))

    if fresh:
        result_path = "fresh_links_result.txt"
        with open(result_path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(fresh))
        with open(result_path, "rb") as doc:
            bot.send_document(
                message.chat.id,
                doc,
                caption=f"✅ {len(fresh)} fresh links • Ready to use",
            )


# ---------------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------------
@bot.message_handler(commands=["start", "help"])
def handle_start(message: telebot.types.Message):
    """Welcome message with styled buttons."""
    if not is_channel_member(message.from_user.id):
        send_join_message(message)
        return

    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=False)
    markup.row("📋 How to Use", "📊 Status")

    bot.reply_to(
        message,
        "━━━━━━━━━━━━━━━━━━━━\n"
        "  🔍  *SAMS Link Checker*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Check Google One promo links\n"
        "instantly to find fresh ones.\n\n"
        "📌 *How to use:*\n"
        "├ Paste links in chat (one per line)\n"
        "├ Or upload a `.txt` file\n"
        "└ Any number of links accepted\n\n"
        "⚡ Fast parallel checking\n"
        "🛑 Cancel anytime\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"  📢 Channel: {REQUIRED_CHANNEL}\n"
        "━━━━━━━━━━━━━━━━━━━━",
        parse_mode="Markdown",
        reply_markup=markup,
    )


@bot.message_handler(func=lambda m: m.text == "📋 How to Use")
def handle_how_to(message: telebot.types.Message):
    """How to use guide."""
    bot.reply_to(
        message,
        "📋 *How to Check Links:*\n\n"
        "*Option 1 — Paste directly:*\n"
        "Copy your links and paste them in this chat.\n"
        "One link per line.\n\n"
        "*Option 2 — Upload file:*\n"
        "Send a `.txt` file containing your links.\n"
        "One link per line.\n\n"
        "The bot will check all links and send back\n"
        "the fresh ones as a file. ✅",
        parse_mode="Markdown",
    )


@bot.message_handler(func=lambda m: m.text == "📊 Status")
def handle_status(message: telebot.types.Message):
    """Bot status."""
    chat_id = message.chat.id
    if chat_id in active_tasks:
        bot.reply_to(message, "🔄 A check is currently running...")
    else:
        bot.reply_to(message, "✅ Bot is ready. Send links to check.")


@bot.callback_query_handler(func=lambda call: call.data == "check_membership")
def handle_membership_check(call: types.CallbackQuery):
    """Verify channel membership after user clicks 'I've Joined'."""
    if is_channel_member(call.from_user.id):
        bot.answer_callback_query(call.id, "✅ Verified! You can now use the bot.")
        bot.edit_message_text(
            "✅ *Access Granted!*\n\n"
            "Send /start to begin using the bot.",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            parse_mode="Markdown",
        )
    else:
        bot.answer_callback_query(
            call.id,
            "❌ You haven't joined the channel yet. Please join first.",
            show_alert=True,
        )


@bot.callback_query_handler(func=lambda call: call.data.startswith("cancel_"))
def handle_cancel(call: types.CallbackQuery):
    """Handle cancel button press."""
    chat_id = int(call.data.split("_", 1)[1])
    task_info = active_tasks.get(chat_id)
    if task_info:
        task_info["cancelled"] = True
        bot.answer_callback_query(call.id, "🛑 Cancelling...")
        bot.edit_message_text(
            "🛑 *Cancelling...* Finishing current links.",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            parse_mode="Markdown",
        )
    else:
        bot.answer_callback_query(call.id, "No active check to cancel.")


@bot.message_handler(content_types=["document"])
def handle_document(message: telebot.types.Message):
    """Accept a .txt file upload."""
    if not is_channel_member(message.from_user.id):
        send_join_message(message)
        return

    chat_id = message.chat.id
    if chat_id in active_tasks:
        bot.reply_to(message, "⚠️ A check is already running. Cancel it first or wait.")
        return

    doc = message.document
    if not doc.file_name.endswith(".txt"):
        bot.reply_to(message, "⚠️ Please upload a `.txt` file.", parse_mode="Markdown")
        return

    file_info = bot.get_file(doc.file_id)
    downloaded = bot.download_file(file_info.file_path)
    text = downloaded.decode("utf-8", errors="ignore")
    urls = extract_urls(text)

    if not urls:
        bot.reply_to(message, "❌ No valid URLs found in the file.")
        return

    bot.reply_to(message, f"📥 *{len(urls)} links received.*", parse_mode="Markdown")
    logger.info("Received %d links from document (chat %s)", len(urls), chat_id)

    results = check_links(urls, message)
    send_results(message, results)


@bot.message_handler(content_types=["text"])
def handle_text(message: telebot.types.Message):
    """Extract URLs from text messages."""
    if not is_channel_member(message.from_user.id):
        send_join_message(message)
        return

    chat_id = message.chat.id
    if chat_id in active_tasks:
        bot.reply_to(message, "⚠️ A check is already running. Cancel it first or wait.")
        return

    urls = extract_urls(message.text)
    if not urls:
        bot.reply_to(message, "❌ No valid URLs detected. Send links or upload a `.txt` file.", parse_mode="Markdown")
        return

    bot.reply_to(message, f"📥 *{len(urls)} links received.*", parse_mode="Markdown")
    logger.info("Received %d links from text (chat %s)", len(urls), chat_id)

    results = check_links(urls, message)
    send_results(message, results)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    server_thread = threading.Thread(target=keep_alive, daemon=True)
    server_thread.start()
    logger.info("Health-check thread started.")

    logger.info("Starting Telegram bot polling…")
    bot.infinity_polling(logger_level=logging.INFO)
