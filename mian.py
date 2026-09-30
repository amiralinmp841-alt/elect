from flask import Flask, render_template, request, redirect, session, url_for, flash
import os
import time
import storage
import scraper
# ایمپورت کردن ربات و bot برای ارسال نوتیفیکیشن ادمین
from bot import handle_webhook_update, bot, is_student_id_allowed

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "bale_elective_secret_key_12345")

# بارگذاری اولیه داده‌ها
storage.load_data()

# شناسه ادمین یا ادمین‌ها برای دریافت هشدارهای امنیتی ربات
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

ADMIN_IDS = [
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
]

# ================== توابع کمکی امنیت و مسدودسازی ========================================================


def get_client_ip():
    """به دست آوردن IP واقعی کاربر با بررسی هدرهای پروکسی"""
    x_real_ip = request.headers.get("X-Real-IP")
    if x_real_ip:
        return x_real_ip.strip()

    x_forwarded_for = request.headers.get("X-Forwarded-For")
    if x_forwarded_for:
        return x_forwarded_for.split(",")[0].strip()

    return request.remote_addr


def send_admin_notification(message):
    """ارسال اعلان به تمام ادمین‌های بله"""
    for admin_id in ADMIN_IDS:
        try:
            bot.send_message(admin_id, message)
        except Exception as e:
            print(f"Failed to send admin notification to {admin_id}: {e}")


def notify_site_visit():
    """اعلان باز شدن صفحه اصلی سایت توسط یک IP"""
    ip = get_client_ip()
    now = time.strftime("%Y-%m-%d %H:%M:%S")

    message = (
        "🌐 بازدید جدید از سایت\n\n"
        f"🌐 IP: `{ip}`\n"
        "📄 صفحه: `/`\n"
        f"🕐 زمان: {now}"
    )

    send_admin_notification(message)


def notify_successful_login(student_id, full_name, login_type):
    """اعلان ورود موفق کاربر"""
    ip = get_client_ip()
    now = time.strftime("%Y-%m-%d %H:%M:%S")

    login_type_text = {
        "gpa": "با هم‌آوا",
        "no_gpa": "بدون معدل"
    }.get(login_type, login_type)

    message = (
        "🔐 ورود موفق به سایت\n\n"
        f"👤 نام: {full_name or '—'}\n"
        f"🆔 کد دانشجویی: `{student_id}`\n"
        f"🌐 IP: `{ip}`\n"
        f"🔑 نوع ورود: {login_type_text}\n"
        f"🕐 زمان: {now}"
    )

    send_admin_notification(message)


def is_blocked():
    """بررسی اینکه آیا IP فعلی یا کاربر فعلی لاگین شده مسدود است یا خیر"""
    data = storage.load_data()
    blocked_ips = data.setdefault("blocked_ips", [])
    blocked_users = data.setdefault("blocked_users", [])

    # ۱. بررسی IP
    ip = get_client_ip()
    if ip in blocked_ips:
        return True

    # ۲. بررسی شماره دانشجویی (در صورت لاگین بودن)
    if "student_id" in session:
        if str(session["student_id"]) in blocked_users:
            return True

    return False


def track_web_attempt(student_id, source):
    """ثبت تلاش‌های ورود از سایت و تشخیص رفتارهای مشکوک"""

    ip = get_client_ip()
    data = storage.load_data()

    security = data.setdefault("security_web", {})
    attempts = security.setdefault("attempts", [])
    alerts = security.setdefault("alerts", {})

    now = int(time.time())

    attempts.append({
        "ip": ip,
        "student_id": str(student_id),
        "source": source,
        "ts": now
    })

    # فقط تلاش‌های ۱ ساعت اخیر نگه داشته شوند
    security["attempts"] = [
        x for x in attempts
        if x["ts"] >= now - 3600 * 1
    ]

    # تلاش‌های همین IP
    recent_attempts = [
        x for x in security["attempts"]
        if x["ip"] == ip
    ]

    unique_student_ids = sorted({
        x["student_id"] for x in recent_attempts
    })

    current_count = len(unique_student_ids)

    # آخرین تعدادی که بابتش هشدار ارسال شده
    last_alert_count = int(alerts.get(ip, 0))

    should_alert = (
        current_count >= 5 and
        current_count >= last_alert_count + 5
    )

    if should_alert:

        source_text = {
            "gpa": "ورود با هم‌آوا",
            "no_gpa": "ورود بدون معدل"
        }.get(source, source)

        alert_text = (
            "🚨 **هشدار فعالیت مشکوک در سایت**\n\n"
            f"🌐 IP: `{ip}`\n"
            f"📍 بخش: {source_text}\n"
            f"📊 تعداد شماره دانشجویی تست شده: {current_count}\n"
            f"🆔 شماره‌ها: {', '.join(unique_student_ids)}"
        )

        for admin_id in ADMIN_IDS:
            try:
                bot.send_message(admin_id, alert_text)
            except Exception as e:
                print(
                    f"Failed to send admin security alert to {admin_id}: {e}")

        # ثبت آخرین تعداد هشدار داده شده
        alerts[ip] = current_count

    storage.save_data(data)

# هک/میان‌افزار برای بررسی سراسری مسدود بودن قبل از هر ریکوئست وب


@app.before_request
def check_block_status():
    if request.path.startswith(f"/webhook/{BOT_TOKEN}") or request.path.startswith("/static"):
        return

    if is_blocked():
        return "Access Denied: Your IP or Account has been blocked by administrators.", 403

# ================== مسیرهای وب‌سایت ==================


BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")


@app.route(f"/webhook/{BOT_TOKEN}", methods=["POST"])
def webhook():
    try:
        json_string = request.get_data(as_text=True)

        print("========== BALE WEBHOOK ==========")
        print(json_string)

        handle_webhook_update(json_string)

        return "OK", 200

    except Exception as e:
        print("Webhook Error:", e)
        return "ERROR", 500

# ================== صفحات وب‌سایت کاربران عادی ==================


@app.route("/")
def home():
    # هر بار که صفحه اصلی سایت باز شود، IP برای ادمین ارسال می‌شود.
    notify_site_visit()

    if "student_id" in session:
        return redirect(url_for("panel"))
    return render_template("login.html")


@app.route("/login", methods=["POST"])
def login():
    login_type = request.form.get("login_type")

    if login_type == "gpa":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()

        if not username or not password:
            flash("وارد کردن نام کاربری و کلمه عبور الزامی است.")
            return redirect(url_for("home"))
        if not username.isdigit():
            flash("نام کاربری (شماره دانشجویی) باید فقط شامل اعداد باشد.")
            return redirect(url_for("home"))

        if not is_student_id_allowed(username):
            flash("در حال حاضر ورود با این شماره دانشجویی مجاز نیست.")
            return redirect(url_for("home"))

        # ثبت تلاش برای ورود با هم‌آوا و بررسی رفتار مشکوک
        track_web_attempt(username, "gpa")

        res = scraper.get_student_info(username, password)

        if res["status"] in ["success", "gpa_not_found"]:
            existing_user = storage.get_user_by_student_id(username)
            gpa_val = res.get("gpa") if res["status"] == "success" else None

            if existing_user:
                existing_user["mode"] = "gpa"
                existing_user["username"] = username
                existing_user["full_name"] = res["name"]
                existing_user["gpa"] = gpa_val
                existing_user["terms_gpa"] = res.get("terms_gpa", {})
                existing_user["gpa_login_required"] = True
                user_obj = existing_user
            else:
                user_obj = {
                    "telegram_id": None,
                    "mode": "gpa",
                    "username": username,
                    "student_id": username,
                    "full_name": res["name"],
                    "gpa": gpa_val,
                    "terms_gpa": res.get("terms_gpa", {}),
                    "rank": None,
                    "selected_courses": [],
                    "gpa_login_required": True
                }
            storage.save_user(user_obj)
            session["student_id"] = username

            # اعلان ورود موفق برای ادمین
            notify_successful_login(
                student_id=username,
                full_name=res.get("name"),
                login_type="gpa"
            )

            return redirect(url_for("panel"))
        else:
            flash("ورود ناموفق! اطلاعات هم‌آوا اشتباه است یا دانشگاه در دسترس نیست.")
            return redirect(url_for("home"))

    elif login_type == "no_gpa":
        student_id = request.form.get("student_id", "").strip()
        full_name = request.form.get("full_name", "").strip()

        if not student_id:
            flash("وارد کردن شماره دانشجویی الزامی است.")
            return redirect(url_for("home"))

        if not student_id.isdigit():
            flash("شماره دانشجویی باید فقط شامل اعداد باشد.")
            return redirect(url_for("home"))

        if not is_student_id_allowed(student_id):
            flash("در حال حاضر ورود با این شماره دانشجویی مجاز نیست.")
            return redirect(url_for("home"))

        # ردیابی و تحلیل رفتار برای ورودهای بدون معدل
        track_web_attempt(student_id, "no_gpa")

        existing_user = storage.get_user_by_student_id(student_id)
        if existing_user:
            if existing_user.get("gpa_login_required") is True:
                flash("برای این شماره دانشجویی فقط ورود با هم‌آوا مجاز است.")
                return redirect(url_for("home"))

            session["student_id"] = student_id

            # اعلان ورود موفق برای ادمین
            notify_successful_login(
                student_id=student_id,
                full_name=existing_user.get("full_name"),
                login_type="no_gpa"
            )

            return redirect(url_for("panel"))

        if not full_name:
            flash(
                "کاربر یافت نشد. برای ثبت‌نام اولیه، وارد کردن نام و نام خانوادگی الزامی است.")
            return redirect(url_for("home"))

        user_obj = {
            "telegram_id": None,
            "mode": "no_gpa",
            "username": None,
            "student_id": student_id,
            "full_name": full_name,
            "gpa": None,
            "terms_gpa": {},
            "selected_courses": [],
            "gpa_login_required": False
        }
        storage.save_user(user_obj)
        session["student_id"] = student_id

        # اعلان ورود موفق برای ادمین
        notify_successful_login(
            student_id=student_id,
            full_name=full_name,
            login_type="no_gpa"
        )

        return redirect(url_for("panel"))

    return redirect(url_for("home"))


@app.route("/panel")
def panel():
    if "student_id" not in session:
        return redirect(url_for("home"))

    user = storage.get_user_by_student_id(session["student_id"])
    if not user:
        session.pop("student_id", None)
        return redirect(url_for("home"))

    data = storage.load_data()
    courses_map = {c["id"]: c["name"] for c in data.get("courses", [])}

    # اجرای تطبیق و دریافت نتایج
    courses_results, user_results = storage.run_matching()
    res = user_results.get(str(user["student_id"]))
    accepted_course_id = res.get("course_id") if res else None

    # محاسبه معدل تاثیرگذار و رتبه کاربر
    effective_gpa = storage.get_user_effective_gpa(user, data)
    user_rank = user.get("rank")

    # استخراج دروس انتخابی و بررسی وضعیت هر اولویت
    selected_list = []
    for idx, cid in enumerate(user.get("selected_courses", []), start=1):
        if cid not in courses_map:
            continue

        course_data = courses_results.get(str(cid)) or courses_results.get(cid)
        if not course_data:
            continue

        accepted = course_data.get("accepted", [])
        capacity = course_data.get("capacity", 0)
        accepted_count = len(accepted)

        # استخراج آخرین رتبه و معدل قبولی در این کلاس
        last_accepted = accepted[-1] if accepted else None
        last_rank = last_accepted.get("rank") if last_accepted else None
        last_gpa = last_accepted.get("gpa") if last_accepted else None

        # اگر معدل ثبت نشده باشد، وضعیت‌ها بنفش رنگ و بدون شانس‌سنجی رندر می‌شوند
        if effective_gpa is None:
            if accepted_count < capacity:
                status_icon = "🟣"
                status_text = f"کلاس دارای ظرفیت خالی است ({capacity - accepted_count} نفر باقی‌مانده)"
                status_class = "priority-no-gpa"
            else:
                status_icon = "🟣"
                status_text = f"کلاس پر شده است | آخرین رتبه قبولی: {last_rank or '—'} | آخرین معدل قبولی: {last_gpa or '—'}"
                status_class = "priority-no-gpa"
        else:
            # تعیین وضعیت عادی بر اساس قوانین جدید (معدل وجود دارد)
            if accepted_course_id and cid == accepted_course_id:
                status_icon = "✅"
                status_text = "قبولی فعلی شما در این کلاس"
                status_class = "priority-accepted"

            elif accepted_count < capacity:
                # آبی: ظرفیت خالی
                free_count = capacity - accepted_count
                status_icon = "🔵"
                status_text = f"ظرفیت خالی ({free_count} نفر)"
                status_class = "priority-empty"

            else:
                # کلاس پر شده؛ بررسی شانس قبولی کاربر
                can_reach = (
                    user_rank is not None and
                    last_rank is not None and
                    int(user_rank) <= int(last_rank)
                )

                if can_reach:
                    # سبز: پر شده ولی کاربر می‌توانست قبول شود
                    status_icon = "🟢"
                    status_text = f"کلاس پر است، ولی شانس قبولی دارید | آخرین رتبه: {last_rank} | آخرین معدل: {last_gpa}"
                    status_class = "priority-reachable"
                else:
                    # قرمز: پر شده و کاربر نمی‌توانست قبول شود
                    status_icon = "🔴"
                    status_text = f"کلاس پر شده و با شرایط فعلی، شانس قبولی ندارید | آخرین رتبه: {last_rank} | آخرین معدل: {last_gpa}"
                    status_class = "priority-unreachable"

        selected_list.append({
            "index": idx,
            "name": courses_map[cid],
            "status_icon": status_icon,
            "status_text": status_text,
            "status_class": status_class
        })

    # نتیجه قبولی موقت
    result_text = None
    if effective_gpa is not None:
        if res and res.get("course_id") is not None:
            result_text = f"پذیرفته شده در اولویت {res['priority']}، کلاس {res['course_name']}"
        else:
            result_text = "در هیچ‌کدام از اولویت‌ها قبول نشده‌اید.(و یا هنوز، اولویت‌هاتونو انتخاب نکرده‌اید.)"

    return render_template(
        "panel.html",
        user=user,
        selected_list=selected_list,
        result_text=result_text,
        effective_gpa=effective_gpa
    )


@app.route('/course-descriptions')
def course_descriptions():
    # اگر کاربر لاگین نیست، هدایت شود به صفحه لاگین (اختیاری)
    if 'student_id' not in session:
        return redirect(url_for('login'))

    # دریافت لیست تمام درس‌ها از دیتابیس (storage)
    import storage
    data = storage.load_data()
    courses = data.get("courses", [])

    return render_template('course_descriptions.html', courses=courses)


@app.route("/select-courses", methods=["GET", "POST"])
def select_courses():
    if "student_id" not in session:
        return redirect(url_for("home"))

    user = storage.get_user_by_student_id(session["student_id"])
    if not user:
        session.pop("student_id", None)
        return redirect(url_for("home"))

    # بستن کامل مسیر در حالت توقف انتخاب واحد
    if not storage.is_voting_open():
        flash("⛔️ در حال حاضر انتخاب درس توسط مدیریت بسته شده است.")
        return redirect(url_for("panel"))

    data = storage.load_data()
    all_courses = data.get("courses", [])

    courses_results, user_results = storage.run_matching()
    course_statuses = {}

    effective_gpa = storage.get_user_effective_gpa(user, data)
    user_rank = user.get("rank")

    for course in all_courses:
        cid = int(course["id"])
        c_res = courses_results.get(str(cid)) or courses_results.get(cid) or {}

        accepted = c_res.get("accepted", [])
        capacity = c_res.get("capacity", course.get("capacity", 0))
        accepted_count = len(accepted)

        last_accepted = accepted[-1] if accepted else None
        last_rank = last_accepted.get("rank") if last_accepted else None
        last_gpa = last_accepted.get("gpa") if last_accepted else None

        if effective_gpa is None:
            status = "no-gpa"
            if accepted_count < capacity:
                info = f"دارای ظرفیت خالی ({capacity - accepted_count} نفر)"
            else:
                info = f"کلاس پر شده است | آخرین رتبه: {last_rank or '—'} | آخرین معدل: {last_gpa or '—'}"
        else:
            if accepted_count < capacity:
                status = "empty"
                info = f"دارای ظرفیت خالی ({capacity - accepted_count} نفر)"
            else:
                can_reach = (
                    user_rank is not None and
                    last_rank is not None and
                    int(user_rank) <= int(last_rank)
                )

                if can_reach:
                    status = "reachable"
                    info = f"کلاس پر است، ولی شانس قبولی دارید | آخرین رتبه: {last_rank} | آخرین معدل: {last_gpa}"
                else:
                    status = "unreachable"
                    info = f"کلاس پر شده و با شرایط فعلی، شانس قبولی ندارید | آخرین رتبه: {last_rank} | آخرین معدل: {last_gpa}"

        course_statuses[cid] = {
            "status": status,
            "info": info,
            "capacity": capacity,
            "accepted_count": accepted_count,
            "remaining": max(0, capacity - accepted_count),
            "last_rank": last_rank,
            "last_gpa": last_gpa,
        }

    if request.method == "POST":
        if "cancel" in request.form:
            return redirect(url_for("panel"))

        # خیلی مهم: دوباره قبل از ذخیره چک کن
        if not storage.is_voting_open():
            flash("⛔️ انتخاب درس توسط مدیریت متوقف شده است.")
            return redirect(url_for("panel"))
        if user.get("selected_courses"):
            flash("⚠️ شما قبلاً انتخاب‌های خود را ثبت کرده‌اید. برای ثبت مجدد ابتدا باید اولویت‌های قبلی را حذف کنید.")
            return redirect(url_for("panel"))

        selected_ids = request.form.getlist("course_priority")
        selected_ids = [int(cid) for cid in selected_ids if cid.isdigit()]

        if len(set(selected_ids)) < 5 or len(selected_ids) != 5:
            flash("باید ۵ درس متفاوت انتخاب کنید.")
            return redirect(url_for("select_courses"))

        user["selected_courses"] = selected_ids
        storage.save_user(user)
        flash("انتخاب‌ها با موفقیت ثبت شد.")
        return redirect(url_for("panel"))

    return render_template(
        "select.html",
        courses=all_courses,
        statuses=course_statuses,
        user_selected=user.get("selected_courses", []),
        effective_gpa=effective_gpa
    )


@app.route("/clear-courses")
def clear_courses():
    if "student_id" not in session:
        return redirect(url_for("home"))

    user = storage.get_user_by_student_id(session["student_id"])
    if not storage.is_voting_open():
        flash("⛔️ در حال حاضر انتخاب و حذف درس، توسط ادمین متوقف شده است و امکان انتخاب یا حذف درس وجود ندارد.")
        return redirect(url_for("panel"))

    if user:
        user["selected_courses"] = []
        storage.save_user(user)
        flash("تمامی اولویت‌های انتخابی شما با موفقیت حذف شدند.")
    return redirect(url_for("panel"))


@app.route("/edit-info")
def edit_info():
    if "student_id" not in session:
        return redirect(url_for("home"))

    user = storage.get_user_by_student_id(session["student_id"])
    if not user:
        session.pop("student_id", None)
        return redirect(url_for("home"))

    data = storage.load_data()
    effective_gpa = storage.get_user_effective_gpa(user, data)
    rank = user.get("rank")
    terms_gpa = user.get("terms_gpa", {})

    manual_gpa = user.get("manual_gpa")
    if manual_gpa not in (None, "", "null"):
        effective_label = "معدل دستی ادمین"
    elif user.get("mode") == "gpa":
        effective_label = "معدل هم‌آوا"
    elif user.get("mode") == "no_gpa":
        effective_label = "معدل ثبت‌شده کاربر"
    else:
        effective_label = "نامشخص"

    return render_template(
        "edit_info.html",
        user=user,
        effective_gpa=effective_gpa,
        rank=rank,
        terms_gpa=terms_gpa,
        effective_label=effective_label
    )


@app.route("/update-name", methods=["POST"])
def update_name():
    if "student_id" not in session:
        return redirect(url_for("home"))

    user = storage.get_user_by_student_id(session["student_id"])
    if not user or user.get("mode") != "no_gpa":
        return redirect(url_for("edit_info"))

    new_name = request.form.get("full_name", "").strip()
    if not new_name:
        flash("نام نمی‌تواند خالی باشد.")
        return redirect(url_for("edit_info"))

    storage.update_user_full_name(
        student_id=user["student_id"],
        telegram_id=None,
        full_name=new_name
    )
    flash("✅ نام با موفقیت ویرایش شد.")
    return redirect(url_for("edit_info"))


@app.route("/update-student-id", methods=["POST"])
def update_student_id():
    if "student_id" not in session:
        return redirect(url_for("home"))

    old_student_id = session["student_id"]
    user = storage.get_user_by_student_id(old_student_id)
    if not user or user.get("mode") != "no_gpa":
        return redirect(url_for("edit_info"))

    new_student_id = request.form.get("student_id", "").strip()
    if not new_student_id or not new_student_id.isdigit():
        flash("کد دانشجویی باید عدد باشد.")
        return redirect(url_for("edit_info"))

    result = storage.update_user_student_id(
        old_student_id=old_student_id,
        telegram_id=None,
        new_student_id=new_student_id
    )

    if result["ok"]:
        session["student_id"] = new_student_id
        flash("✅ کد دانشجویی با موفقیت ویرایش شد.")
    else:
        if result.get("reason") == "duplicate_student_id":
            flash("❌ این کد دانشجویی قبلاً ثبت شده است.")
        else:
            flash("❌ خطایی در ویرایش رخ داد.")

    return redirect(url_for("edit_info"))


@app.route("/logout")
def logout():
    session.pop("student_id", None)
    return redirect(url_for("home"))


if __name__ == "__main__":
    # در محیط محلی روی پورت ۵۰۰۰ بالا می‌آید
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
