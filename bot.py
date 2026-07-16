#!/usr/bin/env python3
import os
import re
import signal
import logging
import asyncio
from html import unescape, escape
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup
from pymongo import MongoClient, ASCENDING
from pymongo.errors import DuplicateKeyError
from pyrogram import Client, enums
from pyrogram.errors import FloodWait, BadRequest

# ─────────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger("hdhub4u_bot")

# ─────────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────────
API_ID         = int(os.environ["TELEGRAM_API_ID"])
API_HASH       = os.environ["TELEGRAM_API_HASH"]
BOT_TOKEN      = os.environ["TELEGRAM_BOT_TOKEN"]
CHANNEL_ID     = os.environ["TELEGRAM_CHANNEL_ID"]
MONGO_URI      = os.environ["MONGO_URI"]
WEBSITE_URL    = os.environ.get("WEBSITE_URL", "https://new3.hdhub4u.cl/")
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL_MINUTES", "30"))
MAX_DL_LINKS   = int(os.environ.get("MAX_DL_LINKS", "10"))

# ─────────────────────────────────────────────────
#  MONGODB
# ─────────────────────────────────────────────────
_mongo_client = None

def get_db():
    global _mongo_client
    if _mongo_client is None:
        _mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    return _mongo_client["hdhub4u_bot"]

def setup_db():
    col = get_db()["sent_posts"]
    col.create_index([("url", ASCENDING)], unique=True)
    log.info("✅ MongoDB ready")

def is_sent(url: str) -> bool:
    try:
        return get_db()["sent_posts"].count_documents({"url": url}, limit=1) > 0
    except Exception as e:
        log.warning(f"MongoDB read error: {e}")
        return False

def mark_sent(url: str, title: str):
    try:
        get_db()["sent_posts"].insert_one({
            "url":     url,
            "title":   title,
            "sent_at": datetime.now(timezone.utc),
        })
    except DuplicateKeyError:
        pass
    except Exception as e:
        log.error(f"MongoDB write error: {e}")

# ─────────────────────────────────────────────────
#  REQUESTS SESSION
# ─────────────────────────────────────────────────
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
})

# ─────────────────────────────────────────────────
#  SCRAPER HELPERS
# ─────────────────────────────────────────────────
def is_skip_heading(text: str) -> bool:
    low = text.lower().strip()
    patterns = [
        r'single.?episode.?x264',
        r'\b4k\b', r'\bsdr\b', r'\bhdr\b', r'\bdv\b',
        r'web.?series.?episode',
    ]
    return any(re.search(p, low) for p in patterns)

def is_download_heading(text: str) -> bool:
    low = text.lower()
    return 'download' in low and 'link' in low

# ─────────────────────────────────────────────────
#  SCRAPER — Homepage
# ─────────────────────────────────────────────────
def get_latest_posts() -> list:
    try:
        resp = SESSION.get(WEBSITE_URL, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        log.error(f"Homepage fetch failed: {e}")
        return []

    soup  = BeautifulSoup(resp.text, "html.parser")
    posts = []
    for li in soup.select("li.thumb"):
        try:
            a_tag   = li.select_one("figure a")
            title_p = li.select_one("figcaption a p")
            if not (a_tag and title_p):
                continue
            url = a_tag.get("href", "").strip()
            if not url.startswith("http"):
                continue
            posts.append({
                "url":   url,
                "title": unescape(title_p.get_text(strip=True)),
            })
        except Exception as e:
            log.warning(f"Post parse error: {e}")

    log.info(f"Homepage: {len(posts)} posts found")
    return posts

# ─────────────────────────────────────────────────
#  SCRAPER — Download links
# ─────────────────────────────────────────────────
def get_download_links(post_url: str) -> list:
    try:
        resp = SESSION.get(post_url, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        log.error(f"Post fetch failed: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    body = (
        soup.select_one("main.page-body")
        or soup.select_one(".page-body")
        or soup.select_one("article")
        or soup
    )

    links         = []
    in_dl_section = False

    for tag in body.find_all(["h1","h2","h3","h4","h5","h6","p","a"]):
        text = tag.get_text(strip=True)

        if not in_dl_section:
            if is_download_heading(text):
                in_dl_section = True
            continue

        if tag.name in ["h1","h2","h3","h4","h5","h6"]:
            if is_skip_heading(text):
                break
            continue

        if tag.name == "a":
            href  = tag.get("href", "").strip()
            label = text
            if not (href.startswith("http") and label):
                continue
            if "hdhub4u" in href:
                continue
            if any(w in label.lower() for w in ["watch", "player", "trailer"]):
                continue
            links.append({"label": label, "url": href})
            if len(links) >= MAX_DL_LINKS:
                break

    log.info(f"{len(links)} download links in {post_url}")
    return links

# ─────────────────────────────────────────────────
#  MESSAGE BUILDER — HTML parse mode (reliable)
# ─────────────────────────────────────────────────
def build_message(post: dict, dl_links: list) -> str:
    """
    HTML parse mode use karo — MarkdownV2 se zyada stable.
    Escaping simple: sirf & < > ko escape karo.
    """
    title = escape(post["title"])   # HTML escape

    links_text = ""
    for i, link in enumerate(dl_links, 1):
        label = escape(link["label"])
        url   = link["url"]
        links_text += f"\n🔗 {i}. <a href=\"{url}\">{label}</a>"

    post_url = post["url"]

    msg = (
        f"🎬 <b>{title}</b>"
        f"\n\n"
        f"📥 <b>DOWNLOAD LINKS:</b>"
        f"{links_text}"
        f"\n\n"
        f"🌐 <a href=\"{post_url}\">Original Post</a>"
    )
    return msg

# ─────────────────────────────────────────────────
#  SENDER
# ─────────────────────────────────────────────────
async def send_post(app: Client, post: dict, dl_links: list) -> bool:
    if not dl_links:
        log.warning(f"No DL links — skipping: {post['title'][:50]}")
        return False

    message = build_message(post, dl_links)
    if len(message) > 4096:
        message = message[:4090] + "..."

    for attempt in range(1, 4):
        try:
            await app.send_message(
                chat_id=CHANNEL_ID,
                text=message,
                parse_mode=enums.ParseMode.HTML,
                disable_web_page_preview=True,
            )
            log.info(f"✅ Sent: {post['title'][:60]}")
            return True

        except FloodWait as fw:
            wait = fw.value + 5
            log.warning(f"FloodWait {fw.value}s — sleeping {wait}s")
            await asyncio.sleep(wait)

        except BadRequest as e:
            log.error(f"BadRequest attempt {attempt}: {e}")
            if attempt == 3:
                return await send_plain_fallback(app, post, dl_links)
            await asyncio.sleep(2 ** attempt)

        except Exception as e:
            log.error(f"Error attempt {attempt}: {e}")
            await asyncio.sleep(2 ** attempt)

    return False

async def send_plain_fallback(app: Client, post: dict, dl_links: list) -> bool:
    links_text = "".join(
        f"\n{i}. {lnk['label']}\n{lnk['url']}"
        for i, lnk in enumerate(dl_links, 1)
    )
    text = (
        f"🎬 {post['title']}\n\n"
        f"📥 DOWNLOAD LINKS:{links_text}\n\n"
        f"🌐 {post['url']}"
    )[:4096]
    try:
        await app.send_message(chat_id=CHANNEL_ID, text=text)
        log.info(f"✅ Sent (plain): {post['title'][:50]}")
        return True
    except Exception as e:
        log.error(f"Plain fallback failed: {e}")
        return False

# ─────────────────────────────────────────────────
#  MAIN LOOP
# ─────────────────────────────────────────────────
async def check_and_post(app: Client):
    log.info("=" * 55)
    log.info(f"🔍 Check started [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]")

    posts = get_latest_posts()
    if not posts:
        log.warning("No posts found.")
        return

    new_count = 0
    for post in posts:
        url = post["url"]
        if is_sent(url):
            log.debug(f"Skip: {url}")
            continue

        log.info(f"New post: {post['title'][:70]}")
        dl_links = get_download_links(url)
        await asyncio.sleep(2)

        success = await send_post(app, post, dl_links)
        if success:
            mark_sent(url, post["title"])
            new_count += 1
            await asyncio.sleep(4)

    log.info(f"✅ Done — {new_count} new post(s)." if new_count else "ℹ️ No new posts.")

async def main():
    log.info("🚀 HDHub4u Bot starting")

    try:
        setup_db()
        get_db().client.admin.command("ping")
        log.info("✅ MongoDB connected")
    except Exception as e:
        log.error(f"❌ MongoDB failed: {e}")
        raise SystemExit(1)

    app = Client(
        name="hdhub4u_bot",
        api_id=API_ID,
        api_hash=API_HASH,
        bot_token=BOT_TOKEN,
        in_memory=True,
    )

    async with app:
        me = await app.get_me()
        log.info(f"✅ Connected as @{me.username}")

        await check_and_post(app)

        log.info(f"⏰ Checking every {CHECK_INTERVAL} minutes")
        while True:
            await asyncio.sleep(CHECK_INTERVAL * 60)
            await check_and_post(app)

# ─────────────────────────────────────────────────
#  SIGNAL HANDLING
# ─────────────────────────────────────────────────
def handle_sigterm(*_):
    log.info("Shutting down.")
    raise SystemExit(0)

if __name__ == "__main__":
    signal.signal(signal.SIGTERM, handle_sigterm)
    signal.signal(signal.SIGINT, handle_sigterm)
    try:
        asyncio.run(main())
    except SystemExit:
        log.info("Bot stopped.")