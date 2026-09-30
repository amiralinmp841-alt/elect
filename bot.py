# bot.py
import telebot
from telebot import types, apihelper
import config
import storage
import scraper
import os
import requests
import json
import tempfile
from datetime import datetime
from openpyxl import Workbook
import time
from telebot import BaseMiddleware, CancelUpdate
import pandas as pd

SUSPICIOUS_STUDENT_ID_THRESHOLD = 3
SUSPICIOUS_WINDOW_SECONDS = 3600
SUSPICIOUS_ALERT_COOLDOWN_SECONDS = 6 * 3600

ADMIN_IDS = [
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
]
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

admin_description_states = {}

# تغییر آدرس پایه API تلگرام به آدرس سرور بله
apihelper.API_URL = "https://tapi.bale.ai/bot{0}/{1}"

bot = telebot.TeleBot(config.BOT_TOKEN, use_class_middlewares=True)

# ساختار نگهداری وضعیت موقت کاربران (Stating / FSM)
# user_states = { chat_id: { "state": "...", "data": {} } }
user_states = {}


class BlacklistMiddleware(BaseMiddleware):
    def __init__(self, bot):
        self.bot = bot
        self.update_types = ['message', 'callback_query']

    def pre_process(self, message, data):
        # به دست آوردن chat_id متناسب با نوع آپدیت
        chat_id = message.chat.id if hasattr(
            message, 'chat') else message.message.chat.id

        # بررسی اینکه آیا کاربر مسدود شده است یا خیر
        if is_telegram_user_blocked(chat_id):
            try:
                self.bot.send_message(
                    chat_id, "❌ دسترسی شما به این ربات توسط مدیریت مسدود شده است.")
            except Exception as e:
                print("Failed to send block message:", e)

            # بازگرداندن CancelUpdate برای متوقف کردن هندلرها
            return CancelUpdate()


# ثبت میدل‌ور در ربات
bot.setup_middleware(BlacklistMiddleware(bot))


def send_backup_to_group(caption="بکاپ جدید elective.json"):
    if not config.BACKUP_CHAT_ID:
        return

    data_file = getattr(storage, "DATA_FILE", "elective.json")
    if not os.path.exists(data_file):
        return

    try:
        with open(data_file, "rb") as f:
            bot.send_document(
                config.BACKUP_CHAT_ID,
                f,
                caption=caption
            )
    except Exception as e:
        print(f"خطا در ارسال بکاپ به گروه: {e}")


@bot.message_handler(func=lambda msg: msg.chat.id in config.ADMIN_IDS and msg.text == "📥 دریافت elective.json")
def admin_download_json(message):
    data_file = getattr(storage, "DATA_FILE", "elective.json")

    if not os.path.exists(data_file):
        bot.send_message(message.chat.id, "فایل elective.json پیدا نشد.")
        return

    try:
        with open(data_file, "rb") as f:
            bot.send_document(
                message.chat.id,
                f,
                caption="📥 فایل فعلی elective.json"
            )
    except Exception as e:
        bot.send_message(message.chat.id, f"خطا در ارسال فایل:\n{e}")


@bot.message_handler(func=lambda msg: msg.chat.id in config.ADMIN_IDS and msg.text == "📤 وارد کردن elective.json")
def admin_upload_json_start(message):
    set_state(message.chat.id, "admin_awaiting_elective_json")
    bot.send_message(
        message.chat.id,
        "لطفاً فایل elective.json را به صورت document ارسال کنید.\n"
        "⚠️ با این کار، فایل فعلی جایگزین خواهد شد.",
        reply_markup=back_markup()
    )


@bot.message_handler(content_types=['document'], func=lambda msg: get_state(msg.chat.id) == "admin_awaiting_elective_json")
def admin_receive_json_file(message):
    if message.chat.id not in config.ADMIN_IDS:
        return

    document = message.document
    if not document:
        bot.send_message(message.chat.id, "فایلی دریافت نشد.")
        return

    file_name = document.file_name or ""
    if not file_name.lower().endswith(".json"):
        bot.send_message(message.chat.id, "فایل باید با فرمت .json باشد.")
        return

    try:
        file_info = bot.get_file(document.file_id)
        file_path = getattr(file_info, "file_path", None)

        if not file_path:
            bot.send_message(
                message.chat.id, "مسیر فایل از سرور بله دریافت نشد.")
            return

        file_url = f"https://tapi.bale.ai/file/bot{config.BOT_TOKEN}/{file_path}"
        response = requests.get(file_url, timeout=30)
        response.raise_for_status()
        downloaded_file = response.content

        parsed = json.loads(downloaded_file.decode("utf-8"))

        if not isinstance(parsed, dict):
            bot.send_message(
                message.chat.id, "ساختار فایل نامعتبر است. ریشه JSON باید object باشد.")
            return

        data_file = getattr(storage, "DATA_FILE", "elective.json")
        with open(data_file, "wb") as new_file:
            new_file.write(downloaded_file)

        clear_state(message.chat.id)

        bot.send_message(
            message.chat.id,
            "✅ فایل elective.json با موفقیت جایگزین شد.",
            reply_markup=get_admin_keyboard()
        )

        send_backup_to_group("📦 elective.json توسط ادمین جایگزین شد.")

    except json.JSONDecodeError:
        bot.send_message(message.chat.id, "محتوای فایل JSON معتبر نیست.")
    except requests.RequestException as e:
        bot.send_message(message.chat.id, f"خطا در دانلود فایل از بله:\n{e}")
    except Exception as e:
        print("UPLOAD ERROR:", repr(e))
        bot.send_message(message.chat.id, f"خطا در پردازش فایل:\n{e}")


@bot.message_handler(func=lambda msg: msg.text == "⬅️ بازگشت")
def handle_back(message):
    clear_state(message.chat.id)

    if message.chat.id in config.ADMIN_IDS:
        bot.send_message(
            message.chat.id,
            "بازگشت به منوی ادمین.",
            reply_markup=get_admin_keyboard()
        )
        return

    user = get_active_user_by_chat(message.chat.id)

    if not user:
        bot.send_message(
            message.chat.id,
            "شما از حساب خود خارج شده‌اید. لطفاً نحوه ورود خود را انتخاب کنید:",
            reply_markup=get_login_method_keyboard()
        )
        return

    set_state(
        message.chat.id,
        "user_panel",
        {"active_student_id": str(user["student_id"])}
    )

    bot.send_message(
        message.chat.id,
        "بازگشت به پنل کاربری.",
        reply_markup=get_user_panel_keyboard()
    )


def get_state(chat_id):
    return user_states.get(chat_id, {}).get("state", None)


def set_state(chat_id, state, data=None):
    if chat_id not in user_states:
        user_states[chat_id] = {"state": None, "data": {}}
    user_states[chat_id]["state"] = state
    if data is not None:
        user_states[chat_id]["data"].update(data)


def get_state_data(chat_id):
    return user_states.get(chat_id, {}).get("data", {})


def clear_state(chat_id):
    if chat_id in user_states:
        del user_states[chat_id]


def get_active_user_by_chat(chat_id):
    state_data = get_state_data(chat_id)
    active_student_id = state_data.get("active_student_id")

    if active_student_id:
        user = storage.get_user_by_student_id(active_student_id)
        if user and str(user.get("telegram_id")) == str(chat_id):
            return user

        state_data.pop("active_student_id", None)
        set_state(chat_id, get_state(chat_id), state_data)

    users = storage.get_users_by_telegram_id(chat_id)

    if len(users) == 1:
        return users[0]

    if len(users) > 1:
        valid_users = [
            u for u in users
            if str(u.get("telegram_id")) == str(chat_id)
        ]

        no_gpa_users = [
            u for u in valid_users
            if u.get("mode") == "no_gpa"
        ]

        if len(no_gpa_users) == 1:
            set_state(chat_id, get_state(chat_id), {
                "active_student_id": str(no_gpa_users[0]["student_id"])
            })
            return no_gpa_users[0]

    return None


def set_active_user(chat_id, student_id):
    state_data = get_state_data(chat_id)
    state_data["active_student_id"] = str(student_id)
    set_state(chat_id, get_state(chat_id), state_data)


def get_effective_gpa_label():
    mode = storage.get_effective_gpa_mode()
    if str(mode) == "total":
        return "معدل کل"
    return f"معدل ترم {mode}"


def get_user_effective_gpa_label(user):
    manual_gpa = user.get("manual_gpa")
    if manual_gpa not in (None, "", "null"):
        return "معدل دستی ادمین"

    mode = storage.get_effective_gpa_mode()
    if mode == "total":
        return "معدل کل"
    return f"معدل ترم {mode}"


def format_terms_gpa(terms_gpa):
    if not terms_gpa:
        return "یافت نشد"

    lines = []
    for term_no, gpa in sorted(terms_gpa.items(), key=lambda x: int(x[0]) if str(x[0]).isdigit() else str(x[0])):
        lines.append(f"ترم {term_no}: {gpa}")
    return "\n".join(lines)


def get_personal_info_text(user, data):
    full_name = user.get("full_name", "نامشخص")
    student_id = user.get("student_id", "نامشخص")

    total_gpa = user.get("gpa")
    total_gpa_text = total_gpa if total_gpa not in (
        None, "", "null") else "ثبت نشده"

    terms_text = format_terms_gpa(user.get("terms_gpa", {}))

    effective_value = storage.get_user_effective_gpa(user, data)
    effective_value_text = effective_value if effective_value is not None else "ثبت نشده / موجود نیست"

    manual_gpa = user.get("manual_gpa")
    if manual_gpa not in (None, "", "null"):
        effective_label = "معدل دستی ادمین"
    else:
        effective_label = get_effective_gpa_label()

    rank = user.get("rank")
    rank_text = rank if rank not in (
        None, "", "null") else "ثبت نشده / محاسبه نشده"

    return (
        "ℹ️ اطلاعات شخصی\n\n"
        f"👤 نام و نام خانوادگی: {full_name}\n"
        f"🎓 کد دانشجویی: {student_id}\n"
        f"🏅 رتبه: {rank_text}\n\n"
        f"📚 معدل ترم‌ها:\n{terms_text}\n\n"
        f"📈 معدل کل: {total_gpa_text}\n"
        f"✅ معدل تاثیرگذار فعلی ({effective_label}): {effective_value_text}"
    )


def get_personal_info_keyboard(user):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row("✏️ ویرایش نام و نام خانوادگی")
    markup.row("🆔 ویرایش کد دانشجویی")
    markup.row("🔙 بازگشت به پنل کاربری")
    return markup


def show_admin_menu(chat_id):
    clear_state(chat_id)
    bot.send_message(chat_id, "منوی اصلی ادمین:",
                     reply_markup=get_admin_keyboard())

# --- KEYBOARDS ---


def normalize_digits(value):
    """تبدیل ارقام فارسی/عربی به انگلیسی برای مقایسه شماره دانشجویی."""
    if value is None:
        return ""
    text = str(value).strip()
    trans = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
    return text.translate(trans)


def get_login_filter():
    """خواندن تنظیمات فیلتر ورود از دیتابیس."""
    data = storage.load_data()
    cfg = data.get("login_filter", {})
    if not isinstance(cfg, dict):
        cfg = {}

    mode = cfg.get("mode", "all")
    if mode not in ("all", "even", "odd", "prefixes"):
        mode = "all"

    prefixes = cfg.get("prefixes", [])
    if not isinstance(prefixes, list):
        prefixes = []

    prefixes = [normalize_digits(x)
                for x in prefixes if normalize_digits(x).isdigit()]
    return {"mode": mode, "prefixes": prefixes}


def is_student_id_allowed(student_id):
    """بررسی می‌کند شماره دانشجویی طبق تنظیم ادمین اجازه ورود دارد یا نه."""
    student_id = normalize_digits(student_id)
    if not student_id or not student_id.isdigit():
        return False

    cfg = get_login_filter()
    mode = cfg["mode"]

    if mode == "all":
        return True

    if mode == "even":
        return int(student_id[-1]) % 2 == 0

    if mode == "odd":
        return int(student_id[-1]) % 2 == 1

    if mode == "prefixes":
        prefixes = cfg.get("prefixes", [])
        return bool(prefixes) and any(student_id.startswith(prefix) for prefix in prefixes)

    return True


def save_login_filter(mode, prefixes=None):
    """ذخیره حالت فیلتر ورود."""
    data = storage.load_data()
    clean_prefixes = []
    for prefix in (prefixes or []):
        prefix = normalize_digits(prefix)
        if prefix.isdigit() and prefix not in clean_prefixes:
            clean_prefixes.append(prefix)

    data["login_filter"] = {
        "mode": mode if mode in ("all", "even", "odd", "prefixes") else "all",
        "prefixes": clean_prefixes,
    }
    storage.save_data(data)


def login_filter_status_text():
    cfg = get_login_filter()
    mode = cfg["mode"]
    if mode == "all":
        return "🔓 همه شماره‌های دانشجویی"
    if mode == "even":
        return "2️⃣ فقط شماره‌های زوج"
    if mode == "odd":
        return "1️⃣ فقط شماره‌های فرد"
    return "🔢 فقط Prefixهای انتخاب‌شده:\n" + "، ".join(cfg["prefixes"])


def get_login_filter_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row("🔓 همه شماره‌ها")
    markup.row("2️⃣ فقط زوج", "1️⃣ فقط فرد")
    markup.row("🔢 تنظیم Prefixها")
    markup.row("📋 وضعیت حالت ورود")
    markup.row("🔙 بازگشت به منوی ادمین")
    return markup


def get_admin_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row("📚 درس ها", "👥 لیست افراد")
    markup.row("✍️ افزودن دستی افراد", "❌ حذف افراد")
    markup.row("📊 نتیجه انتخابات")
    markup.row("⚙️ حالت ورود")
    markup.row("🎯 تعیین معدل تاثیرگذار", "⏸️ توقف/ازسرگیری رای گیری")
    markup.row("📥 دریافت elective.json", "📤 وارد کردن elective.json")
    return markup


def get_admin_effective_gpa_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row("📈 معدل کل")
    markup.row("🔢 تعیین بر اساس شماره ترم")
    markup.row("🔙 بازگشت به منوی ادمین")
    return markup


def get_admin_courses_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row("➕ افزودن درس", "📋 لیست درس ها")
    markup.row("📝 اضافه کردن توضیح درس", "🔢 تغییر ظرفیت کلاس")
    markup.row("🔙 بازگشت به منوی ادمین")
    return markup


def get_admin_course_capacity_keyboard(courses):
    markup = types.InlineKeyboardMarkup()
    for course in courses:
        markup.add(
            types.InlineKeyboardButton(
                f"{course['name']} (ظرفیت: {course['capacity']})",
                callback_data=f"admin_capacity_{course['id']}"
            )
        )

    markup.add(
        types.InlineKeyboardButton(
            "🔙 بازگشت",
            callback_data="admin_capacity_back"
        )
    )
    return markup


def get_admin_manual_user_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row("⚙️ تنظیم دستی افراد فاقد معدل")
    markup.row("➕ افزودن فرد جدید")
    markup.row("🔙 بازگشت به منوی ادمین")
    return markup


def get_login_method_keyboard():
    markup = types.ReplyKeyboardMarkup(
        resize_keyboard=True, one_time_keyboard=True)
    markup.row("🔐 با ثبت معدل (ورود با هم‌آوا)")
    markup.row("📝 بدون ثبت معدل (انتخاب مستقیم درس)")
    return markup


def get_user_panel_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row("📥 انتخاب درس ها")
    markup.row("🔎 درس های انتخابی من", "🗑️ حذف درس های انتخابی من")
    markup.row("ℹ️ اطلاعات شخصی", "🏆 نتیجه قبولی")
    markup.row("🚪 خروج")
    return markup

# --- HANDLERS ---


def build_results_by_users_excel(users, user_results, data):
    wb = Workbook()
    ws = wb.active
    ws.title = "Results By Users"

    ws.append([
        "نام و نام خانوادگی",
        "کد دانشجویی",
        "رتبه",
        "معدل تاثیرگذار",
        "کلاس اختصاص یافته",
        "اولویت قبولی"
    ])

    for user in users:
        res = user_results.get(str(user["student_id"]))
        course_name = res["course_name"] if res else "مردود/بدون اولویت"
        priority = res["priority"] if res and res["priority"] else ""

        effective_gpa = storage.get_user_effective_gpa(user, data)
        gpa_val = effective_gpa if effective_gpa is not None else "بدون معدل"

        ws.append([
            safe_text(user.get("full_name")),
            safe_text(user.get("student_id")),
            user.get("rank", "فاقد رتبه"),
            gpa_val,
            course_name,
            priority
        ])

    autosize_worksheet(ws)
    return save_workbook_temp(wb, "results_by_users")


def build_results_by_class_excel(courses):
    wb = Workbook()
    ws = wb.active
    ws.title = "Results By Classes"

    ws.append([
        "نام کلاس",
        "ظرفیت",
        "ردیف قبولی",
        "نام دانشجو",
        "کد دانشجویی",
        "رتبه",
        "معدل"
    ])

    for course_id, info in courses.items():
        if info["accepted"]:
            for idx, student in enumerate(info["accepted"], 1):
                ws.append([
                    safe_text(info.get("name")),
                    info.get("capacity"),
                    idx,
                    safe_text(student.get("name")),
                    safe_text(student.get("student_id")),
                    student.get("rank"),
                    student.get("gpa")
                ])
        else:
            ws.append([
                safe_text(info.get("name")),
                info.get("capacity"),
                "",
                "هیچ قبولی ندارد",
                "",
                "",
                ""
            ])

    autosize_worksheet(ws)
    return save_workbook_temp(wb, "results_by_class")


def build_courses_list_excel(courses, all_users, data):
    wb = Workbook()
    ws = wb.active
    ws.title = "Courses"

    ws.append([
        "شناسه درس",
        "نام درس",
        "ظرفیت",
        "نام دانشجو",
        "کد دانشجویی",
        "اولویت",
        "رتبه",
        "معدل تاثیرگذار"
    ])

    for course in courses:
        course_id = course["id"]

        selecting_users = []
        for user in all_users:
            if course_id in user.get("selected_courses", []):
                priority = user["selected_courses"].index(course_id) + 1
                eff_gpa = storage.get_user_effective_gpa(user, data)
                selecting_users.append((user, priority, eff_gpa))

        selecting_users.sort(
            key=lambda item: item[2] if item[2] is not None else -1,
            reverse=True
        )

        if selecting_users:
            for user, priority, eff_gpa in selecting_users:
                ws.append([
                    course_id,
                    safe_text(course.get("name")),
                    course.get("capacity"),
                    safe_text(user.get("full_name")),
                    safe_text(user.get("student_id")),
                    priority,
                    user.get("rank", "فاقد رتبه"),
                    eff_gpa if eff_gpa is not None else "نامشخص"
                ])
        else:
            ws.append([
                course_id,
                safe_text(course.get("name")),
                course.get("capacity"),
                "متقاضی ندارد",
                "",
                "",
                "",
                ""
            ])

    autosize_worksheet(ws)
    return save_workbook_temp(wb, "courses_list")


def send_chunked_message(chat_id, text, parse_mode=None, chunk_size=1400):
    if text is None:
        text = ""

    text = str(text)

    if not text:
        bot.send_message(
            chat_id, "پیامی برای نمایش وجود ندارد.", parse_mode=parse_mode)
        return

    lines = text.splitlines(keepends=True)
    current_chunk = ""

    for line in lines:
        if len(line) > chunk_size:
            if current_chunk:
                bot.send_message(chat_id, current_chunk, parse_mode=parse_mode)
                current_chunk = ""

            for i in range(0, len(line), chunk_size):
                bot.send_message(
                    chat_id, line[i:i + chunk_size], parse_mode=parse_mode)

            continue

        if len(current_chunk) + len(line) > chunk_size:
            bot.send_message(chat_id, current_chunk, parse_mode=parse_mode)
            current_chunk = line
        else:
            current_chunk += line

    if current_chunk:
        bot.send_message(chat_id, current_chunk, parse_mode=parse_mode)


def autosize_worksheet(ws, max_width=60):
    for column_cells in ws.columns:
        max_length = 0
        column_letter = column_cells[0].column_letter
        for cell in column_cells:
            value = "" if cell.value is None else str(cell.value)
            if len(value) > max_length:
                max_length = len(value)
        ws.column_dimensions[column_letter].width = min(
            max_length + 2, max_width)


def save_workbook_temp(workbook, prefix):
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    temp_dir = tempfile.gettempdir()
    file_path = os.path.join(temp_dir, f"{prefix}_{timestamp}.xlsx")
    workbook.save(file_path)
    return file_path


def send_excel_file(chat_id, file_path, caption):
    try:
        with open(file_path, "rb") as f:
            bot.send_document(chat_id, f, caption=caption)
    finally:
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
        except Exception:
            pass


def safe_text(value, default=""):
    if value is None:
        return default
    return str(value)


def build_users_list_excel(users_sorted, course_map, data):
    wb = Workbook()
    ws = wb.active
    ws.title = "Users"

    ws.append([
        "رتبه",
        "نام و نام خانوادگی",
        "کد دانشجویی",
        "معدل تاثیرگذار",
        "دروس انتخابی"
    ])

    for idx, user in enumerate(users_sorted, 1):
        effective_gpa = storage.get_user_effective_gpa(user, data)
        gpa_val = effective_gpa if effective_gpa is not None else "بدون معدل"

        selected_courses = user.get("selected_courses", [])
        course_names = [course_map[cid]
                        for cid in selected_courses if cid in course_map]
        selected_text = "، ".join(
            course_names) if course_names else "انتخاب نکرده است"

        ws.append([
            idx,
            safe_text(user.get("full_name")),
            safe_text(user.get("student_id")),
            gpa_val,
            selected_text
        ])

    autosize_worksheet(ws)
    return save_workbook_temp(wb, "users_list")


@bot.message_handler(commands=['start'])
def cmd_start(message):
    chat_id = message.chat.id
    clear_state(chat_id)

    # بررسی اینکه آیا کاربر ادمین است یا خیر
    if chat_id in config.ADMIN_IDS:
        bot.send_message(
            chat_id,
            "سلام ادمین گرامی! خوش آمدید. لطفا یک گزینه را انتخاب کنید:",
            reply_markup=get_admin_keyboard()
        )
    else:
        # کاربر عادی
        bot.send_message(
            chat_id,
            "سلام به ربات انتخاب دروس انتخابی خوش آمدید.\nلطفاً نحوه ورود خود را انتخاب کنید:",
            reply_markup=get_login_method_keyboard()
        )

# هندلر عمومی دکمه بازگشت ادمین


@bot.message_handler(func=lambda msg: msg.chat.id in config.ADMIN_IDS and msg.text == "🔙 بازگشت به منوی ادمین")
def admin_back(message):
    clear_state(message.chat.id)
    bot.send_message(message.chat.id, "منوی اصلی ادمین:",
                     reply_markup=get_admin_keyboard())

# --- بخش مدیریت ادمین ---


@bot.message_handler(func=lambda msg: msg.chat.id in config.ADMIN_IDS and msg.text == "⚙️ حالت ورود")
def admin_login_filter_menu(message):
    clear_state(message.chat.id)
    bot.send_message(
        message.chat.id,
        "⚙️ تنظیم حالت ورود\n\n"
        "حالت فعلی:\n"
        + login_filter_status_text()
        + "\n\nیکی از گزینه‌های زیر را انتخاب کنید:",
        reply_markup=get_login_filter_keyboard()
    )


@bot.message_handler(func=lambda msg: msg.chat.id in config.ADMIN_IDS and msg.text == "🔓 همه شماره‌ها")
def admin_login_filter_all(message):
    save_login_filter("all", [])
    clear_state(message.chat.id)
    bot.send_message(
        message.chat.id,
        "✅ حالت ورود روی «همه شماره‌ها» تنظیم شد.",
        reply_markup=get_admin_keyboard()
    )


@bot.message_handler(func=lambda msg: msg.chat.id in config.ADMIN_IDS and msg.text == "2️⃣ فقط زوج")
def admin_login_filter_even(message):
    save_login_filter("even", [])
    clear_state(message.chat.id)
    bot.send_message(
        message.chat.id,
        "✅ از این لحظه فقط شماره‌های دانشجویی زوج اجازه ورود دارند.",
        reply_markup=get_admin_keyboard()
    )


@bot.message_handler(func=lambda msg: msg.chat.id in config.ADMIN_IDS and msg.text == "1️⃣ فقط فرد")
def admin_login_filter_odd(message):
    save_login_filter("odd", [])
    clear_state(message.chat.id)
    bot.send_message(
        message.chat.id,
        "✅ از این لحظه فقط شماره‌های دانشجویی فرد اجازه ورود دارند.",
        reply_markup=get_admin_keyboard()
    )


@bot.message_handler(func=lambda msg: msg.chat.id in config.ADMIN_IDS and msg.text == "📋 وضعیت حالت ورود")
def admin_login_filter_status(message):
    bot.send_message(
        message.chat.id,
        "📋 حالت فعلی ورود:\n\n" + login_filter_status_text(),
        reply_markup=get_login_filter_keyboard()
    )


@bot.message_handler(func=lambda msg: msg.chat.id in config.ADMIN_IDS and msg.text == "🔢 تنظیم Prefixها")
def admin_login_filter_prefix_start(message):
    set_state(message.chat.id, "admin_login_filter_prefixes", {})
    bot.send_message(
        message.chat.id,
        "🔢 Prefixهای مجاز را ارسال کنید.\n\n"
        "می‌توانید چند Prefix را با فاصله، ویرگول یا در چند خط وارد کنید.\n"
        "مثال:\n"
        "9912 9913 401\n\n"
        "در این حالت هر شماره‌ای که با یکی از این Prefixها شروع شود، مجاز خواهد بود.\n\n"
        "برای لغو: «⬅️ بازگشت»",
        reply_markup=back_markup()
    )


@bot.message_handler(func=lambda msg: get_state(msg.chat.id) == "admin_login_filter_prefixes")
def admin_login_filter_prefix_receive(message):
    if message.text == "⬅️ بازگشت":
        clear_state(message.chat.id)
        bot.send_message(
            message.chat.id,
            "بازگشت به تنظیم حالت ورود.",
            reply_markup=get_login_filter_keyboard()
        )
        return

    import re
    raw = (message.text or "").strip()
    parts = [x for x in re.split(r"[\s,،;؛]+", raw) if x]
    prefixes = []
    invalid = []

    for part in parts:
        normalized = normalize_digits(part)
        if normalized.isdigit():
            if normalized not in prefixes:
                prefixes.append(normalized)
        else:
            invalid.append(part)

    if not prefixes:
        bot.send_message(
            message.chat.id,
            "❌ هیچ Prefix معتبری دریافت نشد. فقط عدد وارد کنید.\nمثال: 9912 9913 401",
            reply_markup=back_markup()
        )
        return

    save_login_filter("prefixes", prefixes)
    clear_state(message.chat.id)

    text = (
        "✅ Prefixهای مجاز با موفقیت ثبت شدند.\n\n"
        "🔢 Prefixها:\n"
        + "\n".join(f"• {p}" for p in prefixes)
        + "\n\n📌 هر شماره دانشجویی که با حداقل یکی از این Prefixها شروع شود، اجازه ورود خواهد داشت."
    )
    if invalid:
        text += "\n\n⚠️ موارد نامعتبر نادیده گرفته شدند:\n" + \
            ", ".join(invalid)

    bot.send_message(message.chat.id, text, reply_markup=get_admin_keyboard())


@bot.message_handler(func=lambda msg: msg.chat.id in config.ADMIN_IDS and msg.text == "📚 درس ها")
def admin_courses_menu(message):
    bot.send_message(message.chat.id, "مدیریت درس‌ها:",
                     reply_markup=get_admin_courses_keyboard())


@bot.message_handler(func=lambda msg: msg.chat.id in config.ADMIN_IDS and msg.text == "📋 لیست درس ها")
def admin_courses_list(message):
    data = storage.load_data()
    courses = data.get("courses", [])
    if not courses:
        bot.send_message(message.chat.id, "هیچ درسی در سامانه ثبت نشده است.")
        return

    all_users = data.get("users", [])

    text = "📋 لیست درس‌های ثبت شده:\n\n"
    for course in courses:
        course_id = course["id"]
        text += f"🏷️ {course['name']} (ظرفیت: {course['capacity']})\n"

        selecting_users = []
        for user in all_users:
            if course_id in user.get("selected_courses", []):
                priority = user["selected_courses"].index(course_id) + 1
                eff_gpa = storage.get_user_effective_gpa(user, data)
                selecting_users.append((user, priority, eff_gpa))

        selecting_users.sort(
            key=lambda item: item[2] if item[2] is not None else -1,
            reverse=True
        )

        if selecting_users:
            text += "متقاضیان به ترتیب معدل تاثیرگذار:\n"
            for idx, (user, priority, eff_gpa) in enumerate(selecting_users, 1):
                gpa_display = eff_gpa if eff_gpa is not None else "نامشخص"
                text += (
                    f"{idx}. {user['full_name']} | "
                    f"رتبه: {user.get('rank', 'فاقد رتبه')} | "
                    f"معدل: {gpa_display} | "
                    f"(اولویت: {priority})\n"
                )
        else:
            text += "⚠️ متقاضی ندارد.\n"

        text += "━" * 15 + "\n"

    send_chunked_message(message.chat.id, text)

    try:
        excel_path = build_courses_list_excel(courses, all_users, data)
        send_excel_file(message.chat.id, excel_path, "📚 فایل اکسل لیست درس‌ها")
    except Exception as e:
        bot.send_message(
            message.chat.id, f"خطا در ساخت یا ارسال فایل اکسل لیست درس‌ها:\n{e}")


@bot.message_handler(func=lambda msg: msg.chat.id in config.ADMIN_IDS and msg.text == "➕ افزودن درس")
def admin_add_course_start(message):
    set_state(message.chat.id, "admin_awaiting_course_name")
    bot.send_message(message.chat.id, "لطفاً **نام درس** جدید را ارسال کنید:")


@bot.message_handler(
    func=lambda msg:
        msg.chat.id in config.ADMIN_IDS
        and get_state(msg.chat.id) == "admin_awaiting_course_name"
)
def admin_add_course_name(message):
    if message.text == "🔙 بازگشت به منوی ادمین":
        admin_add_course_back(message)
        return

    course_name = message.text.strip()
    if not course_name:
        bot.send_message(
            message.chat.id,
            "نام درس نمی‌تواند خالی باشد:",
            reply_markup=get_admin_courses_keyboard()
        )
        return

    set_state(
        message.chat.id,
        "admin_awaiting_course_capacity",
        {"course_name": course_name}
    )
    bot.send_message(
        message.chat.id,
        f"نام درس: {course_name}\nحالا ظرفیت کلاس را وارد کنید:",
        reply_markup=get_admin_courses_keyboard()
    )


@bot.message_handler(
    func=lambda msg:
        msg.chat.id in config.ADMIN_IDS
        and get_state(msg.chat.id) == "admin_awaiting_course_capacity"
)
def admin_add_course_capacity(message):
    if message.text == "🔙 بازگشت به منوی ادمین":
        admin_add_course_back(message)
        return

    try:
        capacity = int(message.text.strip())
        if capacity <= 0:
            raise ValueError
    except ValueError:
        bot.send_message(
            message.chat.id,
            "خطا! ظرفیت باید یک عدد مثبت باشد. مجدداً ارسال کنید:",
            reply_markup=get_admin_courses_keyboard()
        )
        return

    data = get_state_data(message.chat.id)
    course_name = data["course_name"]

    storage.add_course(course_name, capacity)
    clear_state(message.chat.id)
    bot.send_message(
        message.chat.id,
        f"درس «{course_name}» با ظرفیت {capacity} با موفقیت افزوده شد.",
        reply_markup=get_admin_courses_keyboard()
    )


def admin_add_course_back(message):
    clear_state(message.chat.id)
    bot.send_message(
        message.chat.id,
        "به مدیریت درس‌ها بازگشتید.",
        reply_markup=get_admin_courses_keyboard()
    )


@bot.message_handler(func=lambda msg: msg.chat.id in config.ADMIN_IDS and msg.text == "🔢 تغییر ظرفیت کلاس")
def admin_change_capacity_start(message):
    data = storage.load_data()
    courses = data.get("courses", [])

    if not courses:
        bot.send_message(
            message.chat.id,
            "هیچ کلاسی ثبت نشده است.",
            reply_markup=get_admin_courses_keyboard()
        )
        return

    set_state(message.chat.id, "admin_awaiting_capacity_course")
    bot.send_message(
        message.chat.id,
        "کلاس موردنظر را انتخاب کنید:",
        reply_markup=get_admin_course_capacity_keyboard(courses)
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_capacity_"))
def admin_change_capacity_course(call):
    chat_id = call.message.chat.id

    if chat_id not in config.ADMIN_IDS:
        bot.answer_callback_query(call.id, "دسترسی غیرمجاز است.")
        return

    if call.data == "admin_capacity_back":
        clear_state(chat_id)
        bot.answer_callback_query(call.id)
        bot.send_message(
            chat_id,
            "مدیریت درس‌ها:",
            reply_markup=get_admin_courses_keyboard()
        )
        return

    course_id = call.data.rsplit("_", 1)[1]
    set_state(
        chat_id,
        "admin_awaiting_capacity_value",
        {"course_id": course_id}
    )

    bot.answer_callback_query(call.id)
    bot.send_message(
        chat_id,
        "ظرفیت جدید را به صورت عدد مثبت وارد کنید:",
        reply_markup=get_admin_courses_keyboard()
    )


@bot.message_handler(func=lambda msg: msg.chat.id in config.ADMIN_IDS and msg.text == "📝 اضافه کردن توضیح درس")
def admin_course_description_start(message):
    data = storage.load_data()
    courses = data.get("courses", [])

    if not courses:
        bot.send_message(
            message.chat.id,
            "هیچ درسی ثبت نشده است.",
            reply_markup=get_admin_courses_keyboard()
        )
        return

    markup = types.InlineKeyboardMarkup()
    for course in courses:
        markup.add(
            types.InlineKeyboardButton(
                text=f"{course['name']}",
                callback_data=f"admin_desc_select_{course['id']}"
            )
        )

    markup.add(
        types.InlineKeyboardButton(
            text="❌ لغو",
            callback_data="admin_desc_cancel"
        )
    )

    bot.send_message(
        message.chat.id,
        "لطفاً درس موردنظر را برای ثبت/ویرایش توضیح انتخاب کنید:",
        reply_markup=markup
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_desc_select_") or call.data == "admin_desc_cancel" or call.data == "admin_desc_save")
def handle_course_description_callback(call):
    chat_id = call.message.chat.id

    if chat_id not in config.ADMIN_IDS:
        bot.answer_callback_query(call.id, "دسترسی غیرمجاز است.")
        return

    if call.data == "admin_desc_cancel":
        admin_description_states.pop(chat_id, None)
        bot.answer_callback_query(call.id)
        bot.send_message(
            chat_id,
            "❌ عملیات ثبت توضیح درس لغو شد.",
            reply_markup=get_admin_courses_keyboard()
        )
        return

    if call.data.startswith("admin_desc_select_"):
        try:
            course_id = int(call.data.split("_")[-1])
        except Exception:
            bot.answer_callback_query(call.id, "شناسه درس نامعتبر است.")
            return

        course = storage.get_course_by_id(course_id)
        if not course:
            bot.answer_callback_query(call.id, "درس پیدا نشد.")
            return

        admin_description_states[chat_id] = {
            "course_id": course_id,
            "state": "waiting_for_description_text"
        }

        current_desc = (course.get("description") or "").strip()
        if not current_desc:
            current_desc = "— فعلاً توضیحی ندارد —"

        bot.answer_callback_query(call.id)
        bot.send_message(
            chat_id,
            f"درس انتخاب شد: {course['name']}\n\n"
            # f"توضیح فعلی:\n{current_desc}\n\n"
            f"حالا متن توضیح جدید را بفرستید:"
        )
        return

    if call.data == "admin_desc_save":
        pending = admin_description_states.get(chat_id)
        if not pending or pending.get("state") != "waiting_for_save_confirm":
            bot.answer_callback_query(
                call.id, "اطلاعاتی برای ذخیره وجود ندارد.")
            return

        course_id = pending.get("course_id")
        description_text = pending.get("description_text", "").strip()

        if not description_text:
            bot.answer_callback_query(call.id, "متن توضیح خالی است.")
            return

        ok = storage.update_course_description(course_id, description_text)
        course = storage.get_course_by_id(course_id)

        admin_description_states.pop(chat_id, None)
        bot.answer_callback_query(call.id)

        if ok:
            bot.send_message(
                chat_id,
                f"✅ توضیح درس «{course['name'] if course else course_id}» با موفقیت ذخیره شد.",
                reply_markup=get_admin_courses_keyboard()
            )
        else:
            bot.send_message(
                chat_id,
                "❌ ذخیره توضیح درس با خطا مواجه شد.",
                reply_markup=get_admin_courses_keyboard()
            )


@bot.message_handler(func=lambda msg: msg.chat.id in config.ADMIN_IDS and chat_id_has_pending_description(msg.chat.id))
def handle_course_description_text(message):
    chat_id = message.chat.id
    pending = admin_description_states.get(chat_id)

    if not pending or pending.get("state") != "waiting_for_description_text":
        return

    text = (message.text or "").strip()
    if not text:
        bot.send_message(chat_id, "متن توضیح نمی‌تواند خالی باشد.")
        return

    course = storage.get_course_by_id(pending["course_id"])
    if not course:
        bot.send_message(chat_id, "درس پیدا نشد.")
        admin_description_states.pop(chat_id, None)
        return

    pending["description_text"] = text
    pending["state"] = "waiting_for_save_confirm"

    current_desc = (course.get("description") or "").strip()
    if not current_desc:
        current_desc = "— ندارد —"

    preview_text = (
        f"📘 درس: {course['name']}\n\n"
        # f"📝 توضیح فعلی:\n{current_desc}\n\n"
        # f"🆕 توضیح جدید:\n{text}\n\n"
        f"اگر ذخیره کنید، توضیح قبلی جایگزین می‌شود."
    )

    markup = types.InlineKeyboardMarkup()
    markup.row(
        types.InlineKeyboardButton("✅ ذخیره", callback_data="admin_desc_save"),
        types.InlineKeyboardButton("❌ لغو", callback_data="admin_desc_cancel")
    )

    bot.send_message(chat_id, preview_text, reply_markup=markup)


def chat_id_has_pending_description(chat_id):
    pending = admin_description_states.get(chat_id)
    return bool(pending and pending.get("state") == "waiting_for_description_text")


@bot.message_handler(
    func=lambda msg:
        msg.chat.id in config.ADMIN_IDS
        and get_state(msg.chat.id) == "admin_awaiting_capacity_value"
)
def admin_change_capacity_value(message):
    if message.text == "🔙 بازگشت به منوی ادمین":
        clear_state(message.chat.id)
        bot.send_message(
            message.chat.id,
            "عملیات لغو شد.",
            reply_markup=get_admin_courses_keyboard()
        )
        return

    try:
        new_capacity = int(message.text.strip())
        if new_capacity <= 0:
            raise ValueError
    except ValueError:
        bot.send_message(
            message.chat.id,
            "خطا! ظرفیت باید یک عدد مثبت باشد. دوباره ارسال کنید:",
            reply_markup=get_admin_courses_keyboard()
        )
        return

    state_data = get_state_data(message.chat.id)
    course_id = state_data["course_id"]

    success = storage.update_course_capacity(course_id, new_capacity)
    clear_state(message.chat.id)

    if success:
        bot.send_message(
            message.chat.id,
            f"✅ ظرفیت کلاس با موفقیت به {new_capacity} تغییر کرد.",
            reply_markup=get_admin_courses_keyboard()
        )
    else:
        bot.send_message(
            message.chat.id,
            "❌ کلاس پیدا نشد یا مقدار نامعتبر بود.",
            reply_markup=get_admin_courses_keyboard()
        )


@bot.message_handler(func=lambda msg: msg.chat.id in config.ADMIN_IDS and msg.text == "🎯 تعیین معدل تاثیرگذار")
def admin_effective_gpa_menu(message):
    current_mode = storage.get_effective_gpa_mode()
    current_text = "معدل کل" if str(
        current_mode) == "total" else f"معدل ترم {current_mode}"

    bot.send_message(
        message.chat.id,
        f"معیار فعلی: {current_text}\n\nلطفاً معیار جدید را انتخاب کنید:",
        reply_markup=get_admin_effective_gpa_keyboard()
    )


@bot.message_handler(func=lambda msg: msg.chat.id in config.ADMIN_IDS and msg.text == "📈 معدل کل")
def admin_set_effective_gpa_total(message):
    storage.set_effective_gpa_mode("total")
    bot.send_message(
        message.chat.id,
        "✅ معیار معدل تاثیرگذار روی «معدل کل» تنظیم شد.",
        reply_markup=get_admin_keyboard()
    )


@bot.message_handler(func=lambda msg: msg.chat.id in config.ADMIN_IDS and msg.text == "🔢 تعیین بر اساس شماره ترم")
def admin_set_effective_gpa_term_start(message):
    set_state(message.chat.id, "admin_awaiting_effective_term_number")
    bot.send_message(
        message.chat.id,
        "لطفاً شماره ترم را وارد کنید. مثال: 1 یا 2 یا 3",
        reply_markup=back_markup()
    )


@bot.message_handler(func=lambda msg: get_state(msg.chat.id) == "admin_awaiting_effective_term_number")
def admin_set_effective_gpa_term_save(message):
    if message.text == "⬅️ بازگشت":
        clear_state(message.chat.id)
        bot.send_message(
            message.chat.id,
            "تنظیم معیار معدل لغو شد.",
            reply_markup=get_admin_effective_gpa_keyboard()
        )
        return

    term_no = message.text.strip()

    if not term_no.isdigit():
        bot.send_message(
            message.chat.id,
            "شماره ترم نامعتبر است. فقط عدد وارد کنید. مثال: 1",
            reply_markup=back_markup()
        )
        return

    if int(term_no) <= 0:
        bot.send_message(
            message.chat.id,
            "شماره ترم باید بزرگ‌تر از صفر باشد.",
            reply_markup=back_markup()
        )
        return

    storage.set_effective_gpa_mode(term_no)
    clear_state(message.chat.id)

    bot.send_message(
        message.chat.id,
        f"✅ معیار معدل تاثیرگذار روی «معدل ترم {term_no}» تنظیم شد.",
        reply_markup=get_admin_keyboard()
    )


@bot.message_handler(func=lambda msg: msg.chat.id in config.ADMIN_IDS and msg.text == "⏸️ توقف/ازسرگیری رای گیری")
def admin_toggle_voting(message):
    current_status = storage.is_voting_open()
    new_status = not current_status
    storage.set_voting_open(new_status)

    if new_status:
        text = "✅ رای‌گیری از سر گرفته شد.\nکاربران دوباره می‌توانند انتخاب درس انجام دهند."
    else:
        text = "⛔ رای‌گیری متوقف شد.\nاز این لحظه هیچ کاربری نمی‌تواند انتخاب درس انجام دهد."

    bot.send_message(
        message.chat.id,
        text,
        reply_markup=get_admin_keyboard()
    )

# --- بخش مدیریت اعضا ---


@bot.message_handler(func=lambda msg: msg.chat.id in config.ADMIN_IDS and msg.text == "👥 لیست افراد")
def admin_list_users(message):
    data = storage.load_data()
    users = data.get("users", [])
    if not users:
        bot.send_message(
            message.chat.id, "هیچ کاربری در ربات ثبت‌نام نکرده است.")
        return

    course_map = {
        course["id"]: course["name"]
        for course in data.get("courses", [])
    }

    users_with_gpa = []
    for user in users:
        effective_gpa = storage.get_user_effective_gpa(user, data)
        users_with_gpa.append((user, effective_gpa))

    users_with_gpa.sort(
        key=lambda item: item[1] if item[1] is not None else -1,
        reverse=True
    )

    text = "👥 رتبه‌بندی کل اعضا بر اساس معدل تأثیرگذار:\n\n"
    users_sorted = []

    for idx, (user, effective_gpa) in enumerate(users_with_gpa, 1):
        users_sorted.append(user)

        gpa_val = effective_gpa if effective_gpa is not None else "بدون معدل"
        selected_courses = user.get("selected_courses", [])
        course_names = [course_map[cid]
                        for cid in selected_courses if cid in course_map]
        selected_text = "، ".join(
            course_names) if course_names else "انتخاب نکرده است"

        text += f"🏅 رتبه {idx} | {user['full_name']}\n"
        text += f"کد دانشجویی: {user['student_id']}\n"
        text += f"معدل تأثیرگذار: {gpa_val}\n"
        text += f"دروس انتخابی: {selected_text}\n"
        text += "━" * 15 + "\n"

    send_chunked_message(message.chat.id, text)

    try:
        excel_path = build_users_list_excel(users_sorted, course_map, data)
        send_excel_file(message.chat.id, excel_path, "📊 فایل اکسل لیست افراد")
    except Exception as e:
        bot.send_message(
            message.chat.id, f"خطا در ساخت یا ارسال فایل اکسل لیست افراد:\n{e}")


@bot.message_handler(func=lambda msg: msg.chat.id in config.ADMIN_IDS and msg.text == "✍️ افزودن دستی افراد")
def admin_manual_users_menu(message):
    bot.send_message(message.chat.id, "تنظیم دستی افراد:",
                     reply_markup=get_admin_manual_user_keyboard())

# تنظیم معدل فرد فاقد معدل --- --- --- --- ---


@bot.message_handler(func=lambda msg: msg.chat.id in config.ADMIN_IDS and msg.text == "⚙️ تنظیم دستی افراد فاقد معدل")
def admin_set_gpa_start(message):
    no_gpa_users = storage.get_users_without_effective_gpa()

    if not no_gpa_users:
        bot.send_message(
            message.chat.id,
            "کاربر فاقد معدلِ تأثیرگذار در سیستم وجود ندارد.",
            reply_markup=get_admin_manual_user_keyboard()
        )
        return

    text = "لیست افراد فاقد معدل:\n\n"
    for i, u in enumerate(no_gpa_users, 1):
        full_name = u.get("full_name", "بدون نام")
        student_id = u.get("student_id", "---")
        telegram_id = u.get("telegram_id", "---")
        text += (
            f"{i}) نام: {full_name}\n"
            f"   کد دانشجویی: {student_id}\n"
            f"   آیدی: {telegram_id}\n\n"
        )

    send_chunked_message(message.chat.id, text)
    set_state(message.chat.id, "admin_awaiting_gpa_bulk")

    bot.send_message(
        message.chat.id,
        "شماره دانشجویی و معدل را برای هر نفر در هر خط بفرستید.\n"
        "فرمت:\n"
        "4031134001410222 19.5\n"
        "40311340014102221 20\n"
        "4031134001410223 17\n\n"
        "برای بازگشت، «⬅️ بازگشت» را بزنید.",
        reply_markup=back_markup()
    )


@bot.message_handler(func=lambda msg: get_state(msg.chat.id) == "admin_awaiting_gpa_bulk")
def admin_set_gpa_bulk_save(message):
    text = (message.text or "").strip()

    if text == "⬅️ بازگشت":
        clear_state(message.chat.id)
        bot.send_message(
            message.chat.id,
            "به منوی قبل برگشتید.",
            reply_markup=get_admin_manual_user_keyboard()
        )
        return

    if not text:
        bot.send_message(
            message.chat.id,
            "ورودی خالی است. شماره دانشجویی و معدل را در هر خط بفرستید.",
            reply_markup=back_markup()
        )
        return

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    results = []
    errors = []
    updated_student_ids = []

    for idx, line in enumerate(lines, 1):
        parts = line.split()
        if len(parts) != 2:
            errors.append(
                f"خط {idx}: فرمت باید دقیقاً به شکل `شماره_دانشجویی معدل` باشد.")
            continue

        student_id = parts[0].strip()
        gpa_text = parts[1].strip().replace("/", ".")

        try:
            gpa = float(gpa_text)
            if gpa < 0 or gpa > 20:
                raise ValueError
        except ValueError:
            errors.append(f"خط {idx}: معدل برای {student_id} نامعتبر است.")
            continue

        success = storage.update_user_gpa(student_id, gpa)
        if success:
            updated_student_ids.append(student_id)
            results.append(f"✅ {student_id} -> {gpa}")
        else:
            errors.append(
                f"خط {idx}: کاربر با شماره دانشجویی {student_id} یافت نشد یا ذخیره نشد.")

    for student_id in updated_student_ids:
        try:
            user = storage.get_user_by_student_id(student_id)

            if user:
                storage.save_user(user)
            else:
                errors.append(
                    f"معدل {student_id} ثبت شد، اما کاربر برای "
                    f"به‌روزرسانی رتبه پیدا نشد."
                )
        except Exception as e:
            errors.append(
                f"معدل {student_id} ثبت شد، اما به‌روزرسانی "
                f"رتبه ناموفق بود: {e}"
            )

    clear_state(message.chat.id)

    response_parts = []
    if results:
        response_parts.append("نتایج موفق:\n" + "\n".join(results))
    if errors:
        response_parts.append("خطاها:\n" + "\n".join(errors))

    final_text = "\n\n".join(
        response_parts) if response_parts else "هیچ ورودی معتبری دریافت نشد."
    send_chunked_message(message.chat.id, final_text)

    bot.send_message(
        message.chat.id,
        "عملیات تمام شد.",
        reply_markup=get_admin_manual_user_keyboard()
    )

# افزودن دستی فرد جدید توسط ادمین
# =========================
# افزودن دستی فرد جدید توسط ادمین - BULK
# با حفظ کامل منطق قبلی
# =========================


def back_markup():
    markup = types.ReplyKeyboardMarkup(
        resize_keyboard=True, one_time_keyboard=True)
    markup.add("⬅️ بازگشت")
    return markup


def remove_keyboard():
    return types.ReplyKeyboardRemove()


def safe_float_gpa(text):
    try:
        val = float(str(text).strip().replace(
            "/", ".").replace("٫", ".").replace(",", "."))
        if val < 0 or val > 20:
            return None
        return val
    except Exception:
        return None


def get_courses_map():
    courses = storage.get_courses()
    course_map = {}
    for c in courses:
        course_map[int(c["id"])] = c
    return courses, course_map


def find_user_by_student_id(student_id):
    """
    پیدا کردن کاربر قبلی برای overwrite
    این قسمت باید با ساختار واقعی storage شما سازگار باشد.
    """
    try:
        users = storage.get_users()
    except Exception:
        users = getattr(storage, "users", [])

    for u in users:
        if str(u.get("student_id", "")).strip() == str(student_id).strip():
            return u
    return None


def delete_existing_user_by_student_id(student_id):
    """
    حذف رکورد قبلی قبل از ذخیره‌ی نسخه‌ی جدید
    """
    existing = find_user_by_student_id(student_id)
    if not existing:
        return None

    # اگر متد حذف اختصاصی دارید، اول آن را امتحان می‌کنیم
    if hasattr(storage, "delete_user_by_student_id"):
        try:
            storage.delete_user_by_student_id(student_id)
            return existing
        except Exception:
            pass

    if hasattr(storage, "delete_user"):
        try:
            storage.delete_user(existing)
            return existing
        except Exception:
            pass

    # fallback: اگر لیست users در storage در دسترس بود
    try:
        users = storage.get_users()
    except Exception:
        users = getattr(storage, "users", None)

    if users is not None:
        try:
            new_users = [u for u in users if str(
                u.get("student_id", "")).strip() != str(student_id).strip()]
            if hasattr(storage, "users"):
                storage.users = new_users
            if hasattr(storage, "save_all_users"):
                storage.save_all_users(new_users)
            return existing
        except Exception:
            pass

    return existing


def parse_admin_bulk_line(line, valid_course_ids):
    """
    فرمت:
    نام آزاد کاربر  student_id  gpa  p1 p2 p3 p4 p5
    نام می‌تواند چندکلمه‌ای باشد.
    """
    raw = line.strip()
    if not raw:
        raise ValueError("خط خالی است.")

    parts = raw.split()
    if len(parts) < 8:
        raise ValueError(
            "فرمت نادرست است. حداقل باید: نام + کد دانشجویی + معدل + 5 انتخاب باشد.")

    # چون نام آزاد و چندکلمه‌ای است:
    # - اولی را اسم نمی‌گیریم چون ممکن است چندکلمه‌ای باشد
    # - 5 تای آخر انتخاب‌ها هستند
    # - یکی قبل از آن معدل است
    # - یکی قبل از آن student_id
    student_id = parts[-7]
    gpa_text = parts[-6]
    priority_texts = parts[-5:]
    name_parts = parts[:-7]

    full_name = " ".join(name_parts).strip()
    if not full_name:
        raise ValueError("نام و نام خانوادگی نمی‌تواند خالی باشد.")

    if not student_id.isdigit():
        raise ValueError("شماره دانشجویی باید فقط شامل عدد باشد.")

    gpa = safe_float_gpa(gpa_text)
    if gpa is None:
        raise ValueError("معدل نامعتبر است. باید عددی بین 0 تا 20 باشد.")

    try:
        priorities = [int(x) for x in priority_texts]
    except Exception:
        raise ValueError("تمام انتخاب‌ها باید عددی باشند.")

    if len(priorities) != 5:
        raise ValueError("باید دقیقاً 5 انتخاب ثبت شود.")

    if len(set(priorities)) != 5:
        raise ValueError("انتخاب‌ها تکراری هستند.")

    invalid_courses = [x for x in priorities if x not in valid_course_ids]
    if invalid_courses:
        raise ValueError(
            f"این کدهای درس در سیستم وجود ندارند: {invalid_courses}")

    return {
        "student_id": student_id,
        "full_name": full_name,
        "gpa": gpa,
        "selected_courses": priorities
    }


@bot.message_handler(func=lambda msg: msg.chat.id in config.ADMIN_IDS and msg.text == "➕ افزودن فرد جدید")
def admin_add_user_start(message):
    courses = storage.get_courses()
    if len(courses) < 5:
        bot.send_message(
            message.chat.id,
            "خطا! برای ثبت دستی کاربر باید حداقل ۵ درس در سیستم وجود داشته باشد.",
            reply_markup=remove_keyboard()
        )
        show_admin_menu(message.chat.id)
        return

    courses_text = "\n".join([f"• {c['id']} - {c['name']}" for c in courses])

    text = (
        "لطفاً اطلاعات افراد جدید را در چند خط ارسال کنید.\n\n"
        "فرمت هر خط:\n"
        "`نام آزاد کاربر student_id gpa p1 p2 p3 p4 p5`\n\n"
        "مثال:\n"
        "`امیر ممد ناصری 4031134001410222 19.5 1 2 5 3 7`\n"
        "`امیر علی نقی مقدم پور 4031134001410221 11.5 10 2 5 4 7`\n"
        "`علی ناصری 34001410221 20 3 5 1 9 12`\n\n"
        "نکات:\n"
        "- نام می‌تواند چندکلمه‌ای باشد\n"
        "- انتخاب‌ها بر اساس شماره درس هستند و از 1 شروع می‌شوند\n"
        "- اگر کد دانشجویی قبلاً وجود داشته باشد، رکورد قبلی حذف و این یکی جایگزین می‌شود\n"
        "- هر خط جداگانه بررسی می‌شود و خطا/موفقیت همان خط گزارش می‌شود\n\n"
        "درس‌های موجود سیستم:\n"
        f"{courses_text}"
    )

    set_state(message.chat.id, "admin_awaiting_bulk_new_users", {})
    bot.send_message(message.chat.id, text, reply_markup=back_markup())


@bot.message_handler(func=lambda msg: get_state(msg.chat.id) == "admin_awaiting_bulk_new_users")
def admin_receive_bulk_new_users(message):
    if message.text == "⬅️ بازگشت":
        clear_state(message.chat.id)
        show_admin_menu(message.chat.id)
        return

    raw_text = (message.text or "").strip()
    if not raw_text:
        bot.send_message(
            message.chat.id,
            "❌ ورودی خالی است. لطفاً دوباره ارسال کنید:",
            reply_markup=back_markup()
        )
        return

    courses, course_map = get_courses_map()
    valid_course_ids = set(course_map.keys())

    if len(courses) < 5:
        bot.send_message(
            message.chat.id,
            "خطا! برای ثبت دستی کاربر باید حداقل ۵ درس در سیستم وجود داشته باشد.",
            reply_markup=remove_keyboard()
        )
        clear_state(message.chat.id)
        show_admin_menu(message.chat.id)
        return

    lines = [ln.strip() for ln in raw_text.splitlines() if ln.strip()]

    success_lines = []
    fail_lines = []

    for idx, line in enumerate(lines, start=1):
        try:
            parsed = parse_admin_bulk_line(line, valid_course_ids)

            student_id = parsed["student_id"]
            full_name = parsed["full_name"]
            gpa = parsed["gpa"]
            selected_courses = parsed["selected_courses"]

            existing_user = find_user_by_student_id(student_id)

            preserved_telegram_id = None
            preserved_username = ""

            if existing_user:
                preserved_telegram_id = existing_user.get("telegram_id", None)
                preserved_username = existing_user.get("username", "") or ""
                delete_existing_user_by_student_id(student_id)

            new_user = {
                "telegram_id": preserved_telegram_id,
                "mode": "gpa",
                "username": preserved_username,
                "student_id": student_id,
                "full_name": full_name,
                "manual_gpa": gpa,
                "gpa": gpa,
                "terms_gpa": {str(term): gpa for term in range(1, 11)},
                "rank": None,
                "selected_courses": selected_courses.copy(),
                "gpa_login_required": True
            }

            storage.save_user(new_user)

            selected_course_names = [
                course_map[cid]["name"] if cid in course_map else str(cid)
                for cid in selected_courses
            ]

            success_lines.append(
                f"✅ خط {idx}:\n"
                f"👤 {full_name}\n"
                f"🎓 {student_id}\n"
                f"📊 معدل: {gpa}\n"
                f"📚 انتخاب‌ها: {', '.join(selected_course_names)}"
            )

        except Exception as e:
            fail_lines.append(
                f"❌ خط {idx} ذخیره نشد.\n"
                f"📝 متن: {line}\n"
                f"⚠️ دلیل: {str(e)}"
            )

    clear_state(message.chat.id)

    report_parts = []
    if success_lines:
        report_parts.append("🟩 موارد موفق:\n\n" + "\n\n".join(success_lines))
    if fail_lines:
        report_parts.append("🟥 موارد ناموفق:\n\n" + "\n\n".join(fail_lines))

    if not report_parts:
        report_parts.append("هیچ خطی برای پردازش وجود نداشت.")

    final_report = "\n\n" + ("=" * 25) + "\n\n"
    final_report = final_report.join(report_parts)

    send_chunked_message(message.chat.id, final_report, chunk_size=1400)
    show_admin_menu(message.chat.id)


def send_chunked_message(chat_id, text, parse_mode=None, chunk_size=1400):
    if text is None:
        text = ""

    text = str(text)

    if not text.strip():
        bot.send_message(
            chat_id, "پیامی برای نمایش وجود ندارد.", parse_mode=parse_mode)
        return

    lines = text.splitlines(keepends=True)
    current_chunk = ""

    for line in lines:
        if len(line) > chunk_size:
            if current_chunk:
                bot.send_message(chat_id, current_chunk, parse_mode=parse_mode)
                current_chunk = ""

            for i in range(0, len(line), chunk_size):
                bot.send_message(
                    chat_id, line[i:i + chunk_size], parse_mode=parse_mode)
            continue

        if len(current_chunk) + len(line) > chunk_size:
            bot.send_message(chat_id, current_chunk, parse_mode=parse_mode)
            current_chunk = line
        else:
            current_chunk += line

    if current_chunk:
        bot.send_message(chat_id, current_chunk, parse_mode=parse_mode)

# حذف افراد ==== ==== ==== ==== ==== ==== ==== ====


@bot.message_handler(func=lambda msg: msg.chat.id in config.ADMIN_IDS and msg.text == "❌ حذف افراد")
def admin_delete_user_start(message):
    set_state(message.chat.id, "admin_awaiting_delete_student_ids")
    bot.send_message(
        message.chat.id,
        "لطفاً شماره دانشجویی افراد مورد نظر را برای حذف، هر کدام در یک سطر بفرستید:\n\n"
        "مثال:\n"
        "4031134001410222\n"
        "4031134001410221\n"
        "34001410221",
        reply_markup=back_markup()
    )


@bot.message_handler(func=lambda msg: get_state(msg.chat.id) == "admin_awaiting_delete_student_ids")
def admin_delete_user_exec(message):
    if message.text == "⬅️ بازگشت":
        clear_state(message.chat.id)
        show_admin_menu(message.chat.id)
        return

    raw_text = (message.text or "").strip()
    if not raw_text:
        bot.send_message(
            message.chat.id,
            "❌ ورودی خالی است. لطفاً شماره دانشجویی‌ها را هر کدام در یک سطر ارسال کنید:",
            reply_markup=back_markup()
        )
        return

    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]

    success_list = []
    fail_list = []

    for idx, student_id in enumerate(lines, start=1):
        if not student_id.isdigit():
            fail_list.append(
                f"❌ خط {idx}: `{student_id}`\n"
                f"⚠️ دلیل: شماره دانشجویی باید فقط شامل عدد باشد."
            )
            continue

        try:
            success = storage.delete_user_by_student_id(student_id)
            if success:
                success_list.append(
                    f"✅ خط {idx}: کاربر با شماره دانشجویی {student_id} با موفقیت حذف شد."
                )
            else:
                fail_list.append(
                    f"❌ خط {idx}: {student_id}\n"
                    f"⚠️ دلیل: کاربری با این شماره دانشجویی در سیستم یافت نشد."
                )
        except Exception as e:
            fail_list.append(
                f"❌ خط {idx}: {student_id}\n"
                f"⚠️ دلیل: خطا در حذف کاربر: {str(e)}"
            )

    clear_state(message.chat.id)

    report_parts = []
    if success_list:
        report_parts.append("🟩 موارد حذف‌شده:\n\n" + "\n".join(success_list))
    if fail_list:
        report_parts.append("🟥 موارد ناموفق:\n\n" + "\n".join(fail_list))

    final_report = "\n\n========================\n\n".join(
        report_parts) if report_parts else "هیچ موردی برای پردازش وجود نداشت."

    send_chunked_message(message.chat.id, final_report, chunk_size=1400)
    show_admin_menu(message.chat.id)

# نتایج انتخابات


@bot.message_handler(func=lambda msg: msg.chat.id in config.ADMIN_IDS and msg.text == "📊 نتیجه انتخابات")
def admin_results_menu(message):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row("🏫 برحسب کلاس", "👤 برحسب افراد")
    markup.row("🔙 بازگشت به منوی ادمین")
    bot.send_message(
        message.chat.id, "نوع فیلتر نمایش نتیجه انتخابات:", reply_markup=markup)


@bot.message_handler(func=lambda msg: msg.chat.id in config.ADMIN_IDS and msg.text == "👤 برحسب افراد")
def admin_results_by_users(message):
    courses, user_results = storage.run_matching()
    data = storage.load_data()

    text = "👤 نتایج نهایی به تفکیک افراد:\n\n"
    for user in data["users"]:
        res = user_results.get(str(user["student_id"]))
        course_name = res["course_name"] if res else "مردود/بدون اولویت"
        priority = f"(اولویت {res['priority']})" if res and res["priority"] else ""

        effective_gpa = storage.get_user_effective_gpa(user, data)
        gpa_val = effective_gpa if effective_gpa is not None else "بدون معدل"

        text += f"👤 {user['full_name']}\n"
        text += f"کد دانشجویی: {user['student_id']} | رتبه: {user.get('rank', 'فاقد رتبه')} | معدل تأثیرگذار: {gpa_val}\n"
        text += f"📥 کلاس اختصاص یافته: {course_name} {priority}\n"
        text += "━" * 15 + "\n"

    send_chunked_message(message.chat.id, text)

    try:
        excel_path = build_results_by_users_excel(
            data["users"], user_results, data)
        send_excel_file(message.chat.id, excel_path,
                        "📊 فایل اکسل نتایج انتخابات برحسب افراد")
    except Exception as e:
        bot.send_message(
            message.chat.id, f"خطا در ساخت یا ارسال فایل اکسل نتایج برحسب افراد:\n{e}")


def build_single_course_excel(course_info):
    course_name = str(course_info.get("name", "course"))

    # نام فایل ایمن
    safe_name = "".join(
        c if c.isalnum() else "_"
        for c in course_name
    ).strip("_")

    # جلوگیری از طولانی‌شدن بیش از حد نام فایل
    safe_name = safe_name[:80] or "course"

    # ساخت فایل یکتا در مسیر موقت و قابل‌نوشتن سیستم
    temp_file = tempfile.NamedTemporaryFile(
        mode="wb",
        prefix=f"Class_{safe_name}_",
        suffix=".xlsx",
        delete=False
    )

    filename = temp_file.name
    temp_file.close()

    try:
        accepted_students = course_info.get("accepted", [])

        # ترتیب ستون‌های اصلی
        df = pd.DataFrame(
            accepted_students,
            columns=["name", "student_id", "rank", "gpa"]
        )

        rename_map = {
            "name": "نام و نام خانوادگی",
            "student_id": "شماره دانشجویی",
            "rank": "رتبه",
            "gpa": "معدل"
        }

        df = df.rename(columns=rename_map)

        # ذخیره در مسیر موقت
        df.to_excel(filename, index=False, engine="openpyxl")

        return filename

    except Exception:
        # اگر ساخت فایل ناموفق بود، فایل ناقص حذف شود
        if os.path.exists(filename):
            os.remove(filename)
        raise


@bot.message_handler(func=lambda msg: msg.chat.id in config.ADMIN_IDS and msg.text == "🏫 برحسب کلاس")
def admin_results_by_class(message):
    courses, user_results = storage.run_matching()

    # ۱. فرستادن گزارش متنی
    text = "🏫 نتایج نهایی به تفکیک کلاس‌ها:\n\n"
    for cid, info in courses.items():
        text += f"📚 کلاس {info['name']} (ظرفیت: {info['capacity']})\n"

        if info["accepted"]:
            for idx, student in enumerate(info["accepted"], 1):
                text += (
                    f"{idx}. {student['name']} | "
                    f"شماره دانشجویی: {student['student_id']} | "
                    f"رتبه: {student['rank']} | "
                    f"معدل: {student['gpa']}\n"
                )

            single_excel = None

            try:
                single_excel = build_single_course_excel(info)

                with open(single_excel, "rb") as file:
                    bot.send_document(
                        message.chat.id,
                        file,
                        caption=f"📊 فایل اکسل کلاس {info['name']}"
                    )

            except Exception as e:
                bot.send_message(
                    message.chat.id,
                    f"❌ خطا در ارسال فایل کلاس {info['name']}:\n{e}"
                )

            finally:
                if single_excel and os.path.exists(single_excel):
                    try:
                        os.remove(single_excel)
                    except OSError:
                        pass

        else:
            text += "⚠️ هیچ قبولی در این کلاس وجود ندارد.\n"

        text += "━" * 15 + "\n"

    send_chunked_message(message.chat.id, text)

    # ۳. فرستادن فایل اکسل کلی (طبق روال قبلی شما)
    try:
        excel_path = build_results_by_class_excel(
            courses)  # تابعی که قبلاً داشتید
        send_excel_file(message.chat.id, excel_path,
                        "🏫 فایل اکسل کلی نتایج انتخابات")
    except Exception as e:
        bot.send_message(
            message.chat.id, f"خطا در ساخت یا ارسال فایل اکسل کلی:\n{e}")

# دستور مسدودسازی توسط ادمین
# ساختار دستور:
# /block 123456789 (مسدودسازی چت آیدی ربات)
# /block 192.168.1.1 (مسدودسازی آی‌پی سایت)
# /block 99123456 (مسدودسازی کد دانشجویی در سایت)


@bot.message_handler(func=lambda msg: msg.chat.id in config.ADMIN_IDS and msg.text.startswith("/block"))
def admin_block_command(message):
    parts = message.text.split()
    if len(parts) < 2:
        bot.send_message(
            message.chat.id, "❌ دستور نامعتبر است.\nراهنما: `/block [ip/chat_id/student_id]`")
        return

    target = parts[1].strip()
    data = storage.load_data()

    blocked_ips = data.setdefault("blocked_ips", [])
    blocked_telegram_ids = data.setdefault("blocked_telegram_ids", [])
    blocked_users = data.setdefault("blocked_users", [])

    # تشخیص نوع هدف برای مسدودسازی
    if "." in target:  # مسدودسازی بر اساس IP
        if target not in blocked_ips:
            blocked_ips.append(target)
            msg_txt = f"🌐 آی‌پی `{target}` با موفقیت مسدود شد."
        else:
            msg_txt = "⚠️ این آی‌پی از قبل مسدود شده بود."
    elif target.isdigit() and len(target) > 7:  # مسدودسازی بر اساس شناسه عددی چت بله/تلگرام
        if target not in blocked_telegram_ids:
            blocked_telegram_ids.append(target)
            msg_txt = f"📱 کاربر تلگرام/بله با شناسه `{target}` مسدود شد."
        else:
            msg_txt = "⚠️ این کاربر تلگرام/بله از قبل مسدود شده بود."
    else:  # مسدودسازی بر اساس کد دانشجویی
        if target not in blocked_users:
            blocked_users.append(target)
            msg_txt = f"👤 کد دانشجویی `{target}` در سایت مسدود شد."
        else:
            msg_txt = "⚠️ این کد دانشجویی از قبل مسدود شده بود."

    storage.save_data(data)
    bot.send_message(message.chat.id, msg_txt)


# دستور رفع مسدودسازی توسط ادمین
@bot.message_handler(func=lambda msg: msg.chat.id in config.ADMIN_IDS and msg.text.startswith("/unblock"))
def admin_unblock_command(message):
    parts = message.text.split()
    if len(parts) < 2:
        bot.send_message(
            message.chat.id, "❌ دستور نامعتبر است.\nراهنما: `/unblock [ip/chat_id/student_id]`")
        return

    target = parts[1].strip()
    data = storage.load_data()

    blocked_ips = data.setdefault("blocked_ips", [])
    blocked_telegram_ids = data.setdefault("blocked_telegram_ids", [])
    blocked_users = data.setdefault("blocked_users", [])

    removed = False

    if target in blocked_ips:
        blocked_ips.remove(target)
        removed = True
    if target in blocked_telegram_ids:
        blocked_telegram_ids.remove(target)
        removed = True
    if target in blocked_users:
        blocked_users.remove(target)
        removed = True

    if removed:
        storage.save_data(data)
        bot.send_message(
            message.chat.id, f"✅ رفع مسدودسازی `{target}` با موفقیت انجام شد.")
    else:
        bot.send_message(
            message.chat.id, "❌ این مشخصات در لیست مسدودین یافت نشد.")


def is_telegram_user_blocked(chat_id):
    data = storage.load_data()
    blocked_telegram_ids = data.setdefault("blocked_telegram_ids", [])
    return str(chat_id) in blocked_telegram_ids

# --- بخش کاربر عادی -------------------------------------------------------------------------------------
# --- بخش کاربر عادی -------------------------------------------------------------------------------------
# --- بخش کاربر عادی -------------------------------------------------------------------------------------
# --- بخش کاربر عادی -------------------------------------------------------------------------------------
# --- بخش کاربر عادی -------------------------------------------------------------------------------------


@bot.message_handler(func=lambda msg: msg.text == "🔐 با ثبت معدل (ورود با هم‌آوا)")
def user_login_gpa_start(message):
    clear_state(message.chat.id)
    set_state(message.chat.id, "user_awaiting_username", {})
    bot.send_message(
        message.chat.id,
        "لطفاً نام کاربری هم‌آوا (شماره دانشجویی) را بفرستید:",
        reply_markup=back_markup()
    )


@bot.message_handler(func=lambda msg: get_state(msg.chat.id) == "user_awaiting_username")
def user_login_gpa_username(message):
    if message.text == "⬅️ بازگشت":
        clear_state(message.chat.id)
        bot.send_message(
            message.chat.id,
            "لطفاً نحوه ورود خود را انتخاب کنید:",
            reply_markup=get_login_method_keyboard()
        )
        return

    username = message.text.strip()

    # نام کاربری هم‌آوا (شماره دانشجویی) باید فقط عدد باشد
    if not username.isdigit():
        bot.send_message(
            message.chat.id,
            "❌ نام کاربری نامعتبر است.\n"
            "نام کاربری هم‌آوا باید فقط شامل اعداد (شماره دانشجویی) باشد.\n\n"
            "لطفاً دوباره وارد کنید:",
            reply_markup=back_markup()
        )
        return

    if not is_student_id_allowed(username):
        bot.send_message(
            message.chat.id,
            "⛔️ در حال حاضر ورود با این شماره دانشجویی مجاز نیست.\n"
            "لطفاً با شماره دانشجویی مجاز دوباره تلاش کنید.",
            reply_markup=back_markup()
        )
        return

    set_state(message.chat.id, "user_awaiting_password",
              {"username": username})
    bot.send_message(
        message.chat.id,
        "حالا کلمه عبور هم‌آوا را ارسال کنید:",
        reply_markup=back_markup()
    )


@bot.message_handler(func=lambda msg: get_state(msg.chat.id) == "user_awaiting_password")
def user_login_gpa_password(message):
    if message.text == "⬅️ بازگشت":
        state_data = get_state_data(message.chat.id)
        set_state(message.chat.id, "user_awaiting_username", state_data)
        bot.send_message(
            message.chat.id,
            "لطفاً نام کاربری هم‌آوا (شماره دانشجویی) را بفرستید:",
            reply_markup=back_markup()
        )
        return

    password = message.text.strip()
    if not password:
        bot.send_message(
            message.chat.id,
            "کلمه عبور نامعتبر است. دوباره وارد کنید:",
            reply_markup=back_markup()
        )
        return

    data = get_state_data(message.chat.id)
    username = data["username"]

    # ثبت تلاش برای جلوگیری از تست تعداد زیادی شماره دانشجویی
    track_student_id_attempt(message, username, source="gpa")

    msg_wait = bot.send_message(
        message.chat.id,
        "⏳ در حال برقراری ارتباط با پرتال هم‌آوا دانشگاه... لطفا صبر کنید."
    )
    res = scraper.get_student_info(username, password)

    if res["status"] == "success":
        bot.delete_message(message.chat.id, msg_wait.message_id)

        existing_user = storage.get_user_by_student_id(username)
        if existing_user:
            existing_user["telegram_id"] = message.chat.id
            existing_user["mode"] = "gpa"
            existing_user["username"] = username
            existing_user["student_id"] = username
            existing_user["active_student_id"] = username
            existing_user["full_name"] = res["name"]
            existing_user["gpa"] = res["gpa"]
            existing_user["terms_gpa"] = res.get("terms_gpa", {})
            existing_user["gpa_login_required"] = True
            if "rank" not in existing_user:
                existing_user["rank"] = None
            if "selected_courses" not in existing_user:
                existing_user["selected_courses"] = []
            user_obj = existing_user
        else:
            user_obj = {
                "telegram_id": message.chat.id,
                "mode": "gpa",
                "username": username,
                "student_id": username,
                "active_student_id": username,
                "full_name": res["name"],
                "gpa": res["gpa"],
                "terms_gpa": res.get("terms_gpa", {}),
                "rank": None,
                "selected_courses": [],
                "gpa_login_required": True
            }

        storage.save_user(user_obj)
        clear_state(message.chat.id)
        set_state(message.chat.id, "user_panel", {
                  "active_student_id": str(user_obj["student_id"])})

        bot.send_message(
            message.chat.id,
            f"خوش آمدید {res['name']} عزیز!",
            reply_markup=get_user_panel_keyboard(),
            parse_mode="Markdown"
        )

    elif res["status"] == "gpa_not_found":
        bot.delete_message(message.chat.id, msg_wait.message_id)

        existing_user = storage.get_user_by_student_id(username)
        if existing_user:
            existing_user["telegram_id"] = message.chat.id
            existing_user["mode"] = "gpa"
            existing_user["username"] = username
            existing_user["student_id"] = username
            existing_user["active_student_id"] = username
            existing_user["full_name"] = res["name"]
            existing_user["gpa"] = None
            existing_user["terms_gpa"] = res.get("terms_gpa", {})
            existing_user["gpa_login_required"] = True
            if "rank" not in existing_user:
                existing_user["rank"] = None
            if "selected_courses" not in existing_user:
                existing_user["selected_courses"] = []
            user_obj = existing_user
        else:
            user_obj = {
                "telegram_id": message.chat.id,
                "mode": "gpa",
                "username": username,
                "student_id": username,
                "active_student_id": username,
                "full_name": res["name"],
                "gpa": None,
                "terms_gpa": res.get("terms_gpa", {}),
                "rank": None,
                "selected_courses": [],
                "gpa_login_required": True
            }

        storage.save_user(user_obj)
        clear_state(message.chat.id)
        set_state(message.chat.id, "user_panel", {
                  "active_student_id": str(user_obj["student_id"])})

        bot.send_message(
            message.chat.id,
            f"خوش آمدید {res['name']}.\nنام شما تایید شد اما امکان استخراج خودکار معدل نبود. معدل شما موقتا ثبت نشد (در صورت نیاز ادمین دستی ثبت می‌کند).",
            reply_markup=get_user_panel_keyboard()
        )

    else:
        bot.delete_message(message.chat.id, msg_wait.message_id)
        bot.send_message(
            message.chat.id,
            "❌ ورود ناموفق بود! نام کاربری یا کلمه عبور اشتباه است، یا ارتباط با سایت دانشگاه برقرار نشد.",
            reply_markup=get_login_method_keyboard()
        )

# ورود بدون معدل


def track_student_id_attempt(message, student_id, source="no_gpa"):
    data = storage.load_data()
    security = data.setdefault("security", {})
    attempts = security.setdefault("student_id_attempts", [])
    alerts = security.setdefault("student_id_alerts", {})

    chat_id = str(message.chat.id)
    username = getattr(message.from_user, "username", None)

    now = int(time.time())
    attempts.append({
        "chat_id": chat_id,
        "student_id": str(student_id),
        "username": username,
        "source": source,
        "ts": now,
    })

    # لاگ‌های قدیمی‌تر از 7 روز حذف شوند
    keep_after = now - (7 * 24 * 3600)
    security["student_id_attempts"] = [
        x for x in attempts if x["ts"] >= keep_after
    ]

    # تلاش‌های همین کاربر در بازه زمانی مشخص
    recent = [
        x for x in security["student_id_attempts"]
        if x["chat_id"] == chat_id
        and x["ts"] >= now - SUSPICIOUS_WINDOW_SECONDS
    ]

    unique_student_ids = sorted({x["student_id"] for x in recent})
    current_count = len(unique_student_ids)

    # آخرین تعداد شماره‌ای که بابتش هشدار داده شده
    last_alert_count = int(alerts.get(chat_id, 0))

    should_alert = (
        current_count >= SUSPICIOUS_STUDENT_ID_THRESHOLD
        and current_count >= last_alert_count + SUSPICIOUS_STUDENT_ID_THRESHOLD
    )

    if should_alert:
        display_username = f"@{username}" if username else "-"

        source_text = {
            "gpa": "ورود با هم‌آوا",
            "no_gpa": "ورود بدون معدل"
        }.get(source, source)

        admin_text = (
            "🚨 هشدار فعالیت مشکوک\n\n"
            f"👤 کاربر: {display_username}\n"
            f"🆔 Chat ID: {chat_id}\n"
            f"📍 بخش: {source_text}\n"
            f"🔍 تعداد شماره دانشجویی‌های مختلف: {current_count}\n"
            f"🔢 شماره‌ها: {', '.join(unique_student_ids)}"
        )

        for admin_id in ADMIN_IDS:
            try:
                bot.send_message(admin_id, admin_text)
            except Exception as e:
                print(f"Error sending alert to {admin_id}: {e}")

        # ثبت آخرین تعداد هشدار داده‌شده
        alerts[chat_id] = current_count

    storage.save_data(data)


@bot.message_handler(func=lambda msg: msg.text == "📝 بدون ثبت معدل (انتخاب مستقیم درس)")
def user_no_gpa_start(message):
    clear_state(message.chat.id)
    set_state(message.chat.id, "user_awaiting_studentid_only", {})
    bot.send_message(
        message.chat.id,
        "لطفاً ابتدا شماره دانشجویی خود را ارسال کنید:",
        reply_markup=back_markup()
    )


@bot.message_handler(func=lambda msg: get_state(msg.chat.id) == "user_awaiting_studentid_only")
def user_no_gpa_studentid(message):
    if message.text == "⬅️ بازگشت":
        clear_state(message.chat.id)
        bot.send_message(
            message.chat.id,
            "لطفاً نحوه ورود خود را انتخاب کنید:",
            reply_markup=get_login_method_keyboard()
        )
        return

    student_id = message.text.strip()

    # فقط عدد مجاز است
    if not student_id.isdigit():
        bot.send_message(
            message.chat.id,
            "❌ شماره دانشجویی نامعتبر است.\n"
            "شماره دانشجویی باید فقط شامل اعداد باشد.\n\n"
            "لطفاً دوباره ارسال کنید:",
            reply_markup=back_markup()
        )
        return

    if not is_student_id_allowed(student_id):
        bot.send_message(
            message.chat.id,
            "⛔️ در حال حاضر ورود با این شماره دانشجویی مجاز نیست.\n"
            "لطفاً با شماره دانشجویی مجاز دوباره تلاش کنید.",
            reply_markup=back_markup()
        )
        return

    track_student_id_attempt(message, student_id, source="no_gpa")

    existing_user = storage.get_user_by_student_id(student_id)
    if existing_user:
        if existing_user.get("gpa_login_required") is True:
            clear_state(message.chat.id)
            bot.send_message(
                message.chat.id,
                "⚠️ برای این شماره دانشجویی، ورود بدون معدل غیرفعال است.\nلطفاً فقط از طریق «ورود با هم‌آوا» وارد شوید.",
                reply_markup=get_login_method_keyboard()
            )
            return

        changed = False
        if str(existing_user.get("telegram_id")) != str(message.chat.id):
            existing_user["telegram_id"] = message.chat.id
            changed = True
        if existing_user.get("mode") is None:
            existing_user["mode"] = "no_gpa"
            changed = True
        if changed:
            storage.save_user(existing_user)

        clear_state(message.chat.id)
        set_state(message.chat.id, "user_panel", {
                  "active_student_id": str(student_id)})

        bot.send_message(
            message.chat.id,
            f"خوش آمدید {existing_user['full_name']} عزیز! اطلاعات شما از قبل موجود بود و وارد همان حساب شدید.",
            reply_markup=get_user_panel_keyboard()
        )
        return

    set_state(message.chat.id, "user_awaiting_name_only",
              {"student_id": student_id})
    bot.send_message(
        message.chat.id,
        "شماره دانشجویی یافت نشد. لطفاً نام و نام خانوادگی خود را ارسال کنید:",
        reply_markup=back_markup()
    )


@bot.message_handler(func=lambda msg: get_state(msg.chat.id) == "user_awaiting_name_only")
def user_no_gpa_name(message):
    if message.text == "⬅️ بازگشت":
        clear_state(message.chat.id)
        set_state(message.chat.id, "user_awaiting_studentid_only", {})
        bot.send_message(
            message.chat.id,
            "لطفاً ابتدا شماره دانشجویی خود را ارسال کنید:",
            reply_markup=back_markup()
        )
        return

    full_name = message.text.strip()
    if not full_name:
        bot.send_message(
            message.chat.id,
            "نام و نام خانوادگی نامعتبر است. دوباره ارسال کنید:",
            reply_markup=back_markup()
        )
        return

    data = get_state_data(message.chat.id)
    student_id = data["student_id"]

    user_obj = {
        "telegram_id": message.chat.id,
        "mode": "no_gpa",
        "username": None,
        "student_id": student_id,
        "active_student_id": student_id,
        "full_name": full_name,
        "gpa": None,
        "terms_gpa": {},
        "selected_courses": [],
        "gpa_login_required": False
    }
    storage.save_user(user_obj)

    clear_state(message.chat.id)
    set_state(message.chat.id, "user_panel", {
              "active_student_id": str(student_id)})

    bot.send_message(
        message.chat.id,
        f"خوش آمدید {full_name} عزیز! شما وارد پنل کاربری شدید.",
        reply_markup=get_user_panel_keyboard()
    )

# انتخاب درس‌ها (۵ اولویت)


@bot.message_handler(func=lambda msg: msg.text == "📥 انتخاب درس ها")
def user_course_selection_start(message):

    user = get_active_user_by_chat(message.chat.id)
    if not user:
        bot.send_message(
            message.chat.id, "شما هنوز ثبت نام نکرده‌اید. با /start شروع کنید.")
        return

    if not storage.is_voting_open():
        bot.send_message(
            message.chat.id,
            "⛔ در حال حاضر انتخاب درس، توسط ادمین متوقف شده است و امکان انتخاب درس وجود ندارد."
        )
        return

    if len(user.get("selected_courses", [])) == 5:
        bot.send_message(
            message.chat.id, "شما قبلاً ۵ اولویت خود را ثبت کرده‌اید. اگر لازم است ابتدا حذفشان کنید.")
        return

    courses = storage.get_courses()
    if len(courses) < 5:
        bot.send_message(
            message.chat.id, "کمتر از ۵ درس ثبت شده است؛ فعلاً امکان انتخاب وجود ندارد.")
        return

    user["selected_courses"] = []

    set_state(message.chat.id, "user_selecting_courses", {
        "selected_courses": [],
        "active_student_id": str(user["student_id"])
    })
    send_next_course_to_user(message.chat.id)


def send_next_course_to_user(chat_id):
    state_data = get_state_data(chat_id)
    selected = state_data.get("selected_courses", [])
    priority_num = len(selected) + 1

    courses = storage.get_courses()
    markup = types.InlineKeyboardMarkup()
    for c in courses:
        if c["id"] not in selected:
            markup.add(types.InlineKeyboardButton(
                c["name"], callback_data=f"userselect_{c['id']}"))

    markup.add(types.InlineKeyboardButton(
        "⬅️ انصراف", callback_data="userselect_cancel"))

    bot.send_message(
        chat_id,
        f"لطفاً درس اولویت **{priority_num}** خود را مشخص کنید:",
        reply_markup=markup,
        parse_mode="Markdown"
    )


@bot.callback_query_handler(func=lambda call: call.data == "userselect_cancel")
def user_course_selection_cancel(call):
    chat_id = call.message.chat.id
    state_data = get_state_data(chat_id)
    active_student_id = state_data.get("active_student_id")

    clear_state(chat_id)
    if active_student_id:
        set_state(chat_id, "user_panel", {
                  "active_student_id": str(active_student_id)})

    try:
        bot.delete_message(chat_id, call.message.message_id)
    except Exception:
        pass

    bot.answer_callback_query(call.id, "فرآیند انتخاب درس لغو شد.")
    bot.send_message(
        chat_id,
        "به پنل کاربری بازگشتید.",
        reply_markup=get_user_panel_keyboard()
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("userselect_"))
def user_course_selection_click(call):
    chat_id = call.message.chat.id
    if get_state(chat_id) != "user_selecting_courses":
        return

    if not storage.is_voting_open():
        clear_state(chat_id)
        bot.answer_callback_query(call.id, "رای‌گیری متوقف شده است.")
        try:
            bot.delete_message(chat_id, call.message.message_id)
        except Exception:
            pass

        bot.send_message(
            chat_id,
            "⛔ رای‌گیری توسط ادمین متوقف شده است. فرآیند انتخاب درس لغو شد.",
            reply_markup=get_user_panel_keyboard()
        )
        return

    course_id = int(call.data.split("_")[1])
    state_data = get_state_data(chat_id)
    selected = state_data.get("selected_courses", [])

    if course_id in selected:
        bot.answer_callback_query(call.id, "این درس قبلاً انتخاب شده است.")
        return

    selected.append(course_id)
    set_state(chat_id, "user_selecting_courses", {
        "selected_courses": selected,
        "active_student_id": state_data["active_student_id"]
    })

    try:
        bot.delete_message(chat_id, call.message.message_id)
    except Exception:
        pass

    if len(selected) < 5:
        send_next_course_to_user(chat_id)
        return

    user = storage.get_user_by_student_id(state_data["active_student_id"])
    if not user:
        clear_state(chat_id)
        bot.send_message(chat_id, "کاربر یافت نشد. دوباره /start را بزنید.")
        return

    user["selected_courses"] = selected
    user["telegram_id"] = chat_id
    storage.save_user(user)

    clear_state(chat_id)
    set_state(chat_id, "user_panel", {
              "active_student_id": str(user["student_id"])})

    courses_all = storage.get_courses()
    list_names = []
    for idx, cid in enumerate(selected, 1):
        c_name = next((c["name"]
                      for c in courses_all if c["id"] == cid), "نامشخص")
        list_names.append(f"{idx}. {c_name}")

    bot.send_message(
        chat_id,
        "✅ پنج اولویت شما با موفقیت ثبت شد:\n" + "\n".join(list_names),
        reply_markup=get_user_panel_keyboard()
    )

# درس‌های انتخابی من


@bot.message_handler(func=lambda msg: msg.text == "🔎 درس های انتخابی من")
def user_courses_selected_list(message):
    user = get_active_user_by_chat(message.chat.id)
    if not user:
        bot.send_message(
            message.chat.id,
            "شما از حساب خود خارج شده‌اید. لطفاً نحوه ورود خود را انتخاب کنید:",
            reply_markup=get_login_method_keyboard()
        )
        return

    selected = user.get("selected_courses", [])
    if not selected:
        bot.send_message(
            message.chat.id, "شما هنوز هیچ درسی را انتخاب نکرده‌اید.")
        return

    courses_all = storage.get_courses()
    list_names = []
    for idx, cid in enumerate(selected, 1):
        c_name = next((c["name"]
                      for c in courses_all if c["id"] == cid), "نامشخص")
        list_names.append(f"{idx}. {c_name}")

    bot.send_message(
        message.chat.id, "📋 لیست اولویت‌های انتخابی شما:\n" + "\n".join(list_names))

# حذف درس‌های انتخابی من


@bot.message_handler(func=lambda msg: msg.text == "🗑️ حذف درس های انتخابی من")
def user_courses_delete(message):
    user = get_active_user_by_chat(message.chat.id)
    if not user:
        bot.send_message(
            message.chat.id,
            "شما از حساب خود خارج شده‌اید. لطفاً نحوه ورود خود را انتخاب کنید:",
            reply_markup=get_login_method_keyboard()
        )
        return

    # چک اینکه اصلاً انتخاب درس انجام شده یا نه
    selected_courses = user.get("selected_courses", [])
    if not selected_courses:
        bot.send_message(
            message.chat.id,
            "📭 شما هنوز هیچ اولویتی برای انتخاب درس ثبت نکرده‌اید."
        )
        return

    if not storage.is_voting_open():
        bot.send_message(
            message.chat.id,
            "⛔️ در حال حاضر انتخاب و حذف درس، توسط ادمین متوقف شده است و امکان انتخاب یا حذف درس وجود ندارد."
        )
        return
    user["selected_courses"] = []
    user["telegram_id"] = message.chat.id
    storage.save_user(user)
    bot.send_message(
        message.chat.id, "🗑️ تمامی اولویت‌های انتخابی شما پاک شدند.")

# نتیجه قبولی کاربر


@bot.message_handler(func=lambda msg: msg.text == "🏆 نتیجه قبولی")
def user_result_status(message):
    user = get_active_user_by_chat(message.chat.id)
    if not user:
        bot.send_message(
            message.chat.id,
            "شما از حساب خود خارج شده‌اید. لطفاً نحوه ورود خود را انتخاب کنید:",
            reply_markup=get_login_method_keyboard()
        )
        return

    data = storage.load_data()
    effective_gpa = storage.get_user_effective_gpa(user, data)

    # چک معدل تاثیرگذار
    if effective_gpa is None:
        bot.send_message(
            message.chat.id,
            "⚠️ معدل شما در سیستم قابل محاسبه نیست (یا ثبت نشده) و فعلاً در رتبه‌بندی شرکت داده نمی‌شوید."
        )
        return

    selected_courses = user.get("selected_courses", [])
    if not selected_courses:
        bot.send_message(
            message.chat.id,
            "📭 شما هنوز هیچ اولویتی برای انتخاب درس ثبت نکرده‌اید."
        )
        return

    courses, user_results = storage.run_matching()
    cutoffs = storage.get_course_cutoffs()
    res = user_results.get(str(user["student_id"]))
    user_rank = user.get("rank")

    # پیدا کردن شناسه درس قبولی کاربر (اگر قبول شده باشد)
    accepted_course_id = res.get("course_id") if res else None

    priority_lines = []

    for idx, course_id in enumerate(selected_courses, start=1):
        cutoff = cutoffs.get(course_id)
        if not cutoff:
            priority_lines.append(
                f"🔴 اولویت {idx}: درس نامعتبر / حذف‌شده از سیستم"
            )
            continue

        course_name = cutoff["course_name"]
        last_rank = cutoff["last_rank"]
        last_gpa = cutoff["last_gpa"]
        has_capacity = cutoff["has_capacity"]
        accepted_count = cutoff["accepted_count"]
        capacity = cutoff["capacity"]

        # تعیین وضعیت رنگ و آیکون اولویت
        if accepted_course_id and course_id == accepted_course_id:
            status_icon = "✅ [محل قبولی شما]"
        elif accepted_count == 0 and capacity > 0:
            status_icon = "🟢"
        elif has_capacity:
            status_icon = "🟢"
        else:
            can_reach = (
                user_rank is not None and
                last_rank is not None and
                int(user_rank) <= int(last_rank)
            )
            status_icon = "🟢" if can_reach else "🔴"

        # متن کمکی کف قبولی
        if accepted_count == 0:
            cutoff_text = "هنوز کسی در این درس پذیرفته نشده (ظرفیت خالی است)"
        else:
            cutoff_text = (
                f"آخرین رتبه قبولی: {last_rank if last_rank is not None else '-'} | "
                f"آخرین معدل قبولی: {last_gpa if last_gpa is not None else '-'}"
            )

        priority_lines.append(
            f"{status_icon} اولویت {idx}: {course_name}\n"
            f"   ↳ {cutoff_text}"
        )

    # نمایش گزارش نهایی بر اساس قبولی یا عدم قبولی
    if not res or accepted_course_id is None:
        bot.send_message(
            message.chat.id,
            f"❌ شما انتخاب درس انجام داده‌اید، اما در هیچ‌کدام از اولویت‌های خود قبول نشده‌اید.\n\n"
            f"👤 نام: {user.get('full_name', '-')}\n"
            f"🏅 رتبه شما: {user_rank if user_rank is not None else 'بدون رتبه'}\n"
            f"📊 معدل موثر شما: {effective_gpa}\n\n"
            f"📋 وضعیت اولویت‌های شما:\n\n" + "\n\n".join(priority_lines)
        )
        return

    bot.send_message(
        message.chat.id,
        f"🎉 نتیجه قبولی موقت:\n\n"
        f"👤 نام: {user.get('full_name', '-')}\n"
        f"🏅 رتبه شما: {user_rank if user_rank is not None else 'بدون رتبه'}\n"
        f"📊 معدل موثر شما: {effective_gpa}\n"
        f"🏆 محل قبولی: اولویت {res['priority']} کلاس {res['course_name']}\n\n"
        f"📋 وضعیت اولویت‌های شما:\n\n" + "\n\n".join(priority_lines)
    )

# == == == == ویرایش اطلاعات == == == ==


@bot.message_handler(func=lambda msg: msg.text == "ℹ️ اطلاعات شخصی")
def user_personal_info(message):
    user = get_active_user_by_chat(message.chat.id)
    if not user:
        bot.send_message(
            message.chat.id,
            "شما از حساب خود خارج شده‌اید. لطفاً نحوه ورود خود را انتخاب کنید:",
            reply_markup=get_login_method_keyboard()
        )
        return

    set_state(message.chat.id, "user_personal_info", {
        "active_student_id": str(user["student_id"])
    })
    data = storage.load_data()
    bot.send_message(
        message.chat.id,
        get_personal_info_text(user, data),
        reply_markup=get_personal_info_keyboard(user)
    )


@bot.message_handler(func=lambda msg: msg.text == "🔙 بازگشت به پنل کاربری")
def user_back_to_panel(message):
    user = get_active_user_by_chat(message.chat.id)
    clear_state(message.chat.id)

    if not user:
        bot.send_message(
            message.chat.id,
            "شما از حساب خود خارج شده‌اید. لطفاً نحوه ورود خود را انتخاب کنید:",
            reply_markup=get_login_method_keyboard()
        )
        return

    if user:
        set_state(message.chat.id, "user_panel", {
            "active_student_id": str(user["student_id"])
        })

    bot.send_message(
        message.chat.id,
        "به پنل کاربری بازگشتید.",
        reply_markup=get_user_panel_keyboard()
    )


@bot.message_handler(func=lambda msg: get_state(msg.chat.id) == "user_editing_full_name")
def user_edit_name_save(message):
    chat_id = message.chat.id
    state_data = get_state_data(chat_id)
    active_student_id = state_data.get("active_student_id")

    if not active_student_id:
        clear_state(chat_id)
        bot.send_message(
            chat_id,
            "اطلاعات حساب فعال پیدا نشد. دوباره /start را بزنید."
        )
        return

    user = storage.get_user_by_student_id(active_student_id)

    if (
        not user
        or str(user.get("telegram_id")) != str(chat_id)
    ):
        clear_state(chat_id)
        bot.send_message(
            chat_id,
            "کاربر یافت نشد یا این حساب متعلق به شما نیست. دوباره /start را بزنید."
        )
        return

    if user.get("mode") != "no_gpa":
        clear_state(chat_id)
        set_state(
            chat_id,
            "user_panel",
            {"active_student_id": str(user["student_id"])}
        )
        bot.send_message(
            chat_id,
            "اطلاعات این حساب قابل ویرایش نیست.",
            reply_markup=get_user_panel_keyboard()
        )
        return

    if message.text == "⬅️ بازگشت":
        set_state(
            chat_id,
            "user_personal_info",
            {"active_student_id": str(user["student_id"])}
        )

        data = storage.load_data()
        bot.send_message(
            chat_id,
            get_personal_info_text(user, data),
            reply_markup=get_personal_info_keyboard(user)
        )
        return

    full_name = message.text.strip()

    if not full_name:
        bot.send_message(
            chat_id,
            "نام و نام خانوادگی نامعتبر است. دوباره وارد کنید:",
            reply_markup=back_markup()
        )
        return

    updated_user = storage.update_user_full_name(
        student_id=active_student_id,
        telegram_id=chat_id,
        full_name=full_name
    )

    if not updated_user:
        clear_state(chat_id)
        bot.send_message(
            chat_id,
            "ویرایش نام انجام نشد؛ اطلاعات حساب معتبر نیست. دوباره /start را بزنید."
        )
        return

    set_state(
        chat_id,
        "user_personal_info",
        {"active_student_id": str(updated_user["student_id"])}
    )

    data = storage.load_data()

    bot.send_message(
        chat_id,
        "✅ نام و نام خانوادگی با موفقیت ویرایش شد.\n\n"
        + get_personal_info_text(updated_user, data),
        reply_markup=get_personal_info_keyboard(updated_user)
    )


@bot.message_handler(func=lambda msg: msg.text == "✏️ ویرایش نام و نام خانوادگی")
def user_edit_name_start(message):
    chat_id = message.chat.id
    user = get_active_user_by_chat(chat_id)

    if not user:
        bot.send_message(
            message.chat.id,
            "شما از حساب خود خارج شده‌اید. لطفاً نحوه ورود خود را انتخاب کنید:",
            reply_markup=get_login_method_keyboard()
        )
        return

    if str(user.get("telegram_id")) != str(chat_id):
        clear_state(chat_id)
        bot.send_message(
            chat_id,
            "اطلاعات حساب فعال معتبر نیست. دوباره /start را بزنید."
        )
        return

    if user.get("mode") != "no_gpa":
        bot.send_message(
            chat_id,
            "اطلاعات شما قابل ویرایش نیست. "
            "ویرایش فقط برای کاربران واردشده بدون معدل فعال است.",
            reply_markup=get_personal_info_keyboard(user)
        )
        return

    set_state(
        chat_id,
        "user_editing_full_name",
        {"active_student_id": str(user["student_id"])}
    )

    bot.send_message(
        chat_id,
        "لطفاً نام و نام خانوادگی جدید را وارد کنید:",
        reply_markup=back_markup()
    )


@bot.message_handler(func=lambda msg: msg.text == "🆔 ویرایش کد دانشجویی")
def user_edit_student_id_start(message):
    chat_id = message.chat.id
    user = get_active_user_by_chat(chat_id)

    if not user:
        bot.send_message(
            message.chat.id,
            "شما از حساب خود خارج شده‌اید. لطفاً نحوه ورود خود را انتخاب کنید:",
            reply_markup=get_login_method_keyboard()
        )
        return

    if str(user.get("telegram_id")) != str(chat_id):
        clear_state(chat_id)
        bot.send_message(
            chat_id,
            "اطلاعات حساب فعال معتبر نیست. دوباره /start را بزنید."
        )
        return

    if user.get("mode") != "no_gpa":
        bot.send_message(
            chat_id,
            "اطلاعات شما قابل ویرایش نیست. ویرایش فقط برای کاربران واردشده بدون معدل فعال است.",
            reply_markup=get_personal_info_keyboard(user)
        )
        return

    set_state(chat_id, "user_editing_student_id", {
        "active_student_id": str(user["student_id"])
    })

    bot.send_message(
        chat_id,
        "لطفاً کد دانشجویی جدید را وارد کنید:",
        reply_markup=back_markup()
    )


@bot.message_handler(func=lambda msg: get_state(msg.chat.id) == "user_editing_student_id")
def user_edit_student_id_save(message):
    chat_id = message.chat.id
    state_data = get_state_data(chat_id)
    active_student_id = state_data.get("active_student_id")

    if not active_student_id:
        clear_state(chat_id)
        bot.send_message(
            chat_id, "اطلاعات حساب فعال پیدا نشد. دوباره /start را بزنید.")
        return

    user = storage.get_user_by_student_id(active_student_id)

    if not user or str(user.get("telegram_id")) != str(chat_id):
        clear_state(chat_id)
        bot.send_message(
            chat_id, "کاربر یافت نشد یا این حساب متعلق به شما نیست. دوباره /start را بزنید.")
        return

    if user.get("mode") != "no_gpa":
        clear_state(chat_id)
        bot.send_message(
            chat_id,
            "اطلاعات این حساب قابل ویرایش نیست.",
            reply_markup=get_personal_info_keyboard(user)
        )
        return

    if message.text == "⬅️ بازگشت":
        set_state(chat_id, "user_personal_info", {
            "active_student_id": str(user["student_id"])
        })
        data = storage.load_data()
        bot.send_message(
            chat_id,
            get_personal_info_text(user, data),
            reply_markup=get_personal_info_keyboard(user)
        )
        return

    new_student_id = message.text.strip()

    if not new_student_id:
        bot.send_message(
            chat_id,
            "کد دانشجویی نامعتبر است. دوباره وارد کنید:",
            reply_markup=back_markup()
        )
        return

    result = storage.update_user_student_id(
        old_student_id=active_student_id,
        telegram_id=chat_id,
        new_student_id=new_student_id
    )

    if not result["ok"]:
        if result["reason"] == "duplicate_student_id":
            bot.send_message(
                chat_id,
                "این کد دانشجویی قبلاً در سیستم ثبت شده است. یک کد دیگر وارد کنید:",
                reply_markup=back_markup()
            )
            return

        clear_state(chat_id)
        bot.send_message(
            chat_id,
            "ویرایش کد دانشجویی انجام نشد. دوباره /start را بزنید."
        )
        return

    updated_user = result["user"]

    set_state(chat_id, "user_personal_info", {
        "active_student_id": str(updated_user["student_id"])
    })

    data = storage.load_data()
    bot.send_message(
        chat_id,
        "✅ کد دانشجویی با موفقیت ویرایش شد.\n\n" +
        get_personal_info_text(updated_user, data),
        reply_markup=get_personal_info_keyboard(updated_user)
    )

# خروج


@bot.message_handler(func=lambda msg: msg.text == "🚪 خروج")
def user_exit(message):
    clear_state(message.chat.id)
    bot.send_message(
        message.chat.id,
        "خروج موفقیت‌آمیز بود. برای ورود مجدد از دستور /start استفاده کنید.",
        reply_markup=types.ReplyKeyboardRemove()
    )


# def storage_after_save_callback(file_path):
 #   send_backup_to_group("📦 فایل elective.json بروزرسانی شد.")

# storage.set_after_save_callback(storage_after_save_callback)

def handle_webhook_update(json_string):
    update = telebot.types.Update.de_json(json_string)
    bot.process_new_updates([update])


if __name__ == "__main__":
    storage.load_data()
    if os.getenv("LIARA") != "true":
        bot.remove_webhook()
        print("Running in local polling mode...")
        bot.infinity_polling()
