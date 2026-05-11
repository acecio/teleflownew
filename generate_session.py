"""
Run this ONCE locally to get your TELEGRAM_SESSION string.
Then paste it into Railway as an environment variable.

    pip install telethon
    python generate_session.py
"""
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

api_id   = input("API_ID   (from my.telegram.org): ").strip()
api_hash = input("API_HASH (from my.telegram.org): ").strip()

with TelegramClient(StringSession(), int(api_id), api_hash) as client:
    client.start()          # asks for phone number + OTP in terminal
    session = client.session.save()

print("\n" + "="*60)
print("TELEGRAM_SESSION (copy the whole line below):")
print("="*60)
print(session)
print("="*60)
print("\nPaste this as TELEGRAM_SESSION in Railway → Variables.")
