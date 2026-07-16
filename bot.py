#!/usr/bin/env python3
"""
HDHub4u → Telegram Auto Poster  (Pyrogram + MongoDB Edition)
=============================================================
Changes in this version:
  ✅ Thumbnail completely removed — sirf title + DL links
  ✅ Redis hataya — MongoDB se duplicate filter
  ✅ MongoDB Atlas free tier pe kaam karta hai (Render-safe)
  ✅ Unique index on 'url' field — DB level duplicate protection
  ✅ Baaki saare fixes pehle jaisi hain
"""

import os
import re
import signal
import logging
import asyncio
from html    import unescape
from datetime import datetime, timezone

import requests
from bs4      import BeautifulSoup
from pymongo  import MongoClient, ASCENDING
from pymongo.errors import DuplicateKeyError
from pyrogram import Client
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
#  CONFIG — Render Dashboard > Environment mein set karo
# ─────────────────────────────────────────────────
API_ID         = int(os.environ["TELEGRAM_API_ID"])
API_HASH       = os.environ["TELEGRAM_API_HASH"]
BOT_TOKEN      = os.environ["TELEGRAM_BOT_TOKEN"]
CHANNEL_ID     = os.environ["TELEGRAM_CHANNEL_ID"]
MONGO_URI      = os.environ["MONGO_URI"]             # MongoDB Atlas connection string
WEBSITE_URL    = os.environ.get("WEBSITE_URL", "https://new3.hdhub4u.cl/")
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL_MINUTES", "30"))
MAX_DL_LINKS   = int(os.environ.get("MAX_DL_LINKS", "10"))

# ─────────────────────────────────────────────────
#  MONGODB SETUP
# ─────────────────────────────────────────────────
_mongo_client: MongoClient | None = None

def get_db():
    """MongoDB connection — singleton pattern"""
    global _mongo_client
    if _mongo_client is None:
        _mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    return _mongo_client["hdhub4u_bot"]

def setup_db():
    """
    Collection aur unique index banao.
    Agar already exist karta hai to silently skip.
    """
    db = get_db()
    col = db["sent_posts"]
    # Unique index on url — DB level pe duplicate impossible
    col.create_index([("url", ASCENDING)], unique=True)
    log.info("✅ MongoDB ready — 'sent_posts' collection indexed")
    return col

def is_sent(url: str) -> bool:
    """Check karo ki yeh URL pehle se DB mein hai ya nahi"""
    try:
        col = get_db()["sent_posts"]
        return col.count_documents({"url": url}, limit=1) > 0
    except Exception as e:
        log.warning(f"MongoDB read failed: {e}")
        return False  # Fail-open: duplicate bhi bhej do, safe side

def mark_sent(url: str, title: str):
    """
    Post ko sent mark karo DB mein.
    DuplicateKeyError ignore karo — race condition safe.
    """
    try:
        col = get_db()["sent_posts"]
        col.insert_one({
            "url":        url,
            "title":      title,
            "sent_at":    datetime.now(timezone.utc),
        })
        log.debug(f"MongoDB: marked sent — {url}")
    except DuplicateKeyError:
        log.debug(f"MongoDB: already exists (duplicate key) — {url}")
    except Exception as e:
        log.error(f"MongoDB write failed: {e}")

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
#  MARKDOWN UTILITY
# ─────────────────────────────────────────────────
def escape_md(text: str) -> str:
    """Telegram MarkdownV2 ke liye text escape karo"""
    text = unescape(str(text))
    for ch in r'\_*[]()~`>#+-=|{}.!':
        text = text.replace(ch, f'\\{ch}')
    return text

def escape_url(url: str) -> str:
    """MarkdownV2 URL ke andar sirf ) aur \\ escape hote hain"""
    return url.replace('\\', '\\\\').replace(')', '\\)')

# ─────────────────────────────────────────────────
#  SCRAPER HELPERS
# ─────────────────────────────────────────────────
def is_skip_heading(text: str) -> bool:
    low = text.lower().strip()
    patterns = [
        r'single.?episode.?x264',
        r'\b4k\b',
        r'\bsdr\b',
        r'\bhdr\b',
        r'\bdv\b',
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
    """
    Returns: [{"title": str, "url": str}, ...]
    Thumbnail field completely removed.
    """
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
    """Returns: [{"label": str, "url": str}, ...]"""
    try:
        resp = SESSION.get(post_url, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        log.error(f"Post fetch failed ({post_url}): {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    body = (
        soup.select_one("main.page-body")
        or soup.select_one(".page-body")
        or soup.select_one("article")
        or soup
    )

    links        = []
    in_dl_section = False

    for tag in body.find_all(["h1","h2","h3","h4","h5","h6","p","a"]):
        text = tag.get_text(strip=True)

        if not in_dl_section:
            if is_download_heading(text):
                in_dl_section = True
            continue

        if tag.name in ["h1","h2","h3","h4","h5","h6"]:
            if is_skip_heading(text):
                log.debug(f"Stop at heading: {text[:60]}")
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
#  MESSAGE BUILDER — Sirf title + links (no thumbnail)
# ─────────────────────────────────────────────────
def build_message(post: dict, dl_links: list) -> str:
    """
    Pure text message — koi photo nahi, koi thumbnail nahi.
    Format:
      🎬 *TITLE*

      📥 *DOWNLOAD LINKS:*
      🔗 1. [Label](url)
      🔗 2. [Label](url)
      ...

      🌐 [Original Post](url)
    """
    title_esc    = escape_md(post["title"])
    post_url_esc = escape_url(post["url"])

    links_text = ""
    for i, link in enumerate(dl_links, 1):
        label_esc = escape_md(link["label"])
        url_esc   = escape_url(link["url"])
        links_text += f"\n🔗 {i}\\. [{label_esc}]({url_esc})"

    msg = (
        f"🎬 *{title_esc}*"
        f"\n\n"
        f"📥 *DOWNLOAD LINKS:*"
        f"{links_text}"
        f"\n\n"
        f"🌐 [Original Post]({post_url_esc})"
    )
    return msg

# ─────────────────────────────────────────────────
#  SENDER — Pyrogram
# ─────────────────────────────────────────────────
async def send_post(app: Client, post: dict, dl_links: list) -> bool:
    if not dl_links:
        log.warning(f"No DL links — skipping: {post['title'][:50]}")
        return False

    message     = build_message(post, dl_links)
    max_retries = 3

    # Message limit 4096 chars
    if len(message) > 4096:
        message = message[:4092] + "\\.\\.\\."

    for attempt in range(1, max_retries + 1):
        try:
            await app.send_message(
                chat_id=CHANNEL_ID,
                text=message,
                parse_mode="md",               # Pyrogram MarkdownV2
                disable_web_page_preview=True, # Links ke previews band — clean look
            )
            log.info(f"✅ Sent: {post['title'][:60]}")
            return True

        except FloodWait as fw:
            wait = fw.value + 5
            log.warning(f"FloodWait {fw.value}s — waiting {wait}s (attempt {attempt})")
            await asyncio.sleep(wait)

        except BadRequest as e:
            log.error(f"BadRequest attempt {attempt}: {e}")
            if attempt == max_retries:
                return await send_plain_fallback(app, post, dl_links)
            await asyncio.sleep(2 ** attempt)

        except Exception as e:
            log.error(f"Error attempt {attempt}: {e}")
            await asyncio.sleep(2 ** attempt)

    return False

async def send_plain_fallback(app: Client, post: dict, dl_links: list) -> bool:
    """Markdown fail ho to plain text bhejo"""
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
#  MAIN CHECK LOOP
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

        # MongoDB se duplicate check
        if is_sent(url):
            log.debug(f"Skip (already sent): {url}")
            continue

        log.info(f"New post: {post['title'][:70]}")
        dl_links = get_download_links(url)

        await asyncio.sleep(2)   # Crawl delay

        success = await send_post(app, post, dl_links)

        if success:
            mark_sent(url, post["title"])   # MongoDB mein save
            new_count += 1
            await asyncio.sleep(4)          # Telegram rate limit gap

    log.info(f"✅ Done — {new_count} new post(s) sent." if new_count else "ℹ️ No new posts.")

# ─────────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────────
async def main():
    log.info("🚀 HDHub4u Bot starting (Pyrogram + MongoDB)")

    # MongoDB check + index setup
    try:
        setup_db()
        # Ping karo confirm karne ke liye
        get_db().client.admin.command("ping")
        log.info("✅ MongoDB connected")
    except Exception as e:
        log.error(f"❌ MongoDB connection failed: {e}")
        raise SystemExit(1)

    # Pyrogram client
    app = Client(
        name="hdhub4u_bot",
        api_id=API_ID,
        api_hash=API_HASH,
        bot_token=BOT_TOKEN,
        in_memory=True,     # Disk pe session file nahi — Render restart safe
    )

    async with app:
        me = await app.get_me()
        log.info(f"✅ Pyrogram ready — @{me.username}")

        # Pehli baar turant run karo
        await check_and_post(app)

        # Phir interval pe
        log.info(f"⏰ Next check every {CHECK_INTERVAL} minutes")
        while True:
            await asyncio.sleep(CHECK_INTERVAL * 60)
            await check_and_post(app)

# ─────────────────────────────────────────────────
#  SIGNAL HANDLING
# ─────────────────────────────────────────────────
def handle_sigterm(*_):
    log.info("SIGTERM received — shutting down.")
    if _mongo_client:
        _mongo_client.close()
    raise SystemExit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, handle_sigterm)
    signal.signal(signal.SIGINT,  handle_sigterm)
    try:
        asyncio.run(main())
    except SystemExit:
        log.info("Bot stopped cleanly.")
