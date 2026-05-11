# TeleFlow — Complete Deploy Guide

Everything runs on ONE Railway service.
- `zestyycourses.up.railway.app/`         → userbot dashboard
- `zestyycourses.up.railway.app/courses`  → courses page

---

## STEP 1 — Get Telegram API credentials

1. Go to https://my.telegram.org
2. Log in with your phone number
3. Click **API development tools**
4. Create an app (name/platform don't matter)
5. Note your **api_id** (number) and **api_hash** (string)

---

## STEP 2 — Generate Session String (run once, locally)

```bash
pip install telethon
python generate_session.py
```

Enter your phone → enter the OTP Telegram sends you → copy the printed string.
That is your `TELEGRAM_SESSION`. Keep it secret — it's your account access.

---

## STEP 3 — Deploy to Railway

### Option A — GitHub (recommended)
1. Push this folder to a GitHub repo
2. Railway → New Project → Deploy from GitHub repo → pick your repo
3. Railway auto-detects `Procfile` and runs `python bot.py`

### Option B — Railway CLI
```bash
npm i -g @railway/cli
railway login
railway init
railway up
```

### Add Environment Variables
In Railway → your service → **Variables** tab, add:

| Variable              | Value                              |
|-----------------------|------------------------------------|
| `TELEGRAM_API_ID`     | your numeric api_id                |
| `TELEGRAM_API_HASH`   | your api_hash string               |
| `TELEGRAM_SESSION`    | long string from generate_session  |
| `OPENROUTER_API_KEY`  | from https://openrouter.ai/keys    |
| `OPENROUTER_MODEL`    | `stepfun/step-3.5-flash:free`      |
| `MESSAGES_PER_CHAT`   | `30`                               |
| `MAX_CHATS`           | `10`                               |

> Do NOT set PORT — Railway injects it automatically.

---

## STEP 4 — Verify

Once deployed:
- Open `https://zestyycourses.up.railway.app` → userbot dashboard
- Open `https://zestyycourses.up.railway.app/courses` → courses page

In Railway logs you should see:
```
✅ Logged in as YourName (@yourhandle)
✅ Web server running on port XXXX
✅ Dashboard → /   Courses → /courses
```

---

## Using the Course Scraper

1. Go to `/courses`
2. Click **⚙ Groups** → add Telegram group links (`https://t.me/groupname`)
3. Click **Update Courses** → bot scrapes those groups for mega.nz links
4. Courses appear as bento cards — title parsed from message, Mega link as download button
5. Hit **Update Courses** any time to refresh

---

## Troubleshooting

**500 Internal Server Error on Railway URL**
→ Check Railway logs — almost always means `TELEGRAM_SESSION` is missing or wrong.
→ Re-run `generate_session.py` and update the variable.

**"Session invalid" in logs**
→ Same fix — regenerate session string.

**Courses page shows "Cannot reach bot API"**
→ This shouldn't happen since courses.html is served from the same Railway URL.
→ If you're running locally, open http://localhost:8080/courses instead.

**No courses found after scraping**
→ The group may not have mega.nz links, or the userbot isn't a member of that group.
→ Join the group on Telegram first, then scrape.
