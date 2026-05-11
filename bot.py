import os
import asyncio
import re
import json
from datetime import datetime, timezone
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import User, Channel
import httpx
from dotenv import load_dotenv
from aiohttp import web

load_dotenv()

API_ID            = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH          = os.getenv("TELEGRAM_API_HASH", "")
OPENROUTER_API_KEY= os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL  = os.getenv("OPENROUTER_MODEL", "stepfun/step-3.5-flash:free")
MESSAGES_PER_CHAT = int(os.getenv("MESSAGES_PER_CHAT", "30"))
MAX_CHATS         = int(os.getenv("MAX_CHATS", "10"))
SESSION_STRING    = os.getenv("TELEGRAM_SESSION", "")
PORT              = int(os.getenv("PORT", "8080"))

# Resolve paths relative to this file — works on Railway too
BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
INDEX_HTML = os.path.join(BASE_DIR, "index.html")
COURSES_HTML = os.path.join(BASE_DIR, "courses.html")

client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

# ─── GLOBAL STATE ────────────────────────────────────────────────────
state = {
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
    # ── Course Scraper ──
    "course_groups": [],
    "courses": [],
    "courses_status": "idle",
    "courses_last_updated": None,
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
    print(msg, flush=True)
    state["status_log"].append(msg)
    if len(state["status_log"]) > 50:
        state["status_log"].pop(0)


# ─── OPENROUTER ──────────────────────────────────────────────────────
async def call_openrouter(prompt: str) -> str:
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/telegram-userbot",
        "X-Title": "TeleFlow AI",
    }
    payload = {
        "model": OPENROUTER_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 512,
    }
    async with httpx.AsyncClient(timeout=60) as http:
        r = await http.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=payload)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()


async def summarise_chat(chat_name, messages):
    if not messages:
        return None
    conversation = "\n".join(messages)
    prompt = (
        f"Summarise this Telegram conversation from '{chat_name}' in 2-4 sentences. "
        f"Focus on key topics, decisions, or anything needing attention. Be concise.\n\n{conversation}"
    )
    try:
        return await call_openrouter(prompt)
    except Exception as e:
        return f"Error: {e}"


# ─── BOT COMMANDS ────────────────────────────────────────────────────
@client.on(events.NewMessage(pattern=r"\.help", outgoing=True))
async def handle_help(event):
    await event.delete()
    await client.send_message("me", """🤖 **TeleFlow AI Commands**

📋 **SUMMARISE**
`.summarise` — Top 10 chats
`.summarise 20` — Top N chats
`.sum John` — One specific chat
`.folder` — List folders
`.folder Work` — Summarise folder

🔗 **LINKS**
`.links` — All links from all groups
`.links mega` — Only Mega links
`.links drive` — Google Drive only
`.links mega Movies` — Mega from one group
`.scrape add Movies` — Save group to scrape list
`.scrape remove Movies` — Remove from list
`.scrape list` — See saved groups
`.links mega scrape` — Scrape saved groups only

🤖 **AI REPLY**
`.aireply on` — AI replies to ALL DMs
`.aireply off` — Turn off

🧠 **AI MEMORY**
`.memory` — See all learned facts summary
`.memory John` — Facts learned about John

📊 **ANALYTICS**
`.analytics` — Top senders, peak hours, response time

💬 **SIMPLE REPLY**
`.autoreply on Busy!` — Fixed reply
`.autoreply off` — Turn off

📢 `.broadcast Hello!` — Send to 10 contacts ⚠️""")


@client.on(events.NewMessage(pattern=r"\.scrape (add|remove|list)(?: (.+))?", outgoing=True))
async def handle_scrape(event):
    await event.delete()
    action = event.pattern_match.group(1)
    name = (event.pattern_match.group(2) or "").strip()
    if action == "add" and name:
        if name not in state["scrape_groups"]:
            state["scrape_groups"].append(name)
        await client.send_message("me", f"✅ Added **{name}** to scrape list.\nGroups: {', '.join(state['scrape_groups'])}")
    elif action == "remove" and name:
        state["scrape_groups"] = [g for g in state["scrape_groups"] if g.lower() != name.lower()]
        await client.send_message("me", f"🗑️ Removed **{name}**.\nGroups: {', '.join(state['scrape_groups']) or 'empty'}")
    elif action == "list":
        if state["scrape_groups"]:
            await client.send_message("me", "📋 Scrape groups:\n" + "\n".join(f"• {g}" for g in state["scrape_groups"]))
        else:
            await client.send_message("me", "📋 No saved groups. Use `.scrape add GroupName`")


@client.on(events.NewMessage(pattern=r"\.summarise(all)?(\s+\d+)?", outgoing=True))
async def handle_summarise(event):
    await event.delete()
    args = event.pattern_match.group(2)
    limit = int(args.strip()) if args else MAX_CHATS
    status = await client.send_message("me", f"⏳ Summarising top {limit} chats...")
    results = []
    count = 0
    async for dialog in client.iter_dialogs():
        if count >= limit:
            break
        if dialog.archived:
            continue
        entity = dialog.entity
        chat_name = dialog.name or "Unknown"
        messages = []
        async for msg in client.iter_messages(entity, limit=MESSAGES_PER_CHAT):
            if msg.text:
                try:
                    sender = chat_name if isinstance(entity, User) else (getattr(await msg.get_sender(), "first_name", None) or "Someone")
                except:
                    sender = "Someone"
                messages.append(f"{sender}: {msg.text}")
        if not messages:
            continue
        messages.reverse()
        summary = await summarise_chat(chat_name, messages)
        if summary:
            icon = "👤" if isinstance(entity, User) else "👥"
            unread = f" • 🔴 {dialog.unread_count} unread" if dialog.unread_count > 0 else ""
            results.append(f"{icon} **{chat_name}**{unread}\n{summary}")
            count += 1
        if count % 5 == 0 and count > 0:
            await status.edit(f"⏳ Done {count}/{limit}...")
    if not results:
        await status.edit("📭 No messages found.")
        return
    await status.delete()
    full = f"📋 Summary of {count} chats\n\n" + "\n\n".join(results)
    for chunk in [full[i:i+4000] for i in range(0, len(full), 4000)]:
        await client.send_message("me", chunk)


@client.on(events.NewMessage(pattern=r"\.sum (.+)", outgoing=True))
async def handle_single(event):
    await event.delete()
    query = event.pattern_match.group(1).strip()
    status = await client.send_message("me", f"🔍 Looking for '{query}'...")
    async for dialog in client.iter_dialogs():
        if query.lower() in dialog.name.lower():
            messages = []
            async for msg in client.iter_messages(dialog.entity, limit=MESSAGES_PER_CHAT):
                if msg.text:
                    try:
                        sender = getattr(await msg.get_sender(), "first_name", None) or dialog.name
                    except:
                        sender = "Someone"
                    messages.append(f"{sender}: {msg.text}")
            if not messages:
                await status.edit(f"📭 No messages in '{dialog.name}'.")
                return
            messages.reverse()
            summary = await summarise_chat(dialog.name, messages)
            await status.edit(f"📋 **{dialog.name}**\n\n{summary}")
            return
    await status.edit(f"❌ No chat matching '{query}'")


@client.on(events.NewMessage(pattern=r"\.folder ?(.+)?", outgoing=True))
async def handle_folder(event):
    await event.delete()
    query = (event.pattern_match.group(1) or "").strip().lower()
    from telethon.tl.functions.messages import GetDialogFiltersRequest
    filters = await client(GetDialogFiltersRequest())
    if not query:
        names = []
        for f in filters.filters:
            title = getattr(f, "title", None)
            if title:
                t = title if isinstance(title, str) else getattr(title, "text", str(title))
                names.append(t)
        await client.send_message("me", "📁 Folders:\n" + "\n".join(f"• {n}" for n in names) if names else "No folders found.")
        return
    target_filter = None
    for f in filters.filters:
        title = getattr(f, "title", None)
        if not title:
            continue
        t = title if isinstance(title, str) else getattr(title, "text", str(title))
        if query in t.lower():
            target_filter = f
            break
    if not target_filter:
        await client.send_message("me", f"❌ No folder matching '{query}'.")
        return
    status = await client.send_message("me", f"📁 Summarising '{query}'...")
    results = []
    for peer in getattr(target_filter, "include_peers", []):
        try:
            entity = await client.get_entity(peer)
            chat_name = getattr(entity, "title", None) or getattr(entity, "first_name", "Unknown")
            messages = []
            async for msg in client.iter_messages(entity, limit=MESSAGES_PER_CHAT):
                if msg.text:
                    try:
                        sender = getattr(await msg.get_sender(), "first_name", None) or chat_name
                    except:
                        sender = "Someone"
                    messages.append(f"{sender}: {msg.text}")
            if not messages:
                continue
            messages.reverse()
            summary = await summarise_chat(chat_name, messages)
            if summary:
                results.append(f"💬 **{chat_name}**\n{summary}")
        except:
            continue
    if not results:
        await status.edit("📭 No messages found.")
        return
    await status.delete()
    full = f"📁 **{query}**\n\n" + "\n\n".join(results)
    for chunk in [full[i:i+4000] for i in range(0, len(full), 4000)]:
        await client.send_message("me", chunk)


@client.on(events.NewMessage(pattern=r"\.links ?(.+)?", outgoing=True))
async def handle_links(event):
    await event.delete()
    raw = (event.pattern_match.group(1) or "").strip()
    parts = raw.split(" ", 1)
    filter_key = parts[0].lower() if parts else "all"
    group_query = parts[1].strip() if len(parts) > 1 else ""
    use_scrape_list = group_query == "scrape"
    if filter_key not in LINK_FILTERS:
        pattern = LINK_FILTERS["all"]
        filter_label = "all"
        group_query = raw
    else:
        pattern = LINK_FILTERS[filter_key]
        filter_label = filter_key
    URL_REGEX = re.compile(pattern, re.IGNORECASE)
    status = await client.send_message("me", f"🔍 Scraping {filter_label} links...")
    all_links = []
    groups_checked = 0
    start = asyncio.get_event_loop().time()
    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        if isinstance(entity, User):
            continue
        name = dialog.name or ""
        if use_scrape_list:
            if not any(g.lower() in name.lower() for g in state["scrape_groups"]):
                continue
        elif group_query and group_query != "scrape":
            if group_query.lower() not in name.lower():
                continue
        groups_checked += 1
        group_links = []
        async for msg in client.iter_messages(entity, limit=200):
            if not msg.text:
                continue
            found = URL_REGEX.findall(msg.text)
            group_links.extend(found)
            if len(group_links) >= 50:
                break
        if group_links:
            unique = list(dict.fromkeys(group_links))
            all_links.append(f"📌 **{name}** ({len(unique)})\n" + "\n".join(unique))
        if group_query and group_query != "scrape" and all_links:
            break
    elapsed = asyncio.get_event_loop().time() - start
    if not all_links:
        await status.edit(f"📭 No {filter_label} links found. ({elapsed:.0f}s)")
        return
    await status.delete()
    total = sum(len(g.split("\n")) - 1 for g in all_links)
    full = f"🔗 {filter_label.upper()} — {total} links, {groups_checked} groups ({elapsed:.0f}s)\n\n" + "\n\n".join(all_links)
    for chunk in [full[i:i+4000] for i in range(0, len(full), 4000)]:
        await client.send_message("me", chunk)


@client.on(events.NewMessage(pattern=r"\.aireply (on|off)", outgoing=True))
async def handle_aireply_toggle(event):
    await event.delete()
    state["ai_reply"] = event.pattern_match.group(1) == "on"
    status = "✅ ON — replying to ALL DMs" if state["ai_reply"] else "🔕 OFF"
    await client.send_message("me", f"🤖 AI reply: {status}")
    log(f"AI reply: {status}")


@client.on(events.NewMessage(pattern=r"\.memory ?(.+)?", outgoing=True))
async def handle_memory_cmd(event):
    await event.delete()
    query = (event.pattern_match.group(1) or "").strip()
    if not state["ai_memory"]:
        await client.send_message("me", "🧠 No memories yet — AI learns as it chats!")
        return
    if query:
        matches = {k: v for k, v in state["ai_memory"].items() if query.lower() in k.lower()}
        if not matches:
            await client.send_message("me", f"🧠 No memory for '{query}'")
            return
        lines = []
        for name, facts in matches.items():
            lines.append(f"**{name}** ({len(facts)} facts)")
            lines.extend(f"  • {f}" for f in facts)
        await client.send_message("me", "🧠 **AI Memory**\n\n" + "\n".join(lines))
    else:
        summary = "\n".join(f"• {name}: {len(facts)} facts" for name, facts in sorted(state["ai_memory"].items(), key=lambda x: -len(x[1])))
        await client.send_message("me", f"🧠 **AI Memory** — {len(state['ai_memory'])} people\n\n{summary}")


@client.on(events.NewMessage(pattern=r"\.analytics", outgoing=True))
async def handle_analytics_cmd(event):
    await event.delete()
    if not state["analytics"]:
        await client.send_message("me", "📊 No analytics yet — start chatting!")
        return
    leaderboard = sorted(state["analytics"].items(), key=lambda x: -x[1]["count"])[:10]
    lines = ["📊 **Chat Analytics**\n", "**Top DMs by messages:**"]
    for i, (name, data) in enumerate(leaderboard, 1):
        lines.append(f"{i}. {name} — {data['count']} msgs")
    total_hours = [0] * 24
    for data in state["analytics"].values():
        for i, c in enumerate(data.get("hours", [0]*24)):
            total_hours[i] += c
    peak_hour = total_hours.index(max(total_hours))
    lines.append(f"\n**Peak hour:** {peak_hour:02d}:00–{(peak_hour+1)%24:02d}:00")
    if state["response_times"]:
        all_times = [t for times in state["response_times"].values() for t in times]
        avg = sum(all_times) / len(all_times)
        lines.append(f"**Avg AI response time:** {avg:.1f}s")
    await client.send_message("me", "\n".join(lines))


# ─── AI PERSONA + GOODBYE LOGIC ─────────────────────────────────────
DEFAULT_PERSONA = """You are replying on behalf of the user named {my_name} in a Telegram chat.

About the user:
- Casual and friendly, plain English
- Uses short forms naturally: irl, imo, ig, obv, fs, ngl, rn, rly, tbh, lmao, fr
- Replies are SHORT — 1 to 3 sentences max
- No emojis unless the other person uses them first
- Sounds human and natural, never robotic or formal
- Never starts a reply with "Hey" every single time — vary the opening
- Never reveals being an AI
- If asked something personal the user hasn't shared, give a vague natural deflection
- Reads the FULL conversation history carefully before replying
- Matches the energy and tone of the conversation

CRITICAL RULES ABOUT CONTINUING THE CONVERSATION:
- NEVER treat "ok", "okay", "hm", "lol", "haha", "nice", "cool", "k" as conversation-enders
- These are just acknowledgements — always keep the conversation going naturally
- Ask a follow-up question, share a thought, or react to what they said
- Only end the conversation if they explicitly say a FINAL goodbye like "bye", "gotta go", "ttyl", "talk later", "gtg", "see you", "good night"
- Even then, send ONE warm closing message like "buh bye!", "talk soon!", "catch you later!" — then stop replying
- If unsure whether it's goodbye, keep chatting"""

state["ai_persona"] = DEFAULT_PERSONA

goodbye_senders = set()

FINAL_GOODBYE_WORDS = [
    "bye", "goodbye", "gotta go", "gtg", "ttyl", "talk later",
    "see you", "see ya", "good night", "gn", "take care", "cya"
]

def is_final_goodbye(text: str) -> bool:
    t = text.lower().strip()
    return any(w in t for w in FINAL_GOODBYE_WORDS)

def is_just_acknowledgement(text: str) -> bool:
    t = text.lower().strip()
    acks = [
        "ok", "okay", "k", "lol", "haha", "hah", "hm", "hmm", "nice", "cool",
        "wow", "oh", "ah", "yeah", "yep", "yup", "sure", "alright", "aight",
        "ikr", "ik", "true", "facts", "fr", "lmao", "lmfao", "😂", "😅", "👍", "🙏"
    ]
    return t in acks


# ─── AI MEMORY ───────────────────────────────────────────────────────
def get_memory_for(name: str) -> str:
    facts = state["ai_memory"].get(name, [])
    if not facts:
        return ""
    return f"\nWhat you've learned about {name} from past conversations:\n" + "\n".join(f"- {f}" for f in facts[-20:]) + "\n"

async def extract_and_store_memory(sender_name: str, convo: str):
    existing = state["ai_memory"].get(sender_name, [])
    existing_text = "\n".join(f"- {f}" for f in existing) if existing else "None yet."
    prompt = f"""You are a memory extraction system. Read this conversation and extract NEW facts about {sender_name} that would help future AI replies sound more personal and accurate.

Existing known facts:
{existing_text}

Conversation:
{convo}

Extract only NEW facts not already listed. Focus on: their interests, job, mood, relationships, plans, preferences, or anything personal they shared.
If there's nothing genuinely new, return exactly: NONE

Return as a plain list, one fact per line, no bullets or numbers. Max 5 facts. Be specific."""
    try:
        raw = await call_openrouter(prompt)
        if raw.strip().upper() == "NONE" or not raw.strip():
            return
        new_facts = [line.strip() for line in raw.strip().splitlines() if line.strip() and len(line.strip()) > 8]
        if not new_facts:
            return
        if sender_name not in state["ai_memory"]:
            state["ai_memory"][sender_name] = []
        existing_lower = [f.lower() for f in state["ai_memory"][sender_name]]
        for fact in new_facts:
            if fact.lower() not in existing_lower:
                state["ai_memory"][sender_name].append(fact)
        state["ai_memory"][sender_name] = state["ai_memory"][sender_name][-50:]
        log(f"Memory updated for {sender_name} (+{len(new_facts)} facts)")
    except Exception as e:
        log(f"Memory extraction error: {e}")


# ─── ANALYTICS TRACKING ──────────────────────────────────────────────
def track_incoming(sender_name: str, sender_id: int, msg_time: datetime):
    hour = msg_time.hour
    if sender_name not in state["analytics"]:
        state["analytics"][sender_name] = {"count": 0, "last_seen": "", "hours": [0]*24}
    state["analytics"][sender_name]["count"] += 1
    state["analytics"][sender_name]["last_seen"] = msg_time.isoformat()
    state["analytics"][sender_name]["hours"][hour] += 1
    state["last_incoming_ts"][sender_id] = msg_time.timestamp()

def track_response_time(sender_name: str, sender_id: int):
    ts = state["last_incoming_ts"].get(sender_id)
    if ts:
        elapsed = datetime.now(timezone.utc).timestamp() - ts
        if elapsed < 3600:
            if sender_name not in state["response_times"]:
                state["response_times"][sender_name] = []
            state["response_times"][sender_name].append(round(elapsed, 1))
            state["response_times"][sender_name] = state["response_times"][sender_name][-100:]


@client.on(events.NewMessage(incoming=True))
async def handle_ai_autoreply(event):
    if not state["ai_reply"] or not event.is_private:
        return
    try:
        sender = await event.get_sender()
        if not sender or getattr(sender, "bot", False):
            return
        sender_name = getattr(sender, "first_name", None) or "Someone"
        sender_id   = event.sender_id
        my_name     = state.get("my_name", "me")

        msg_time = event.date if event.date.tzinfo else event.date.replace(tzinfo=timezone.utc)
        track_incoming(sender_name, sender_id, msg_time)

        if sender_id in goodbye_senders:
            return

        history = []
        async for msg in client.iter_messages(event.chat_id, limit=40):
            if msg.text:
                who = my_name if msg.out else sender_name
                history.append(f"{who}: {msg.text}")
        history.reverse()
        full_convo = "\n".join(history)

        persona      = state["ai_persona"].replace("{my_name}", my_name)
        chat_context = state["chat_contexts"].get(sender_name, "")
        extra        = f"\nExtra context about {sender_name}:\n{chat_context}\n" if chat_context else ""
        memory_context = get_memory_for(sender_name)

        if is_final_goodbye(event.text):
            goodbye_senders.add(sender_id)
            farewell_prompt = f"""{persona}
{extra}{memory_context}
Conversation:
{full_convo}

{sender_name} just said goodbye: "{event.text}"

Send ONE short warm farewell like "buh bye!", "talk soon!", "catch you later!" — keep it natural and brief:"""
            reply = await call_openrouter(farewell_prompt)
            await event.reply(reply)
            track_response_time(sender_name, sender_id)
            state["messages_today"] = state.get("messages_today", 0) + 1
            log(f"AI sent farewell to {sender_name}, stopping replies")
            return

        convo_note = ""
        if is_just_acknowledgement(event.text):
            convo_note = f'\nNOTE: "{event.text}" is just an acknowledgement, NOT a goodbye. Keep the conversation going.\n'

        prompt = f"""{persona}
{extra}{memory_context}{convo_note}
Full conversation with {sender_name}:
{full_convo}

{sender_name} just sent: {event.text}

Reply as {my_name} (short, casual, keep conversation going, no unnecessary emojis):"""

        reply = await call_openrouter(prompt)
        await event.reply(reply)
        track_response_time(sender_name, sender_id)
        state["messages_today"] = state.get("messages_today", 0) + 1
        log(f"AI replied to {sender_name}")
        asyncio.create_task(extract_and_store_memory(sender_name, full_convo))

    except Exception as e:
        log(f"AI reply error: {e}")


@client.on(events.NewMessage(pattern=r"\.autoreply (on|off)(?: (.+))?", outgoing=True))
async def handle_autoreply_toggle(event):
    await event.delete()
    mode = event.pattern_match.group(1)
    msg  = (event.pattern_match.group(2) or "").strip()
    if mode == "on":
        state["autoreply"]["active"]  = True
        state["autoreply"]["message"] = msg or "Hey! Busy rn, will reply soon 👍"
        state["autoreply_replied"].clear()
        await client.send_message("me", f"✅ Auto-reply ON: {state['autoreply']['message']}")
    else:
        state["autoreply"]["active"] = False
        state["autoreply_replied"].clear()
        await client.send_message("me", "🔕 Auto-reply OFF")


@client.on(events.NewMessage(incoming=True))
async def handle_incoming_autoreply(event):
    if not state["autoreply"]["active"] or not event.is_private:
        return
    if event.sender_id in state["autoreply_replied"]:
        return
    state["autoreply_replied"].add(event.sender_id)
    await event.reply(state["autoreply"]["message"])


@client.on(events.NewMessage(pattern=r"\.broadcast (.+)", outgoing=True))
async def handle_broadcast(event):
    await event.delete()
    msg    = event.pattern_match.group(1).strip()
    status = await client.send_message("me", "📢 Broadcasting to up to 10 contacts...")
    sent   = 0
    async for dialog in client.iter_dialogs():
        if sent >= 10:
            break
        if not isinstance(dialog.entity, User) or dialog.entity.bot or dialog.entity.is_self:
            continue
        try:
            await client.send_message(dialog.entity, msg)
            sent += 1
            await asyncio.sleep(2)
        except:
            continue
    await status.edit(f"✅ Sent to {sent} people.")


# ─── WEB API ─────────────────────────────────────────────────────────
async def api_status(request):
    me = state["me"]
    return web.json_response({
        "ok": True,
        "user": {"name": me.first_name, "username": me.username, "id": me.id} if me else None,
        "ai_reply": state["ai_reply"],
        "autoreply": state["autoreply"]["active"],
        "autoreply_message": state["autoreply"]["message"],
        "scrape_groups": state["scrape_groups"],
        "model": OPENROUTER_MODEL,
        "log": state["status_log"][-10:],
        "ai_persona": state.get("ai_persona", ""),
        "my_name": state.get("my_name", "me"),
        "task_status": state["task_status"],
        "results": state["results"],
        "messages_today": state.get("messages_today", 0),
    })

async def api_clear_results(request):
    state["results"]     = []
    state["task_status"] = "idle"
    return web.json_response({"ok": True})

async def api_get_memory(request):
    return web.json_response({"ok": True, "memory": state["ai_memory"]})

async def api_edit_memory(request):
    data = await request.json()
    name = data.get("name", "")
    if not name:
        return web.json_response({"ok": False, "error": "name required"}, status=400)
    if data.get("delete"):
        state["ai_memory"].pop(name, None)
        log(f"Memory cleared for {name}")
    elif "facts" in data:
        state["ai_memory"][name] = [f for f in data["facts"] if f.strip()]
        log(f"Memory updated for {name}")
    return web.json_response({"ok": True, "memory": state["ai_memory"]})

async def api_analytics(request):
    analytics      = state["analytics"]
    response_times = state["response_times"]
    top_senders = sorted(
        [{"name": k, "count": v["count"], "last_seen": v.get("last_seen", "")} for k, v in analytics.items()],
        key=lambda x: x["count"], reverse=True
    )[:20]
    total_hours = [0] * 24
    for data in analytics.values():
        for i, c in enumerate(data.get("hours", [0]*24)):
            total_hours[i] += c
    peak_hours = sorted(
        [{"hour": i, "count": c} for i, c in enumerate(total_hours) if c > 0],
        key=lambda x: x["count"], reverse=True
    )[:8]
    all_times = [t for times in response_times.values() for t in times]
    avg_response_time = round(sum(all_times) / len(all_times), 1) if all_times else None
    return web.json_response({
        "ok": True,
        "top_senders": top_senders,
        "peak_hours": peak_hours,
        "avg_response_time": avg_response_time,
        "total_tracked": sum(v["count"] for v in analytics.values()),
    })

async def api_update_persona(request):
    data = await request.json()
    if "persona" in data:
        state["ai_persona"] = data["persona"]
    if "my_name" in data and data["my_name"].strip():
        state["my_name"] = data["my_name"].strip()
    log("AI persona updated via web")
    return web.json_response({"ok": True})

async def api_toggle_ai(request):
    data = await request.json()
    state["ai_reply"] = data.get("active", False)
    log(f"AI reply {'ON' if state['ai_reply'] else 'OFF'} via web")
    return web.json_response({"ok": True, "ai_reply": state["ai_reply"]})

async def api_toggle_autoreply(request):
    data = await request.json()
    state["autoreply"]["active"]  = data.get("active", False)
    state["autoreply"]["message"] = data.get("message", "Hey! Busy rn, will reply soon 👍")
    state["autoreply_replied"].clear()
    log(f"Autoreply {'ON' if state['autoreply']['active'] else 'OFF'} via web")
    return web.json_response({"ok": True})

async def api_summarise(request):
    data = await request.json()
    limit = data.get("limit", MAX_CHATS)
    state["results"]     = []
    state["task_status"] = "running"
    asyncio.create_task(do_summarise(limit))
    return web.json_response({"ok": True, "message": f"Summarising {limit} chats..."})

async def do_summarise(limit):
    try:
        results = []
        count   = 0
        async for dialog in client.iter_dialogs():
            if count >= limit:
                break
            if dialog.archived:
                continue
            entity    = dialog.entity
            chat_name = dialog.name or "Unknown"
            state["results"].append({"type": "status", "content": f"Processing {chat_name}..."})
            messages = []
            async for msg in client.iter_messages(entity, limit=MESSAGES_PER_CHAT):
                if msg.text:
                    try:
                        sender = chat_name if isinstance(entity, User) else (getattr(await msg.get_sender(), "first_name", None) or "Someone")
                    except:
                        sender = "Someone"
                    messages.append(f"{sender}: {msg.text}")
            if not messages:
                continue
            messages.reverse()
            summary = await summarise_chat(chat_name, messages)
            if summary:
                icon   = "👤" if isinstance(entity, User) else "👥"
                unread = f" • {dialog.unread_count} unread" if dialog.unread_count > 0 else ""
                state["results"].append({"type": "summary", "name": f"{icon} {chat_name}{unread}", "content": summary})
                results.append(f"{icon} {chat_name}{unread}\n{summary}")
                count += 1
        state["task_status"] = "done"
        if results:
            full = f"📋 Summary of {count} chats\n\n" + "\n\n".join(results)
            for chunk in [full[i:i+4000] for i in range(0, len(full), 4000)]:
                await client.send_message("me", chunk)
    except Exception as e:
        state["results"].append({"type": "error", "content": str(e)})
        state["task_status"] = "done"

async def api_scrape_links(request):
    data      = await request.json()
    filter_key= data.get("filter", "all")
    group     = data.get("group", "").strip()
    use_saved = data.get("use_saved", False)
    state["results"]     = []
    state["task_status"] = "running"
    asyncio.create_task(do_scrape_links(filter_key, group, use_saved))
    return web.json_response({"ok": True, "message": "Scraping started..."})

async def do_scrape_links(filter_key, group_query, use_saved):
    def push(item_type, content):
        state["results"].append({"type": item_type, "content": content})
        log(content)
    try:
        pattern   = LINK_FILTERS.get(filter_key, LINK_FILTERS["all"])
        URL_REGEX = re.compile(pattern, re.IGNORECASE)
        all_links = []
        groups_checked = 0
        start = asyncio.get_event_loop().time()

        tme_match = re.match(r'(?:https?://)?t\.me/([a-zA-Z0-9_]+)', group_query or "")
        if tme_match:
            username = tme_match.group(1)
            push("status", f"Resolving t.me/{username}...")
            try:
                entity = await client.get_entity(username)
                name   = getattr(entity, "title", None) or getattr(entity, "username", username)
                push("status", f"Scraping {name}...")
                group_links = []
                async for msg in client.iter_messages(entity, limit=200):
                    if not msg.text:
                        continue
                    found = URL_REGEX.findall(msg.text)
                    group_links.extend(found)
                elapsed = asyncio.get_event_loop().time() - start
                if group_links:
                    unique = list(dict.fromkeys(group_links))
                    push("result", f"📌 {name} — {len(unique)} {filter_key} links found ({elapsed:.0f}s)")
                    for link in unique:
                        push("link", link)
                    await client.send_message("me", f"🔗 {filter_key.upper()} from {name} ({len(unique)} links)\n\n" + "\n".join(unique))
                else:
                    push("empty", f"No {filter_key} links found in {name} ({elapsed:.0f}s)")
                    await client.send_message("me", f"📭 No {filter_key} links found in {name}")
            except Exception as e:
                push("error", f"Could not access t.me/{username}: {e}")
            state["task_status"] = "done"
            return

        async for dialog in client.iter_dialogs():
            entity = dialog.entity
            if isinstance(entity, User):
                continue
            name = dialog.name or ""
            if use_saved:
                if not any(g.lower() in name.lower() for g in state["scrape_groups"]):
                    continue
            elif group_query:
                if group_query.lower() not in name.lower():
                    continue
            groups_checked += 1
            push("status", f"Checking {name}...")
            group_links = []
            async for msg in client.iter_messages(entity, limit=200):
                if not msg.text:
                    continue
                found = URL_REGEX.findall(msg.text)
                group_links.extend(found)
                if len(group_links) >= 50:
                    break
            if group_links:
                unique = list(dict.fromkeys(group_links))
                all_links.append((name, unique))
                push("result", f"📌 {name} — {len(unique)} links")
                for link in unique:
                    push("link", link)
            else:
                push("empty", f"No {filter_key} links in {name}")
            if group_query and all_links:
                break

        elapsed = asyncio.get_event_loop().time() - start
        if all_links:
            total = sum(len(l) for _, l in all_links)
            push("done", f"Done — {total} {filter_key} links from {groups_checked} groups ({elapsed:.0f}s)")
            full  = f"🔗 {filter_key.upper()} — {total} links, {groups_checked} groups ({elapsed:.0f}s)\n\n"
            full += "\n\n".join(f"📌 {n} ({len(l)})\n" + "\n".join(l) for n, l in all_links)
            for chunk in [full[i:i+4000] for i in range(0, len(full), 4000)]:
                await client.send_message("me", chunk)
        else:
            push("empty", f"No {filter_key} links found in any group ({elapsed:.0f}s)")
            await client.send_message("me", f"📭 No {filter_key} links found ({elapsed:.0f}s)")
    except Exception as e:
        state["results"].append({"type": "error", "content": str(e)})
        log(f"Scrape error: {e}")
    finally:
        state["task_status"] = "done"

async def api_scrape_groups(request):
    data   = await request.json()
    action = data.get("action")
    name   = data.get("name", "")
    if action == "add" and name:
        if name not in state["scrape_groups"]:
            state["scrape_groups"].append(name)
    elif action == "remove":
        state["scrape_groups"] = [g for g in state["scrape_groups"] if g != name]
    return web.json_response({"ok": True, "groups": state["scrape_groups"]})

async def api_dialogs(request):
    try:
        dms    = []
        groups = []
        count  = 0
        async for dialog in client.iter_dialogs():
            if count >= 60:
                break
            if dialog.archived:
                continue
            try:
                entity  = dialog.entity
                name    = dialog.name or "Unknown"
                is_user = isinstance(entity, User) and not getattr(entity, "bot", False) and not getattr(entity, "is_self", False)
                item = {
                    "name": name,
                    "type": "dm" if is_user else "group",
                    "unread": dialog.unread_count or 0,
                    "id": str(dialog.id),
                    "context": state["chat_contexts"].get(name, ""),
                }
                if is_user:
                    dms.append(item)
                else:
                    groups.append(item)
                count += 1
            except:
                continue
        return web.json_response({"ok": True, "dms": dms, "groups": groups})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)

async def api_set_context(request):
    data    = await request.json()
    name    = data.get("name", "")
    context = data.get("context", "")
    if name:
        state["chat_contexts"][name] = context
        log(f"Context set for {name}")
    return web.json_response({"ok": True})

async def api_summarise_chat(request):
    data      = await request.json()
    chat_name = data.get("name", "")
    state["results"]     = []
    state["task_status"] = "running"
    asyncio.create_task(do_summarise_single(chat_name))
    return web.json_response({"ok": True, "message": f"Summarising {chat_name}..."})

async def do_summarise_single(query):
    try:
        async for dialog in client.iter_dialogs():
            if query.lower() in (dialog.name or "").lower():
                messages = []
                async for msg in client.iter_messages(dialog.entity, limit=MESSAGES_PER_CHAT):
                    if msg.text:
                        try:
                            sender = getattr(await msg.get_sender(), "first_name", None) or dialog.name
                        except:
                            sender = "Someone"
                        messages.append(f"{sender}: {msg.text}")
                if not messages:
                    state["results"].append({"type": "empty", "content": f"No messages in '{dialog.name}'"})
                    state["task_status"] = "done"
                    return
                messages.reverse()
                summary = await summarise_chat(dialog.name, messages)
                state["results"].append({"type": "summary", "name": dialog.name, "content": summary})
                state["task_status"] = "done"
                await client.send_message("me", f"📋 **{dialog.name}**\n\n{summary}")
                return
        state["results"].append({"type": "error", "content": f"No chat matching '{query}'"})
        state["task_status"] = "done"
    except Exception as e:
        state["results"].append({"type": "error", "content": str(e)})
        state["task_status"] = "done"


# ─── COURSE SCRAPER ──────────────────────────────────────────────────
MEGA_REGEX = re.compile(r'https?://mega\.nz/\S+', re.IGNORECASE)

def extract_course_title(text: str) -> str:
    if not text:
        return "Untitled Course"
    lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
    non_link = [l for l in lines if not re.match(r'^https?://', l) and not re.match(r'^t\.me/', l)]
    def clean(s):
        out = re.sub(r'[^\w\s\-\|:\(\)\/\.,!&+#@]', '', s, flags=re.UNICODE).strip()
        return re.sub(r'\s+', ' ', out)
    for line in non_link[:3]:
        c = clean(line)
        if len(c) > 4:
            return c[:120]
    return re.sub(r'\s+', ' ', lines[0])[:80] if lines else "Untitled Course"

async def do_scrape_courses():
    state["courses_status"] = "running"
    state["courses"] = []
    try:
        for group_link in state["course_groups"]:
            tme = re.match(r'(?:https?://)?t\.me/([a-zA-Z0-9_]+)', group_link.strip())
            if not tme:
                continue
            username = tme.group(1)
            try:
                entity     = await client.get_entity(username)
                group_name = getattr(entity, "title", None) or username
                async for msg in client.iter_messages(entity, limit=300):
                    if not msg.text:
                        continue
                    links = MEGA_REGEX.findall(msg.text)
                    if not links:
                        continue
                    title    = extract_course_title(msg.text)
                    date_str = msg.date.strftime("%Y-%m-%d") if msg.date else ""
                    for link in links:
                        state["courses"].append({
                            "title": title,
                            "mega_link": link,
                            "group": group_name,
                            "group_link": f"https://t.me/{username}",
                            "date": date_str,
                            "msg_id": msg.id,
                        })
            except Exception as e:
                log(f"Course scrape error for {group_link}: {e}")
        seen    = set()
        deduped = []
        for c in state["courses"]:
            if c["mega_link"] not in seen:
                seen.add(c["mega_link"])
                deduped.append(c)
        state["courses"]             = deduped
        state["courses_last_updated"]= datetime.now(timezone.utc).isoformat()
        log(f"✅ Scraped {len(state['courses'])} courses from {len(state['course_groups'])} groups")
    except Exception as e:
        log(f"Course scrape fatal error: {e}")
    finally:
        state["courses_status"] = "done"

async def api_course_groups(request):
    if request.method == "GET":
        return web.json_response({"ok": True, "groups": state["course_groups"]})
    data   = await request.json()
    action = data.get("action")
    link   = (data.get("link") or "").strip()
    if action == "add" and link:
        if link not in state["course_groups"]:
            state["course_groups"].append(link)
    elif action == "remove" and link:
        state["course_groups"] = [g for g in state["course_groups"] if g != link]
    return web.json_response({"ok": True, "groups": state["course_groups"]})

async def api_courses(request):
    if request.method == "GET":
        return web.json_response({
            "ok": True,
            "courses": state["courses"],
            "status": state["courses_status"],
            "last_updated": state["courses_last_updated"],
            "group_count": len(state["course_groups"]),
        })
    if state["courses_status"] == "running":
        return web.json_response({"ok": False, "message": "Scrape already in progress"})
    asyncio.create_task(do_scrape_courses())
    return web.json_response({"ok": True, "message": "Course scrape started"})


# ─── SERVE HTML ──────────────────────────────────────────────────────
async def serve_index(request):
    return web.FileResponse(INDEX_HTML)

async def serve_courses(request):
    return web.FileResponse(COURSES_HTML)


# ─── CORS MIDDLEWARE ─────────────────────────────────────────────────
@web.middleware
async def cors_middleware(request, handler):
    if request.method == "OPTIONS":
        resp = web.Response(status=200)
        resp.headers["Access-Control-Allow-Origin"]  = "*"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        return resp
    resp = await handler(request)
    resp.headers["Access-Control-Allow-Origin"]  = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


# ─── APP FACTORY ─────────────────────────────────────────────────────
def make_app():
    app = web.Application(middlewares=[cors_middleware])
    # Pages
    app.router.add_get("/",           serve_index)
    app.router.add_get("/courses",    serve_courses)
    # API
    app.router.add_get("/api/status",          api_status)
    app.router.add_post("/api/ai-reply",       api_toggle_ai)
    app.router.add_post("/api/autoreply",      api_toggle_autoreply)
    app.router.add_post("/api/summarise",      api_summarise)
    app.router.add_post("/api/summarise-chat", api_summarise_chat)
    app.router.add_post("/api/links",          api_scrape_links)
    app.router.add_post("/api/scrape-groups",  api_scrape_groups)
    app.router.add_get("/api/dialogs",         api_dialogs)
    app.router.add_post("/api/persona",        api_update_persona)
    app.router.add_post("/api/chat-context",   api_set_context)
    app.router.add_post("/api/clear-results",  api_clear_results)
    app.router.add_get("/api/memory",          api_get_memory)
    app.router.add_post("/api/memory",         api_edit_memory)
    app.router.add_get("/api/analytics",       api_analytics)
    app.router.add_get("/api/course-groups",               api_course_groups)
    app.router.add_post("/api/course-groups",              api_course_groups)
    app.router.add_route("OPTIONS", "/api/course-groups",  api_course_groups)
    app.router.add_get("/api/courses",                     api_courses)
    app.router.add_post("/api/courses",                    api_courses)
    app.router.add_route("OPTIONS", "/api/courses",        api_courses)
    return app


# ─── MAIN ────────────────────────────────────────────────────────────
async def main():
    log("Starting TeleFlow AI...")
    await client.start()
    if not await client.is_user_authorized():
        log("❌ Session invalid — set TELEGRAM_SESSION env var")
        return
    me = await client.get_me()
    state["me"]      = me
    state["my_name"] = me.first_name or "me"
    log(f"✅ Logged in as {me.first_name} (@{me.username})")
    log(f"✅ Model: {OPENROUTER_MODEL}")

    async def midnight_reset():
        while True:
            await asyncio.sleep(86400)
            state["messages_today"] = 0
            goodbye_senders.clear()
            log("Daily counters reset")

    asyncio.create_task(midnight_reset())

    app    = make_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log(f"✅ Web server running on port {PORT}")
    log(f"✅ Dashboard → /   Courses → /courses")

    await client.run_until_disconnected()


asyncio.run(main())
