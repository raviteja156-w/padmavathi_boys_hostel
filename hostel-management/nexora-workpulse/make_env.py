"""Beginner helper: creates your local .env file (never committed to Git).  Run:  python make_env.py"""
import re
import secrets
from pathlib import Path

target = Path(__file__).parent / ".env"
if target.exists():
    if input(".env already exists. Overwrite it? (yes/no): ").strip().lower() != "yes":
        raise SystemExit("Kept your existing .env. Nothing changed.")

print("\nPaste the values below. (Right-click pastes in most terminals.)\n")
while True:
    url = input("1) Supabase Session pooler DATABASE_URL: ").strip().strip('"')
    if re.match(r"^postgres(ql)?://.+@.+/.*", url) and "YOUR" not in url.upper().replace("YOUR_", "YOUR_"):
        break
    print("   That does not look right. It must start with postgresql:// and contain your real password.")
while True:
    email = input("2) Admin email (you will sign in with this): ").strip()
    if re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        break
    print("   Enter a valid email like you@example.com")
while True:
    pw = input("3) Admin password (at least 12 characters): ").strip()
    if len(pw) >= 12:
        break
    print("   Too short. Use 12 or more characters.")

target.write_text(
    f"DATABASE_URL={url}\n"
    f"FLASK_SECRET_KEY={secrets.token_urlsafe(48)}\n"
    f"ADMIN_EMAIL={email}\n"
    f"ADMIN_PASSWORD={pw}\n"
    "ADMIN_NAME=Administrator\n"
    "APP_ENV=development\n"
    "APP_TIMEZONE=Asia/Kolkata\n"
    "DAILY_TARGET_HOURS=4\n"
    "DB_SSLMODE=require\n", encoding="utf-8")
print("\n.env created (secret key generated for you). It is ignored by Git.\n")
