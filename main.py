"""
Telegram Bot — Bulk Google One Link Checker
============================================
Uses Playwright (headless Chromium) to visit Google One promo links,
bypass the login wall via a pre-exported session state (cookies.json),
and determine whether each link is FRESH or USED based on DOM text.

A lightweight Flask health-check server runs in a daemon thread so the
Render Web Service stays alive and passes port-binding checks.
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
# Telegram Bot initialisation
# ---------------------------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    logger.critical("BOT_TOKEN environment variable is not set. Exiting.")
    raise SystemExit("BOT_TOKEN is missing.")

bot = telebot.TeleBot(BOT_TOKEN)

# ---------------------------------------------------------------------------
# Active task tracking (cancel support)
# ---------------------------------------------------------------------------
# Maps chat_id -> {"cancelled": bool}
active_tasks: dict[int, dict] = {}

# ---------------------------------------------------------------------------
# Flask health-check server (Render requirement)
# ---------------------------------------------------------------------------
app = Flask(__name__)


@app.route("/")
def health_check():
    """Return HTTP 200 so Render's health probe is satisfied."""
    return "Bot is active and running!", 200


def keep_alive():
    """Run the Flask app on the Render-assigned PORT (default 10000)."""
    port = int(os.environ.get("PORT", 10000))
    logger.info("Flask health-check server starting on port %s", port)
    app.run(host="0.0.0.0", port=port)


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

# Phrases indicating a used/claimed link (multi-language)
USED_PHRASES = [
    # Arabic
    "الاشتراك قيد الاستخدام",
    "سبق أن تم استخدام",
    "لا يمكنك استرداد هذا الرابط",
    # English
    "This promotion has already been redeemed",
    "Subscription already in use",
    "You need a new activation link",
    # Chinese Traditional
    "已兌換此促銷活動",
    "已有人使用這個促銷優惠",
    "已有人兌換這個促銷",
    "您無法兌換此連結",
    # Chinese Simplified
    "此促销活动已被兑换",
    "无法兑换此链接",
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

# How many links to check in parallel
CONCURRENCY = 5

# Hard timeout per single link (seconds) — prevents stuck links
LINK_TIMEOUT = 20

# How often to send a progress update (every N links)
PROGRESS_INTERVAL = 10


def convert_cookies_to_playwright(cookie_editor_path: str) -> dict:
    """
    Convert a Cookie-Editor JSON export into the Playwright storage_state
    format that ``browser.new_context(storage_state=...)`` expects.
    """
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
    """
    Check one link in its own page. Returns (url, "FRESH"|"USED"|"ERROR").
    Has a hard timeout so it never hangs.
    """
    page = await context.new_page()
    try:
        logger.info("[%d/%d] Checking: %s", idx, total, url)
        await page.goto(url, timeout=15000)
        await page.wait_for_timeout(2000)

        # Login wall detection (URL-based — reliable)
        current_url = page.url
        if "accounts.google.com" in current_url:
            logger.warning("  → LOGIN WALL (redirected)")
            return (url, "USED")

        # Content-based used/fresh detection
        content = await page.content()
        if any(phrase in content for phrase in USED_PHRASES):
            logger.info("  → INVALID / USED")
            return (url, "USED")
        else:
            logger.info("  → FRESH")
            return (url, "FRESH")

    except Exception as exc:
        logger.error("  → ERROR: %s", exc)
        return (url, "USED")
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
    """
    Async engine: opens CONCURRENCY pages in parallel to check links fast.
    Supports cancellation via active_tasks flag.
    """
    chat_id = message.chat.id
    fresh: list[str] = []
    used: list[str] = []
    first_done = False
    completed = 0
    total = len(urls)
    lock = asyncio.Lock()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            storage_state=storage,
            locale="ar-EG",
            user_agent=USER_AGENT,
        )

        semaphore = asyncio.Semaphore(CONCURRENCY)

        async def process_with_guard(url: str, idx: int):
            nonlocal first_done, completed

            # Check for cancellation
            task_info = active_tasks.get(chat_id)
            if task_info and task_info["cancelled"]:
                return

            async with semaphore:
                # Check again after acquiring semaphore
                task_info = active_tasks.get(chat_id)
                if task_info and task_info["cancelled"]:
                    return

                try:
                    # Hard timeout per link to prevent hanging
                    result_url, status = await asyncio.wait_for(
                        _process_single_link(context, url, idx, total),
                        timeout=LINK_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    logger.error("[%d/%d] TIMEOUT after %ds: %s", idx, total, LINK_TIMEOUT, url)
                    result_url, status = url, "USED"

                # Debug screenshot (first link only)
                if not first_done:
                    async with lock:
                        if not first_done:
                            first_done = True
                            try:
                                screenshot_page = await context.new_page()
                                await screenshot_page.goto(url, timeout=15000)
                                await screenshot_page.wait_for_timeout(2000)
                                await screenshot_page.screenshot(path="debug.png")
                                await screenshot_page.close()
                                with open("debug.png", "rb") as photo:
                                    bot.send_photo(
                                        chat_id, photo,
                                        caption="📸 Debug: View of the first link (Checking if bypass worked...)",
                                    )
                            except Exception as e:
                                logger.warning("Debug screenshot failed: %s", e)

                # Record result
                if status == "FRESH":
                    fresh.append(result_url)
                else:
                    used.append(result_url)

                completed += 1

                # Progress update
                if (
                    total > PROGRESS_INTERVAL
                    and completed % PROGRESS_INTERVAL == 0
                    and completed < total
                ):
                    try:
                        bot.send_message(
                            chat_id,
                            f"⏳ Progress: {completed}/{total} checked "
                            f"(✅ {len(fresh)} fresh | ❌ {len(used)} used)",
                        )
                    except Exception:
                        pass

        # Fire all tasks
        tasks = [process_with_guard(url, idx) for idx, url in enumerate(urls, 1)]
        await asyncio.gather(*tasks)

        # Cleanup
        await context.close()
        await browser.close()

    return {"fresh": fresh, "used": used}


def check_links(urls: list[str], message: telebot.types.Message) -> dict:
    """
    Synchronous wrapper that runs the async checking engine.
    """
    chat_id = message.chat.id

    # Guard: cookies.json must exist
    if not os.path.exists("cookies.json"):
        bot.reply_to(
            message,
            "⚠️ Error: `cookies.json` is missing from the server. Authentication required.",
            parse_mode="Markdown",
        )
        return {"fresh": [], "used": []}

    # Convert cookies
    try:
        storage = convert_cookies_to_playwright("cookies.json")
    except Exception as conv_err:
        bot.reply_to(message, f"⚠️ Error reading cookies.json: {conv_err}")
        return {"fresh": [], "used": []}

    # Register active task
    active_tasks[chat_id] = {"cancelled": False}

    # Send cancel button
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("❌ Cancel Checking", callback_data=f"cancel_{chat_id}"))
    bot.send_message(
        chat_id,
        f"🔍 Checking {len(urls)} links ({CONCURRENCY} at a time)...\nPress the button below to cancel.",
        reply_markup=markup,
    )

    # Run async engine
    try:
        results = asyncio.run(_check_links_async(urls, storage, message))
    finally:
        active_tasks.pop(chat_id, None)

    # Check if cancelled
    if active_tasks.get(chat_id, {}).get("cancelled"):
        bot.send_message(chat_id, "🛑 Checking was cancelled. Sending partial results...")

    return results


# ---------------------------------------------------------------------------
# Result reporting helper
# ---------------------------------------------------------------------------
def send_results(message: telebot.types.Message, results: dict):
    """Format and send the checking results back to the user."""
    fresh = results["fresh"]
    used = results["used"]
    total = len(fresh) + len(used)

    if total == 0:
        bot.reply_to(message, "⚠️ No links were checked.")
        return

    summary = (
        f"📊 *Check Complete!*\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🔗 Total checked: {total}\n"
        f"✅ Fresh links: {len(fresh)}\n"
        f"❌ Invalid/Used links: {len(used)}"
    )
    bot.reply_to(message, summary, parse_mode="Markdown")
    logger.info("Results — Fresh: %d | Used: %d", len(fresh), len(used))

    if fresh:
        result_path = "fresh_links_result.txt"
        with open(result_path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(fresh))
        with open(result_path, "rb") as doc:
            bot.send_document(
                message.chat.id,
                doc,
                caption=f"🟢 {len(fresh)} fresh links ready to use.",
            )
        logger.info("Sent fresh_links_result.txt to chat %s", message.chat.id)


# ---------------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------------
@bot.message_handler(commands=["start", "help"])
def handle_start(message: telebot.types.Message):
    """Welcome message."""
    bot.reply_to(
        message,
        "👋 *Welcome to the Bulk Link Checker!*\n\n"
        "I check Google One promo links to see if they're fresh or used.\n\n"
        "📌 *How to use:*\n"
        "• Paste links directly in chat (one per line)\n"
        "• Or upload a `.txt` file with links\n"
        "• Any number of links accepted\n\n"
        "I'll check each link and return the fresh ones as a file.\n"
        "You can cancel anytime with the cancel button.",
        parse_mode="Markdown",
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("cancel_"))
def handle_cancel(call: types.CallbackQuery):
    """Handle cancel button press."""
    chat_id = int(call.data.split("_", 1)[1])
    task_info = active_tasks.get(chat_id)
    if task_info:
        task_info["cancelled"] = True
        bot.answer_callback_query(call.id, "🛑 Cancelling... will send partial results.")
        bot.edit_message_text(
            "🛑 *Cancelling...* Waiting for current links to finish.",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            parse_mode="Markdown",
        )
        logger.info("User cancelled checking for chat %s", chat_id)
    else:
        bot.answer_callback_query(call.id, "No active check to cancel.")


@bot.message_handler(content_types=["document"])
def handle_document(message: telebot.types.Message):
    """Accept a .txt file upload, extract URLs, and check them."""
    chat_id = message.chat.id

    # Prevent overlapping checks
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
        bot.reply_to(message, "❌ No valid URLs found in the uploaded file.")
        return

    bot.reply_to(
        message,
        f"⏳ Received {len(urls)} links. Initializing headless browser...",
    )
    logger.info("Received %d links from document upload (chat %s)", len(urls), chat_id)

    results = check_links(urls, message)
    send_results(message, results)


@bot.message_handler(content_types=["text"])
def handle_text(message: telebot.types.Message):
    """Extract URLs from a plain text message and check them."""
    chat_id = message.chat.id

    # Prevent overlapping checks
    if chat_id in active_tasks:
        bot.reply_to(message, "⚠️ A check is already running. Cancel it first or wait.")
        return

    urls = extract_urls(message.text)

    if not urls:
        bot.reply_to(message, "❌ No valid URLs detected in your message.")
        return

    bot.reply_to(
        message,
        f"⏳ Received {len(urls)} links. Initializing headless browser...",
    )
    logger.info("Received %d links from text message (chat %s)", len(urls), chat_id)

    results = check_links(urls, message)
    send_results(message, results)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # Start Flask health-check in a background daemon thread
    server_thread = threading.Thread(target=keep_alive, daemon=True)
    server_thread.start()
    logger.info("Health-check thread started.")

    # Start Telegram long-polling (blocks the main thread)
    logger.info("Starting Telegram bot polling…")
    bot.infinity_polling(logger_level=logging.INFO)
