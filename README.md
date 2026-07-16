# HDHub4u → Telegram Bot (Pyrogram + MongoDB)

Latest posts scrape karta hai aur Telegram channel pe bhejta hai.
**Sirf Title + Download Links** — koi thumbnail nahi.
**MongoDB** se duplicate filtering — restart-safe.

---

## 📁 Files

```
hdhub4u_bot/
├── bot.py            ← Main code
├── requirements.txt  ← Dependencies
├── render.yaml       ← Render auto-deploy
├── .env.example      ← Environment variables template
└── README.md
```

---

## ⚙️ Step 1 — Telegram Setup

**API_ID + API_HASH:**
1. https://my.telegram.org/auth pe jao
2. "API development tools" → App banao
3. `api_id` aur `api_hash` copy karo

**BOT_TOKEN:**
1. @BotFather → `/newbot`
2. Token copy karo
3. Bot ko apne channel ka **Admin** banao (Post Messages permission)

---

## 🍃 Step 2 — MongoDB Atlas Setup (Free)

1. https://cloud.mongodb.com pe account banao
2. **Free M0 cluster** banao (us-east-1 select karo)
3. **Database Access** → User banao (username + password note karo)
4. **Network Access** → `0.0.0.0/0` add karo (Render ke liye)
5. **Connect** → "Drivers" → Connection string copy karo:
   ```
   mongodb+srv://USER:PASSWORD@cluster0.xxxxx.mongodb.net/?retryWrites=true&w=majority
   ```

---

## 🚀 Step 3 — Render Deploy

### Option A — Blueprint (Recommended)

```bash
git init && git add . && git commit -m "init"
git remote add origin https://github.com/USERNAME/hdhub4u-bot.git
git push -u origin main
```

1. https://dashboard.render.com → **New** → **Blueprint**
2. GitHub repo connect karo
3. `render.yaml` auto detect hoga
4. **Environment** mein manually set karo:

| Key | Value |
|-----|-------|
| `TELEGRAM_API_ID` | my.telegram.org se |
| `TELEGRAM_API_HASH` | my.telegram.org se |
| `TELEGRAM_BOT_TOKEN` | @BotFather se |
| `TELEGRAM_CHANNEL_ID` | `@channel` ya `-100xxxxx` |
| `MONGO_URI` | Atlas connection string |

5. **Apply** → Deploy shuru!

### Option B — Manual Worker

1. Render → **New Background Worker**
2. Build: `pip install -r requirements.txt`
3. Start: `python bot.py`
4. Environment variables upar wali table se add karo

---

## 💬 Telegram Message Format

```
🎬 Movie Title (2024) Hindi 1080p WEB-DL

📥 DOWNLOAD LINKS:
🔗 1. 1080p x265 HEVC [2.1GB]
🔗 2. 1080p x264 [3.8GB]
🔗 3. 720p x265 [1.1GB]
🔗 4. 480p x264 [600MB]

🌐 Original Post
```

---

## 🍃 MongoDB Collection Structure

```json
{
  "_id": "ObjectId(...)",
  "url": "https://new3.hdhub4u.cl/movie-name/",
  "title": "Movie Title (2024) ...",
  "sent_at": "2024-07-16T10:30:00Z"
}
```

Unique index on `url` → DB level pe duplicate impossible.
