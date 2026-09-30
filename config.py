# config.py
import os

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
BACKUP_CHAT_ID = os.getenv("BACKUP_CHAT_ID")

admin_ids_raw = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [int(x.strip()) for x in admin_ids_raw.split(",") if x.strip()]
if not ADMIN_IDS:
    ADMIN_IDS = []  # جایگزین با آیدی بله ادمین

DATA_FILE = os.getenv("DATA_FILE", "/tmp/elective.json")
