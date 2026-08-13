"""
Telegram Bot — Bulk Google One Link Checker
============================================
Uses Playwright (headless Chromium) to visit Google One promo links,
bypass the login wall via a pre-exported session state (cookies.json),
and determine whether each link is FRESH or USED based on Arabic DOM text.

A lightweight Flask health-check server runs in a daemon thread so the
Render Web Service stays alive and passes port-binding checks.
"""

import os
import re
import logging
import threading
import tempfile

import telebot
from flask import Flask
from playwright.sync_api import sync_playwright

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
# Playwright link-checking engine
# ---------------------------------------------------------------------------
ARABIC_USED_PHRASES = [
    "الاشتراك قيد الاستخدام",
    "سبق أن تم استخدام",
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

# Phrases that indicate Google is showing a login wall (bypass failed)
LOGIN_WALL_PHRASES = [
    "تسجيل الدخول",
    "Sign in",
    "accounts.google.com/v3/signin",
]


def convert_cookies_to_playwright(cookie_editor_path: str) -> dict:
    """
    Convert a Cookie-Editor JSON export into the Playwright storage_state
    format that ``browser.new_context(storage_state=...)`` expects.

    Cookie-Editor schema → Playwright cookie schema:
      expirationDate → expires  (float epoch)
      sameSite       → sameSite ("None" | "Lax" | "Strict")
      Fields like hostOnly / storeId / session are dropped.
    """
    import json as _json

    with open(cookie_editor_path, "r", encoding="utf-8") as fh:
        raw_cookies = _json.load(fh)

    pw_cookies = []
    for c in raw_cookies:
        pw_cookie = {
            "name": c["name"],
            "value": c["value"],
            "domain": c["domain"],
            "path": c.get("path", "/"),
            "expires": c.get("expirationDate", -1),
            "httpOnly": c.get("httpOnly", False),
            "secure": c.get("secure", False),
            "sameSite": SAME_SITE_MAP.get(
                (c.get("sameSite") or "").lower(), "Lax"
            ),
        }
        pw_cookies.append(pw_cookie)

    return {"cookies": pw_cookies, "origins": []}


def check_links(urls: list[str], message: telebot.types.Message) -> dict:
    """
    Visit every URL with Playwright, classify as FRESH or USED.

    Returns a dict with keys: fresh (list), used (list).
    Sends a debug screenshot of the very first link to the Telegram chat.
    """
    # Guard: cookies.json must exist on the server
    if not os.path.exists("cookies.json"):
        bot.reply_to(
            message,
            "⚠️ Error: `cookies.json` is missing from the server. Authentication required.",
            parse_mode="Markdown",
        )
        logger.error("cookies.json not found – aborting link check.")
        return {"fresh": [], "used": []}

    # Convert Cookie-Editor format → Playwright storage_state
    try:
        storage = convert_cookies_to_playwright("cookies.json")
    except Exception as conv_err:
        bot.reply_to(message, f"⚠️ Error reading cookies.json: {conv_err}")
        logger.error("Cookie conversion failed: %s", conv_err)
        return {"fresh": [], "used": []}

    fresh: list[str] = []
    used: list[str] = []
    is_first_link = True

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            storage_state=storage,
            locale="ar-EG",
            user_agent=USER_AGENT,
        )
        page = context.new_page()

        for idx, url in enumerate(urls, start=1):
            logger.info("[%d/%d] Checking: %s", idx, len(urls), url)
            try:
                page.goto(url, timeout=60000)
                page.wait_for_timeout(4000)

                # Debug screenshot for the first link only
                if is_first_link:
                    screenshot_path = "debug.png"
                    page.screenshot(path=screenshot_path)
                    try:
                        with open(screenshot_path, "rb") as photo:
                            bot.send_photo(
                                message.chat.id,
                                photo,
                                caption="📸 Debug: View of the first link (Checking if bypass worked...)",
                            )
                    except Exception as send_err:
                        logger.warning("Could not send debug screenshot: %s", send_err)
                    is_first_link = False

                # Read the fully-rendered DOM
                content = page.content()

                # Check if we hit a login wall (bypass failed)
                if any(phrase in content for phrase in LOGIN_WALL_PHRASES):
                    used.append(url)
                    logger.warning("  → LOGIN WALL detected (bypass failed) — marked INVALID")
                elif any(phrase in content for phrase in ARABIC_USED_PHRASES):
                    used.append(url)
                    logger.info("  → INVALID / USED")
                else:
                    fresh.append(url)
                    logger.info("  → FRESH")

            except Exception as exc:
                logger.error("  → ERROR (marked INVALID): %s", exc)
                used.append(url)

        # Cleanup
        page.close()
        context.close()
        browser.close()

    return {"fresh": fresh, "used": used}


# ---------------------------------------------------------------------------
# Result reporting helper
# ---------------------------------------------------------------------------
def send_results(message: telebot.types.Message, results: dict):
    """Format and send the checking results back to the user."""
    fresh = results["fresh"]
    used = results["used"]

    summary = (
        f"✅ Fresh links: {len(fresh)}\n"
        f"❌ Invalid/Used links: {len(used)}"
    )
    bot.reply_to(message, summary)
    logger.info("Results — Fresh: %d | Used: %d", len(fresh), len(used))

    if fresh:
        result_path = "fresh_links_result.txt"
        with open(result_path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(fresh))
        with open(result_path, "rb") as doc:
            bot.send_document(message.chat.id, doc)
        logger.info("Sent fresh_links_result.txt to chat %s", message.chat.id)


# ---------------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------------
@bot.message_handler(commands=["start"])
def handle_start(message: telebot.types.Message):
    """Welcome message."""
    bot.reply_to(
        message,
        "👋 Welcome to the *Bulk Link Checker* bot!\n\n"
        "Send me a list of Google One promo links (one per line) "
        "or upload a `.txt` file containing the links.",
        parse_mode="Markdown",
    )


@bot.message_handler(content_types=["document"])
def handle_document(message: telebot.types.Message):
    """Accept a .txt file upload, extract URLs, and check them."""
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
        f"⏳ Received {len(urls)} links. Initializing headless browser and checking started...",
    )
    logger.info("Received %d links from document upload (chat %s)", len(urls), message.chat.id)

    results = check_links(urls, message)
    send_results(message, results)


@bot.message_handler(content_types=["text"])
def handle_text(message: telebot.types.Message):
    """Extract URLs from a plain text message and check them."""
    urls = extract_urls(message.text)

    if not urls:
        bot.reply_to(message, "❌ No valid URLs detected in your message.")
        return

    bot.reply_to(
        message,
        f"⏳ Received {len(urls)} links. Initializing headless browser and checking started...",
    )
    logger.info("Received %d links from text message (chat %s)", len(urls), message.chat.id)

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
