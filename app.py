"""
TeleFlow — Unified App
Merges TeleFlow AI (userbot + AI reply) and VideoFlow (multi-user video downloader)
into a single aiohttp server on one PORT.

TeleFlow AI routes:  /            → index.html (telefow dashboard)
VideoFlow routes:    /videos      → videoflow dashboard (embedded tab)
                     /api/vf/*    → videoflow API
                     /api/auth/*  → videoflow auth
                     /api/status  (GET) → telefow status (bot)
                     /api/vf/status/{id} → videoflow download status
"""

import os
import asyncio
import re
import json
import uuid
import sqlite3
import time
import subprocess
import tempfile
import shutil
import stat
import urllib.request
import tarfile
from datetime import datetime, timezone, timedelta
from typing import Optional
from pathlib import Path

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import User, Channel
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError
import httpx
from dotenv import load_dotenv
from aiohttp import web

load_dotenv()

# ─── CONFIG ───────────────────────────────────────────────────────────
# TeleFlow AI (userbot)
API_ID            = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH          = os.getenv("TELEGRAM_API_HASH", "")
OPENROUTER_API_KEY= os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL  = os.getenv("OPENROUTER_MODEL", "stepfun/step-3.5-flash:free")
MESSAGES_PER_CHAT = int(os.getenv("MESSAGES_PER_CHAT", "30"))
MAX_CHATS         = int(os.getenv("MAX_CHATS", "10"))
SESSION_STRING    = os.getenv("TELEGRAM_SESSION", "")
PORT              = int(os.getenv("PORT", "8080"))

# VideoFlow
_default_db = "/data/videoflow.db" if os.path.isdir("/data") else "videoflow.db"
DB_PATH     = os.getenv("DB_PATH", _default_db)
YOUTUBE_COOKIES_FILE = os.getenv("YOUTUBE_COOKIES_FILE", "")

# ─── TELEFOW USERBOT CLIENT ───────────────────────────────────────────
bot_client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH) if SESSION_STRING else None

bot_state = {
    "ai_reply": True,
    "autoreply": {"active": False, "message": ""},
    "autoreply_replied": set(),
    "scrape_groups": [],
    "me": None,
    "status_log": [],
    "chat_contexts": {},
    "results": [],
    "task_status": "idle",
    "messages_today": 0,
    "ai_memory": {},
    "analytics": {},
    "response_times": {},
    "last_incoming_ts": {},
    "ai_persona": "",
    "my_name": "me",
    "notes": [],
}

LINK_FILTERS = {
    "mega":      r'https?://mega\.nz/\S+',
    "drive":     r'https?://drive\.google\.com/\S+',
    "gdrive":    r'https?://drive\.google\.com/\S+',
    "youtube":   r'https?://(?:www\.)?youtu(?:be\.com|\.be)/\S+',
    "instagram": r'https?://(?:www\.)?instagram\.com/\S+',
    "twitter":   r'https?://(?:twitter|x)\.com/\S+',
    "telegram":  r't\.me/\S+',
    "github":    r'https?://github\.com/\S+',
    "all":       r'https?://\S+|t\.me/\S+',
}

def log(msg):
    print(msg)
    bot_state["status_log"].append(msg)
    if len(bot_state["status_log"]) > 50:
        bot_state["status_log"].pop(0)

DEFAULT_PERSONA = """You are replying on behalf of the user named {my_name} in a Telegram chat.

About the user:
- Casual and friendly, plain English
- Uses short forms naturally: irl, imo, ig, obv, fs, ngl, rn, rly, tbh, lmao, fr
- Replies are SHORT — 1 to 3 sentences max
- No emojis unless the other person uses them first
- Sounds human and natural, never robotic or formal
- Never starts a reply with "Hey" every single time — vary the opening
- Never reveals being an AI
- Reads the FULL conversation history carefully before replying
- Matches the energy and tone of the conversation

CRITICAL: Never stop chatting. "ok", "lol", "haha", "nice", "k" are NOT goodbyes — keep the conversation going. Only send a farewell when they explicitly say bye/gtg/ttyl/gn."""

bot_state["ai_persona"] = DEFAULT_PERSONA

goodbye_senders = set()
FINAL_GOODBYE_WORDS = ["bye","goodbye","gotta go","gtg","ttyl","talk later","see you","see ya","good night","gn","take care","cya"]

def is_final_goodbye(text):
    t = text.lower().strip()
    return any(w in t for w in FINAL_GOODBYE_WORDS)

def is_just_acknowledgement(text):
    t = text.lower().strip()
    acks = ["ok","okay","k","lol","haha","hah","hm","hmm","nice","cool","wow","oh","ah","yeah","yep","yup","sure","alright","aight","ikr","ik","true","facts","fr","lmao","lmfao","😂","😅","👍","🙏"]
    return t in acks

# ─── OPENROUTER ───────────────────────────────────────────────────────
async def call_openrouter(prompt):
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/telegram-userbot",
        "X-Title": "TeleFlow AI",
    }
    payload = {"model": OPENROUTER_MODEL,"messages":[{"role":"user","content":prompt}],"max_tokens":512}
    async with httpx.AsyncClient(timeout=60) as http:
        r = await http.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=payload)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()

async def summarise_chat(chat_name, messages):
    if not messages: return None
    conversation = "\n".join(messages)
    prompt = (f"Summarise this Telegram conversation from '{chat_name}' in 2-4 sentences. "
              f"Focus on key topics, decisions, or anything needing attention. Be concise.\n\n{conversation}")
    try: return await call_openrouter(prompt)
    except Exception as e: return f"Error: {e}"

def get_memory_for(name):
    facts = bot_state["ai_memory"].get(name, [])
    if not facts: return ""
    return f"\nWhat you've learned about {name} from past conversations:\n" + "\n".join(f"- {f}" for f in facts[-20:]) + "\n"

async def extract_and_store_memory(sender_name, convo):
    existing = bot_state["ai_memory"].get(sender_name, [])
    existing_text = "\n".join(f"- {f}" for f in existing) if existing else "None yet."
    prompt = f"""You are a memory extraction system. Read this conversation and extract NEW facts about {sender_name}.
Existing known facts:\n{existing_text}\nConversation:\n{convo}\nExtract only NEW facts. Return as plain list, one per line, max 5. If nothing new: NONE"""
    try:
        raw = await call_openrouter(prompt)
        if raw.strip().upper() == "NONE" or not raw.strip(): return
        new_facts = [line.strip() for line in raw.strip().splitlines() if line.strip() and len(line.strip()) > 8]
        if not new_facts: return
        if sender_name not in bot_state["ai_memory"]: bot_state["ai_memory"][sender_name] = []
        existing_lower = [f.lower() for f in bot_state["ai_memory"][sender_name]]
        for fact in new_facts:
            if fact.lower() not in existing_lower: bot_state["ai_memory"][sender_name].append(fact)
        bot_state["ai_memory"][sender_name] = bot_state["ai_memory"][sender_name][-50:]
        log(f"Memory updated for {sender_name} (+{len(new_facts)} facts)")
    except Exception as e: log(f"Memory extraction error: {e}")

def track_incoming(sender_name, sender_id, msg_time):
    hour = msg_time.hour
    if sender_name not in bot_state["analytics"]:
        bot_state["analytics"][sender_name] = {"count":0,"last_seen":"","hours":[0]*24}
    bot_state["analytics"][sender_name]["count"] += 1
    bot_state["analytics"][sender_name]["last_seen"] = msg_time.isoformat()
    bot_state["analytics"][sender_name]["hours"][hour] += 1
    bot_state["last_incoming_ts"][sender_id] = msg_time.timestamp()

def track_response_time(sender_name, sender_id):
    ts = bot_state["last_incoming_ts"].get(sender_id)
    if ts:
        elapsed = datetime.now(timezone.utc).timestamp() - ts
        if elapsed < 3600:
            if sender_name not in bot_state["response_times"]: bot_state["response_times"][sender_name] = []
            bot_state["response_times"][sender_name].append(round(elapsed, 1))
            bot_state["response_times"][sender_name] = bot_state["response_times"][sender_name][-100:]

# ─── BOT EVENTS (only if bot_client exists) ───────────────────────────
if bot_client:
    @bot_client.on(events.NewMessage(incoming=True))
    async def handle_ai_autoreply(event):
        if not bot_state["ai_reply"] or not event.is_private: return
        try:
            sender = await event.get_sender()
            if not sender or getattr(sender, "bot", False): return
            sender_name = getattr(sender, "first_name", None) or "Someone"
            sender_id = event.sender_id
            my_name = bot_state.get("my_name", "me")
            msg_time = event.date if event.date.tzinfo else event.date.replace(tzinfo=timezone.utc)
            track_incoming(sender_name, sender_id, msg_time)
            if sender_id in goodbye_senders: return
            history = []
            async for msg in bot_client.iter_messages(event.chat_id, limit=40):
                if msg.text:
                    who = my_name if msg.out else sender_name
                    history.append(f"{who}: {msg.text}")
            history.reverse()
            full_convo = "\n".join(history)
            persona = bot_state["ai_persona"].replace("{my_name}", my_name)
            chat_context = bot_state["chat_contexts"].get(sender_name, "")
            extra = f"\nExtra context about {sender_name}:\n{chat_context}\n" if chat_context else ""
            memory_context = get_memory_for(sender_name)
            if is_final_goodbye(event.text):
                goodbye_senders.add(sender_id)
                farewell_prompt = f"{persona}\n{extra}{memory_context}\nConversation:\n{full_convo}\n\n{sender_name} just said goodbye. Send ONE short warm farewell:"
                reply = await call_openrouter(farewell_prompt)
                await event.reply(reply)
                track_response_time(sender_name, sender_id)
                bot_state["messages_today"] = bot_state.get("messages_today", 0) + 1
                return
            convo_note = ""
            if is_just_acknowledgement(event.text):
                convo_note = f'\nNOTE: "{event.text}" is just an acknowledgement. Keep the conversation going.\n'
            prompt = f"{persona}\n{extra}{memory_context}{convo_note}\nFull conversation with {sender_name}:\n{full_convo}\n\n{sender_name} just sent: {event.text}\n\nReply as {my_name} (short, casual):"
            reply = await call_openrouter(prompt)
            await event.reply(reply)
            track_response_time(sender_name, sender_id)
            bot_state["messages_today"] = bot_state.get("messages_today", 0) + 1
            log(f"AI replied to {sender_name}")
            asyncio.create_task(extract_and_store_memory(sender_name, full_convo))
        except Exception as e: log(f"AI reply error: {e}")

    @bot_client.on(events.NewMessage(incoming=True))
    async def handle_incoming_autoreply(event):
        if not bot_state["autoreply"]["active"] or not event.is_private: return
        if event.sender_id in bot_state["autoreply_replied"]: return
        bot_state["autoreply_replied"].add(event.sender_id)
        await event.reply(bot_state["autoreply"]["message"])

# ─── DATABASE (VideoFlow) ─────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY, phone TEXT UNIQUE NOT NULL,
        session TEXT, token TEXT UNIQUE,
        created_at TEXT DEFAULT (datetime('now')), last_seen TEXT
    );
    CREATE TABLE IF NOT EXISTS auth_sessions (
        phone TEXT PRIMARY KEY, phone_code_hash TEXT,
        tmp_session TEXT, created_at REAL
    );
    CREATE TABLE IF NOT EXISTS downloads (
        id TEXT PRIMARY KEY, user_id TEXT, url TEXT, title TEXT,
        site TEXT, quality TEXT, status TEXT DEFAULT 'pending',
        progress INTEGER DEFAULT 0, eta TEXT, speed TEXT,
        total_size TEXT, file_size INTEGER, error TEXT,
        chat_target TEXT DEFAULT 'me', created_at TEXT DEFAULT (datetime('now'))
    );
    """)
    conn.commit(); conn.close()

def get_user_by_token(token):
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE token=?", (token,)).fetchone()
    conn.close()
    return dict(row) if row else None

# ─── VIDEOFLOW SESSIONS ───────────────────────────────────────────────
vf_sessions: dict = {}
user_downloads: dict = {}
scheduled_jobs: dict = {}

async def start_user_session(user_id, session_str):
    if user_id in vf_sessions:
        try: await vf_sessions[user_id]["client"].disconnect()
        except: pass
    client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            print(f"[{user_id[:8]}] session expired")
            return False
        me = await client.get_me()
        vf_sessions[user_id] = {"client": client, "me": me}
        print(f"[{user_id[:8]}] ✅ Online as {me.first_name}")
        return True
    except Exception as e:
        print(f"[{user_id[:8]}] connect error: {e}")
        return False

def _get_downloads(uid):
    if uid not in user_downloads: user_downloads[uid] = []
    return user_downloads[uid]

# ─── BINARY RESOLUTION ────────────────────────────────────────────────
def _find_bin(name):
    import shutil as _sh, glob as _gl
    found = _sh.which(name)
    if found: return found
    for d in (["/app/.venv/bin","/usr/bin","/usr/local/bin","/bin",
               "/nix/var/nix/profiles/default/bin","/root/.nix-profile/bin"]
              + _gl.glob("/nix/store/*/bin")):
        p = Path(d) / name
        try:
            if p.is_file() and p.stat().st_mode & 0o111: return str(p)
        except: continue
    return ""

def _ensure_ffmpeg():
    for bin_dir in [Path("/app/ffmpeg_bin"), Path("/tmp/ffmpeg_bin")]:
        ff = bin_dir / "ffmpeg"; ffp = bin_dir / "ffprobe"
        if ff.is_file() and ffp.is_file(): return str(ff), str(ffp)
    ff_path = _find_bin("ffmpeg"); ffp_path = _find_bin("ffprobe")
    if ff_path and ffp_path: return ff_path, ffp_path
    bin_dir = Path("/tmp/ffmpeg_bin"); bin_dir.mkdir(parents=True, exist_ok=True)
    ff = bin_dir / "ffmpeg"; ffp = bin_dir / "ffprobe"
    print("[ffmpeg] downloading static build...")
    url = "https://github.com/yt-dlp/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz"
    try:
        with urllib.request.urlopen(url, timeout=120) as resp:
            with tarfile.open(fileobj=resp, mode="r|xz") as tar:
                for member in tar:
                    bn = Path(member.name).name
                    if bn in ("ffmpeg","ffprobe") and member.isfile():
                        member.name = bn
                        tar.extract(member, path=str(bin_dir), filter="data")
        for p in [ff, ffp]:
            if p.is_file(): p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        if ff.is_file() and ffp.is_file(): return str(ff), str(ffp)
    except Exception as e: print(f"[ffmpeg] download failed: {e}")
    raise RuntimeError("ffmpeg not found.")

YTDLP = _find_bin("yt-dlp") or "/app/.venv/bin/yt-dlp"
FFMPEG, FFPROBE = _ensure_ffmpeg()
print(f"[bins] ffmpeg={FFMPEG}  ffprobe={FFPROBE}  yt-dlp={YTDLP}")

_YT_CLIENT_STRATEGIES = [
    "ios,android_embedded,tv_embedded",
    "ios,tv_embedded",
    "android_vr,android_embedded",
    "android,tv_embedded",
    "mweb",
]

def _is_youtube(url):
    try:
        from urllib.parse import urlparse
        h = urlparse(url).hostname or ""
        return "youtube" in h or "youtu.be" in h
    except: return False

def _get_site(url):
    try:
        from urllib.parse import urlparse
        return urlparse(url).hostname.replace("www.","")
    except: return "unknown"

def _yt_base_args(url, attempt=0):
    args = ["--socket-timeout","30","--retries","3","--fragment-retries","3"]
    if _is_youtube(url):
        strategy = _YT_CLIENT_STRATEGIES[min(attempt, len(_YT_CLIENT_STRATEGIES)-1)]
        args += ["--extractor-args", f"youtube:player_client={strategy}"]
    if YOUTUBE_COOKIES_FILE and os.path.isfile(YOUTUBE_COOKIES_FILE):
        args += ["--cookies", YOUTUBE_COOKIES_FILE]
    return args

def _sort_string(height): return f"res:{height}"

def _format_id_for_height(height):
    return (f"bestvideo[height<={height}]+bestaudio"
            f"/bestvideo[height<={height}]+bestaudio[ext=m4a]"
            f"/best[height<={height}]/bestvideo+bestaudio/best")

def _build_formats(info):
    duration = info.get("duration") or 0
    all_fmts = info.get("formats", [])
    seen = {}
    for f in all_fmts:
        fh = f.get("height") or 0
        if fh == 0: continue
        vc = f.get("vcodec","")
        if not vc or vc == "none": continue
        def score(fmt):
            if fmt.get("filesize"): return 3
            if fmt.get("filesize_approx"): return 2
            tbr = fmt.get("tbr") or fmt.get("vbr",0)
            if tbr and duration: return 1
            return 0
        if fh not in seen or score(f) > score(seen[fh]): seen[fh] = f
    heights = sorted(seen.keys(), reverse=True)
    max_avail = max(heights) if heights else 1080
    for h in [1080,720,480,360,240,144]:
        if h not in heights and h <= max_avail: heights.append(h)
    heights = sorted(set(heights), reverse=True)
    result = []
    for fh in heights:
        f = seen.get(fh); size = None; src = "unknown"
        if f:
            if f.get("filesize"): size = f["filesize"]; src = "exact"
            elif f.get("filesize_approx"): size = f["filesize_approx"]; src = "approx"
            else:
                tbr = (f.get("tbr") or f.get("vbr",0) or 0) + (f.get("abr",0) or 0)
                if tbr and duration: size = int(tbr*1000/8*duration); src = "est"
        result.append({"resolution":f"{fh}p","format_id":_format_id_for_height(fh),
                       "sort_string":_sort_string(fh),"height":fh,"ext":"mp4",
                       "filesize":size,"size_source":src})
    return result

def _vf_for_ratio(ratio, source_height=480):
    h = max((source_height if source_height > 0 else 480), 2)
    h = h if h % 2 == 0 else h - 1
    ratio_map = {"21:9":21/9,"2.35:1":2.35,"2:1":2.0,"16:9":16/9,"16:10":16/10,
                 "3:2":3/2,"5:4":5/4,"4:3":4/3,"1:1":1.0,"3:4":3/4,"9:16":9/16,"9:18":9/18,"9:21":9/21}
    if ratio == "original" or ratio not in ratio_map:
        return (f"scale=trunc(iw*min(1\\\\,{h}/max(ih\\\\,1))/2)*2:"
                f"trunc(min(ih\\\\,{h})/2)*2,setsar=1")
    w = int(h * ratio_map[ratio]); w = w if w % 2 == 0 else w - 1
    return (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:-1:-1:black,setsar=1")

_NOISE = ("github.com/yt-dlp","yt-dlp/wiki","how-do-i-pass-cookies",
          "from-browser or --cookies","manually pass cookies","effectively exporting",
          "Extractors#exporting","See https://","Also see https://","cookies for the",
          "pass cookies")

_bgutil_proc = None

async def _process_download(user_id, dl_id, url, format_id, title,
                             sort_string=None, height=480,
                             chat_target="me", aspect_ratio="16:9"):
    downloads = _get_downloads(user_id)
    dl = next((d for d in downloads if d["id"] == dl_id), None)
    if not dl: return
    sess = vf_sessions.get(user_id)
    if not sess:
        if dl: dl["status"] = "failed"; dl["error"] = "Session not active"
        return
    client = sess["client"]
    site = _get_site(url)
    dl.update({"progress":0,"eta":"Starting...","speed":None,"total_size_str":None})

    async def resolve_target():
        if not chat_target or chat_target == "me": return "me"
        try: return await client.get_input_entity(int(chat_target) if chat_target.lstrip("-").isdigit() else chat_target)
        except: return "me"

    try:
        target = await resolve_target()
        tmpdir = tempfile.mkdtemp(prefix="vf_")
        try:
            dl["status"] = "downloading"
            eff_sort = sort_string or _sort_string(height)
            eff_fmt = format_id or _format_id_for_height(height)

            async def _run_attempt(attempt):
                yt_args = _yt_base_args(url, attempt)
                raw_dir = os.path.join(tmpdir, f"raw{attempt}"); os.makedirs(raw_dir, exist_ok=True)
                p = await asyncio.create_subprocess_exec(
                    YTDLP, "--no-playlist", "-S", eff_sort, "-f", eff_fmt,
                    "--merge-output-format", "mp4",
                    "-o", os.path.join(raw_dir, "%(title).80s.%(ext)s"),
                    "--no-warnings","--no-part","--no-check-certificates",
                    "--extractor-retries","2","--newline",
                    *yt_args, url,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                errs = []
                async def _read():
                    async for raw in p.stdout:
                        line = raw.decode("utf-8", errors="ignore").strip()
                        if "[download]" in line and "%" in line:
                            try:
                                parts = line.split()
                                pct = next((x for x in parts if "%" in x), None)
                                if pct: dl["progress"] = min(round(float(pct.replace("%","")) * 0.8), 79)
                                if "ETA" in parts: dl["eta"] = parts[parts.index("ETA")+1]
                                if "at" in parts: dl["speed"] = parts[parts.index("at")+1]
                                if "of" in parts: dl["total_size_str"] = parts[parts.index("of")+1]
                            except: pass
                        elif "[Merger]" in line or "Merging" in line:
                            dl["progress"] = 75; dl["eta"] = "Merging streams..."
                    async for raw_line in p.stderr:
                        errs.append(raw_line.decode("utf-8", errors="ignore").strip())
                await asyncio.gather(_read(), p.wait())
                return p.returncode, errs, raw_dir

            last_msg = ""; downloaded = False; raw_path = None
            for attempt in range(len(_YT_CLIENT_STRATEGIES)):
                if attempt > 0: dl["eta"] = f"Retrying (method {attempt+1})..."
                rc, errs, raw_dir = await _run_attempt(attempt)
                if rc == 0:
                    files = list(Path(raw_dir).glob("*"))
                    if files: raw_path = files[0]; downloaded = True; break
                clean = [l for l in errs if l and not any(n in l for n in _NOISE)]
                if clean: last_msg = "\n".join(clean[-3:])
            if not downloaded:
                if "Sign in to confirm" in last_msg or "not a bot" in last_msg or not last_msg:
                    raise Exception("YouTube blocked this IP. Wait a moment and retry.")
                raise Exception(last_msg or "Download failed")

            dl["progress"] = 80; dl["eta"] = "Encoding for Telegram..."
            enc_dir = os.path.join(tmpdir, "enc"); os.makedirs(enc_dir, exist_ok=True)
            enc_path = os.path.join(enc_dir, raw_path.stem + "_tc.mp4")
            vidMeta_dur = None
            try:
                dur_p = await asyncio.create_subprocess_exec(
                    FFPROBE, "-v","error","-show_entries","format=duration",
                    "-of","default=noprint_wrappers=1:nokey=1", str(raw_path),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
                dur_out, _ = await dur_p.communicate()
                vidMeta_dur = float(dur_out.decode().strip())
            except: pass

            async def _run_ffmpeg(in_path, out_path, copy=False):
                cmd = [FFMPEG, "-y", "-i", str(in_path)]
                if copy:
                    cmd += ["-c","copy","-movflags","+faststart","-f","mp4", out_path]
                else:
                    vf_chain = f"format=yuv420p,{_vf_for_ratio(aspect_ratio, height)}"
                    cmd += ["-c:v","libx264","-preset","fast","-crf","23","-vf",vf_chain,
                            "-c:a","aac","-b:a","128k","-ar","44100","-ac","2",
                            "-max_muxing_queue_size","9999","-movflags","+faststart","-f","mp4",out_path]
                p = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
                try:
                    _, stderr_bytes = await asyncio.wait_for(p.communicate(), timeout=600)
                    for line in (stderr_bytes or b"").decode("utf-8", errors="ignore").splitlines():
                        if "time=" in line and vidMeta_dur:
                            try:
                                t = line.split("time=")[1].split()[0]
                                h_, m_, s_ = t.split(":")
                                secs = float(h_)*3600+float(m_)*60+float(s_)
                                dl["progress"] = min(80+int((secs/vidMeta_dur)*17), 97)
                                dl["eta"] = f"Encoding {t} / {int(vidMeta_dur)}s"
                            except: pass
                except asyncio.TimeoutError:
                    p.kill(); raise Exception("ffmpeg timed out")
                return p.returncode

            rc_ff = await _run_ffmpeg(raw_path, enc_path)
            if rc_ff != 0:
                dl["eta"] = "Re-muxing (fallback)..."
                copy_path = enc_path.replace("_tc.mp4","_copy.mp4")
                rc_copy = await _run_ffmpeg(raw_path, copy_path, copy=True)
                if rc_copy == 0: enc_path = copy_path
                else: raise Exception("ffmpeg encoding failed on both attempts")

            fpath = Path(enc_path); fsize = fpath.stat().st_size; vtitle = raw_path.stem
            dl["progress"] = 98; dl["eta"] = "Sending to Telegram..."; dl["speed"] = None

            def upload_progress(sent, total):
                if total:
                    dl["progress"] = min(98+int((sent/total)*2), 99)
                    dl["eta"] = f"Uploading {sent//1024//1024}/{total//1024//1024}MB..."

            await client.send_file(target, str(fpath),
                caption=f"📥 {vtitle}\n🌐 {site}\n🎬 via TeleFlow",
                supports_streaming=True, progress_callback=upload_progress)
            dl["status"] = "completed"; dl["title"] = vtitle
            dl["file_size"] = fsize; dl["progress"] = 100; dl["eta"] = None
            print(f"[{user_id[:8]}] ✅ Sent: {vtitle}")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
    except Exception as e:
        err = str(e); el = err.lower()
        if "format is not available" in el or "requested format" in el: friendly = "Quality unavailable — try a different resolution."
        elif "video unavailable" in el or "private video" in el: friendly = "Video is unavailable or private."
        elif "age" in el and ("sign" in el or "restrict" in el): friendly = "Age-restricted video."
        elif "live" in el and "stream" in el: friendly = "Live streams cannot be downloaded."
        elif "blocked" in el or "not a bot" in el: friendly = "YouTube blocked this server IP. Try again in a moment."
        elif "ffmpeg" in el: friendly = "Encoding failed. Try a different quality or aspect ratio."
        elif "session not active" in el: friendly = "Session expired — please log out and log back in."
        else: friendly = err[:300]
        print(f"[{user_id[:8]}] ❌ {err}")
        if dl: dl["status"] = "failed"; dl["error"] = friendly; dl["progress"] = 0; dl["eta"] = None
        try:
            sess2 = vf_sessions.get(user_id)
            if sess2: await sess2["client"].send_message("me", f"❌ Download failed\n🔗 {url}\n\n{friendly}")
        except: pass

async def fetch_playlist_entries(url):
    yt_args = _yt_base_args(url)
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, lambda: subprocess.run(
        [YTDLP,"--flat-playlist","-J","--no-warnings"] + yt_args + [url],
        capture_output=True, text=True, timeout=60))
    if result.returncode != 0: raise Exception(result.stderr[-300:] or "Playlist fetch failed")
    info = json.loads(result.stdout)
    base = "https://www.youtube.com/watch?v="; out = []
    for e in (info.get("entries") or []):
        if not e: continue
        eu = e.get("url") or e.get("webpage_url") or ""
        if eu and not eu.startswith("http"): eu = base + eu
        if eu: out.append({"url":eu,"title":e.get("title","")})
    return out

async def auto_update_ytdlp():
    try:
        p = await asyncio.create_subprocess_exec(
            "pip","install","-U","yt-dlp","--break-system-packages","--quiet",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        await asyncio.wait_for(p.communicate(), timeout=120)
        print("[yt-dlp] ✅ updated")
    except Exception as e: print(f"[yt-dlp] update error: {e}")

# ─── AUTH MIDDLEWARE (VideoFlow) ──────────────────────────────────────
def require_vf_auth(handler):
    async def wrapper(request):
        token = (request.headers.get("Authorization") or "").replace("Bearer ","").strip()
        if not token: token = request.cookies.get("vf_token","")
        if not token: return web.json_response({"ok":False,"error":"Unauthorized"}, status=401)
        user = get_user_by_token(token)
        if not user: return web.json_response({"ok":False,"error":"Unauthorized"}, status=401)
        request["user"] = user
        return await handler(request)
    return wrapper

# ─── CORS MIDDLEWARE ──────────────────────────────────────────────────
@web.middleware
async def cors_middleware(request, handler):
    if request.method == "OPTIONS":
        return web.Response(headers={
            "Access-Control-Allow-Origin":"*",
            "Access-Control-Allow-Methods":"GET, POST, OPTIONS",
            "Access-Control-Allow-Headers":"Content-Type, Authorization",
        })
    resp = await handler(request)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp

# ═══════════════════════════════════════════════════════════════════════
# API ROUTES — TELEFOW AI (userbot)
# ═══════════════════════════════════════════════════════════════════════
async def tf_api_status(request):
    me = bot_state["me"]
    return web.json_response({
        "ok": True,
        "user": {"name":me.first_name,"username":me.username,"id":me.id} if me else None,
        "ai_reply": bot_state["ai_reply"],
        "autoreply": bot_state["autoreply"]["active"],
        "autoreply_message": bot_state["autoreply"]["message"],
        "scrape_groups": bot_state["scrape_groups"],
        "model": OPENROUTER_MODEL,
        "log": bot_state["status_log"][-10:],
        "ai_persona": bot_state.get("ai_persona",""),
        "my_name": bot_state.get("my_name","me"),
        "task_status": bot_state["task_status"],
        "results": bot_state["results"],
        "messages_today": bot_state.get("messages_today",0),
    })

async def tf_api_clear_results(request):
    bot_state["results"] = []; bot_state["task_status"] = "idle"
    return web.json_response({"ok":True})

async def tf_api_toggle_ai(request):
    data = await request.json()
    bot_state["ai_reply"] = data.get("active", False)
    log(f"AI reply {'ON' if bot_state['ai_reply'] else 'OFF'} via web")
    return web.json_response({"ok":True,"ai_reply":bot_state["ai_reply"]})

async def tf_api_toggle_autoreply(request):
    data = await request.json()
    bot_state["autoreply"]["active"] = data.get("active", False)
    bot_state["autoreply"]["message"] = data.get("message","Hey! Busy rn, will reply soon 👍")
    bot_state["autoreply_replied"].clear()
    return web.json_response({"ok":True})

async def tf_api_summarise(request):
    data = await request.json()
    limit = data.get("limit", MAX_CHATS)
    bot_state["results"] = []; bot_state["task_status"] = "running"
    asyncio.create_task(do_summarise(limit))
    return web.json_response({"ok":True})

async def do_summarise(limit):
    try:
        results = []; count = 0
        async for dialog in bot_client.iter_dialogs():
            if count >= limit: break
            if dialog.archived: continue
            entity = dialog.entity; chat_name = dialog.name or "Unknown"
            bot_state["results"].append({"type":"status","content":f"Processing {chat_name}..."})
            messages = []
            async for msg in bot_client.iter_messages(entity, limit=MESSAGES_PER_CHAT):
                if msg.text:
                    try: sender = chat_name if isinstance(entity,User) else (getattr(await msg.get_sender(),"first_name",None) or "Someone")
                    except: sender = "Someone"
                    messages.append(f"{sender}: {msg.text}")
            if not messages: continue
            messages.reverse()
            summary = await summarise_chat(chat_name, messages)
            if summary:
                icon = "👤" if isinstance(entity,User) else "👥"
                unread = f" • {dialog.unread_count} unread" if dialog.unread_count > 0 else ""
                bot_state["results"].append({"type":"summary","name":f"{icon} {chat_name}{unread}","content":summary})
                count += 1
        bot_state["task_status"] = "done"
    except Exception as e:
        bot_state["results"].append({"type":"error","content":str(e)}); bot_state["task_status"] = "done"

async def tf_api_summarise_chat(request):
    data = await request.json()
    chat_name = data.get("name","")
    bot_state["results"] = []; bot_state["task_status"] = "running"
    asyncio.create_task(do_summarise_single(chat_name))
    return web.json_response({"ok":True})

async def do_summarise_single(query):
    try:
        async for dialog in bot_client.iter_dialogs():
            if query.lower() in (dialog.name or "").lower():
                messages = []
                async for msg in bot_client.iter_messages(dialog.entity, limit=MESSAGES_PER_CHAT):
                    if msg.text:
                        try: sender = getattr(await msg.get_sender(),"first_name",None) or dialog.name
                        except: sender = "Someone"
                        messages.append(f"{sender}: {msg.text}")
                if not messages:
                    bot_state["results"].append({"type":"empty","content":f"No messages in '{dialog.name}'"}); bot_state["task_status"] = "done"; return
                messages.reverse()
                summary = await summarise_chat(dialog.name, messages)
                bot_state["results"].append({"type":"summary","name":dialog.name,"content":summary}); bot_state["task_status"] = "done"; return
        bot_state["results"].append({"type":"error","content":f"No chat matching '{query}'"}); bot_state["task_status"] = "done"
    except Exception as e:
        bot_state["results"].append({"type":"error","content":str(e)}); bot_state["task_status"] = "done"

async def tf_api_scrape_links(request):
    data = await request.json()
    filter_key = data.get("filter","all"); group = data.get("group","").strip(); use_saved = data.get("use_saved",False)
    bot_state["results"] = []; bot_state["task_status"] = "running"
    asyncio.create_task(do_scrape_links(filter_key, group, use_saved))
    return web.json_response({"ok":True})

async def do_scrape_links(filter_key, group_query, use_saved):
    def push(t,c): bot_state["results"].append({"type":t,"content":c})
    try:
        pattern = LINK_FILTERS.get(filter_key, LINK_FILTERS["all"])
        URL_REGEX = re.compile(pattern, re.IGNORECASE)
        all_links = []; groups_checked = 0; start = asyncio.get_event_loop().time()
        tme_match = re.match(r'(?:https?://)?t\.me/([a-zA-Z0-9_]+)', group_query or "")
        if tme_match:
            username = tme_match.group(1); push("status",f"Resolving t.me/{username}...")
            try:
                entity = await bot_client.get_entity(username)
                name = getattr(entity,"title",None) or getattr(entity,"username",username)
                push("status",f"Scraping {name}...")
                group_links = []
                async for msg in bot_client.iter_messages(entity, limit=200):
                    if not msg.text: continue
                    group_links.extend(URL_REGEX.findall(msg.text))
                elapsed = asyncio.get_event_loop().time() - start
                if group_links:
                    unique = list(dict.fromkeys(group_links))
                    push("result",f"📌 {name} — {len(unique)} {filter_key} links ({elapsed:.0f}s)")
                    for link in unique: push("link",link)
                else: push("empty",f"No {filter_key} links in {name}")
            except Exception as e: push("error",f"Could not access: {e}")
            bot_state["task_status"] = "done"; return
        async for dialog in bot_client.iter_dialogs():
            entity = dialog.entity
            if isinstance(entity,User): continue
            name = dialog.name or ""
            if use_saved:
                if not any(g.lower() in name.lower() for g in bot_state["scrape_groups"]): continue
            elif group_query:
                if group_query.lower() not in name.lower(): continue
            groups_checked += 1; push("status",f"Checking {name}...")
            group_links = []
            async for msg in bot_client.iter_messages(entity, limit=200):
                if not msg.text: continue
                group_links.extend(URL_REGEX.findall(msg.text))
                if len(group_links) >= 50: break
            if group_links:
                unique = list(dict.fromkeys(group_links)); all_links.append((name,unique))
                push("result",f"📌 {name} — {len(unique)} links")
                for link in unique: push("link",link)
            else: push("empty",f"No {filter_key} links in {name}")
            if group_query and all_links: break
        elapsed = asyncio.get_event_loop().time() - start
        if all_links:
            total = sum(len(l) for _,l in all_links)
            push("done",f"Done — {total} {filter_key} links from {groups_checked} groups ({elapsed:.0f}s)")
        else: push("empty",f"No {filter_key} links found ({elapsed:.0f}s)")
    except Exception as e:
        bot_state["results"].append({"type":"error","content":str(e)})
    finally: bot_state["task_status"] = "done"

async def tf_api_scrape_groups(request):
    data = await request.json(); action = data.get("action"); name = data.get("name","")
    if action == "add" and name:
        if name not in bot_state["scrape_groups"]: bot_state["scrape_groups"].append(name)
    elif action == "remove":
        bot_state["scrape_groups"] = [g for g in bot_state["scrape_groups"] if g != name]
    return web.json_response({"ok":True,"groups":bot_state["scrape_groups"]})

async def tf_api_dialogs(request):
    try:
        dms = []; groups = []; count = 0
        async for dialog in bot_client.iter_dialogs():
            if count >= 60: break
            if dialog.archived: continue
            try:
                entity = dialog.entity; name = dialog.name or "Unknown"
                is_user = isinstance(entity,User) and not getattr(entity,"bot",False) and not getattr(entity,"is_self",False)
                item = {"name":name,"type":"dm" if is_user else "group","unread":dialog.unread_count or 0,
                        "id":str(dialog.id),"context":bot_state["chat_contexts"].get(name,"")}
                if is_user: dms.append(item)
                else: groups.append(item)
                count += 1
            except: continue
        return web.json_response({"ok":True,"dms":dms,"groups":groups})
    except Exception as e: return web.json_response({"ok":False,"error":str(e)}, status=500)

async def tf_api_set_context(request):
    data = await request.json(); name = data.get("name",""); context = data.get("context","")
    if name: bot_state["chat_contexts"][name] = context; log(f"Context set for {name}")
    return web.json_response({"ok":True})

async def tf_api_update_persona(request):
    data = await request.json()
    if "persona" in data: bot_state["ai_persona"] = data["persona"]
    if "my_name" in data and data["my_name"].strip(): bot_state["my_name"] = data["my_name"].strip()
    return web.json_response({"ok":True})

async def tf_api_get_memory(request):
    return web.json_response({"ok":True,"memory":bot_state["ai_memory"]})

async def tf_api_edit_memory(request):
    data = await request.json(); name = data.get("name","")
    if not name: return web.json_response({"ok":False,"error":"name required"}, status=400)
    if data.get("delete"): bot_state["ai_memory"].pop(name, None)
    elif "facts" in data: bot_state["ai_memory"][name] = [f for f in data["facts"] if f.strip()]
    return web.json_response({"ok":True,"memory":bot_state["ai_memory"]})

async def tf_api_analytics(request):
    analytics = bot_state["analytics"]; response_times = bot_state["response_times"]
    top_senders = sorted([{"name":k,"count":v["count"],"last_seen":v.get("last_seen","")} for k,v in analytics.items()], key=lambda x:x["count"], reverse=True)[:20]
    total_hours = [0]*24
    for data in analytics.values():
        for i,c in enumerate(data.get("hours",[0]*24)): total_hours[i] += c
    peak_hours = sorted([{"hour":i,"count":c} for i,c in enumerate(total_hours) if c>0], key=lambda x:x["count"], reverse=True)[:8]
    all_times = [t for times in response_times.values() for t in times]
    avg_response_time = round(sum(all_times)/len(all_times),1) if all_times else None
    return web.json_response({"ok":True,"top_senders":top_senders,"peak_hours":peak_hours,"avg_response_time":avg_response_time,"total_tracked":sum(v["count"] for v in analytics.values())})

async def tf_api_notes(request):
    if request.method == "GET": return web.json_response({"ok":True,"notes":list(reversed(bot_state["notes"]))})
    data = await request.json(); action = data.get("action")
    if action == "add":
        text = data.get("text","").strip()
        if text: bot_state["notes"].append({"text":text,"ts":datetime.now().strftime("%b %d, %H:%M")})
    elif action == "delete":
        idx = data.get("index",0)
        reversed_notes = list(reversed(bot_state["notes"]))
        if 0 <= idx < len(reversed_notes):
            original_idx = len(bot_state["notes"]) - 1 - idx
            bot_state["notes"].pop(original_idx)
    return web.json_response({"ok":True,"notes":list(reversed(bot_state["notes"]))})

# ═══════════════════════════════════════════════════════════════════════
# API ROUTES — VIDEOFLOW (multi-user video downloader)
# ═══════════════════════════════════════════════════════════════════════
async def vf_api_auth_send_code(request):
    data = await request.json(); phone = data.get("phone","").strip()
    if not phone: return web.json_response({"ok":False,"error":"Phone required"}, status=400)
    if not API_ID or not API_HASH: return web.json_response({"ok":False,"error":"Server not configured"}, status=500)
    tmp = TelegramClient(StringSession(), API_ID, API_HASH)
    try:
        await tmp.connect()
        result = await tmp.send_code_request(phone)
        tmp_session = tmp.session.save()
        conn = get_db()
        conn.execute("INSERT OR REPLACE INTO auth_sessions VALUES (?,?,?,?)",(phone,result.phone_code_hash,tmp_session,time.time()))
        conn.commit(); conn.close(); await tmp.disconnect()
        return web.json_response({"ok":True,"message":"OTP sent to your Telegram"})
    except Exception as e:
        try: await tmp.disconnect()
        except: pass
        return web.json_response({"ok":False,"error":str(e)}, status=400)

async def vf_api_auth_verify_code(request):
    data = await request.json()
    phone = data.get("phone","").strip(); code = data.get("code","").strip(); password = data.get("password","").strip()
    if not phone or not code: return web.json_response({"ok":False,"error":"Phone and code required"}, status=400)
    conn = get_db(); auth_row = conn.execute("SELECT * FROM auth_sessions WHERE phone=?", (phone,)).fetchone(); conn.close()
    if not auth_row: return web.json_response({"ok":False,"error":"No pending auth. Send code first."}, status=400)
    if time.time()-auth_row["created_at"] > 600: return web.json_response({"ok":False,"error":"Code expired."}, status=400)
    tmp = TelegramClient(StringSession(auth_row["tmp_session"]), API_ID, API_HASH)
    try:
        await tmp.connect()
        try: await tmp.sign_in(phone, code, phone_code_hash=auth_row["phone_code_hash"])
        except SessionPasswordNeededError:
            if not password: await tmp.disconnect(); return web.json_response({"ok":False,"error":"2FA_REQUIRED"}, status=200)
            await tmp.sign_in(password=password)
        except PhoneCodeInvalidError:
            await tmp.disconnect(); return web.json_response({"ok":False,"error":"Invalid OTP code"}, status=400)
        session_str = tmp.session.save(); me = await tmp.get_me(); await tmp.disconnect()
        user_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, phone)); token = str(uuid.uuid4())
        conn = get_db()
        conn.execute("INSERT OR REPLACE INTO users (id,phone,session,token,last_seen) VALUES (?,?,?,?,datetime('now'))",(user_id,phone,session_str,token))
        conn.execute("DELETE FROM auth_sessions WHERE phone=?",(phone,)); conn.commit(); conn.close()
        await start_user_session(user_id, session_str)
        return web.json_response({"ok":True,"token":token,"session":session_str,"user":{"name":me.first_name,"username":me.username,"id":me.id,"phone":phone}})
    except Exception as e:
        try: await tmp.disconnect()
        except: pass
        return web.json_response({"ok":False,"error":str(e)}, status=400)

async def vf_api_auth_verify_session(request):
    data = await request.json(); phone = data.get("phone","").strip(); session = data.get("session","").strip()
    if not phone or not session: return web.json_response({"ok":False,"error":"phone and session required"}, status=400)
    tmp = TelegramClient(StringSession(session), API_ID, API_HASH)
    try:
        await tmp.connect()
        if not await tmp.is_user_authorized(): await tmp.disconnect(); return web.json_response({"ok":False,"error":"Session expired"}, status=401)
        me = await tmp.get_me(); session_str = tmp.session.save(); await tmp.disconnect()
        user_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, phone)); token = str(uuid.uuid4())
        conn = get_db()
        conn.execute("INSERT OR REPLACE INTO users (id,phone,session,token,last_seen) VALUES (?,?,?,?,datetime('now'))",(user_id,phone,session_str,token))
        conn.commit(); conn.close()
        await start_user_session(user_id, session_str)
        return web.json_response({"ok":True,"token":token,"session":session_str,"user":{"name":me.first_name,"username":me.username,"id":me.id,"phone":phone}})
    except Exception as e:
        try: await tmp.disconnect()
        except: pass
        return web.json_response({"ok":False,"error":str(e)}, status=400)

async def vf_api_auth_logout(request):
    data = await request.json(); token = data.get("token","")
    conn = get_db(); row = conn.execute("SELECT id FROM users WHERE token=?",(token,)).fetchone()
    if row:
        user_id = row["id"]
        conn.execute("UPDATE users SET token=NULL WHERE id=?",(user_id,)); conn.commit()
        sess = vf_sessions.pop(user_id, None)
        if sess:
            try: await sess["client"].disconnect()
            except: pass
    conn.close()
    return web.json_response({"ok":True})

@require_vf_auth
async def vf_api_status(request):
    user = request["user"]; user_id = user["id"]
    sess = vf_sessions.get(user_id)
    if not sess:
        asyncio.create_task(start_user_session(user_id, user.get("session","")))
        return web.json_response({"ok":True,"online":False})
    me = sess.get("me")
    return web.json_response({"ok":True,"online":True,"user":{"name":me.first_name,"username":me.username,"id":me.id} if me else None})

@require_vf_auth
async def vf_api_formats(request):
    url = request.rel_url.query.get("url","")
    if not url: return web.json_response({"error":"url required"}, status=400)
    try:
        loop = asyncio.get_event_loop()
        def _run(attempt):
            yt_args = _yt_base_args(url, attempt)
            return subprocess.run([YTDLP,"--no-playlist","-J","--no-warnings","--no-check-certificates"]+yt_args+[url], capture_output=True, text=True, timeout=60)
        result = None; fetch_err = ""
        for attempt in range(len(_YT_CLIENT_STRATEGIES)):
            result = await loop.run_in_executor(None, lambda a=attempt: _run(a))
            if result.returncode == 0: break
            clean = [l for l in result.stderr.splitlines() if l and not any(n in l for n in _NOISE)]
            fetch_err = "\n".join(clean[-3:]) if clean else fetch_err
        if result.returncode != 0:
            err_msg = "YouTube blocked this server IP." if ("Sign in to confirm" in result.stderr or "not a bot" in result.stderr) else (fetch_err or "Failed to fetch video info")
            return web.json_response({"error":err_msg}, status=400)
        info = json.loads(result.stdout)
        return web.json_response({"title":info.get("title"),"thumbnail":info.get("thumbnail"),"duration":info.get("duration"),"uploader":info.get("uploader"),"site":info.get("extractor_key") or _get_site(url),"formats":_build_formats(info)})
    except Exception as e: return web.json_response({"error":str(e)}, status=500)

@require_vf_auth
async def vf_api_chats(request):
    user_id = request["user"]["id"]; sess = vf_sessions.get(user_id)
    if not sess: return web.json_response({"error":"Session not active"}, status=400)
    try:
        chats = []
        async for dialog in sess["client"].iter_dialogs(limit=40):
            if dialog.archived: continue
            chats.append({"id":str(dialog.id),"name":dialog.name or "Unknown","type":"dm" if isinstance(dialog.entity,User) else "group"})
        return web.json_response({"ok":True,"chats":chats})
    except Exception as e: return web.json_response({"error":str(e)}, status=500)

@require_vf_auth
async def vf_api_playlist(request):
    url = request.rel_url.query.get("url","")
    if not url: return web.json_response({"error":"url required"}, status=400)
    try:
        entries = await fetch_playlist_entries(url)
        return web.json_response({"ok":True,"count":len(entries),"entries":entries})
    except Exception as e: return web.json_response({"error":str(e)}, status=500)

@require_vf_auth
async def vf_api_download(request):
    user = request["user"]; user_id = user["id"]; data = await request.json()
    url = data.get("url",""); format_id = data.get("format_id") or _format_id_for_height(480)
    sort_string = data.get("sort_string",""); height = int(data.get("height",480))
    title = data.get("title",url); quality = data.get("quality_label",f"{height}p")
    chat_target = data.get("chat_target","me"); aspect_ratio = data.get("aspect_ratio","16:9")
    if not url: return web.json_response({"error":"url required"}, status=400)
    if not vf_sessions.get(user_id): return web.json_response({"error":"Session not active"}, status=400)
    dl_id = str(uuid.uuid4())[:8]
    dl = {"id":dl_id,"url":url,"title":title,"site":_get_site(url),"quality":quality,
          "status":"pending","progress":0,"created_at":datetime.now(timezone.utc).isoformat(),
          "file_size":None,"error":None,"chat_target":chat_target}
    downloads = _get_downloads(user_id); downloads.insert(0,dl); user_downloads[user_id] = downloads[:200]
    task = asyncio.create_task(_process_download(user_id,dl_id,url,format_id,title,sort_string=sort_string,height=height,chat_target=chat_target,aspect_ratio=aspect_ratio))
    scheduled_jobs[dl_id] = task
    return web.json_response({"ok":True,"downloadId":dl_id})

@require_vf_auth
async def vf_api_batch(request):
    user = request["user"]; user_id = user["id"]; data = await request.json()
    height = int(data.get("height",480)); chat_target = data.get("chat_target","me"); aspect_ratio = data.get("aspect_ratio","16:9")
    urls = data.get("urls") or []; playlist_url = data.get("playlist_url","")
    if playlist_url:
        try: entries = await fetch_playlist_entries(playlist_url); urls = [e["url"] for e in entries] + urls
        except Exception as e: return web.json_response({"error":f"Playlist fetch failed: {e}"}, status=400)
    if not urls: return web.json_response({"error":"No URLs"}, status=400)
    if not vf_sessions.get(user_id): return web.json_response({"error":"Session not active"}, status=400)
    ids = []
    for url in urls[:50]:
        url = url.strip()
        if not url: continue
        dl_id = str(uuid.uuid4())[:8]
        dl = {"id":dl_id,"url":url,"title":url,"site":_get_site(url),"quality":f"{height}p","status":"pending","progress":0,"created_at":datetime.now(timezone.utc).isoformat(),"file_size":None,"error":None,"chat_target":chat_target}
        _get_downloads(user_id).insert(0,dl)
        task = asyncio.create_task(_process_download(user_id,dl_id,url,_format_id_for_height(height),url,sort_string=_sort_string(height),height=height,chat_target=chat_target,aspect_ratio=aspect_ratio))
        scheduled_jobs[dl_id] = task; ids.append(dl_id)
    return web.json_response({"ok":True,"queued":len(ids),"ids":ids})

@require_vf_auth
async def vf_api_schedule(request):
    user = request["user"]; user_id = user["id"]; data = await request.json()
    url = data.get("url",""); run_at_str = data.get("run_at",""); height = int(data.get("height",480)); chat_target = data.get("chat_target","me")
    if not url or not run_at_str: return web.json_response({"error":"url and run_at required"}, status=400)
    try: run_at = datetime.fromisoformat(run_at_str.replace("Z","+00:00"))
    except: return web.json_response({"error":"Invalid run_at format"}, status=400)
    if not vf_sessions.get(user_id): return web.json_response({"error":"Session not active"}, status=400)
    sch_id = str(uuid.uuid4())[:8]; delay = max((run_at-datetime.now(timezone.utc)).total_seconds(),0)
    async def _delayed():
        if delay > 0: await asyncio.sleep(delay)
        dl_id = str(uuid.uuid4())[:8]
        dl = {"id":dl_id,"url":url,"title":url,"site":_get_site(url),"quality":f"{height}p","status":"pending","progress":0,"created_at":datetime.now(timezone.utc).isoformat(),"file_size":None,"error":None,"chat_target":chat_target}
        _get_downloads(user_id).insert(0,dl)
        await _process_download(user_id,dl_id,url,_format_id_for_height(height),url,sort_string=_sort_string(height),height=height,chat_target=chat_target)
    scheduled_jobs[sch_id] = asyncio.create_task(_delayed())
    return web.json_response({"ok":True,"scheduledId":sch_id,"run_at":run_at.isoformat(),"delay_seconds":int(delay)})

@require_vf_auth
async def vf_api_schedules(request):
    jobs = [{"id":k,"status":"pending" if not t.done() else "done"} for k,t in scheduled_jobs.items()]
    return web.json_response({"ok":True,"schedules":jobs})

@require_vf_auth
async def vf_api_dl_status(request):
    user_id = request["user"]["id"]; dl_id = request.match_info["id"]
    dl = next((d for d in _get_downloads(user_id) if d["id"] == dl_id), None)
    if not dl: return web.json_response({"error":"Not found"}, status=404)
    return web.json_response(dl)

@require_vf_auth
async def vf_api_queue(request):
    user_id = request["user"]["id"]
    return web.json_response({"ok":True,"queue":_get_downloads(user_id)[:50]})

@require_vf_auth
async def vf_api_analytics(request):
    user_id = request["user"]["id"]; downloads = _get_downloads(user_id)
    total = len(downloads); completed = sum(1 for d in downloads if d["status"]=="completed"); failed = sum(1 for d in downloads if d["status"]=="failed")
    sites = {}
    for d in downloads: s = d.get("site","unknown"); sites[s] = sites.get(s,0)+1
    return web.json_response({"downloads":downloads[:100],"stats":{"total":total,"completed":completed,"failed":failed,"pending":total-completed-failed,"unique_sites":len(sites),"total_bytes":sum(d.get("file_size") or 0 for d in downloads)},"bySite":[{"site":k,"count":v} for k,v in sorted(sites.items(),key=lambda x:-x[1])]})

# ─── FRONTEND SERVING ─────────────────────────────────────────────────
BASEDIR = os.path.dirname(__file__)

async def serve_telefow(request):
    return web.FileResponse(os.path.join(BASEDIR, "index.html"))

async def serve_videoflow(request):
    return web.FileResponse(os.path.join(BASEDIR, "videos.html"))

# ─── APP FACTORY ──────────────────────────────────────────────────────
def make_app():
    app = web.Application(middlewares=[cors_middleware])
    # Frontend
    app.router.add_get("/", serve_telefow)
    app.router.add_get("/videos", serve_videoflow)
    app.router.add_get("/videos.html", serve_videoflow)
    # TeleFlow AI API
    app.router.add_get("/api/status", tf_api_status)
    app.router.add_post("/api/ai-reply", tf_api_toggle_ai)
    app.router.add_post("/api/autoreply", tf_api_toggle_autoreply)
    app.router.add_post("/api/summarise", tf_api_summarise)
    app.router.add_post("/api/summarise-chat", tf_api_summarise_chat)
    app.router.add_post("/api/links", tf_api_scrape_links)
    app.router.add_post("/api/scrape-groups", tf_api_scrape_groups)
    app.router.add_get("/api/dialogs", tf_api_dialogs)
    app.router.add_post("/api/persona", tf_api_update_persona)
    app.router.add_post("/api/chat-context", tf_api_set_context)
    app.router.add_post("/api/clear-results", tf_api_clear_results)
    app.router.add_get("/api/memory", tf_api_get_memory)
    app.router.add_post("/api/memory", tf_api_edit_memory)
    app.router.add_get("/api/analytics", tf_api_analytics)
    app.router.add_route("*", "/api/notes", tf_api_notes)
    # VideoFlow API
    app.router.add_post("/api/auth/send-code", vf_api_auth_send_code)
    app.router.add_post("/api/auth/verify-code", vf_api_auth_verify_code)
    app.router.add_post("/api/auth/verify-session", vf_api_auth_verify_session)
    app.router.add_post("/api/auth/logout", vf_api_auth_logout)
    app.router.add_get("/api/vf/status", vf_api_status)
    app.router.add_get("/api/vf/formats", vf_api_formats)
    app.router.add_get("/api/vf/chats", vf_api_chats)
    app.router.add_get("/api/vf/playlist", vf_api_playlist)
    app.router.add_post("/api/vf/download", vf_api_download)
    app.router.add_post("/api/vf/batch", vf_api_batch)
    app.router.add_post("/api/vf/schedule", vf_api_schedule)
    app.router.add_get("/api/vf/schedules", vf_api_schedules)
    app.router.add_get("/api/vf/queue", vf_api_queue)
    app.router.add_get("/api/vf/dl/{id}", vf_api_dl_status)
    app.router.add_get("/api/vf/analytics", vf_api_analytics)
    return app

# ─── MAIN ─────────────────────────────────────────────────────────────
async def main():
    init_db()
    log("Starting TeleFlow Unified...")

    if bot_client:
        await bot_client.start()
        if await bot_client.is_user_authorized():
            me = await bot_client.get_me()
            bot_state["me"] = me
            log(f"✅ Userbot: {me.first_name} (@{me.username})")
        else:
            log("⚠️ Userbot session invalid — AI features disabled")
    else:
        log("⚠️ No TELEGRAM_SESSION — userbot features disabled")

    async def midnight_reset():
        while True:
            await asyncio.sleep(86400)
            bot_state["messages_today"] = 0
            goodbye_senders.clear()
            log("Daily counters reset")
    asyncio.create_task(midnight_reset())

    asyncio.create_task(auto_update_ytdlp())

    # Restore VideoFlow sessions from DB
    conn = get_db()
    users = conn.execute("SELECT id,session FROM users WHERE session IS NOT NULL AND token IS NOT NULL").fetchall()
    conn.close()
    log(f"Restoring {len(users)} VideoFlow sessions...")
    for row in users:
        if row["session"]:
            asyncio.create_task(start_user_session(row["id"], row["session"]))
            await asyncio.sleep(0.3)

    app = make_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log(f"✅ Unified server on port {PORT}")
    log("   / → TeleFlow AI dashboard")
    log("   /videos → VideoFlow downloader")

    if bot_client:
        await bot_client.run_until_disconnected()
    else:
        await asyncio.Event().wait()

asyncio.run(main())
