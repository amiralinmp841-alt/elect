# storage.py
import json
import os
import shutil
import tempfile
import threading
from config import DATA_FILE, ADMIN_IDS

_after_save_callback = None

# قفل سراسری برای جلوگیری از تداخل همزمانی در همین پردازش
_DATA_LOCK = threading.RLock()


def set_after_save_callback(callback):
    global _after_save_callback
    _after_save_callback = callback


def _default_data():
    return {
        "admins": list(ADMIN_IDS),
        "users": [],
        "courses": [],
        "effective_gpa_mode": "total",
        "voting_open": True
    }


def _normalize_data(data):
    """اطمینان از وجود فیلدهای پایه بدون حذف اطلاعات اضافی."""
    if not isinstance(data, dict):
        raise ValueError("database root must be a JSON object")

    if "admins" not in data or not isinstance(data.get("admins"), list):
        data["admins"] = list(ADMIN_IDS)
    if "users" not in data or not isinstance(data.get("users"), list):
        data["users"] = []
    if "courses" not in data or not isinstance(data.get("courses"), list):
        data["courses"] = []

    for course in data.get("courses", []):
        if isinstance(course, dict) and "description" not in course:
            course["description"] = ""

    if "effective_gpa_mode" not in data:
        data["effective_gpa_mode"] = "total"
    if "voting_open" not in data:
        data["voting_open"] = True

    return data


def _read_json_file(path):
    with open(path, "r", encoding="utf-8") as f:
        return _normalize_data(json.load(f))


def load_data():
    """
    خواندن دیتابیس به شکل امن.

    نکته مهم: اگر JSON خراب/ناقص باشد، دیگر دیتابیس را با داده خالی
    overwrite نمی‌کنیم؛ ابتدا از فایل پشتیبان .bak استفاده می‌شود و اگر
    آن هم قابل بازیابی نباشد، خطا بالا می‌رود تا اطلاعات قبلی نابود نشود.
    """
    with _DATA_LOCK:
        if not os.path.exists(DATA_FILE):
            data = _default_data()
            save_data(data)
            return data

        try:
            return _read_json_file(DATA_FILE)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError, OSError) as primary_error:
            backup_file = DATA_FILE + ".bak"

            if os.path.exists(backup_file):
                try:
                    data = _read_json_file(backup_file)
                    print(
                        f"WARNING: {DATA_FILE} was unreadable; restored data from {backup_file}. "
                        f"Original error: {primary_error}"
                    )
                    return data
                except Exception as backup_error:
                    raise RuntimeError(
                        f"Database file is corrupted and backup is also unusable: {backup_error}"
                    ) from primary_error

            raise RuntimeError(
                f"Database file is corrupted/unreadable and no backup exists: {primary_error}"
            ) from primary_error


def ensure_settings():
    data = load_data()

    if "effective_gpa_mode" not in data:
        data["effective_gpa_mode"] = "total"

    if "voting_open" not in data:
        data["voting_open"] = True

    save_data(data)
    return data


def get_effective_gpa_mode():
    data = ensure_settings()
    return str(data.get("effective_gpa_mode", "total"))


def set_effective_gpa_mode(mode):
    data = ensure_settings()
    data["effective_gpa_mode"] = str(mode)
    data = recalculate_ranks(data)
    save_data(data)


def is_voting_open():
    data = ensure_settings()
    return bool(data.get("voting_open", True))


def set_voting_open(status):
    data = ensure_settings()
    data["voting_open"] = bool(status)
    save_data(data)


def get_user_effective_gpa(user, data=None):
    if data is None:
        data = load_data()

    def normalize_gpa(value):
        if value in (None, "", "null"):
            return None
        try:
            return float(str(value).replace("/", "."))
        except Exception:
            return None

    manual_gpa = normalize_gpa(user.get("manual_gpa"))
    if manual_gpa is not None:
        return manual_gpa

    mode = str(data.get("effective_gpa_mode", "total"))

    if mode == "total":
        return normalize_gpa(user.get("gpa"))

    terms_gpa = user.get("terms_gpa", {}) or {}
    if mode.isdigit() and isinstance(terms_gpa, dict):
        term_key_int = int(mode)

        value = None
        if term_key_int in terms_gpa:
            value = terms_gpa.get(term_key_int)
        elif str(term_key_int) in terms_gpa:
            value = terms_gpa.get(str(term_key_int))

        return normalize_gpa(value)

    return None


def get_users_without_gpa():
    return get_users_without_effective_gpa()


def get_users_without_effective_gpa():
    data = load_data()
    return [u for u in data["users"] if get_user_effective_gpa(u, data) is None]


def save_data(data):
    """
    ذخیره اتمیک و thread-safe دیتابیس.

    مستقیماً روی DATA_FILE نمی‌نویسیم؛ ابتدا در فایل موقت می‌نویسیم،
    fsync می‌کنیم و سپس با os.replace جایگزین می‌کنیم. بنابراین حتی اگر
    چند درخواست همزمان باشند، فایل اصلی وسط نوشتن نیمه‌کاره نمی‌شود.
    """
    global _after_save_callback

    with _DATA_LOCK:
        if not isinstance(data, dict):
            raise ValueError("data must be a dictionary")

        # اطمینان از حضور ادمین‌ها در فایل
        admins = data.setdefault("admins", [])
        if not isinstance(admins, list):
            admins = []
            data["admins"] = admins

        for admin in ADMIN_IDS:
            if admin not in admins:
                admins.append(admin)

        data_dir = os.path.dirname(os.path.abspath(DATA_FILE)) or "."
        os.makedirs(data_dir, exist_ok=True)

        temp_path = None
        backup_path = DATA_FILE + ".bak"

        try:
            fd, temp_path = tempfile.mkstemp(
                prefix=os.path.basename(DATA_FILE) + ".",
                suffix=".tmp",
                dir=data_dir
            )

            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
                f.flush()
                os.fsync(f.fileno())

            # پشتیبان از آخرین نسخه سالم
            if os.path.exists(DATA_FILE):
                try:
                    shutil.copy2(DATA_FILE, backup_path)
                except Exception as backup_error:
                    print(
                        f"WARNING: Could not update database backup: {backup_error}")

            # جایگزینی اتمیک فایل اصلی
            os.replace(temp_path, DATA_FILE)
            temp_path = None

        finally:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

        if _after_save_callback:
            try:
                _after_save_callback(DATA_FILE)
            except Exception as e:
                print(f"after_save_callback error: {e}")


# --- مدیریت درس‌ها ---
def add_course(name, capacity):
    data = load_data()
    course_id = 1 if not data["courses"] else max(
        c["id"] for c in data["courses"]) + 1
    new_course = {
        "id": course_id,
        "name": name,
        "capacity": int(capacity),
        "description": "",
        "selected_by": []
    }
    data["courses"].append(new_course)
    save_data(data)
    return new_course


def get_courses():
    data = load_data()
    return data["courses"]


def get_course_by_id(course_id):
    data = load_data()
    try:
        course_id = int(course_id)
    except Exception:
        return None

    for course in data.get("courses", []):
        if int(course.get("id")) == course_id:
            return course
    return None


def update_course_description(course_id, description):
    data = load_data()
    try:
        course_id = int(course_id)
    except Exception:
        return False

    for course in data.get("courses", []):
        if int(course.get("id")) == course_id:
            course["description"] = str(description).strip()
            save_data(data)
            return True

    return False

# --- مدیریت کاربران ---


def get_user_by_student_id(student_id):
    data = load_data()
    for user in data["users"]:
        if str(user.get("student_id")) == str(student_id):
            return user
    return None


def get_users_by_telegram_id(telegram_id):
    data = load_data()
    return [u for u in data["users"] if u.get("telegram_id") == telegram_id]


def get_user(telegram_id):
    """
    برای سازگاری با کدهای قبلی.
    اگر چند کاربر با یک telegram_id وجود داشته باشند، فقط اولین مورد برگردانده می‌شود.
    بهتر است در کدهای جدید از get_users_by_telegram_id یا get_user_by_student_id استفاده شود.
    """
    data = load_data()
    for user in data["users"]:
        if user.get("telegram_id") == telegram_id:
            return user
    return None


def save_user(user_obj):
    """
    ذخیره/به‌روزرسانی کاربر بر اساس student_id
    نه telegram_id
    """
    data = load_data()
    users = data.get("users", [])

    updated = False
    for i, u in enumerate(users):
        if str(u.get("student_id")) == str(user_obj.get("student_id")):
            users[i] = {**u, **user_obj}
            updated = True
            break

    if not updated:
        users.append(user_obj)

    data["users"] = users

    # به‌روزرسانی رتبه‌بندی پس از تغییر اطلاعات کاربر
    data = recalculate_ranks(data)
    save_data(data)


def add_or_update_user(user_obj):
    """
    معادل save_user ولی با اسم واضح‌تر
    """
    save_user(user_obj)


def delete_user_by_student_id(student_id):
    data = load_data()
    initial_len = len(data["users"])
    data["users"] = [u for u in data["users"]
                     if str(u["student_id"]) != str(student_id)]

    if len(data["users"]) < initial_len:
        # حذف شناسه دانشجو از لیست انتخاب‌های دروس
        for c in data["courses"]:
            c["selected_by"] = [uid for uid in c["selected_by"]
                                if str(uid) != str(student_id)]

        data = recalculate_ranks(data)
        save_data(data)
        return True
    return False


def find_user_by_chat_id(chat_id, data=None):
    if data is None:
        data = load_data()

    for user in data.get("users", []):
        if str(user.get("telegram_id")) == str(chat_id):
            return user
    return None


def update_user_gpa(student_id, gpa):
    data = load_data()

    try:
        gpa = float(str(gpa).replace("/", "."))
    except Exception:
        return False

    for user in data.get("users", []):
        if str(user.get("student_id")) == str(student_id):
            user["manual_gpa"] = gpa
            user["gpa"] = gpa
            user["terms_gpa"] = {str(term): gpa for term in range(1, 11)}
            save_data(data)
            return True

    return False


def get_all_users():
    data = load_data()
    return data["users"]

# --- سیستم رتبه‌بندی ---


def recalculate_ranks(data):
    with_gpa = []
    without_gpa = []

    for user in data["users"]:
        effective_gpa = get_user_effective_gpa(user, data)
        if effective_gpa is None:
            user["rank"] = None
            without_gpa.append(user)
        else:
            user["_effective_gpa"] = effective_gpa
            with_gpa.append(user)

    with_gpa.sort(key=lambda x: x["_effective_gpa"], reverse=True)

    for index, user in enumerate(with_gpa, start=1):
        user["rank"] = index
        user.pop("_effective_gpa", None)

    for index, user in enumerate(without_gpa, start=len(with_gpa) + 1):
        user["rank"] = index

    data["users"] = with_gpa + without_gpa
    return data

# --- الگوریتم تخصیص اولویت‌ها (نتایج انتخابات) ---


def run_matching():
    data = load_data()
    courses = {
        c["id"]: {
            "name": c["name"],
            "capacity": c["capacity"],
            "accepted": []
        }
        for c in data["courses"]
    }

    users_to_process = []
    for user in data["users"]:
        effective_gpa = get_user_effective_gpa(user, data)
        if effective_gpa is not None:
            user_copy = dict(user)
            user_copy["_effective_gpa"] = effective_gpa
            users_to_process.append(user_copy)

    users_to_process.sort(key=lambda x: x["_effective_gpa"], reverse=True)

    user_results = {}

    for user in users_to_process:
        student_id = user["student_id"]
        selected = user.get("selected_courses", [])
        accepted_course_id = None
        accepted_priority = None

        for idx, cid in enumerate(selected):
            if cid in courses and len(courses[cid]["accepted"]) < courses[cid]["capacity"]:
                courses[cid]["accepted"].append({
                    "telegram_id": user.get("telegram_id"),
                    "student_id": student_id,
                    "name": user["full_name"],
                    "gpa": user["_effective_gpa"],
                    "rank": user.get("rank")
                })
                accepted_course_id = cid
                accepted_priority = idx + 1
                break

        if accepted_course_id:
            user_results[str(student_id)] = {
                "course_id": accepted_course_id,
                "course_name": courses[accepted_course_id]["name"],
                "priority": accepted_priority
            }
        else:
            user_results[str(student_id)] = {
                "course_id": None,
                "course_name": "مردود (عدم وجود ظرفیت خالی در اولویت‌های انتخابی)",
                "priority": None
            }

    return courses, user_results


def update_user_full_name(student_id, telegram_id, full_name):
    data = load_data()

    target_user = None

    for user in data.get("users", []):
        if str(user.get("student_id")) != str(student_id):
            continue

        if str(user.get("telegram_id")) != str(telegram_id):
            return None

        user["full_name"] = full_name
        user["telegram_id"] = telegram_id
        target_user = dict(user)
        break

    if target_user is None:
        return None

    # تغییر نام روی رتبه‌بندی اثر ندارد؛ نیازی به recalculate_ranks نیست.
    save_data(data)
    return target_user


def update_user_student_id(old_student_id, telegram_id, new_student_id):
    data = load_data()
    users = data.get("users", [])

    old_student_id = str(old_student_id).strip()
    new_student_id = str(new_student_id).strip()
    telegram_id = str(telegram_id)

    if not new_student_id:
        return {"ok": False, "reason": "invalid_student_id"}

    # اگر کد جدید متعلق به کاربر دیگری باشد، نباید تغییر انجام شود
    for user in users:
        if str(user.get("student_id")) == new_student_id and str(user.get("student_id")) != old_student_id:
            return {"ok": False, "reason": "duplicate_student_id"}

    target_user = None

    for user in users:
        if str(user.get("student_id")) != old_student_id:
            continue

        if str(user.get("telegram_id")) != telegram_id:
            return {"ok": False, "reason": "forbidden"}

        user["student_id"] = new_student_id
        user["telegram_id"] = int(
            telegram_id) if telegram_id.isdigit() else telegram_id

        # اگر username قبلاً همان کد دانشجویی قدیمی بوده، دیگر معتبر نیست
        if user.get("username") == old_student_id:
            user["username"] = None

        target_user = dict(user)
        break

    if target_user is None:
        return {"ok": False, "reason": "not_found"}

    save_data(data)
    return {"ok": True, "user": target_user}
 # ===== تغییر کپسیتی ===== ===== ===== ===== ===== =====


def update_course_capacity(course_id, new_capacity):
    data = load_data()

    try:
        course_id = int(course_id)
        new_capacity = int(new_capacity)
        if new_capacity <= 0:
            return False
    except Exception:
        return False

    for course in data.get("courses", []):
        if int(course.get("id")) == course_id:
            course["capacity"] = new_capacity
            save_data(data)
            return True

    return False

# ===== کارنامه سبز ==== ==== ====


def get_course_cutoffs():
    courses, _ = run_matching()

    cutoffs = {}
    for course_id, course_data in courses.items():
        accepted = course_data.get("accepted", [])

        if not accepted:
            cutoffs[course_id] = {
                "course_name": course_data["name"],
                "accepted_count": 0,
                "capacity": course_data["capacity"],
                "last_rank": None,
                "last_gpa": None,
                "has_capacity": course_data["capacity"] > 0
            }
            continue

        # accepted ها به ترتیب پردازش کاربران هستند؛ آخرین نفر، کف قبولی آن درس است
        last_accepted = accepted[-1]

        cutoffs[course_id] = {
            "course_name": course_data["name"],
            "accepted_count": len(accepted),
            "capacity": course_data["capacity"],
            "last_rank": last_accepted.get("rank"),
            "last_gpa": last_accepted.get("gpa"),
            "has_capacity": len(accepted) < course_data["capacity"]
        }

    return cutoffs
