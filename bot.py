from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InputMediaVideo,
    LabeledPrice,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
    PreCheckoutQueryHandler,
)
from telegram.error import BadRequest

from pathlib import Path

from datetime import datetime, date, timezone, timedelta
import sqlite3
import aiohttp
import asyncio
import os
from content_data import VIDEO_IDS, TRAINING_TEXTS, MONTH_DESCRIPTIONS

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("Переменная окружения BOT_TOKEN не установлена")

# ====== НАСТРОЙКИ ПУТИ К БД ======

BASE_DIR = Path(__file__).resolve().parent

# Если бот запущен на Railway и примонтирован volume в /data – используем его.
# Локально (на компе), где /data нет, БД будет лежать рядом со скриптом.
if Path("/data").exists():
    DB_DIR = Path("/data")
else:
    DB_DIR = BASE_DIR

DB_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DB_DIR / "subscriptions.db"

print(">>> DB_PATH =", DB_PATH.resolve())

ADMIN_CHAT_ID = 503160725  # твой Telegram ID


# ====== НАСТРОЙКИ ПОДПИСКИ / TELEGRAM STARS ======
SUBSCRIPTION_YEAR_PAYLOAD = "corpus_subscription_year_v1"
SUBSCRIPTION_MONTH_PAYLOAD = "corpus_subscription_month_v1"
SUBSCRIPTION_YEAR_PRICE_STARS = 4990
SUBSCRIPTION_MONTH_PRICE_STARS = 1490
DEV_USER_IDS = {503160725, 304498036}                            # твой tg user_id
SUBSCRIPTION_YEAR_DURATION_DAYS = 365
SUBSCRIPTION_MONTH_DURATION_DAYS = 30
SUBSCRIPTION_DURATION_DAYS = SUBSCRIPTION_YEAR_DURATION_DAYS

SUBSCRIPTION_PLANS = {
    "month": {
        "payload": SUBSCRIPTION_MONTH_PAYLOAD,
        "price": SUBSCRIPTION_MONTH_PRICE_STARS,
        "duration_days": SUBSCRIPTION_MONTH_DURATION_DAYS,
        "label": "Подписка CORPUS на 1 месяц",
        "title": "Подписка CORPUS (1 месяц)",
        "description": "30 дней доступа ко всем тренировкам бота.",
    },
    "year": {
        "payload": SUBSCRIPTION_YEAR_PAYLOAD,
        "price": SUBSCRIPTION_YEAR_PRICE_STARS,
        "duration_days": SUBSCRIPTION_YEAR_DURATION_DAYS,
        "label": "Годовая подписка CORPUS",
        "title": "Подписка CORPUS (1 год)",
        "description": "12 месяцев доступа ко всем тренировкам бота.",
    },
}
PAYLOAD_TO_PLAN = {plan["payload"]: key for key, plan in SUBSCRIPTION_PLANS.items()}
# совместимость со старым кодом, по умолчанию — годовая подписка
SUBSCRIPTION_PAYLOAD = SUBSCRIPTION_YEAR_PAYLOAD
SUBSCRIPTION_PRICE_STARS = SUBSCRIPTION_YEAR_PRICE_STARS


def revoke_subscription(user_id: int):
    """
    Удаляем/отключаем подписку для пользователя.
    После этого user_has_subscription(user_id) должен вернуть False.
    """
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # Вариант 1 — полностью удалить подписку
    cur.execute("DELETE FROM subscriptions WHERE user_id = ?", (user_id,))

    # Если у тебя другая таблица/столбцы — поправь название таблицы и поля.
    # Например:
    # cur.execute("UPDATE subscriptions SET end = ? WHERE user_id = ?", ("1970-01-01", user_id))

    conn.commit()
    conn.close()


def init_db():
    """Создаём файл БД и таблицы, если их ещё нет."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # таблица подписок
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS subscriptions (
            user_id INTEGER PRIMARY KEY,
            start_date TEXT NOT NULL,
            end_date   TEXT NOT NULL
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_seen TEXT NOT NULL,
            last_seen  TEXT NOT NULL,
            starts_count INTEGER NOT NULL DEFAULT 0,
            trainings_opened INTEGER NOT NULL DEFAULT 0
        )
        """
    )

    conn.commit()
    conn.close()


def track_user_event(
    user_id: int,
    username: str | None = None,
    is_start: bool = False,
    opened_training: bool = False,
):
    """
    Записываем/обновляем информацию о пользователе:
    - first_seen / last_seen
    - счётчик стартов
    - счётчик открытых тренировок
    """
    now_iso = datetime.now(timezone.utc).isoformat()

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # есть ли уже пользователь
    cur.execute("SELECT first_seen, last_seen, starts_count, trainings_opened FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()

    if row is None:
        # новый пользователь
        starts = 1 if is_start else 0
        trainings = 1 if opened_training else 0
        cur.execute(
            """
            INSERT INTO users (user_id, username, first_seen, last_seen, starts_count, trainings_opened)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user_id, username, now_iso, now_iso, starts, trainings),
        )
    else:
        first_seen, last_seen, starts_count, trainings_opened = row
        if is_start:
            starts_count += 1
        if opened_training:
            trainings_opened += 1

        cur.execute(
            """
            UPDATE users
            SET username = COALESCE(?, username),
                last_seen = ?,
                starts_count = ?,
                trainings_opened = ?
            WHERE user_id = ?
            """,
            (username, now_iso, starts_count, trainings_opened, user_id),
        )

    conn.commit()
    conn.close()


def load_subscription(user_id: int):
    """Забрать подписку пользователя из БД. Возвращает dict или None."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT start_date, end_date FROM subscriptions WHERE user_id = ?",
        (user_id,),
    )
    row = cur.fetchone()
    conn.close()

    if not row:
        return None

    start = date.fromisoformat(row[0])
    end = date.fromisoformat(row[1])
    return {"start": start, "end": end}


def save_subscription(user_id: int, start, end):
    """Сохранить/обновить подписку пользователя в БД."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO subscriptions (user_id, start_date, end_date)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            start_date = excluded.start_date,
            end_date   = excluded.end_date
        """,
        (user_id, start.isoformat(), end.isoformat()),
    )
    conn.commit()
    conn.close()


def create_or_extend_subscription(user_id: int, days: int = SUBSCRIPTION_DURATION_DAYS) -> dict:
    """
    Создаём или продлеваем подписку на заданное количество дней.
    Если подписка ещё действует — продлеваем от даты окончания.
    Если уже истекла или не было — считаем от сегодняшней даты.
    """
    today = datetime.now(timezone.utc).date()
    current = load_subscription(user_id)

    if current and current["end"] >= today:
        # продлеваем от текущей даты окончания
        new_start = current["start"]
        new_end = current["end"] + timedelta(days=days)
    else:
        # новая или просроченная
        new_start = today
        new_end = today + timedelta(days=days)

    save_subscription(user_id, new_start, new_end)
    return {"start": new_start, "end": new_end}


# ====== РУЧНАЯ ВЫДАЧА ПОДПИСКИ АДМИНОМ ======
def manual_grant_subscription(user_id: int, days: int = SUBSCRIPTION_DURATION_DAYS):
    """
    Выдать или продлить подписку пользователю вручную (например, без оплаты).
    Если подписка ещё активна — продлеваем от даты окончания.
    Если истекла или её не было — считаем от сегодня.
    """
    today = datetime.now(timezone.utc).date()
    sub = load_subscription(user_id)  # уже существующая функция, читает из БД

    if sub and sub["end"] >= today:
        # уже есть активная подписка — продлеваем от даты окончания
        start = sub["start"]
        end = sub["end"] + timedelta(days=days)
    else:
        # новой/просроченная — создаём с нуля от сегодня
        start = today
        end = today + timedelta(days=days)

    # ⚠️ ВАЖНО: save_subscription принимает только 3 аргумента
    save_subscription(user_id, start, end)

    return {"start": start, "end": end}


def user_has_subscription(user_id: int) -> bool:
    """
    Активна ли подписка на сегодня.
    DEV_USER_IDS считаем всегда с активной подпиской.
    """
    if user_id in DEV_USER_IDS:
        return True

    sub = load_subscription(user_id)
    if not sub:
        return False

    today = datetime.now(timezone.utc).date()
    return today <= sub["end"]


def get_subscription_dates(user_id: int):
    """Вернуть (start_date, end_date) или (None, None)."""
    sub = load_subscription(user_id)
    if not sub:
        return None, None
    return sub["start"], sub["end"]

def save_payment(user_id: int, charge_id: str, amount: int, currency: str):
    """
    Сохраняем информацию о платеже в таблицу payments.
    Это нужно, чтобы потом можно было сделать рефанд по charge_id.
    """
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            charge_id TEXT NOT NULL,
            amount INTEGER NOT NULL,
            currency TEXT NOT NULL,
            paid_at TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        INSERT INTO payments (user_id, charge_id, amount, currency, paid_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (user_id, charge_id, amount, currency, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()


def cancel_subscription_in_db(user_id: int):
    """
    Обрезаем подписку пользователю (используем после рефанда).
    Предполагаем, что есть таблица subscriptions с колонками (user_id, start_date, end_date).
    """
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE subscriptions
        SET end_date = ?
        WHERE user_id = ?
        """,
        (datetime.now(timezone.utc).date().isoformat(), user_id),
    )
    conn.commit()
    conn.close()

async def refund_star_payment(user_id: int, charge_id: str) -> bool:
    """
    Делаем рефанд через Bot API: refundStarPayment.
    Возвращает True, если Telegram сказал ok.
    """
    url = f"https://api.telegram.org/bot{TOKEN}/refundStarPayment"
    payload = {
        "user_id": user_id,
        "telegram_payment_charge_id": charge_id,
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload) as resp:
            data = await resp.json()
            # При успехе Telegram вернёт {"ok": true, "result": true}
            return data.get("ok") and data.get("result") is True

# ====== КНОПКИ ======
MAIN_MENU_BUTTONS = [
    ["✅Подписка", "🏋🏽‍♀️Тренировка"],
    ["⚠️Правила", "🥗Питание"],
]

PLACE_BUTTONS = [
    ["В зале", "Дома"],
    ["Вернуться в меню"],
]

MONTH_BUTTONS = [
    ["1 месяц", "2-3 месяц"],
    ["4-5 месяц", "6-7 месяц"],
    ["8-9 месяц", "10-12 месяц"],
    ["Вернуться в меню"],
]

TRAINING_NUM_BUTTONS = [
    ["1", "2", "3", "4"],
    ["5", "6", "7", "8"],
    ["9", "10", "11", "12"],
    ["Вернуться в меню"],
]

ABC_TRAINING_BUTTONS = [
    ["Ягодицы", "Верх тела", "Ноги"],
    ["Вернуться в меню"],
]

SUBSCRIPTION_MONTH_BUTTON = "🗓 Подписка на 1 месяц"
SUBSCRIPTION_YEAR_BUTTON = "📅 Подписка на 1 год"


def kb_main():
    return ReplyKeyboardMarkup(MAIN_MENU_BUTTONS, resize_keyboard=True)


def kb_place():
    return ReplyKeyboardMarkup(PLACE_BUTTONS, resize_keyboard=True)


def kb_month():
    return ReplyKeyboardMarkup(MONTH_BUTTONS, resize_keyboard=True)


def kb_training_nums():
    return ReplyKeyboardMarkup(TRAINING_NUM_BUTTONS, resize_keyboard=True)


def kb_training_abc():
    return ReplyKeyboardMarkup(ABC_TRAINING_BUTTONS, resize_keyboard=True)


def kb_subscription_plans():
    return ReplyKeyboardMarkup(
        [
            [SUBSCRIPTION_MONTH_BUTTON, SUBSCRIPTION_YEAR_BUTTON],
            ["Вернуться в меню"],
        ],
        resize_keyboard=True,
    )


# ====== СЛОВАРИ С ВИДЕО/ТЕКСТАМИ/ДОКУМЕНТАМИ ======
# Данные вынесены в content_data.py, чтобы не держать file_id и тексты в основном файле.

# ====== TERMS & PAY SUPPORT & DEV ======
async def cmd_terms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Условия использования и оплаты:\n\n"
        "- Подписка даёт доступ ко всем тренировкам бота. Можно оформить на 1 месяц или на 1 год.\n"
        "- Оплата выполняется в Telegram Stars внутри приложения.\n"
        "- Покупая подписку, вы подтверждаете, что ознакомились с этими условиями.\n\n"
        "Важно: поддержка Telegram и @BotSupport не помогают по вопросам платежей за этот бот – "
        "по всем вопросам обращайтесь только к автору бота.",
    )


async def cmd_paysupport(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Если возникли вопросы по оплате или доступу к тренировкам:\n\n"
        "1) Напишите автору бота: @klishinkirill\n"
        "2) В сообщении укажите ваш @username, дату платежа и скриншот чека.\n\n"
        "Важно: поддержка Telegram и @BotSupport не помогают по вопросам платежей за этот бот.",
    )


async def cmd_devsub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if user_id not in DEV_USER_IDS:
        await update.message.reply_text("Эта команда только для разработчика бота.")
        return

    sub = create_or_extend_subscription(user_id)
    start, end = sub["start"], sub["end"]

    await update.message.reply_text(
        "Тестовая подписка активирована ✅\n"
        f"Начало: {start.strftime('%d.%m.%Y')}\n"
        f"Окончание: {end.strftime('%d.%m.%Y')}\n\n"
        "Теперь ты можешь тестировать доступ к тренировкам без реальной оплаты.",
        reply_markup=kb_main(),
    )

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_id = update.effective_user.id
    if admin_id not in DEV_USER_IDS:
        await update.message.reply_text("Эта команда только для администратора.")
        return

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # всего уникальных пользователей
    cur.execute("SELECT COUNT(*) FROM users")
    total_users = cur.fetchone()[0] or 0

    # новые за последние 7 дней
    cur.execute(
        """
        SELECT COUNT(*) FROM users
        WHERE datetime(first_seen) >= datetime('now', '-7 days')
        """
    )
    new_7d = cur.fetchone()[0] or 0

    # кто открывал хоть одну тренировку
    cur.execute("SELECT COUNT(*) FROM users WHERE trainings_opened > 0")
    trained_users = cur.fetchone()[0] or 0

    # активные подписки
    cur.execute(
        """
        SELECT COUNT(*) FROM subscriptions
        WHERE date(end_date) >= date('now')
        """
    )
    active_subs = cur.fetchone()[0] or 0

    conn.close()

    msg = (
        "📊 Статистика бота:\n\n"
        f"👥 Всего уникальных пользователей: <b>{total_users}</b>\n"
        f"🆕 Новых за 7 дней: <b>{new_7d}</b>\n"
        f"🏋️‍♀️ Открывали тренировки: <b>{trained_users}</b>\n"
        f"✅ Активных подписок: <b>{active_subs}</b>\n"
    )

    await update.message.reply_text(msg, parse_mode="HTML")

# ====== /refund — рефанд платежа Stars + удаление подписки ======
async def cmd_refund(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # безопасное сообщение (иногда update.message == None)
    message = update.effective_message
    admin_id = update.effective_user.id

    # ---- проверка админа ----
    if admin_id not in DEV_USER_IDS:
        await message.reply_text("Эта команда только для администратора бота.")
        return

    # ---- проверка аргументов ----
    if len(context.args) != 2:
        await message.reply_text(
            "Использование:\n"
            "/refund <user_id> <charge_id>\n\n"
            "user_id — Telegram ID покупателя,\n"
            "charge_id — telegram_payment_charge_id из таблицы payments."
        )
        return

    # ---- парсим user_id ----
    try:
        target_user_id = int(context.args[0])
    except ValueError:
        await message.reply_text("user_id должен быть числом.")
        return

    charge_id = context.args[1]

    # ---- делаем рефанд через Bot API ----
    ok = await refund_star_payment(target_user_id, charge_id)

    if not ok:
        await message.reply_text(
            "Не удалось выполнить рефанд ❌\n"
            "Проверь user_id и charge_id, либо попробуй позже."
        )
        return

    # ---- если рефанд успешный — удаляем подписку в БД ----
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()

        # Таблица подписок subscriptions(user_id, start_date, end_date)
        cur.execute("DELETE FROM subscriptions WHERE user_id = ?", (target_user_id,))
        conn.commit()
        conn.close()
    except Exception as e:
        await message.reply_text(f"Рефанд прошёл, но подписку удалить не удалось: {e}")
        return

    # ---- очищаем кеш user_data, если там был флаг подписки ----
    try:
        ud = context.application.user_data.get(target_user_id)
        if ud and "has_subscription" in ud:
            ud["has_subscription"] = False
    except:
        pass

    # ---- финальный ответ ----
    await message.reply_text(
        "Рефанд выполнен успешно ✅\n"
        f"Подписка пользователя {target_user_id} отключена.\n\n"
        f"charge_id: {charge_id}"
    )

# ====== /subs — список всех подписок (ТОЛЬКО ДЛЯ АДМИНА) ======
async def cmd_subs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_id = update.effective_user.id
    if admin_id not in DEV_USER_IDS:
        await update.message.reply_text("Эта команда только для администратора.")
        return

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # таблица подписок
    cur.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            user_id INTEGER PRIMARY KEY,
            start_date TEXT NOT NULL,
            end_date TEXT NOT NULL
        )
    """)
    
    cur.execute("SELECT user_id, start_date, end_date FROM subscriptions ORDER BY user_id")
    subs = cur.fetchall()

    # таблица платежей
    cur.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            charge_id TEXT NOT NULL,
            amount INTEGER NOT NULL,
            currency TEXT NOT NULL,
            paid_at TEXT NOT NULL
        )
    """)

    # берём последние платежи по каждому user_id
    cur.execute("""
        SELECT user_id, charge_id, amount, currency, paid_at
        FROM payments
        WHERE id IN (
            SELECT MAX(id)
            FROM payments
            GROUP BY user_id
        )
    """)
    payments_raw = cur.fetchall()

    conn.close()

    # превращаем платежи в удобный dict
    last_payments = {}
    for uid, charge_id, amount, currency, paid_at in payments_raw:
        last_payments[uid] = {
            "charge_id": charge_id,
            "amount": amount,
            "currency": currency,
            "paid_at": paid_at,
        }

    if not subs:
        await update.message.reply_text("Подписок пока нет.")
        return

    # собираем текст
    msg_lines = ["📄 <b>Список подписок:</b>\n"]

    today = datetime.now(timezone.utc).date()

    for user_id, start, end in subs:
        start_d = datetime.fromisoformat(start).date()
        end_d = datetime.fromisoformat(end).date()
        is_active = "🟢 Активна" if end_d >= today else "🔴 Истекла"

        line = (
            f"<b>User ID:</b> {user_id}\n"
            f"— Начало: {start_d.strftime('%d.%m.%Y')}\n"
            f"— Конец: {end_d.strftime('%d.%m.%Y')}\n"
            f"— Статус: {is_active}\n"
        )

        # добавляем истории платежей, если есть
        if user_id in last_payments:
            p = last_payments[user_id]
            paid_date = datetime.fromisoformat(p["paid_at"]).strftime('%d.%m.%Y %H:%M')
            line += (
                f"— 💸 Последний платёж:\n"
                f"   charge_id: <code>{p['charge_id']}</code>\n"
                f"   сумма: {p['amount']} {p['currency']}\n"
                f"   дата: {paid_date}\n"
            )
        else:
            line += "— 💸 Платежей нет\n"

        line += "\n"
        msg_lines.append(line)

    final_msg = "\n".join(msg_lines)

    await update.message.reply_text(
        final_msg,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )

# ====== /grant — выдать/продлить подписку пользователю (ТОЛЬКО ДЛЯ АДМИНА) ======
async def cmd_grant(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_id = update.effective_user.id
    if admin_id not in DEV_USER_IDS:
        await update.message.reply_text("Эта команда только для администратора бота.")
        return

    if len(context.args) < 1:
        await update.message.reply_text(
            "Использование:\n"
            "/grant <user_id> [days]\n\n"
            "<user_id> — Telegram ID пользователя,\n"
            "[days] — на сколько дней выдать/продлить (по умолчанию 365)."
        )
        return

    # user_id
    try:
        target_user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("user_id должен быть числом.")
        return

    # days (опционально)
    if len(context.args) >= 2:
        try:
            days = int(context.args[1])
        except ValueError:
            await update.message.reply_text("days должен быть числом (количество дней).")
            return
    else:
        days = SUBSCRIPTION_DURATION_DAYS  # по умолчанию 365

    sub = manual_grant_subscription(target_user_id, days)

    await update.message.reply_text(
        "Подписка выдана/продлена вручную ✅\n"
        f"user_id: {target_user_id}\n"
        f"Начало: {sub['start'].strftime('%d.%m.%Y')}\n"
        f"Окончание: {sub['end'].strftime('%d.%m.%Y')}"
    )

# ====== /revoke — забрать подписку у пользователя (ТОЛЬКО АДМИН) ======
async def cmd_revoke(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_id = update.effective_user.id
    if admin_id not in DEV_USER_IDS:
        await update.message.reply_text("Эта команда только для администратора бота.")
        return

    if len(context.args) != 1:
        await update.message.reply_text(
            "Использование:\n"
            "/revoke <user_id>\n\n"
            "Пример:\n"
            "/revoke 503160725"
        )
        return

    try:
        target_user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("user_id должен быть числом.")
        return

    # 🔴 Полностью убираем подписку из БД
    try:
        revoke_subscription(target_user_id)  # <- использует твою функцию выше, которая делает DELETE FROM subscriptions
    except Exception as e:
        await update.message.reply_text(f"Не удалось забрать подписку: {e}")
        return

    # 💾 На всякий случай чистим возможный кеш в user_data
    try:
        ud = context.application.user_data.get(target_user_id)
        if ud:
            ud.pop("has_subscription", None)
    except Exception:
        pass

    await update.message.reply_text(
        "Подписка пользователя отозвана ✅\n"
        f"user_id: {target_user_id}"
    )

# ====== /restart — перезапуск бота (ТОЛЬКО ДЛЯ АДМИНА) ======
async def cmd_restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in DEV_USER_IDS:
        await update.message.reply_text("Эта команда только для администратора бота.")
        return

    await update.message.reply_text("Перезапускаю бота…")

    # даём сообщению улететь
    await asyncio.sleep(1)

    # жёстко выходим из процесса — Railway сам перезапустит контейнер
    os._exit(1)

# ====== START ======
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    track_user_event(user.id, user.username, is_start=True)
    # сохраняем только дату последней тренировки
    last_date = context.user_data.get("last_training_date")
    context.user_data.clear()
    if last_date:
        context.user_data["last_training_date"] = last_date

    await update.message.reply_text(
        "Добро пожаловать,\nCORPUS — платформа с продуманной системой тренировок, которая делает самостоятельные занятия безопасными и эффективными. Выберите нужный пункт меню 👇",
        reply_markup=kb_main(),
        protect_content=True,
    )


# ====== ОТПРАВКА ИНВОЙСА НА ПОДПИСКУ ======
async def send_subscription_invoice(update: Update, context: ContextTypes.DEFAULT_TYPE, plan_key: str = "year"):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    if user_has_subscription(user_id):
        start_d, end_d = get_subscription_dates(user_id)
        if start_d and end_d:
            txt = (
                "У Вас уже есть активная подписка ✅\n\n"
                f"Начало: {start_d.strftime('%d.%m.%Y')}\n"
                f"Окончание: {end_d.strftime('%d.%m.%Y')}\n\n"
                "Можеште открывать любые тренировки."
            )
        else:
            txt = (
                "У Вас уже есть активная подписка ✅\n"
                "Можете открывать любые тренировки."
            )

        await update.message.reply_text(txt, reply_markup=kb_main())
        return

    plan = SUBSCRIPTION_PLANS.get(plan_key, SUBSCRIPTION_PLANS["year"])

    prices = [
        LabeledPrice(
            label=plan["label"],
            amount=plan["price"],
        )
    ]

    await context.bot.send_invoice(
        chat_id=chat_id,
        title=plan["title"],
        description=plan["description"],
        payload=plan["payload"],
        provider_token="",
        currency="XTR",
        prices=prices,
        max_tip_amount=0,
    )


# ====== ОБРАБОТКА ПЛАТЕЖА STARS ======
async def precheckout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query

    if query.invoice_payload not in PAYLOAD_TO_PLAN:
        await query.answer(
            ok=False,
            error_message="Неизвестный платёж. Попробуйте ещё раз или напиши в /paysupport.",
        )
        return

    await query.answer(ok=True)


# ====== ОБРАБОТКА УСПЕШНОГО ПЛАТЕЖА STARS ======
async def successful_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Вызывается автоматически, когда Telegram подтверждает успешный платёж Stars.
    """
    sp = update.message.successful_payment
    user_id = update.effective_user.id

    # 💾 Сохраняем платёж для рефандов
    save_payment(
        user_id=user_id,
        charge_id=sp.telegram_payment_charge_id,
        amount=sp.total_amount,
        currency=sp.currency,
    )

    plan_key = PAYLOAD_TO_PLAN.get(sp.invoice_payload)

    # Проверяем правильный payload
    if sp.currency == "XTR" and plan_key:
        plan = SUBSCRIPTION_PLANS[plan_key]
        sub = create_or_extend_subscription(user_id, days=plan["duration_days"])
        start, end = sub["start"], sub["end"]

        await update.message.reply_text(
            "Оплата прошла успешно ✅\n"
            "Ваша подписка активирована.\n\n"
            f"Начало: {start.strftime('%d.%m.%Y')}\n"
            f"Окончание: {end.strftime('%d.%m.%Y')}\n\n"
            "Теперь Вам доступен полный набор тренировок и общий чат с лекциями по питанию, полезными материалами и поддержкой 💗\n\n"
            "[вступить в чат](https://t.me/+AOT_lFEIZzo5NTNi)",
            reply_markup=kb_main(),
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            "Получен неизвестный платёж. Напишите в /paysupport."
        )

# ====== ОСНОВНОЙ ХЕНДЛЕР ТЕКСТА ======
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    user = update.effective_user
    user_id = user.id

    # фиксируем любой визит / активность
    track_user_event(user_id, user.username)

    has_sub = user_has_subscription(user_id)

    # возврат в меню
    if text.lower() in ["меню", "вернуться в меню", "/меню", "/menu", "/вернуться в меню"]:
        await start(update, context)
        return

    # Подписка
    if text in ["✅Подписка", "Оформить подписку"]:
        if has_sub:
            start_d, end_d = get_subscription_dates(user_id)
            if start_d and end_d:
                msg = (
                    "У Вас уже есть активная подписка ✅\n\n"
                    f"Начало: {start_d.strftime('%d.%m.%Y')}\n"
                    f"Окончание: {end_d.strftime('%d.%m.%Y')}\n\n"
                    "Можете открывать любые тренировки."
                )
            else:
                msg = (
                    "У Вас уже есть активная подписка ✅\n"
                    "Можете открыть любые тренировки."
                )

            await update.message.reply_text(
                msg,
                reply_markup=kb_main(),
                protect_content=True,
            )
        else:
            await update.message.reply_text(
                "Подписка даёт доступ ко всем тренировкам бота.\n"
                "Выберите срок: на 1 месяц или на 1 год. Оплата в Telegram Stars.",
                reply_markup=kb_subscription_plans(),
                protect_content=True,
            )
        return

    if text == SUBSCRIPTION_MONTH_BUTTON:
        await send_subscription_invoice(update, context, plan_key="month")
        return

    if text == SUBSCRIPTION_YEAR_BUTTON:
        await send_subscription_invoice(update, context, plan_key="year")
        return

    # Правила
    if text == "⚠️Правила":
        await update.message.reply_text(
           "Условия использования и оплаты:\n\n"
            "- Подписка даёт доступ ко всем тренировкам бота. Можно оформить на 1 месяц или на 1 год.\n"
            "- Оплата выполняется в Telegram Stars внутри приложения.\n"
            "- Покупая подписку, вы подтверждаете, что ознакомились с этими условиями.\n\n"
            "Важно: поддержка Telegram и @BotSupport не помогают по вопросам платежей за этот бот – по всем вопросам обращайтесь только к автору бота.\n\n",
            reply_markup=ReplyKeyboardMarkup([["Вернуться в меню"]], resize_keyboard=True),
            protect_content=True,
        )
        return
    

    # Тренировки
    if text == "🏋🏽‍♀️Тренировка":
        if not has_sub:
            await update.message.reply_text(
                "Тренировки доступны по подписке 🔒\n\n"
                "Но вы можете попробовать две пробные тренировки:\n"
                "— одну в зале\n"
                "— одну дома\n\n"
                "Выберите вариант ниже 👇",
                reply_markup=ReplyKeyboardMarkup(
                    [
                        ["🎁 Пробная (в зале)", "🎁 Пробная (дома)"],
                        ["Оформить подписку", "Вернуться в меню"],
                    ],
                    resize_keyboard=True,
                ),
                protect_content=True,
            )
            return

        await update.message.reply_text(
            "Где будете тренироваться?",
            reply_markup=kb_place(),
            protect_content=True,
        )
        return
    
    #ПИТАНИЕ
    if text == "🥗Питание":
        if not has_sub:
            await update.message.reply_text(
                "Раздел «Питание» доступен только по активной подписке 🔒\n\n"
                "Чтобы получить доступ, сначала оформите подписку.",
                reply_markup=ReplyKeyboardMarkup(
                    [["Подписка", "Вернуться в меню"]],
                    resize_keyboard=True,
                ),
                protect_content=True,
            )
            return

        await update.message.reply_text(
            "Подробнее о питании Вы можете посмотреть в данной группе - https://t.me/+AOT_lFEIZzo5NTNi",
            reply_markup=ReplyKeyboardMarkup([["Вернуться в меню"]], resize_keyboard=True),
            protect_content=True,
        )
        return
    
    # ПРОБНЫЕ ТРЕНИРОВКИ (доступны без подписки)
    if text == "🎁 Пробная (в зале)":
        place = "gym"
        month = "trial"
        training_key = "1"
        await send_training(update, context, place, month, training_key)
        return

    if text == "🎁 Пробная (дома)":
        place = "home"
        month = "trial"
        training_key = "1"
        await send_training(update, context, place, month, training_key)
        return
    

    # выбор места
    if text in ["В зале", "Дома"]:
        if not has_sub:
            await update.message.reply_text(
                "Тренировки доступны только по активной подписке 🔒\n"
                "Сначала оформите подписку.",
                reply_markup=ReplyKeyboardMarkup(
                    [["Подписка", "Вернуться в меню"]],
                    resize_keyboard=True,
                ),
                protect_content=True,
            )
            return

        place = "gym" if text == "В зале" else "home"
        context.user_data["place"] = place
        await update.message.reply_text(
            "Выберите месяц:",
            reply_markup=kb_month(),
            protect_content=True,
        )
        return

 # ===== выбор месяца =====
    if text in ["1 месяц", "2-3 месяц", "4-5 месяц", "6-7 месяц", "8-9 месяц", "10-12 месяц"]:
        if not has_sub:
            await update.message.reply_text(
                "Тренировки доступны только по активной подписке 🔒\n"
                "Сначала оформите подписку.",
                reply_markup=ReplyKeyboardMarkup(
                    [["Подписка", "Вернуться в меню"]],
                    resize_keyboard=True,
                ),
                protect_content=True,
            )
            return

        month_key = text.replace(" месяц", "")
        context.user_data["month"] = month_key

        # клавиатура: 1 месяц — цифры 1–12, остальные — категории (Ягодицы / Верх / Ноги)
        kb = kb_training_nums() if month_key == "1" else kb_training_abc()

        text_to_send = MONTH_DESCRIPTIONS.get(month_key, "Выбирайте тренировку 👇")

        await update.message.reply_text(
            text_to_send,
            reply_markup=kb,
            protect_content=True,
        )
        return

    # выбор тренировки 1..12
    if text.isdigit() and 1 <= int(text) <= 12:
        if not has_sub:
            await update.message.reply_text(
                "Тренировки доступны только по активной подписке 🔒\n"
                "Сначала оформите подписку.",
                reply_markup=ReplyKeyboardMarkup(
                    [["Подписка", "Вернуться в меню"]],
                    resize_keyboard=True,
                ),
                protect_content=True,
            )
            return

        place = context.user_data.get("place")
        month = context.user_data.get("month")

        if not place or not month:
            await update.message.reply_text(
                "Сначала выбери место и месяц 💡",
                reply_markup=kb_main(),
                protect_content=True,
            )
            return

        if month != "1":
            await update.message.reply_text(
                "В этом месяце тренировки сгруппированы по направлению: Ягодицы / Верх тела / Ноги 👇",
                reply_markup=kb_training_abc(),
                protect_content=True,
            )
            return

        training_num = text
        await send_training(update, context, place, month, training_num)
        return

# ===== выбор тренировки по категории (Ягодицы / Верх тела / Ноги) =====
    if text in ["Ягодицы", "Верх тела", "Ноги"]:
        if not has_sub:
            await update.message.reply_text(
                "Тренировки доступны только по активной подписке 🔒\n"
                "Сначала оформите подписку.",
                reply_markup=ReplyKeyboardMarkup(
                    [["Подписка", "Вернуться в меню"]],
                    resize_keyboard=True,
                ),
                protect_content=True,
            )
            return

        place = context.user_data.get("place")
        month = context.user_data.get("month")

        if not place or not month:
            await update.message.reply_text(
                "Сначала выбери место и месяц 💡",
                reply_markup=kb_main(),
                protect_content=True,
            )
            return

        if month == "1":
            await update.message.reply_text(
                "В 1 месяце доступны только тренировки 1–12 👇",
                reply_markup=kb_training_nums(),
                protect_content=True,
            )
            return

        # 🔑 КЛЮЧ В СЛОВАРЯХ = ТО ЖЕ САМОЕ, ЧТО НА КНОПКЕ
        training_key = text   # "Ягодицы" / "Верх тела" / "Ноги"

        await send_training(update, context, place, month, training_key)
        return

    # если ничего не подошло
    await update.message.reply_text(
        "Выберите пункт меню 👇",
        reply_markup=kb_main(),
        protect_content=True,
    )


# ====== ТРЕНИРОВКА + ОГРАНИЧЕНИЕ 1 В ДЕНЬ (кроме админа) ======
async def send_training(update: Update, context: ContextTypes.DEFAULT_TYPE, place: str, month: str, training_num: str):
    chat_id = update.effective_chat.id
    user = update.effective_user
    user_id = user.id

    # считаем открытие тренировки
    track_user_event(user_id, user.username, opened_training=True)

    # 👉 Админ (из DEV_USER_IDS) тренируется без ограничения
    if user_id not in DEV_USER_IDS and month != "trial":
        # 1 тренировка в день
        today = datetime.now(timezone.utc).date().isoformat()
        last_view_date = context.user_data.get("last_training_date")

        if last_view_date == today:
            await context.bot.send_message(
                chat_id,
                "Вы уже смотрели тренировку сегодня ✅\n"
                "Завтра можно будет открыть новую.",
                reply_markup=ReplyKeyboardMarkup([["Вернуться в меню"]], resize_keyboard=True),
                protect_content=True,
            )
            return

        # сохраняем дату просмотра
        context.user_data["last_training_date"] = today

    # ===== дальше идёт твоя логика отправки видео/текста/документа =====

    # соберём все message_id, чтобы удалить
    messages_to_delete = []

    # видео
    videos = VIDEO_IDS.get(place, {}).get(month, {}).get(training_num, [])
    videos = [v for v in videos if v]  # фильтрация пустых

    if videos:
        media = [InputMediaVideo(media=vid) for vid in videos]
        try:
            msgs = await context.bot.send_media_group(chat_id=chat_id, media=media, protect_content=True)
            for m in msgs:
                messages_to_delete.append(m.message_id)
        except BadRequest as e:
            await context.bot.send_message(
                chat_id,
                "Не удалось отправить все видео сразу (возможно, битый file_id). Пробую по одному.",
                protect_content=True,
            )
            for vid in videos:
                try:
                    m = await context.bot.send_video(chat_id=chat_id, video=vid, protect_content=True)
                    messages_to_delete.append(m.message_id)
                except BadRequest as single_err:
                    await context.bot.send_message(
                        ADMIN_CHAT_ID,
                        f"send_video failed for user {chat_id}, place={place}, month={month}, training={training_num}, file_id={vid}. Error: {single_err}",
                    )
    else:
        m = await context.bot.send_message(
            chat_id,
            "Видео для этой тренировки пока не привязаны.",
            protect_content=True,
        )
        messages_to_delete.append(m.message_id)

    # предупреждение
    warn_msg = await context.bot.send_message(
        chat_id,
        "<b><i>Тренировка автоматически удалится через 24 часа</i></b>",
        parse_mode="HTML",
        protect_content=True,
    )
    messages_to_delete.append(warn_msg.message_id)

    # текст тренировки
    training_text = TRAINING_TEXTS.get(place, {}).get(month, {}).get(training_num)
    if training_text:
        txt_msg = await context.bot.send_message(chat_id, training_text, protect_content=True)
    else:
        txt_msg = await context.bot.send_message(
            chat_id,
            f"Описание тренировки {training_num} ({month}): скоро добавим 💪",
            protect_content=True,
        )
    messages_to_delete.append(txt_msg.message_id)


    # кнопка назад
    await context.bot.send_message(
        chat_id,
        "Можешь вернуться в меню:",
        reply_markup=ReplyKeyboardMarkup([["Вернуться в меню"]], resize_keyboard=True),
        protect_content=True,
    )

    # удаление через 24 часа
    if context.job_queue:
        for mid in messages_to_delete:
            context.job_queue.run_once(
                delete_message_job,
                when=5,  # 24 часа
                data={"chat_id": chat_id, "message_id": mid},
            )


async def delete_message_job(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    try:
        await context.bot.delete_message(chat_id=data["chat_id"], message_id=data["message_id"])
    except Exception:
        pass


# ловим медиа, чтобы получать file_id
async def catch_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.video:
        fid = update.message.video.file_id
        print("VIDEO file_id:", fid)
        await update.message.reply_text("Видео получил ✅. Смотри file_id в консоли.", protect_content=True)
    elif update.message.document:
        fid = update.message.document.file_id
        print("DOCUMENT file_id:", fid)
        await update.message.reply_text("Документ получил ✅. Смотри file_id в консоли.", protect_content=True)
    else:
        await update.message.reply_text("Пришли видео или документ — я дам тебе file_id.", protect_content=True)


def main():
    import sys, asyncio

    if sys.platform.startswith("win"):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    # инициализируем БД
    init_db()

    app = Application.builder().token(TOKEN).build()

    # Команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("terms", cmd_terms))
    app.add_handler(CommandHandler("paysupport", cmd_paysupport))
    app.add_handler(CommandHandler("devsub", cmd_devsub))
    app.add_handler(CommandHandler("subs", cmd_subs))
    app.add_handler(CommandHandler("refund", cmd_refund))
    app.add_handler(CommandHandler("grant", cmd_grant))     
    app.add_handler(CommandHandler("revoke", cmd_revoke)) 
    app.add_handler(CommandHandler("restart", cmd_restart))
    app.add_handler(CommandHandler("stats", cmd_stats))


    # Payments
    app.add_handler(PreCheckoutQueryHandler(precheckout_handler))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler))

    # Остальное
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.ALL, catch_media))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("Bot started...")
    app.run_polling()


if __name__ == "__main__":
    main()



