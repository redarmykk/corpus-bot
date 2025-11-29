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

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "subscriptions.db"

from datetime import datetime, date, timezone, timedelta
import sqlite3
import aiohttp
import os

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("Переменная окружения BOT_TOKEN не установлена")

ADMIN_CHAT_ID = 503160725  # твой Telegram ID


# ====== НАСТРОЙКИ ПОДПИСКИ / TELEGRAM STARS ======
SUBSCRIPTION_PAYLOAD = "corpus_subscription_year_v1"  # payload инвойса
SUBSCRIPTION_PRICE_STARS = 4990                       # 🎯 цена в звёздах
DEV_USER_IDS = {503160725, 304498036}                            # твой tg user_id
SUBSCRIPTION_DURATION_DAYS = 365                      # длительность подписки

# ====== БАЗА ДАННЫХ ======
DB_PATH = "corpus_bot.db"
DB_PATH = "subscriptions.db"

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


def create_or_extend_subscription(user_id: int) -> dict:
    """
    Создаём или продлеваем подписку на SUBSCRIPTION_DURATION_DAYS дней.
    Если подписка ещё действует — продлеваем от даты окончания.
    Если уже истекла или не было — считаем от сегодняшней даты.
    """
    today = datetime.now(timezone.utc).date()
    current = load_subscription(user_id)

    if current and current["end"] >= today:
        # продлеваем от текущей даты окончания
        new_start = current["start"]
        new_end = current["end"] + timedelta(days=SUBSCRIPTION_DURATION_DAYS)
    else:
        # новая или просроченная
        new_start = today
        new_end = today + timedelta(days=SUBSCRIPTION_DURATION_DAYS)

    save_subscription(user_id, new_start, new_end)
    return {"start": new_start, "end": new_end}


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


# ====== СЛОВАРИ С ВИДЕО/ТЕКСТАМИ/ДОКУМЕНТАМИ ======
# !!! ЗДЕСЬ ВСТАВЬ СВОИ VIDEO_IDS / TRAINING_TEXTS / DOC_IDS КАК У ТЕБЯ БЫЛО !!!
VIDEO_IDS = {
    "gym": {
        "1": {
            "1": [
                "BAACAgIAAxkBAANLaRBugIGMmYsvxdVxN9S6YvjBaxwAAneKAAJTQ4FIjcW71ASuWEE2BA",
                "BAACAgIAAxkBAANNaRBvCOTElsWiiTUUafTBoP1nHRYAAnuKAAJTQ4FIBW9KAAH7_NoNNgQ",
                "BAACAgIAAxkBAANPaRBvGKx-Olg42ZC642MQ70hfbboAAnyKAAJTQ4FIdwmP7gHgs1I2BA",
                "BAACAgIAAxkBAANRaRBvKlVCSYFsPwku7ZA3Chqkm48AAn2KAAJTQ4FIsTfWDHXB8cA2BA",
                "BAACAgIAAxkBAANTaRBvRrLJpxb0_v0nzcEm3sSUr24AAn-KAAJTQ4FIDUY6VGH-6E42BA",
                "BAACAgIAAxkBAAPLaRByZ98nJtSW6yHGlj60-F8afeYAAqWKAAJTQ4FId40F9PgFpHo2BA",
            ],
            "2": [
                "BAACAgIAAxkBAAPiaRBzxchLBoP8b01N3pviPlHEjMoAAvOKAAJTQ4FIpNRbd0-fUbg2BA",
                "BAACAgIAAxkBAAPjaRBzxV4tag06ola7X3QBe7HixmUAAvSKAAJTQ4FI_9RFjqZW7po2BA",
                "BAACAgIAAxkBAAPfaRBzxdHp7FzBvUOdQMNVbGYWoh8AAu-KAAJTQ4FI3ryPAnB1jYI2BA",
                "BAACAgIAAxkBAAPgaRBzxYSnhKOaAAGQzBza6JRY8SRKAALwigACU0OBSAFjvK46uDBuNgQ",
                "BAACAgIAAxkBAAPhaRBzxYT7l8aXwwK3Qr1npQVso7EAAvGKAAJTQ4FIJZvfdCvLJHI2BA",
            ],
            "3": [
                "BAACAgIAAxkBAAIBv2kQhRqVSJluiIVCtGO0CPzgJhthAAL7iwACU0OBSMRRqTjqoqviNgQ",
                "BAACAgIAAxkBAAIBwWkQhSmxx9EBwgxvQNQmyoQKp9FMAAL8iwACU0OBSMp1HYHHNQLlNgQ",
                "BAACAgIAAxkBAAIBw2kQhTQZ9ZOfQI6FS1q0cQeUAehjAAL9iwACU0OBSDfqtDnL0PcQNgQ",
                "BAACAgIAAxkBAAIBxWkQhUB9EGiEqAFkLpYGrQo90gEbAAL-iwACU0OBSMeQeaiI346FNgQ",
                "BAACAgIAAxkBAAIBx2kQhU5OqYl-dqhU4QyV-jgHAAGDSwAC_4sAAlNDgUi4O_NfnUNUszYE",
            ],
            "4": [
                "BAACAgIAAxkBAAIC-mkQkkICaom9f4w3GEZnpemS7_n4AAKhjAACU0OBSGTrjR9xchCENgQ", 
                "BAACAgIAAxkBAAIC_GkQkkhU6ZWYqBp80Cyjnq4hvk2lAAKijAACU0OBSN2znKa5lHGtNgQ", 
                "BAACAgIAAxkBAAIC_mkQklAXItP6Fdcz_m_s5hTmcl8ZAAKljAACU0OBSL3GOHr22D4NNgQ", 
                "BAACAgIAAxkBAAIDAAFpEJJU2IBMgmr2QJQpOPYooNKRqwACpowAAlNDgUga_M4elQzEnjYE", 
                "BAACAgIAAxkBAAIDAmkQklkmoVvk6ISJ_0lMTpR7is4mAAKnjAACU0OBSBNSQVWGKB3-NgQ", 
                ],
            "5": [
                "BAACAgIAAxkBAAIDL2kQk7tSc4ZZ7iJO7QqY5wyM2AY7AAK-jAACU0OBSDLmSSr0RAuXNgQ", 
                "BAACAgIAAxkBAAIDMWkQk9BXrRebBr2CJ9Yvncul5eYaAAK_jAACU0OBSD2K-xYo0rf8NgQ", 
                "BAACAgIAAxkBAAIDM2kQk9oJPTTEI81-mxOXwhXyVqCOAALAjAACU0OBSN384l3GKhzvNgQ", 
                "BAACAgIAAxkBAAIDNWkQk-NnzuV5Q5qJZ9oUwDFbDLlaAALBjAACU0OBSGscxVeMp2xPNgQ", 
                "BAACAgIAAxkBAAIDN2kQk-5jaa4d4D40N71pQlTLzF3eAALDjAACU0OBSJzV14G57TM6NgQ", 
                ],
            "6": [
                "BAACAgIAAxkBAAIDO2kQlJirs8WS6OwvMunOfgHlQiE1AALPjAACU0OBSBlvhn2_96CvNgQ", 
                "BAACAgIAAxkBAAIDPWkQlRnxRU_IPDK7wduNcr3pdLZ-AALVjAACU0OBSBfVtKF6BPQ7NgQ", 
                "BAACAgIAAxkBAAIDP2kQlS8F1XDTFgk2t2Nn1j4YtRxmAALWjAACU0OBSKsuHCtT9PmMNgQ", 
                "BAACAgIAAxkBAAIDQWkQlUEVzL6c0mqE-4171VYk0-nAAALYjAACU0OBSCsXXrCSDUE_NgQ", 
                "BAACAgIAAxkBAAIDQ2kQlUr14qFPfp9k13V8q8DcMeWbAALajAACU0OBSOHrMSCyVjGUNgQ", 
                ],
            "7": [
                "BAACAgIAAxkBAAIDbWkQnrQW9J8O4qWmdha5wsYYDNWJAAJHiQACU0OJSGZvNG8j-7l3NgQ",
                "BAACAgIAAxkBAAIDb2kQntxT8YeDlP9H0Lu79j7FE0j5AAJLiQACU0OJSEwm9b2N406GNgQ", 
                "BAACAgIAAxkBAAIDcWkQnut1YJNf6bMg-XodWmJwmO5lAAJMiQACU0OJSGoxwfsPRL_ZNgQ", 
                "BAACAgIAAxkBAAIDc2kQnvVZ56Dfiwvbz_vt5cUAAVKHPwACTokAAlNDiUg5ovED3Nkb6zYE", 
                "BAACAgIAAxkBAAIDdWkQnv2DST5cW7LumozDjRGNx3IbAAJPiQACU0OJSG1MNjE210uuNgQ", 
                "BAACAgIAAxkBAAIDd2kQnwKdjZiozwuwyOfFo8l5OZqkAAJQiQACU0OJSCFEbSjjsMRjNgQ"
                ],
            "8": [
                "BAACAgIAAxkBAAIDe2kQn8KcNPV8RIqCkkj4bO0hjS_JAAJgiQACU0OJSDGOhNbpUftWNgQ", 
                "BAACAgIAAxkBAAIDfWkQn8zAULeMvk-Xj6pCqyuYj-wwAAJhiQACU0OJSA7W-vaIlit4NgQ", 
                "BAACAgIAAxkBAAIDf2kQn9Pnwld56-DuMTlHslRR2_n_AAJjiQACU0OJSAgzRwAB9VpfxzYE", 
                "BAACAgIAAxkBAAIDgWkQn94NzYLHBZEY4BrhF97Mga4DAAJmiQACU0OJSI803ktBXLYDNgQ", 
                "BAACAgIAAxkBAAIDg2kQn-V4ea-cuEZFkTGZEfWelDzcAAJpiQACU0OJSLtOK9TBsqweNgQ", 
                "BAACAgIAAxkBAAIDhWkQn-qA7-p1mpfnlQmWmwubbbz1AAJriQACU0OJSKdh9LUpF7tcNgQ"
                ],
            "9": [
                "BAACAgIAAxkBAAIDiWkQoHaogZo31Z1aTytt1_8hagkFAAJ5iQACU0OJSBdLwgo1P35TNgQ", 
                "BAACAgIAAxkBAAIDi2kQoH1E4F1Nng-YjoDucuyPpgIJAAJ7iQACU0OJSHT4OXuQqvDtNgQ", 
                "BAACAgIAAxkBAAIDjWkQoIRR3seqfzSUG0crff7lrFsJAAJ9iQACU0OJSAeUnQK8xCszNgQ", 
                "BAACAgIAAxkBAAIDj2kQoIpkjZ0jpIccA5uY_XsTFfsEAAJ-iQACU0OJSC1HftIoHnmYNgQ", 
                "BAACAgIAAxkBAAIDkWkQoJFKOU4FBunU-x3N85UNw1zJAAKBiQACU0OJSMucV2sEyQemNgQ", 
                ],
            "10": [
                "BAACAgIAAxkBAAIDlWkQoa34d_UFkTaPyGERlkguLGRGAAKdiQACU0OJSH8Hqf7hoIZDNgQ", 
                "BAACAgIAAxkBAAIDl2kQobgI9DokLJ2TZqRV6WnpYZPfAAKeiQACU0OJSIevZU5FgcaZNgQ", 
                "BAACAgIAAxkBAAIDmWkQob_cSlhM7RtP88Xg7ImvUzUqAAKfiQACU0OJSATEnRVKLS7GNgQ", 
                "BAACAgIAAxkBAAIDm2kQocdj6Yi8jzFUbpyOWkM3FoFYAAKgiQACU0OJSJrEdBj4QvGhNgQ", 
                "BAACAgIAAxkBAAIDnWkQocwftiaGbVAGptTR2rnGZV-dAAKhiQACU0OJSLdVF3G6bNdMNgQ",
                ],
            "11": [
                "BAACAgIAAxkBAAIDoWkQosw86BzXhzr8MKFQJR9xLUrVAAKziQACU0OJSH5kk-QJMw8JNgQ", 
                "BAACAgIAAxkBAAIDo2kQots4ezutWlQJ-QGMoEcbXiY9AAK2iQACU0OJSEEwOZmQyE69NgQ", 
                "BAACAgIAAxkBAAIDpWkQouh9GDm44673thh3qygEC7MMAAK4iQACU0OJSCBVP66goDKTNgQ", 
                "BAACAgIAAxkBAAIDp2kQou_bNWz93KHGEJzRfAjk7JBHAAK6iQACU0OJSNtGLNvs6d1tNgQ", 
                "BAACAgIAAxkBAAIDqWkQovQTxCCWxPB49BEJz21JTZsUAAK7iQACU0OJSE_AeSq7i-h5NgQ", 
                "BAACAgIAAxkBAAIDq2kQovlGMuhpT02mO26h6G6nS2JUAAK8iQACU0OJSFEl-1TH7LxTNgQ",
                ],
            "12": [
                "BAACAgIAAxkBAAIDr2kQo3ZySniwcR6jy4B5-iYvrW2GAALEiQACU0OJSPm8Cz6VLDImNgQ", 
                "BAACAgIAAxkBAAIDsWkQo4hUUKp2m-CD7YwLESlfpEIFAALGiQACU0OJSDJk_BcUrKtmNgQ", 
                "BAACAgIAAxkBAAIDs2kQo41oz0eONfVfRoYkSl1pdnRqAALHiQACU0OJSCCY4lfI2S2gNgQ", 
                "BAACAgIAAxkBAAIDtWkQo5Psu8B0V9pHLb4i1XL8HcivAALIiQACU0OJSPvUSwoAAXH_KTYE", 
                "BAACAgIAAxkBAAIDt2kQo5irlDO_cLMQCBaEOqM0Vn0hAALJiQACU0OJSHYv36G_qlXkNgQ", 
                ],
        },
        "2-3": {
            "Ягодицы": [
                "BAACAgIAAxkBAAIEwWkQrJripz9L9C0ijFn0JUNKpHmOAAJ6igACU0OJSHCdMXA2u0APNgQ",
                "BAACAgIAAxkBAAIEw2kQrKdsOOesqcgpAut8_GWSU3XyAAJ8igACU0OJSMg4DBMxjyGiNgQ",
                "BAACAgIAAxkBAAIExWkQrLM1F4Ztbrxs4QOlRIAeIVb1AAJ9igACU0OJSE9aV87OtnqSNgQ",
                "BAACAgIAAxkBAAIEx2kQrL88wCGrIjyoaP8e2oT5DIFxAAJ-igACU0OJSBjO7lJz7VEHNgQ",
                "BAACAgIAAxkBAAIEyWkQrMn-irHSUDQYktpQk21bcl_bAAKCigACU0OJSAIfLpNv-LmpNgQ",
                  ],
            "Верх тела": [
                "BAACAgIAAxkBAAIFYGkiww691thaURBG3LUUQFoy5S3WAAJMiwAChUUYST1wjmnGm6AHNgQ", 
                "BAACAgIAAxkBAAIFYmkiwxcFpHAoiTzbUndiIzQfu3rdAAJNiwAChUUYSTXjvKsiByUYNgQ", 
                "BAACAgIAAxkBAAIFZGkiwx2VYHd--Clen8zCrl9NrtvOAAJOiwAChUUYSapyEMZwQ_vbNgQ", 
                "BAACAgIAAxkBAAIFZmkiwyLBWcBnoQjctlIlf30dpcceAAJPiwAChUUYSQABpQoryO6KnjYE", 
                "BAACAgIAAxkBAAIFaGkiwya2wIHvw4BBJ0sZJzpWXpoaAAJQiwAChUUYSYB5vMwOatQ1NgQ",
                "BAACAgIAAxkBAAIFamkiwyoU43uvJySNVd6KZW2SpAABAgACUYsAAoVFGEkNwcma_XraWDYE"
                ],
            "Ноги": [
                "BAACAgIAAxkBAAIFbGkiw4aBVhVEfz6512v9M6wo4cGGAAJViwAChUUYSUxn0DM_O9bVNgQ", 
                "BAACAgIAAxkBAAIFbmkiw5DYbWVszTl8W7-WCEs5c8HxAAJWiwAChUUYSQuV-5hZy1W0NgQ", 
                "BAACAgIAAxkBAAIFcGkiw5uO86OxVWbbLwLIOTQXDfURAAJXiwAChUUYSdWZZfSQXDmPNgQ", 
                "BAACAgIAAxkBAAIFcmkiw6b_haPZtRTnNahScEVOVjAcAAJZiwAChUUYSWuXZjRPK4QENgQ", 
                "BAACAgIAAxkBAAIFdGkiw68qK4oDt9fYnzmljEr_JSN4AAJbiwAChUUYSWmpNgdUC5Q8NgQ", 
                ],
        },
        "4-5": {
            "Ягодицы": [
                "BAACAgIAAxkBAAIFeGkixQds6VKnhidVw6iZTtjqT_6jAAJiiwAChUUYSVdo5vUWeQ4oNgQ",
                "BAACAgIAAxkBAAIFemkixRPrN8XBoAU6tzJruU_i_jX0AAJjiwAChUUYSbBiWHRfNPjyNgQ",
                "BAACAgIAAxkBAAIFfGkixRpmYu3NZaVbLFMjF0apVynSAAJkiwAChUUYSSVRB_Wk-Z-cNgQ",
                "BAACAgIAAxkBAAIFfmkixSFKioTR3vrdTDuKEMmd0I_gAAJliwAChUUYSVL9wop3WInWNgQ",
                "BAACAgIAAxkBAAIFgGkixSfVZ7cjv8FosDN6inNE8H9EAAJmiwAChUUYSXeLrIvDbQe5NgQ",
                "BAACAgIAAxkBAAIFgmkixS3QIbwT7Nuh-V-CP0sTb4pqAAJniwAChUUYSSlvGcam8oe9NgQ",
                ],
            "Верх тела": [
                "BAACAgIAAxkBAAIFz2kix6SWjPXJ7UugGFzfCaxhpgIIAAKHiwAChUUYSVsOlzA-KFVNNgQ",
                "BAACAgIAAxkBAAIF0Wkix6tM2dgbEQyb6gFAtYMCfOhpAAKJiwAChUUYSezVWEXQRH5XNgQ",
                "BAACAgIAAxkBAAIF02kix7D0s9jJJvYAAWvGKZ7JCHRfzwACiosAAoVFGEkYvVVyzWjPPTYE",
                "BAACAgIAAxkBAAIF1Wkix7exMS8G73jHIG9UomnpuvwYAAKLiwAChUUYSX_omD0lXcqzNgQ",
                "BAACAgIAAxkBAAIF12kix73hgpweffmNEn6INVydYF6dAAKMiwAChUUYSaWd5HTAH1dGNgQ",
                "BAACAgIAAxkBAAIF2Wkix8PjLIyf2wIngnIHH4sDmKimAAKNiwAChUUYSeWh8HIi2Op3NgQ",
                ],
            "Ноги": [
                "BAACAgIAAxkBAAIF3WkiyFha2QoorXoZTsFA3NI_zEyDAAKXiwAChUUYSbQJKMd6-L8DNgQ",
                "BAACAgIAAxkBAAIF32kiyGAQgpawqdAsX8bfgQWdPqmxAAKYiwAChUUYSVa9sXNBYgABdjYE",
                "BAACAgIAAxkBAAIF4WkiyGyqLLUtUip5XRacLMFypkzOAAKbiwAChUUYSd4P8_MlWgLuNgQ",
                "BAACAgIAAxkBAAIF42kiyHLm1WyiZZUDoYwWHiNg4GXKAAKciwAChUUYSbK-zDLN_Nb9NgQ",
                "BAACAgIAAxkBAAIF5WkiyHk3MdYlj1gYYP-ZOvDp7NZWAAKdiwAChUUYSXBWbbfFV-uMNgQ",
                ],
        },
        "6-7": {
            "Ягодицы": [
                "BAACAgIAAxkBAAIF6Wkiyoi2aKiB2ca-6CjBvXeh7QubAAK4iwAChUUYSZuE4aJWrp99NgQ",
                "BAACAgIAAxkBAAIF62kiypLeT1_CHg0QbTNEB1fMsiXsAAK5iwAChUUYSddzoUBGFdbGNgQ",
                "BAACAgIAAxkBAAIF7Wkiyp7lAx45XoAwHk0QUpJSZ-hUAAK7iwAChUUYSQawXifwBq8UNgQ",
                "BAACAgIAAxkBAAIF72kiyqgrJ2WMzUUM7SSSUakvMkF9AAK8iwAChUUYSc7XaCK7q3m6NgQ",
                "BAACAgIAAxkBAAIF8WkiyrDbSNnrZXLpO2jv5cy-nxqoAAK9iwAChUUYSUgqWFDzfML-NgQ",
                "BAACAgIAAxkBAAIF82kiyrmfzdHzparsXzIRDlcFoUbKAAK-iwAChUUYSVPym2Jzv3fKNgQ",
                "BAACAgIAAxkBAAIF9WkiysVN_jR1WlpWH3pXGjgJUidQAAK_iwAChUUYSVQXCWBzfUusNgQ",
                "BAACAgIAAxkBAAIF92kiyswqgIwp0TeCXDSoTfrNT9pzAALAiwAChUUYST86FChtPp_vNgQ",
                "BAACAgIAAxkBAAIF-WkiytGWJo71WMqbfDSirvKxTyrRAALBiwAChUUYScInv3VX8tBtNgQ",
                ],
            "Верх тела": [
                "BAACAgIAAxkBAAIF_Wkiy7G5M96E_k1GnhvbLmDiF3mpAALPiwAChUUYSZihBZb2vhxYNgQ",
                "BAACAgIAAxkBAAIF_2kiy7kbyCOH-1i4ZhN1pfIZGbXxAALRiwAChUUYSYpLS-z12OQwNgQ",
                "BAACAgIAAxkBAAIGAWkiy7-ns3tqHeUf0GrgVp3_MeNXAALSiwAChUUYSXZTFm3j2t6vNgQ",
                "BAACAgIAAxkBAAIGA2kiy8UDdjGr2wXJzNUhWhA2GmJkAALViwAChUUYSfCB4gtwhKQ1NgQ",
                "BAACAgIAAxkBAAIGBWkiy8rE82z56rrxOzcq7L7CAAERnAAC1osAAoVFGElpGLf3SX7HiTYE",
                "BAACAgIAAxkBAAIGB2kiy9CeIXSXZP9Ccc1lidAOGsDnAALXiwAChUUYSXl66LcAAegvVjYE",
                "BAACAgIAAxkBAAIGCWkiy9TxmIjIPWz2Ougr3HYumJD9AALYiwAChUUYSdCTAa6tSQphNgQ",
                "BAACAgIAAxkBAAIGC2kiy9wF0KM2UcN_xP-tojW7e5GdAALZiwAChUUYSSIoYDE_wKdNNgQ",
                "BAACAgIAAxkBAAIGDWkiy-G3daLc3rP8tqXLWnK_jTDiAALbiwAChUUYSR7Wd81AQoC3NgQ",
                ],
            "Ноги": [
                "BAACAgIAAxkBAAIGEWkizLJ-5jGbmxyvhbJRA0CqiJyXAALriwAChUUYScg0TFNzC5FuNgQ",
                "BAACAgIAAxkBAAIGE2kizLn_6MV_9MMh6IWkQxtzSR8JAALsiwAChUUYSZIoS7SIsl53NgQ",
                "BAACAgIAAxkBAAIGFWkizMGOdx417YFewI2xcCzfbft5AALtiwAChUUYSQyf5i1a3CoINgQ",
                "BAACAgIAAxkBAAIGF2kizMbsRmbaIwABK87ScfIRT99RqgAC74sAAoVFGEncQTcd6JSCjzYE",
                "BAACAgIAAxkBAAIGGWkizMpfMGgvR7RewXofvdKK1pyBAALwiwAChUUYSTD-Nzkx2QtKNgQ",
                "BAACAgIAAxkBAAIGG2kizM_y694MdMmPesaC4ZIKqWOLAALxiwAChUUYSWLhkTFQYOPXNgQ",
                ],
        },
        "8-9": {
            "Ягодицы": [
                "BAACAgIAAxkBAAIGH2kizYeBXfpVdHDuuo4cA-KmG9t8AAOMAAKFRRhJpcaE44i0qXA2BA",
                "BAACAgIAAxkBAAIGIWkizZRz8ZU5tnWhKM52rTJAUwL-AAIBjAAChUUYSRcl6uXxpxw2NgQ",
                "BAACAgIAAxkBAAIGI2kizZte51USDdymq2j6Lqljdd2-AAIDjAAChUUYSZm9B1hN8TzuNgQ",
                "BAACAgIAAxkBAAIGJWkizaPMnVQHuboMFGO5_LUn_JaBAAIEjAAChUUYSSDGl32ePuSsNgQ",
                "BAACAgIAAxkBAAIGJ2kizagEdfaI6sig-LthWUi3nyAxAAIFjAAChUUYSU2sBZqxmV2lNgQ",
                "BAACAgIAAxkBAAIGKWkiza9Xkchyrq4g6C3L4Bs85XJYAAIGjAAChUUYSReFtwLu6DFMNgQ",
                ],
            "Верх тела": [
                "BAACAgIAAxkBAAIGLWkizjnshddeJP4zn4vwOCdvEGVlAAIRjAAChUUYSaDtqH1jsxjRNgQ",
                "BAACAgIAAxkBAAIGL2kizkKaY9TOo4cl-PDdZ_wlqO44AAITjAAChUUYScbMxj-l2LceNgQ",
                "BAACAgIAAxkBAAIGMWkizkpO64qtIIAW_qT0bNF4YU2EAAIUjAAChUUYSXsTGWbk3Y4-NgQ",
                "BAACAgIAAxkBAAIGM2kizk82t2OJWTk4Y_21L5s4ZmihAAIVjAAChUUYSXKtLA8Y9LflNgQ",
                "BAACAgIAAxkBAAIGNWkizlZU65AwAcTuc9mzVfk67A8XAAIWjAAChUUYSbosjW4IMkCcNgQ",
                "BAACAgIAAxkBAAIGN2kizlvVdKlByOU9yVLRQRJJuZ1dAAIXjAAChUUYScEpX_abh9EAATYE",
                "BAACAgIAAxkBAAIGOWkizmHjrwHnYh-LVB7UXR8_UzdqAAIYjAAChUUYSTX8cVGKNYkMNgQ",
                "BAACAgIAAxkBAAIGO2kizmbNtLFWEc0OT8GbmozsrL7AAAIZjAAChUUYSULhSp5vJ141NgQ",
                ],
            "Ноги": [
                "BAACAgIAAxkBAAIGP2kizzdSypIrmXVbnARfW0db7-YaAAIfjAAChUUYSZNwFPXR2izKNgQ",
                "BAACAgIAAxkBAAIGQWkiz0BRPcXvFjLycAG1YyPPA2seAAIgjAAChUUYSYDXXkpWRLZzNgQ",
                "BAACAgIAAxkBAAIGQ2kiz0WdC2a6Xy8jrP6T3P6ooG1hAAIhjAAChUUYSaFGgvboMSOYNgQ",
                "BAACAgIAAxkBAAIGRWkiz0rgaj1dmvPaGcVZDBywIxNZAAIijAAChUUYSYxyt-en5xpHNgQ",
                "BAACAgIAAxkBAAIGR2kiz1B4vyzanW4iJzLVCU4P5gkkAAIjjAAChUUYSbexp7-2OgShNgQ",
                "BAACAgIAAxkBAAIGSWkiz1aVx6ZkCCVUzTwO824qHcr8AAIkjAAChUUYSZtYBhTNri_ONgQ",
                ], 
        },
        "10-12": {
            "Ягодицы": [
                "BAACAgIAAxkBAAIGTWki0BdlXnlFGKXxVjzNAvBCe4QuAAIvjAAChUUYSRIXyVbin7yENgQ",
                "BAACAgIAAxkBAAIGT2ki0B8Tqz4s2_iepgsxV6y-ufaGAAIxjAAChUUYSab0tOd6N7foNgQ",
                "BAACAgIAAxkBAAIGUWki0CbARChEIIzSUlHgpOWsbT9AAAIyjAAChUUYScOknc-o01AwNgQ",
                "BAACAgIAAxkBAAIGU2ki0C0_a-K-LFEgiaMgE7nZ5kJUAAIzjAAChUUYSV_x18twbGinNgQ",
                "BAACAgIAAxkBAAIGVWki0DJ-saQJfEKmKQcHpjRHgE6SAAI0jAAChUUYSaqRBnhkuOunNgQ",
                "BAACAgIAAxkBAAIGV2ki0DscoxuN6Uod2ElGCpShwowSAAI1jAAChUUYSV3jqII474-HNgQ",
                ],
            "Верх тела": [
                "BAACAgIAAxkBAAIGW2ki0M0s24LtriVtHLeAl59UehHqAAI6jAAChUUYSdMGnyQzkXeINgQ",
                "BAACAgIAAxkBAAIGXWki0NI5ykvfqu1dqE2qFEhUpWj2AAI8jAAChUUYSYFwYxPRH9N5NgQ",
                "BAACAgIAAxkBAAIGX2ki0Ndkyg6eoNlS0T2Kf1yzb8C1AAI9jAAChUUYSVN_BYV0lIjoNgQ",
                "BAACAgIAAxkBAAIGYWki0NsyFJsEKlVCSWdCPXnWE-CbAAI-jAAChUUYSbCv-KYPzj4pNgQ",
                "BAACAgIAAxkBAAIGY2ki0N98zEVLvdHROmhPDVBgiF07AAI_jAAChUUYScrH0vWgjM1xNgQ",
                "BAACAgIAAxkBAAIGZWki0ONibrtL0HfL_y1MjFnK4I5_AAJBjAAChUUYSbIf-IKAYRJ-NgQ",
                "BAACAgIAAxkBAAIGZ2ki0OeQJEjPl2iGAyYRynUkohsmAAJDjAAChUUYSRdX4nXEoaMGNgQ",
                ],
            "Ноги": [
                "BAACAgIAAxkBAAIGa2ki0X1ErGZ3a-N6G6VK2ItQFlOqAAJbjAAChUUYSS5ntDxTdO1nNgQ",
                "BAACAgIAAxkBAAIGbWki0YW1Io9NiztUGBv7VaxgNJrbAAJcjAAChUUYScb0lqxWa6mONgQ",
                "BAACAgIAAxkBAAIGb2ki0Yv6sDoWyP6uAczfbO-6FVAcAAJejAAChUUYSdf--lEBYi2hNgQ",
                "BAACAgIAAxkBAAIGcWki0ZHt32NrSdneEhUuauPe5f5wAAJfjAAChUUYScz076kCxBD8NgQ",
                "BAACAgIAAxkBAAIGc2ki0ZehTaYZ2DrJdNUDO7BbFIvNAAJgjAAChUUYSZdQq6Usg51zNgQ",
                "BAACAgIAAxkBAAIGdWki0ZyJKl3S1jiT1Uw1n01wz0-cAAJhjAAChUUYSQF7K7OAyA2xNgQ",
                "BAACAgIAAxkBAAIGd2ki0aAKR237-bAxZ13iIvnOzdCMAAJijAAChUUYSTGTtIU1tze3NgQ",
                ],
        },
    },
    "home": {
        "1": {
            "1": [
                "BAACAgIAAxkBAAIGn2ki5zdiii-KWP2szpM4AAG9zvuTwQACDI4AAoVFGEmDRYtq1EspYjYE",
                "BAACAgIAAxkBAAIGoWki505oFuC6R_1VYKS-UI4QioLrAAIOjgAChUUYSULEbdTKft9ZNgQ",
                "BAACAgIAAxkBAAIGo2ki51j_a-MAAW53I80AAUsIRi2ZZtEAAg-OAAKFRRhJ6q782RBowIs2BA",
                "BAACAgIAAxkBAAIGpWki52PKvjgU43Kym0D7TfYz6rr-AAIQjgAChUUYSVEGPlp-Za8YNgQ",
                "BAACAgIAAxkBAAIGp2ki5205art9gWM6vpr0BOcrN_UfAAIRjgAChUUYSVLtHxy6PdtANgQ",
                "BAACAgIAAxkBAAIGqWki53FwarV5QTa3ZzC4MEB0Yrp-AAISjgAChUUYSfjRz05J6jNdNgQ",
            ],
            "2": [
                "BAACAgIAAxkBAAIHG2ki7FLlJIGjWKAWsxdCR224nO_yAAJhjgAChUUYSYgRx8VDUS2fNgQ",
                "BAACAgIAAxkBAAIHHWki7Fuq2ufqWnC4ZTjP_nkpqZlxAAJijgAChUUYSXQPokzpvw6GNgQ",
                "BAACAgIAAxkBAAIHH2ki7GcGg7WjxHgtw8dgiFcqY9HkAAJjjgAChUUYSbUDm_KtXzZONgQ",
                "BAACAgIAAxkBAAIHIWki7HDqCK5jZ3AOlDCcDiOgtPYkAAJkjgAChUUYSSsQrn6QPS8xNgQ",
                "BAACAgIAAxkBAAIHI2ki7HroFBE-8th2G0MeKJW1Q4IvAAJljgAChUUYSRTg1u7XTSvNNgQ",
            ],
            "3": [
                "BAACAgIAAxkBAAIHNmki7R12ROjYZ15qEENz64RoS3R1AAJrjgAChUUYSfHVfbmxR_u5NgQ",
                "BAACAgIAAxkBAAIHOGki7SJ0ysMoHotimEVx-lyOjyH-AAJsjgAChUUYSS-aK5ABYcI-NgQ",
                "BAACAgIAAxkBAAIHOmki7SqhJ3VpzRHMQPBs_ryThV5GAAJvjgAChUUYSdmA7iCeldy-NgQ",
                "BAACAgIAAxkBAAIHPGki7TAUgRoVCw7yIY1-VhJ4hUiBAAJxjgAChUUYSUGLPZ88UseKNgQ",
                "BAACAgIAAxkBAAIHPmki7TTySyXeA770xVQu2abpjszyAAJyjgAChUUYSdAc3DTp2SSyNgQ",
            ],
            "4":[
                "BAACAgIAAxkBAAIHQGki7a_v98FAFs8fRmRiB6jbDNP7AAJ8jgAChUUYSXc792DI2rKBNgQ",
                "BAACAgIAAxkBAAIHQmki7bm4yBTv8Nc1VrU30j6Yhi0NAAJ-jgAChUUYST-b12NXQ-EKNgQ",
                "BAACAgIAAxkBAAIHRGki7b7kpvS2ynhCg26rAAH3f8ZZwwACf44AAoVFGElqxp3UzbbZZjYE",
                "BAACAgIAAxkBAAIHRmki7cS2rm9EqB-lZ7VICTU48xOmAAKBjgAChUUYSSRVnR9Ltg4mNgQ",
                "BAACAgIAAxkBAAIHSGki7cmYIai8_Cc3jP5Vxua9-oU_AAKGjgAChUUYSfY5ScjPxienNgQ",
            ],
            "5":[
                "BAACAgIAAxkBAAIHWWki7mCHGg-9e10MnrmTBI_FRpJgAAKYjgAChUUYSSBTHZTnbB00NgQ",
                "BAACAgIAAxkBAAIHW2ki7mbWjQLJPEVcgsuUbI8Hg2gLAAKZjgAChUUYSYtykd7WZX-8NgQ",
                "BAACAgIAAxkBAAIHXWki7mrONzM8j6GKK_E0v4lX2oe4AAKajgAChUUYSdfKRpvtRN11NgQ",
                "BAACAgIAAxkBAAIHX2ki7m_7Iej9uKRUH6QuH5NE5XJsAAKbjgAChUUYSeTSu_qoYwJyNgQ",
                "BAACAgIAAxkBAAIHYWki7nOsox8MkGKBNQtublm0h5uuAAKcjgAChUUYSct3u1CC5pvjNgQ",
            ],
            "6":[
                "BAACAgIAAxkBAAIHY2ki7ypbrU4V-7uNRCi7bWhigshAAAKsjgAChUUYSS8iSBSavIlNNgQ",
                "BAACAgIAAxkBAAIHZWki7y-qX26yUyb4Ax-iDHPNw5BPAAKtjgAChUUYScAx0R72SOSJNgQ",
                "BAACAgIAAxkBAAIHZ2ki7zWpsOrZ1uC_sqvPSOicpdYLAAKvjgAChUUYSYEkt4-H094INgQ",
                "BAACAgIAAxkBAAIHaWki7zkBJPgcA0XgGDExPM3HgBGvAAKwjgAChUUYSQ8COhbXth8dNgQ",
                "BAACAgIAAxkBAAIHa2ki7zxGaVmPkixtwD2uOUr5rRDsAAKxjgAChUUYSQ0kE0yE1romNgQ",
            ],
            "7":[
                "BAACAgIAAxkBAAIHbWki87S-qyMktiwiS1-nPBHPimi7AALzjgAChUUYSSMlnPhs-goqNgQ",
                "BAACAgIAAxkBAAIHb2ki87uAivz_jJZuP-uml0ltVYmrAAL0jgAChUUYSWXNBlr1FnwcNgQ",
                "BAACAgIAAxkBAAIHcWki88Fx1w2W56rHj-C1m4uaXacOAAL1jgAChUUYScyyFmxostOSNgQ",
                "BAACAgIAAxkBAAIHc2ki88qTW1qaxLj2oog5a1SbBaoHAAL2jgAChUUYSbwc22jRUs8VNgQ",
                "BAACAgIAAxkBAAIHdWki89FTibFsDjykvpK87lWnEJRQAAL3jgAChUUYSQABErZrUeh5mTYE",
                "BAACAgIAAxkBAAIHd2ki89Y4T2gw5ClkQaOb-W5NgGl4AAL4jgAChUUYSTldukMON5cPNgQ",
            ],
            "8":[
                "BAACAgIAAxkBAAIHeWki9HP7BNoMmeuVWdUwvSvLytyLAAIGjwAChUUYSXjGmcDUB74tNgQ",
                "BAACAgIAAxkBAAIHe2ki9HqfFZzmxqNEUFY4sA4Md3yMAAIHjwAChUUYSXeh8slwhgpxNgQ",
                "BAACAgIAAxkBAAIHfWki9IkcfNoU_IJ_5E0RzWrZ6b7NAAIIjwAChUUYSUGc-Pg4P3SeNgQ",
                "BAACAgIAAxkBAAIHf2ki9JQIq5Cez2to79P17Y-lvvO9AAIJjwAChUUYSXymDGfYoBugNgQ",
                "BAACAgIAAxkBAAIHgWki9J4c89q06gYS7kfCPlBAxqsDAAIKjwAChUUYSTpVqkXfS850NgQ",
                "BAACAgIAAxkBAAIHg2ki9LgWGSUoso_whRtDbE3CExPTAAILjwAChUUYSe-HKe7POT11NgQ",
            ],
            "9":[
                "BAACAgIAAxkBAAIHhWki9Qs5HDr70bSNxQ7HaBTO7M6bAAIQjwAChUUYSYdGL_hX73idNgQ",
                "BAACAgIAAxkBAAIHh2ki9RkP1X5frihDzvXnby9aDJvyAAIRjwAChUUYSQmkP_riqG76NgQ",
                "BAACAgIAAxkBAAIHiWki9SfEZ4MPMU0LWsjQfc5ma2czAAISjwAChUUYSXMTqgMm67HUNgQ",
                "BAACAgIAAxkBAAIHi2ki9TPThn6peGZU1Fa5txth72kfAAITjwAChUUYSXQrt4X8ith4NgQ",
                "BAACAgIAAxkBAAIHjWki9T75WBkUgxM5BX3hHsqNuJ6eAAIVjwAChUUYSVKuI9OUGkgMNgQ",
            ],
            "10":[
                "BAACAgIAAxkBAAIHj2ki9YeWAvnYK8beIms10Y3RUeylAAIYjwAChUUYSfRH89TXZHq4NgQ",
                "BAACAgIAAxkBAAIHkWki9ZbovEYeovKxAXN_5CW5md7ZAAIZjwAChUUYSdJjY1w2_GUeNgQ",
                "BAACAgIAAxkBAAIHk2ki9aHiJaEU0YiYkQW7fzYCHzg5AAIajwAChUUYSfV7tNvkNmBoNgQ",
                "BAACAgIAAxkBAAIHlWki9axq8EWLpUsHeVsatZesw1QFAAIcjwAChUUYScDj1I4CiZ_UNgQ",
                "BAACAgIAAxkBAAIHl2ki9bbm3GHlNBeiAns3xBPwtdrUAAIejwAChUUYSWQMT2jfjepaNgQ",
            ],
            "11":[
                "BAACAgIAAxkBAAIHmWki9hJUvOKFYvkC2J5kvcvOQSp9AAIgjwAChUUYSZSV-pTZ7sV9NgQ",
                "BAACAgIAAxkBAAIHm2ki9hycHnEWaYCff3IPJH-WhIDFAAIijwAChUUYSayyOWhgsypMNgQ",
                "BAACAgIAAxkBAAIHnWki9iXrMc5YjY4s2cQiioxC5BmIAAIljwAChUUYSaiubp6mv0Z0NgQ",
                "BAACAgIAAxkBAAIHn2ki9jCm5oFS6TViL5d3xunSkcP7AAImjwAChUUYSVwH74GYO8YsNgQ",
                "BAACAgIAAxkBAAIHoWki9jrvvVurSWrdGqtSqeNn_UclAAInjwAChUUYSY7XrCm4eHd7NgQ",
                "BAACAgIAAxkBAAIHo2ki9khbwKrMzv4gFASF_avkhdZlAAIpjwAChUUYSWXdcrCVnfx2NgQ",
            ],
            "12":[
                "BAACAgIAAxkBAAIHpWki9qglZ4p5X0GKqjXXBW51gFfxAAItjwAChUUYSbLfHfm1OQ15NgQ",
                "BAACAgIAAxkBAAIHp2ki9rXc4fKWogetfzG8fDzKsc0aAAIxjwAChUUYSYAEwJs2t_0JNgQ",
                "BAACAgIAAxkBAAIHqWki9sJ0X9oN3mT0_A__7ssEBeP_AAIyjwAChUUYSd1zSSmBFAABmjYE",
                "BAACAgIAAxkBAAIHq2ki9s8JnpjtXc47FQSERchLSJC_AAIzjwAChUUYSd_Zh4VTAdqHNgQ",
                "BAACAgIAAxkBAAIHrWki9t6bNTwfuLZjF4qnHQk7mz_9AAI0jwAChUUYST6BRAdggjGVNgQ",
            ],


        },
        "2-3": {
            "Ягодицы":[
                "BAACAgIAAxkBAAIH0mkjDQVvWtOP3l6Q2QABhQpbFgyhtAACtYcAAoVFIEl3ZgEe5FA5hTYE",
                "BAACAgIAAxkBAAIH1GkjDRnOjAtFLA877jsM1wj3xnVPAAK4hwAChUUgSc-c9LSRXMYYNgQ",
                "BAACAgIAAxkBAAIH1mkjDS76UC1GcYWmDhIXt7P94tKqAAK6hwAChUUgSSqP5Z3MibKmNgQ",
                "BAACAgIAAxkBAAIH2GkjDTRS0hqKKztM8AJiJpv_PZTHAAK7hwAChUUgSSk4-oLqdtpBNgQ",
                "BAACAgIAAxkBAAIH2mkjDT0UqtauMhY5yQG11TikWR3NAAK8hwAChUUgSRzGXyCSOnRbNgQ",
            ],
            "Верх тела":[
                "BAACAgIAAxkBAAIH3GkjDZUjBFFQWjJZomrx-ERXGG4pAALGhwAChUUgScif4ESxKvuiNgQ",
                "BAACAgIAAxkBAAIH3mkjDaHzrQYg_ozLN76GMYyMnw3iAALKhwAChUUgSVgWJnZcdCwdNgQ",
                "BAACAgIAAxkBAAIH4GkjDazzDEGlAXJE4P0Rv3dzNcpAAALLhwAChUUgSaIGzctrU1F5NgQ",
                "BAACAgIAAxkBAAIH4mkjDbQiFdMVdFNTT3BtI2n4ywgZAALMhwAChUUgSeLKoRJKUZLINgQ",
                "BAACAgIAAxkBAAIH5GkjDb42M14JJaaOiVP3J2aBV1RQAALNhwAChUUgSc9Aay-PhzCTNgQ",
                "BAACAgIAAxkBAAIH5mkjDdS-harmJp25LN9Oe_JwGjnYAALQhwAChUUgSR8-P2yMiVPeNgQ",
            ],
            "Ноги":[
                "BAACAgIAAxkBAAII4Wkkjv1mqpJXia0NbzBSnNdw-EkYAAJKiQAC2eApSVzvG-3OWeexNgQ",
                "BAACAgIAAxkBAAII42kkjwW1A-ynw9rHtGtwdiFkMxnxAAJLiQAC2eApSfEr_8b45MWpNgQ",
                "BAACAgIAAxkBAAII5Wkkjwxy4Ky1wIsp9pOVa27aRJ8iAAJMiQAC2eApSchuOYUB1q_eNgQ",
                "BAACAgIAAxkBAAII52kkjxK3e7AT5DDoqWs7xNzL1083AAJNiQAC2eApSRwMW7wfWEU4NgQ",
                "BAACAgIAAxkBAAII6WkkjxfxmOFOr_qu1a3DtVnPxmf8AAJOiQAC2eApSZSjMh63ndiiNgQ",
            ],
        },
        "4-5": { 
            "Ягодицы":[
                "BAACAgIAAxkBAAII62kkj3RH4EYa9U4V63-eqzWtQNEKAAJRiQAC2eApSTG5svjeSaeUNgQ",
                "BAACAgIAAxkBAAII7Wkkj3yW3o1XfKzYdHzaj3fJ5tkMAAJSiQAC2eApSVhqCmJm62UeNgQ",
                "BAACAgIAAxkBAAII72kkj47VogrE_MwBOcI9bHWvsQG4AAJTiQAC2eApSS8n0eHvUxHDNgQ",
                "BAACAgIAAxkBAAII8Wkkj5zCH4opQhaIHPmMRwMcVwRIAAJViQAC2eApSWrXldf2Wj8mNgQ",
                "BAACAgIAAxkBAAII82kkj6WrAAF-AvZp7nkAAX4XhMmxEUEAAlaJAALZ4ClJwbrHVXyfCf82BA",
                "BAACAgIAAxkBAAII9Wkkj7L6Yg17ydBrVLv7ER6-iqmwAAJYiQAC2eApSZ6Z9VssWdBsNgQ",
            ],
            "Верх тела":[
                "BAACAgIAAxkBAAII92kkkCbscsca0ElcW-ZzOoyjz1CQAAJciQAC2eApSU8YnHBTWZRgNgQ",
                "BAACAgIAAxkBAAII-WkkkDd-OYWd6WYqIX-TBV_JhpUvAAJdiQAC2eApScOddWmdDOpwNgQ",
                "BAACAgIAAxkBAAII-2kkkEWH2F3-GjkcCVSlSJbAZVqjAAJeiQAC2eApSR25nG3Uu_EcNgQ",
                "BAACAgIAAxkBAAII_WkkkFWcuq-UhT_Rh57T31bQUfMEAAJgiQAC2eApSZxvZ-fOGdVcNgQ",
                "BAACAgIAAxkBAAII_2kkkGHyQtsB4lghDP_h5NBGTAmOAAJhiQAC2eApSbOku4w4N2PiNgQ",
                "BAACAgIAAxkBAAIJAWkkkGsrpis-u03IatQZkNndD211AAJiiQAC2eApSS4LiMnztBpuNgQ",
            ],
            "Ноги":[
                "BAACAgIAAxkBAAIJA2kkkLxDdIaYIKEixNr2lm0i-8V8AAJkiQAC2eApSSUzXaRMSuHkNgQ",
                "BAACAgIAAxkBAAIJBWkkkMZ4vxXxPxtlaCN7Yz9l8KEpAAJliQAC2eApSfgfoDWjPG4aNgQ",
                "BAACAgIAAxkBAAIJB2kkkNH4wY5F05yZsYzZ_NO6JrP4AAJmiQAC2eApSbA18dosaVuVNgQ",
                "BAACAgIAAxkBAAIJCWkkkNo_dG6mgrBMXN3y8veSwjlrAAJniQAC2eApSaNAW3LxtH75NgQ",
                "BAACAgIAAxkBAAIJC2kkkOW8Sqb1LugpzVZOM8B97jWcAAJoiQAC2eApSWKVfLE1znU-NgQ",
            ],
        },
        "6-7": {
             "Ягодицы":[
                "BAACAgIAAxkBAAIJDWkkkZxkk-SoFBBORVzGO43rmCUhAAJuiQAC2eApSYk-IR9VDyGnNgQ",
                "BAACAgIAAxkBAAIJD2kkka2cE_tN0sLerSCivqqGpRnGAAJyiQAC2eApSVmm3W1R5WC-NgQ",
                "BAACAgIAAxkBAAIJEWkkkbdX-2Ftey9l03wcHic-BClIAAJziQAC2eApSZ4hpDQyeOh8NgQ",
                "BAACAgIAAxkBAAIJE2kkkb_1Adau9HaC7LTC1qbyMNAHAAJ1iQAC2eApSQ09VL8f6mT_NgQ",
                "BAACAgIAAxkBAAIJFWkkkcxC5VxY_a-1O88ZZiw8CnpwAAJ2iQAC2eApSVvKcqxutkc5NgQ",
                "BAACAgIAAxkBAAIJF2kkkdb9mMcRQ5CHd5xCR7BQ7Vx2AAJ3iQAC2eApSZTu2DUHr1D9NgQ",
                "BAACAgIAAxkBAAIJGWkkkd4I7OLop23EFtTnG-L87hHuAAJ6iQAC2eApSTiI2YCMNTsbNgQ",
                "BAACAgIAAxkBAAIJG2kkkefU7iv8YFDFUK4q7915q061AAJ7iQAC2eApSeXwMkYC_GdvNgQ",
            ],
            "Верх тела":[
                "BAACAgIAAxkBAAIJHWkkkmV3gTNxxGXglcf10rsI6U92AAKHiQAC2eApSe7_AcDySqYeNgQ",
                "BAACAgIAAxkBAAIJH2kkknPDGI1RnwAB_trZgtA2b_HL9wACiYkAAtngKUn2w9pG3iEOMjYE",
                "BAACAgIAAxkBAAIJIWkkkn570QIEjQAB5R_06r0lKVYAAT8AAouJAALZ4ClJfopkj6NtDyA2BA",
                "BAACAgIAAxkBAAIJI2kkkokw70dtC2J7u47jw_Mj0tXyAAKOiQAC2eApSeVNx6AmHdWCNgQ",
                "BAACAgIAAxkBAAIJJWkkkpL3_wErr_FtjXfGsNwAAa_TRgACkokAAtngKUkGgJ05DkLwITYE",
                "BAACAgIAAxkBAAIJJ2kkkpuqLNgJ3GzaGkVhu67z0ZplAAKViQAC2eApSVrdZEZ0Md0VNgQ",
                "BAACAgIAAxkBAAIJKWkkkqV5_Fe1OL4zBXpRmiRJgcugAAKXiQAC2eApSUeAqiQWIwKtNgQ",
                "BAACAgIAAxkBAAIJK2kkkq8TlN2uRcfPthGYf5vWheN0AAKYiQAC2eApSd4yl6UKoD5iNgQ",
                "BAACAgIAAxkBAAIJLWkkkrjIP1S-taJLkSXbEvmYMLakAAKaiQAC2eApSdt04xAKZhGYNgQ",
            ],
            "Ноги":[
                "BAACAgIAAxkBAAIJL2kkkyIFhD116XKOIzkT3YoqUXj2AAKiiQAC2eApSc2LYwFtPRiQNgQ",
                "BAACAgIAAxkBAAIJMWkkky4fGN7DjNz87n3dAAEhtE7ewgACo4kAAtngKUlg6CVPDXPUfDYE",
                "BAACAgIAAxkBAAIJM2kkkzgFmu_B652mMFhJZnQKiyhfAAKliQAC2eApSQ-7UNsqvHvZNgQ",
                "BAACAgIAAxkBAAIJNWkkk0HzsyzpYkak91SuINlDQdzGAAKmiQAC2eApSRDQIsxuEUy2NgQ",
                "BAACAgIAAxkBAAIJN2kkk0vkySAi1nDuXgxGvKyLbipCAAKniQAC2eApSZUHWagy1qW1NgQ",
                "BAACAgIAAxkBAAIJOWkkk1RVSu3CtMhtA0U4dE5jOKkrAAKoiQAC2eApSaPSzdxdHSkENgQ",
            ],
        },
        "8-9": {
             "Ягодицы":[
                "BAACAgIAAxkBAAIJO2kkk7kOB8WYvxJSmrcEijGjrnp8AAK0iQAC2eApSVoidwABE0wCuzYE",
                "BAACAgIAAxkBAAIJPWkkk8dU7T8ndcd0dWCQLr2nul_vAAK2iQAC2eApSV512rNzqfDuNgQ",
                "BAACAgIAAxkBAAIJP2kkk9Ir9MrL5VFBjM81PLPVLiysAAK3iQAC2eApSYRc2LSI6n7NNgQ",
                "BAACAgIAAxkBAAIJQWkkk92W7GRBDbb0pZfI5DEdBBfgAAK4iQAC2eApScfq3IfTAzn8NgQ",
                "BAACAgIAAxkBAAIJQ2kkk-aAKrSHtn-uw_VcYK4Xm_w5AAK6iQAC2eApST5CZH2qDxMjNgQ",
            ],
            "Верх тела":[
                "BAACAgIAAxkBAAIJRWkklFIfPAge7HpAy2asY6uMxNO3AALGiQAC2eApSWLNDLuN1DiUNgQ",
                "BAACAgIAAxkBAAIJR2kklGGAvrB3qTNZVEe753jCY7ThAALIiQAC2eApSR27k1MYxGWbNgQ",
                "BAACAgIAAxkBAAIJSWkklHCuyBQp3QtMlh2oKPKQzE7HAALLiQAC2eApScNtBzDlGlFmNgQ",
                "BAACAgIAAxkBAAIJS2kklHpfQl7nNZDfPotIgMpFshVoAALNiQAC2eApSQuMB5EscqUJNgQ",
                "BAACAgIAAxkBAAIJTWkklIPoWtfCimNIoLrZ-SFFw-yNAALPiQAC2eApSQKcVxjJdcERNgQ",
                "BAACAgIAAxkBAAIJT2kklI0Ln_St9t-YtyRAlqXsonOlAALRiQAC2eApSeWGEhalVI93NgQ",
                "BAACAgIAAxkBAAIJUWkklJYUuxBZX8ZYDsGj2sjKDG6gAALViQAC2eApSXR47CbETFcgNgQ",
                "BAACAgIAAxkBAAIJU2kklKBKbHExXtUmBLHbnJU1RCgaAALXiQAC2eApSYOSqOyy4xJfNgQ",
            ],
            "Ноги":[
                "BAACAgIAAxkBAAIJVWkklOplOO_IhQiESIJYWKArliH0AALdiQAC2eApSWvfKd6VcZIGNgQ",
                "BAACAgIAAxkBAAIJV2kklPUOGsajbSrpX5SzNxcFWAn1AALgiQAC2eApSehTaY16Jc0nNgQ",
                "BAACAgIAAxkBAAIJWWkklP7wRKNsAxI0t3vtpPlbEVkIAALiiQAC2eApSd-TlEskJG6ANgQ",
                "BAACAgIAAxkBAAIJW2kklQdQsxQFwHTUFyz4_gsLWuMPAALkiQAC2eApSf-sPEy5nJRsNgQ",
                "BAACAgIAAxkBAAIJXWkklRD1ZAPQo_2Thg9vupWQtULBAALliQAC2eApSdtG4Y1O1T4UNgQ",
            ],
        },
        "10-12": {
             "Ягодицы":[
                "BAACAgIAAxkBAAIJX2kklWVWPl6sjFocQEQfzovLZpEOAALqiQAC2eApSRLysvmnViy4NgQ",
                "BAACAgIAAxkBAAIJYWkklW5EQBwSniZQemlBVXCm2CzhAALriQAC2eApSXo1jwrbexn5NgQ",
                "BAACAgIAAxkBAAIJY2kklXebUXLx6TFEJ3WZmcRls2GuAALtiQAC2eApSa357bCpfv3hNgQ",
                "BAACAgIAAxkBAAIJZWkklYC07ImdfUE76Uj6CUvCJIEWAALviQAC2eApSVseKt9HR8OINgQ",
                "BAACAgIAAxkBAAIJZ2kklYhXLBDAlNP1HaH96l87XAZQAALxiQAC2eApSU-NlPc3BJkINgQ",
            ],
            "Верх тела":[
                "BAACAgIAAxkBAAIJaWkklckcS35loAN3ocEzdiXXzlPKAAL4iQAC2eApSRdNLzJYGIfaNgQ",
                "BAACAgIAAxkBAAIJa2kklddlpRFQV9iiDnv13RYk7yfUAAL8iQAC2eApSWrmoDgF2oYmNgQ",
                "BAACAgIAAxkBAAIJbWkkleME-Lgi6FoaGShlid-OChcjAAL_iQAC2eApSYm6SJTNNMocNgQ",
                "BAACAgIAAxkBAAIJb2kklfAXh964WBUEWbXcPRSIH9tWAAOKAALZ4ClJCD82tlsGQYs2BA",
                "BAACAgIAAxkBAAIJcWkklfleSnOuZ3GQzhNsJh6GmVtmAAICigAC2eApSZjeocbEQ6z9NgQ",
                "BAACAgIAAxkBAAIJc2kklgPC4TpaqvmD3Lb1dkOb2OS2AAIEigAC2eApSYlocoF1h0KcNgQ",
                "BAACAgIAAxkBAAIJdWkklgzCQVqmaIiFKxEJY2aR0a0qAAIFigAC2eApSSg9vTKJCtefNgQ",
            ],
            "Ноги":[
                "BAACAgIAAxkBAAIJd2kkltAKXfMuCiakeP5sVLp5IL7PAAIMigAC2eApSdDXCri9EAABCTYE",
                "BAACAgIAAxkBAAIJeWkklthsxDCOnlaoHwJ98-JPL96lAAINigAC2eApSbRuXs18NTTtNgQ",
                "BAACAgIAAxkBAAIJe2kkluK5xyrjXaMDnMQJ4eetgg_RAAIPigAC2eApSTNhP7Rsdl3bNgQ",
                "BAACAgIAAxkBAAIJfWkklu5cNtT2CSgHnF6S8wT0JRnqAAIQigAC2eApSYJvs_SR1CNrNgQ",
                "BAACAgIAAxkBAAIJf2kklvpT0uA3Nkh-wYcqFFTI2sE4AAIRigAC2eApSbnXrwABKNm-BjYE",
                "BAACAgIAAxkBAAIJgWkklxHn-34KYCcbatAr7bM38tfFAAISigAC2eApSSbFgct1sT47NgQ", 
            ],
        },
    },
}
TRAINING_TEXTS = {
    "gym": {
        "1": {
            "1": (
                "🏋️‍♀️ Тренировка 1 (Ягодицы, Бёдра, Спина)\n\n"
                "🔹 Упражнения по порядку\n"
                "1. Приседания в Смите — 3×20-15-12\n"
                "2. Ягодичный мостик в Смите/со штангой — 4×20-15-12\n"
                "3. Тяга вертикального блока к груди — 3×15-15-12\n"
                "4. Разгибания ног в тренажёре — 3×15\n"
                "5. Разведения ног в тренажёре — 3×25-20-15\n"
                "6. Подъём гантелей на бицепс стоя — 3×15\n\n"
                "📌 Основная нагрузка:\n"
                "• Ягодицы (мостик, присед, разведения)\n"
                "• Квадрицепсы (присед, разгибания)\n"
                "• Спина (тяга вертикального блока)\n"
                "• Бицепсы (подъём гантелей)\n"
            ),
            "2": (
                "🏋️‍♀️‍ Тренировка 2 (Ягодицы, Бёдра, Спина, Руки)\n\n"
                "🔹 Упражнения по порядку\n"
                "1.  Румынская тяга со штангой — 3 подхода × 20-15-12 повторений\n"
                "2.  Жим ногами в тренажёре с обычной постановкой ног — 3 подхода × 20-15-12 повторений\n"
                "3.  Тяга горизонтального блока к поясу — 3 подхода × 12-12-12 повторений\n"
                "4.  Жим гантелей сидя (плечи) — 3 подхода × 12-12-12 повторений\n"
                "5.  Отжимания от скамьи на трицепс — 3 подхода × 12-12-12 повторений\n\n"
                "📌 Основная нагрузка тренировки:\n"
                "• Ягодицы и задняя поверхность бедра (румынская тяга).\n"
                "• Квадрицепсы и ягодицы (жим ногами).\n"
                "• Средняя часть спины (горизонтальная тяга).\n"
                "• Плечи (жим гантелей).\n"
                "• Трицепсы (отжимания от скамьи).\n"

            ),
            "3": (
                "🏋️‍♀️‍ Тренировка 3 (Ягодицы, Бёдра, Спина, Плечи, Икры)\n\n"
                "🔹 Упражнения по порядку\n"
                "1.  Зашагивания на платформу с гантелями — 3 подхода × 15-15-12 повторений на каждую ногу\n"
                "2.  Подтягивания в гравитроне узким хватом — 3 подхода × 12-12-12 повторений\n"
                "3.  Сгибания ног в тренажёре — 3 подхода × 15-12-12 повторений\n"
                "4.  Разведения гантелей в стороны стоя — 3 подхода × 12-12-12 повторений\n"
                "5.  Подъёмы на носки в тренажёре или с утяжелением — 3 подхода × 20-15-15 повторений\n\n"
                "📌 Основная нагрузка тренировки:\n"
                "• Ягодицы и квадрицепсы (зашагивания).\n"
                "• Спина и бицепсы (подтягивания в гравитроне).\n"
                "• Задняя поверхность бедра (сгибания ног).\n"
                "• Плечи (разведения гантелей).\n"
                "• Икры (подъёмы на носки).\n"

            ),
            "4": (
                "🏋️‍♀️‍ Тренировка 4 (Ягодицы, Квадрицепс, Спина, Плечи)\n\n"
                "🔹 Упражнения по порядку\n"
                "1.  Болгарские сплит-приседания с гантелями — 3 подхода × 20-15-12 повторений на каждую ногу\n"
                "2.  Ягодичный мостик с паузой в верхней точке — 4 подхода × 20-15-15-12 повторений (удержание 2 сек)\n"
                "3.  Тяга верхнего блока к груди широким хватом — 3 подхода × 15-15-15 повторений\n"
                "4.  Выпады назад в Смите — 3 подхода × 15-12-10 повторений на каждую ногу\n"
                "5.  Обратные разведения в тренажёре «бабочка» — 3 подхода × 12–12-12 повторений\n\n"
                "📌 Основная нагрузка тренировки:\n"
                "• Ягодицы (главный акцент: мостик, болгарские, выпады).\n"
                "• Квадрицепсы (болгарские, выпады).\n"
                "• Спина (тяга верхнего блока).\n"
                "• Плечи (задняя дельта через бабочку).\n"
            ),
            "5": (
                "🏋️‍♀️‍ Тренировка 5 (Ягодицы, Спина, Руки)\n\n"
                "🔹 Упражнения по порядку\n"
                "1.  Румынская тяга на одной ноге с гантелями — 3 подхода × 15-12-12 повторений на каждую ногу\n"
                "2.  Тяга штанги к поясу — 3 подхода × 12-12-12 повторений\n"
                "3.  Сгибания рук «молот» с гантелями — 3 подхода × 12-12-12 повторений\n"
                "4.  Тяга верхнего блока канатом на трицепс — 3 подхода × 15-15-15 повторений\n"
                "5.  Гиперэкстензия «лягушка» на ягодицы — 3 подхода × 15-15-15 повторений\n\n"
                "📌 Основная нагрузка тренировки:\n"
                "• Ягодицы и задняя поверхность бедра (румынская тяга на одной ноге, гиперэкстензия).\n"
                "• Спина (тяга штанги к поясу).\n"
                "• Бицепсы и предплечья (сгибания «молот»).\n"
                "• Трицепсы (тяга каната).\n"
            ),
            "6": (
                "🏋️‍♀️‍ Тренировка 6 (Ягодицы, Спина, Бёдра, Икры)\n\n"
                "🔹 Упражнения по порядку\n"
                "1.  Жим ногами в тренажёре (широкая постановка) — 3 подхода × 20-15-12 повторений\n"
                "2.  Подтягивания в гравитроне широким хватом — 3 подхода × 12-10-10 повторений\n"
                "3.  Тяга горизонтального блока к поясу — 3 подхода × 12-12-12 повторений\n"
                "4.  Сгибания ног в тренажёре — 3 подхода × 15-15-15 повторений\n"
                "5.  Подъёмы на носки (тренажёр/гантели) — 3 подхода × 15-15-15 повторений\n\n"
                "📌 Основная нагрузка тренировки:\n"
                "• Ягодицы и внутренняя поверхность бедра (жим ногами).\n"
                "• Спина (подтягивания, тяга горизонтального блока).\n"
                "• Задняя поверхность бедра (сгибания ног).\n"
                "• Икры (подъёмы на носки).\n"

            ),
            "7": (
                "🏋️‍♀️‍ Тренировка 7 (Ягодицы, Спина, Грудь)\n\n"
                "🔹 Упражнения по порядку\n"
                "1.  Приседания в Смите — 4 подхода × 20-15-15-12 повторений\n"
                "2.  Тяга гантели одной рукой в упоре на скамью — 3 подхода × 15-15-15 повторений на каждую руку\n"
                "3.  Гиперэкстензия «лягушка» на ягодицы — 3 подхода × 15-15-15 повторений\n"
                "4.  Ягодичный мостик со штангой/в Смите — 4 подхода × 20-15-15-12 повторений\n"
                "5.  Тяга вертикального блока к груди — 3 подхода × 15-15-15 повторений\n"
                "6.  Отжимания с колен — 3 подхода × 12-12-12 повторений\n\n"
                "📌 Основная нагрузка тренировки:\n"
                "• Ягодицы (главный акцент: приседания, гиперэкстензия, мостик).\n"
                "• Спина (тяга гантели, тяга вертикального блока).\n"
                "• Грудь и трицепсы (отжимания с колен).\n"
            ),
            "8": (
                "🏋️‍♀️‍ Тренировка 8 (Ягодицы, Бёдра, Руки, Грудь)\n\n"
                "🔹 Упражнения по порядку\n"
                "1.  Жим ногами в тренажёре (узкая постановка) — 3 подхода × 20-15-15 повторений\n"
                "2.  Сгибания ног в тренажёре — 3 подхода × 15-15-15 повторений\n"
                "3.  Румынская тяга со штангой — 4 подхода × 20-15-15-12 повторений\n"
                "4.  Сгибания рук на бицепс «21» (гантели) — 3 подхода\n"
                "5.  Французский жим с гантелью сидя — 3 подхода × 15-15-15 повторений\n"
                "6.  Жим гантелей на плечи сидя — 3 подхода × 15-12-12 повторений\n\n"
                "📌 Основная нагрузка тренировки:\n"
                "• Ягодицы и задняя поверхность бедра (румынка, сгибания ног).\n"
                "• Квадрицепсы (жим ногами узкой постановкой).\n"
                "• Бицепсы (сгибания «21»).\n"
                "• Трицепсы (французский жим).\n"
                "• Грудь (жим гантелей на наклонной).\n"
            ),
            "9": (
                "🏋️‍♀️‍ Тренировка 9 (Спина, Ягодицы)\n\n"
                "🔹 Упражнения по порядку\n"
                "1.  Подтягивания в гравитроне (широким хватом) — 3 подхода × 12-10-10 повторений\n"
                "2.  Тяга горизонтального блока к поясу — 3 подхода × 15-12-12 повторений\n"
                "3.  Обратные разведения в тренажёре «бабочка» (на спину) — 3 подхода × 12-12-12 повторений\n"
                "4.  Разведения ног в тренажёре (на ягодицы) — 3 подхода × 25-20-15 повторений\n"
                "5.  Махи ногой назад в кроссовере — 3 подхода × 20-15-12 повторений на каждую ногу\n\n"
                "📌 Основная нагрузка тренировки:\n"
                "• Спина (подтягивания, горизонтальная тяга, бабочка на заднюю дельту).\n"
                "• Ягодицы (разведения в тренажёре, махи назад).\n"
                "• Плечи (задняя дельта через бабочку).\n"
            ),
            "10": (
                "🏋️‍♀️‍ Тренировка 10 (Ягодицы, Квадрицепсы, Спина, Икры)\n\n"
                "🔹 Упражнения по порядку\n"
                "1.  Болгарские сплит-приседания с гантелями — 3 подхода × 15-15-12 повторений на каждую ногу\n"
                "2.  Ягодичный мостик со штангой/в Смите — 4 подхода × 20-15-15-12 повторений\n"
                "3.  Тяга вертикального блока узким хватом (треугольная рукоять) — 3 подхода × 15-15-15 повторений\n"
                "4.  Жим ногами в тренажёре (средняя постановка ног) — 3 подхода × 20-15-12 повторений\n"
                "5.  Подъёмы на носки стоя/в тренажёре — 3 подхода × 15-15-15 повторений\n\n"
                "📌 Основная нагрузка тренировки:\n"
                "• Ягодицы (главный акцент: сплит-приседания, мостик, жим ногами).\n"
                "• Квадрицепсы (сплит-приседания, жим ногами).\n"
                "• Спина (тяга вертикального блока).\n"
                "• Икры (подъёмы на носки).\n"
            ),
            "11": (
                "🏋️‍♀️‍ Тренировка 11 (Ягодицы, Задняя поверхность бедра, Руки, Грудь)\n\n"
                "🔹 Упражнения по порядку\n"
                "1.  Сгибания ног в тренажёре — 3 подхода × 15-15-15 повторений\n"
                "2.  Румынская тяга со штангой — 4 подхода × 20-15-15-12 повторений\n"
                "3.  Выпады в Смите — 3 подхода × 15-12-10 повторений на каждую ногу\n"
                "4.  Сгибания рук с супинацией (гантели) — 3 подхода × 12–15 повторений\n"
                "5.  Тяга каната на трицепс — 3 подхода × 15-15-15 повторений\n"
                "6.  Сведение рук в тренажёре «бабочка» — 3 подхода × 12-12-12 повторений\n\n"
                "📌 Основная нагрузка тренировки:\n"
                "• Ягодицы и задняя поверхность бедра (сгибания, румынка, выпады).\n"
                "• Квадрицепсы (выпады).\n"
                "• Бицепсы и трицепсы (изолированная работа руками).\n"
                "• Грудные мышцы (сведение рук).\n"
            ),
            "12": (
                "🏋️‍♀️‍ Тренировка 12 (Спина, Ягодицы, Плечи)\n\n"
                "🔹 Упражнения по порядку\n"
                "1.  Подтягивания в гравитроне (узким хватом) — 3 подхода × 12-12-12 повторений\n"
                "2.  Тяга штанги к поясу — 3 подхода × 15-15-15 повторений\n"
                "3.  Зашагивания на возвышенность с гантелями — 3 подхода × 12-12-12 повторений на каждую ногу\n"
                "4.  Разведения гантелей в стороны сидя (плечи) — 3 подхода × 15-15-15 повторений\n"
                "5.  Обратные разведения в тренажёре «бабочка» (задняя дельта) — 3 подхода × 12-12-12 повторений\n\n"
                "📌 Основная нагрузка тренировки:\n"
                "• Спина (подтягивания, тяга штанги).\n"
                "• Ягодицы и квадрицепсы (зашагивания).\n"
                "• Плечи (средние и задние дельты).\n"
            )
        },
        "2-3": {
            "Ягодицы":(
                "🏋️‍ Тренировка А (Ягодицы, 2-3 месяц)\n\n"
                "🔹 Упражнения по порядку\n"
                "1.  Гиперэкстензия «лягушка» на ягодицы — 3×15-15-15\n"
                "2.  Ягодичный мостик со штангой или в тренажёре — 4×25-20-15-12\n"
                "3.  Болгарские сплит-приседания с гантелями — 3×15-15-12 на каждую ногу\n"
                "4.  Махи назад в кроссовере — 3×20-15-12 на каждую ногу\n"
                "5.  Жим ногами (в тренажёре) с широкой постановкой ног — 4×20-15-15-12\n\n"
                "📌 Основная нагрузка тренировки:\n"
                "• Ягодицы (главный акцент: гиперэкстензия, мостик, махи, жим ногами).\n"
                "• Задняя поверхность бедра (гиперэкстензия, мостик, жим ногами).\n"
                "• Квадрицепсы (болгарские сплит-приседания, жим ногами).\n"
                "• Кор (статическая работа в болгарских сплитах, махах).\n"

            ),
            "Верх тела":(
                "🏋️‍ Тренировка Б (Верх тела, 2-3 месяц)\n\n"
                "🔹 Упражнения по порядку\n"
                "1.  Подтягивания в гравитроне широким хватом — 4×15-12-12-10\n"
                "2.  Тяга штанги к поясу — 4×15-15-12-12\n"
                "3.  Разведения гантелей в стороны сидя на наклонной скамье — 3×15-15-12\n"
                "4.  Жим гантелей сидя — 3×15-15-12\n"
                "5.  Французский жим с гантелью сидя — 3×12-12-12\n"
                "6.  Сгибания рук со штангой стоя — 3×12-12-12\n\n"
                "📌 Основная нагрузка тренировки:\n"
                "• Спина (широчайшие, ромбовидные, трапеции — подтягивания, тяги).\n"
                "• Плечи (дельтовидные — жим, разведения).\n"
                "• Трицепсы (французский жим).\n"
                "• Бицепсы (сгибания рук, подтягивания).\n"
            ),
            "Ноги":(
                "🏋️‍♀️ Тренировка C (Ноги, 2-3 месяц)\n\n"
                "🔹 Упражнения по порядку\n"
                "1.  Приседания со штангой или в Смите — 4 подхода × 20-15-15-12 повторений\n"
                "2.  Сгибания ног в тренажёре — 3 подхода × 15 повторений\n"
                "3.  Румынская тяга со штангой — 4 подхода × 20-15-15-12 повторений\n"
                "4.  Выпады с гантелями шагая по залу — 3 подхода × 15 повторений на каждую ногу (30 шагов)\n"
                "5.  Подъёмы на носки в тренажёре или с утяжелением — 4 подхода × 20-20-20-20 повторений\n\n"
                "📌 Основная нагрузка тренировки:\n"
                "• Ягодицы (главный акцент: присед, румынская тяга, выпады).\n"
                "• Задняя поверхность бедра (сгибания, румынская тяга).\n"
                "• Квадрицепсы (присед, выпады).\n"
                "• Икры (подъёмы на носки).\n"
            ),
        },
        "4-5": {
            "Ягодицы": (
                "🏋️‍♀️ Тренировка А (Ягодицы, 4-5 месяц)\n\n"
                "🔹 Упражнения по порядку\n"
                "1.  Приседания в Смите — 4 подхода × 20-15-15-12 повторений\n"
                "2.  Ягодичный мостик со штангой — 4 подхода × 20-15-15-12 повторений\n"
                "3.  Выпады в диагональ — 3 подхода × 12-12-12 повторений на каждую ногу\n"
                "4.  Жим ногами (широкая постановка стоп) — 4 подхода × 15-15-12-12 повторений\n"
                "5.  Разведения ног в тренажёре — 3 подхода × 25-20-15 повторений\n"
                "6.  Румынская тяга со штангой — 4 подхода × 20-15-12-12 повторений\n\n"
                "📌 Основная нагрузка тренировки:\n"
                "• Ягодицы (главный акцент: приседания, мостик, выпады, жим ногами, разведения, румынская тяга).\n"
                "• Задняя поверхность бедра (румынская тяга, мостик).\n"
                "• Внутренняя поверхность бедра (жим ногами широкая постановка, выпады в диагональ).\n"
                "• Квадрицепсы (приседания, выпады, жим ногами).\n"
            ),
            "Верх тела": (
                "🏋️‍♀️ Тренировка Б (Верх тела, 4–5 месяц)\n\n"
                "🔹 Упражнения по порядку\n"
                "1.  Подтягивания в гравитроне широким хватом — 4 подхода × 12-12-10-10 повторений\n"
                "2.  Тяга горизонтального блока к поясу — 4 подхода × 15-15-12-12 повторений\n"
                "3.  Тяга гантели в упоре на скамью — 3 подхода × 15-15-15 повторений на каждую руку\n"
                "4.  Подъём гантелей на бицепс стоя — 3 подхода × 12-12-12 повторений\n"
                "5.  Тяга каната (косичка) на трицепс в кроссовере — 3 подхода × 15-15-15 повторений\n"
                "6.  Разведение рук в тренажёре «бабочка» — 3 подхода × 15-15-15 повторений\n\n"
                "📌 Основная нагрузка тренировки:\n"
                "• Спина (широчайшие, ромбовидные, трапеции — подтягивания, горизонтальная тяга, тяга гантели).\n"
                "• Плечи (задняя дельта — разведения, стабилизация в тягах).\n"
                "• Бицепсы (подтягивания, подъём гантелей).\n"
                "• Трицепсы (тяга каната).\n"
            ),
            "Ноги": (
                "🏋️‍♀️ Тренировка C (Ноги, 4-5 месяц)\n\n"
                "🔹 Упражнения по порядку\n"
                "1.  Болгарские сплит-приседания с гантелями — 4 подхода × 15-15-12-10 повторений на каждую ногу\n"
                "2.  Разгибания ног в тренажёре — 3 подхода × 15 повторений\n"
                "3.  Сгибания ног в тренажёре — 3 подхода × 15-15-15 повторений\n"
                "4.  Подъём на носки стоя (икры) — 4 подхода × 20-20-20-20 повторений\n"
                "5.  Гиперэкстензия с акцентом на спину — 3 подхода × 15-15-15 повторений\n\n"
                "📌 Основная нагрузка тренировки:\n"
                "• Ягодицы (главный акцент: болгарские сплит-приседания, гиперэкстензия).\n"
                "• Квадрицепсы (разгибания, болгарские).\n"
                "• Задняя поверхность бедра (сгибания, гиперэкстензия).\n"
                "• Икры (подъёмы на носки).\n"
                "• Разгибатели спины (гиперэкстензия).\n"
            ),
        },
        "6-7": {
            "Ягодицы":(
                "🏋️‍♀️ Тренировка А (Ягодицы, 6-7 месяц)\n\n"
                "🔹 Упражнения по порядку\n"
                "1.  Суперсет: Приседания в Смите (широкая постановка ног) + Обратный ягодичный мостик в Смите — 4 подхода × 20-15-15-12 повторений\n"
                "2.  Румынская тяга на одной ноге с гантелями — 3 подхода × 15-15-12 повторений на каждую ногу\n"
                "3.  Жим ногой в пол в гравитроне — 3 подхода × 15-12-12 повторений на каждую ногу\n"
                "4.  Суперсет: Ягодичный мостик со штангой — 4 подхода × 20-15-15-12 + Гиперэкстензия «лягушка» на ягодицы — 4 подхода × 15-15-15-15\n"
                "5.  Выпады назад с гантелями или в Смите — 3 подхода × 15-15-12 повторений на каждую ногу\n"
                "6.  Суперсет: Разведения ног в тренажёре — 3 подхода × 25-20-15 + Махи ногой накрест в кроссовере — 3 подхода × 20-15-12\n\n"
                "📌 Основная нагрузка тренировки:\n"
                "• Ягодицы\n"
                "• Задняя поверхность бедра\n"
                "• Внутренняя поверхность бедра\n"
                "• Квадрицепсы\n"
                "• Разгибатели спины\n\n"
                "📌 Что такое суперсет:\n"
                "Суперсет — это когда два (или больше) упражнения выполняются подряд, без отдыха. Отдых даём только после второго (или последнего) упражнения, и это считается одним подходом.\n\n"
                "Как делать суперсет:\n"
                "1.  Выполни первое упражнение.\n"
                "2.  Сразу переходи ко второму (и третьему, если есть).\n"
                "3.  После выполнения всех — отдых 1,5–2 минуты.\n"
                "4.  Повтори столько раз, сколько указано в программе.\n\n"
                "👉 «Суперсет» = упражнения подряд + один отдых = один подход.\n"
            ),
            "Верх тела":(
                "🏋️‍♀️ Тренировка Б (Верх тела, 6-7 месяц)\n\n"
                "🔹 Упражнения по порядку\n"
                "1.  Суперсет: Тяга верхнего блока к груди + Тяга горизонтального блока — 4 подхода × 15-15-12-12\n"
                "2.  Тяга штанги к поясу в наклоне — 4 подхода × 15-15-12-12\n"
                "3.  Гиперэкстензия с акцентом на спину — 3 подхода × 15-15-15\n"
                "4.  Суперсет на плечи: Жим гантелей вверх сидя + Разведения гантелей в стороны + «Бабочка» (обратные разведения на заднюю дельту) — 3 круга по 15-15-15 повторений\n"
                "5.  Суперсет: Бицепс «21» + Тяга каната на трицепс (косичка) — 3 подхода × 15-15-15\n\n"
                "📌 Основная нагрузка тренировки:\n"
                "• Спина (широчайшие, ромбовидные, трапеции, разгибатели).\n"
                "• Плечи (передние, средние и задние дельты).\n"
                "• Руки (бицепсы, трицепсы).\n"
                "• Кор стабилизирует во всех упражнениях.\n\n"
                "📌 Что такое суперсет:\n"
                "Суперсет — это когда два (или больше) упражнения выполняются подряд, без отдыха. Отдых даём только после второго (или последнего) упражнения, и это считается одним подходом.\n\n"
                "Как делать суперсет:\n"
                "1.  Выполни первое упражнение.\n"
                "2.  Сразу переходи ко второму (и третьему, если есть).\n"
                "3.  После выполнения всех — отдых 1,5–2 минуты.\n"
                "4.  Повтори столько раз, сколько указано в программе.\n\n"
                "👉 «Суперсет» = упражнения подряд + один отдых = один подход.\n"
            ),
            "Ноги":(
                "🏋️‍♀️ Тренировка C (Ноги, 6-7 месяц)\n\n"
                "🔹 Упражнения по порядку\n"
                "1.  Болгарские сплит-приседания — 4 подхода × 15-15-12-10\n"
                "2.  Жим ногами в тренажёре (узкая постановка ног) — 4 подхода × 20-15-15-12\n"
                "3.  Суперсет: Разгибания ног в тренажёре + Сгибания ног в тренажёре — 3 подхода × 15-15-15\n"
                "4.  Румынская тяга со штангой — 4 подхода × 20-15-12-12\n"
                "5.  Подъёмы на носки стоя — 4 подхода × 20-20-20-20\n\n"
                "📌 Основная нагрузка тренировки:\n"
                "• Квадрицепсы (главный акцент — приседания узкой постановкой, разгибания ног).\n"
                "• Бицепсы бедра и ягодицы (румынская тяга, сгибания ног).\n"
                "• Икры (подъёмы на носки).\n"
                "• В меньшей степени корпус и мышцы-стабилизаторы.\n\n"
                "📌 Что такое суперсет:\n"
                "Суперсет — это когда два (или больше) упражнения выполняются подряд, без отдыха. Отдых даём только после второго (или последнего) упражнения, и это считается одним подходом.\n\n"
                "Как делать суперсет:\n"
                "1.  Выполни первое упражнение.\n"
                "2.  Сразу переходи ко второму (и третьему, если есть).\n"
                "3.  После выполнения всех — отдых 1,5–2 минуты.\n"
                "4.  Повтори столько раз, сколько указано в программе.\n\n"
                "👉 «Суперсет» = упражнения подряд + один отдых = один подход.\n"
            ),
        },
        "8-9": {
            "Ягодицы":(
                "🏋️‍♀️ Тренировка А (Ягодицы, 8-9 месяц)\n\n"
                "🔹 Упражнения по порядку\n"
                "1.  Глубокие приседания в Смите / со штангой / с гантелями (широкая постановка ног) — 4 подхода × 20-15-15-12\n"
                "2.  Ягодичный мостик со штангой или в тренажере — 4 подхода × 20-15-15-12\n"
                "3.  Румынская тяга с гантелями на одной ноге — 4 подхода × 15-15-12-12\n"
                "4.  Гиперэкстензия «лягушка» с акцентом на ягодицы — 3 подхода × 15-15-15\n"
                "5.  Суперсет: Разведения ног в тренажёре — 4 подхода × 30-25-20-15 + Махи ногой назад в кроссовере — 4 подхода × 20-15-15-12\n\n"
                "📌 Основная нагрузка тренировки:\n"
                "• Ягодицы (главный акцент — во всех упражнениях).\n"
                "• Задняя поверхность бедра (румынская тяга, гиперэкстензия).\n"
                "• Внутренняя поверхность бедра (широкие приседания, разведения).\n"
                "• Квадрицепсы (приседания, частично ягодичный мостик).\n"
                "• Стабилизаторы и мышцы кора (в балансе на одной ноге).\n\n"
                "📌 Что такое суперсет:\n"
                "Суперсет — это когда два (или больше) упражнения выполняются подряд, без отдыха. Отдых даём только после второго (или последнего) упражнения, и это считается одним подходом.\n\n"
                "Как делать суперсет:\n"
                "1.  Выполни первое упражнение.\n"
                "2.  Сразу переходи ко второму (и третьему, если есть).\n"
                "3.  После выполнения всех — отдых 1,5–2 минуты.\n"
                "4.  Повтори столько раз, сколько указано в программе.\n\n"
                "👉 «Суперсет» = упражнения подряд + один отдых = один подход.\n"
            ),
            "Верх тела":(
                "🏋️‍♀️ Тренировка Б (Верх тела, 8-9 месяц)\n\n"
                "🔹 Упражнения по порядку\n"
                "1.  Подтягивания в гравитроне или с резинкой широким хватом — 3 подхода × 15-12-10\n"
                "2.  Тяга горизонтального блока — 3 подхода × 15-15-15\n"
                "3.  Тяга штанги к поясу в наклоне — 3 подхода × 15-15-15\n"
                "4.  Разведение рук в тренажёре «бабочка» (задняя дельта) — 3 подхода × 12-12-12\n"
                "5.  Суперсет на плечи: Подъём гантелей в стороны — 3 подхода × 15-15-15 + Жим гантелей вверх — 3 подхода × 15-15-12\n"
                "6.  Отжимания на брусьях в гравитроне (трицепс) — 3 подхода × 12-12-12\n"
                "7.  Подъём штанги на бицепс — 3 подхода × 12-12-12\n\n"
                "📌 Основная нагрузка тренировки:\n"
                "• Спина (подтягивания, тяги блока, тяга штанги).\n"
                "• Плечи (разведения, жим, подъём гантелей).\n"
                "• Руки: трицепс (брусья), бицепс (подъём штанги).\n"
                "• Задняя дельта (разведения в «бабочке»).\n"
                "• Кор стабилизирует корпус в базовых тягах.\n\n"
                "📌 Что такое суперсет:\n"
                "Суперсет — это когда два (или больше) упражнения выполняются подряд, без отдыха. Отдых даём только после второго (или последнего) упражнения, и это считается одним подходом.\n\n"
                "Как делать суперсет:\n"
                "1.  Выполни первое упражнение.\n"
                "2.  Сразу переходи ко второму (и третьему, если есть).\n"
                "3.  После выполнения всех — отдых 1,5–2 минуты.\n"
                "4.  Повтори столько раз, сколько указано в программе.\n\n"
                "👉 «Суперсет» = упражнения подряд + один отдых = один подход.\n"
            ),
            "Ноги":(
                "🏋️‍♀️ Тренировка C (Ноги, 8-9 месяц)\n\n"
                "🔹 Упражнения по порядку\n"
                "1.  Суперсет: Приседания со штангой (узкая постановка) + Разгибания ног в тренажёре — 3×20–15–12\n"
                "2.  Жим ногами в тренажёре (узкая постановка) — 3×12–15\n"
                "3.  Сгибания ног в тренажёре — 3×12–15\n"
                "4.  Румынская тяга со штангой — 3×12–15\n"
                "5.  Выпады с гантелями (шагая по залу, 30 шагов) — 3 подхода\n\n"
                "📌 Основная нагрузка тренировки:\n"
                "• Квадрицепсы (главный акцент — приседания, разгибания, жим).\n"
                "• Бицепсы бедра (сгибания, румынская тяга).\n"
                "• Ягодицы (румынская тяга, выпады).\n"
                "• Кор (баланс и стабилизация во всех упражнениях).\n\n"
                "📌 Что такое суперсет:\n"
                "Суперсет — это когда два (или больше) упражнения выполняются подряд, без отдыха. Отдых даём только после второго (или последнего) упражнения, и это считается одним подходом.\n\n"
                "Как делать суперсет:\n"
                "1.  Выполни первое упражнение.\n"
                "2.  Сразу переходи ко второму (и третьему, если есть).\n"
                "3.  После выполнения всех — отдых 1,5–2 минуты.\n"
                "4.  Повтори столько раз, сколько указано в программе.\n\n"
                "👉 «Суперсет» = упражнения подряд + один отдых = один подход.\n"
            ),
        },
        "10-12": {
            "Ягодицы":(
                "🏋️‍♀️ Тренировка А (Ягодицы, 10–12 месяц)\n\n"
                "🔹 Упражнения по порядку\n"
                "1.  Ягодичный мостик в тренажёре / со штангой — 4 подхода × 15-12-12-10\n"
                "2.  Болгарские сплит-приседания с гантелями (акцент на ягодицы) — 4 подхода × 15-15-12-12\n"
                "3.  Тяга сумо с гантелью / штангой — 3 подхода × 20-15-12\n"
                "4.  Гиперэкстензия «лягушка» с весом (блин / гантель у груди) — 3 подхода × 15-15-15\n"
                "5.  Суперсет: Разведения ног сидя — 4 подхода × 25-20-15-12 + Махи назад в кроссовере — 4 подхода × 20-15-15-12\n\n"
                "📌 Основная нагрузка тренировки:\n"
                "• Ягодицы (главный акцент — мостик, болгарские, махи и отведения).\n"
                "• Бицепсы бедра (сумо-тяга, гиперэкстензия).\n"
                "• Внутренняя поверхность бедра (сумо-тяга, отведения).\n"
                "• Квадрицепсы (болгарские приседания).\n"
                "• Стабилизаторы и мышцы кора (баланс в болгарских приседаниях).\n\n"
                "📌 Что такое суперсет:\n"
                "Суперсет — это когда два (или больше) упражнения выполняются подряд, без отдыха. Отдых даём только после второго (или последнего) упражнения, и это считается одним подходом.\n\n"
                "Как делать суперсет:\n"
                "1.  Выполни первое упражнение.\n"
                "2.  Сразу переходи ко второму (и третьему, если есть).\n"
                "3.  После выполнения всех — отдых 1,5–2 минуты.\n"
                "4.  Повтори столько раз, сколько указано в программе.\n\n"
                "👉 «Суперсет» = упражнения подряд + один отдых = один подход.\n"
            ),
            "Верх тела":(
                "🏋️‍♀️ Тренировка B (Верх, 10–12 месяц)\n\n"
                "🔹 Упражнения по порядку\n"
                "1.  Тяга вертикального блока к груди (широкий хват) — 4 подхода × 20-15-15-12\n"
                "2.  Тяга гантелей в наклоне (нейтральный хват) — 3 подхода × 15-15-15\n"
                "3.  Суперсет: Жим гантелей сидя — 3 подхода × 15-15-15 + Разведения гантелей в стороны — 3 подхода × 15-15-15\n"
                "4.  Бабочка (на грудь) — 3 подхода × 15-15-15\n"
                "5.  Суперсет: Подъём штанги на бицепс — 3 подхода × 12-12-12 + Французский жим на трицепс — 3 подхода × 12-12-12\n\n"
                "📌 Основная нагрузка тренировки:\n"
                "• Спина (тяга вертикального блока, тяга гантелей в наклоне).\n"
                "• Плечи (жим гантелей сидя, разведения в стороны).\n"
                "• Грудь (бабочка, жим гантелей).\n"
                "• Руки: бицепс (подъём штанги) и трицепс (французский жим).\n"
                "• Вторично включаются мышцы кора (стабилизация в тягах и жиме).\n\n"
                "📌 Что такое суперсет:\n"
                "Суперсет — это когда два (или больше) упражнения выполняются подряд, без отдыха. Отдых даём только после второго (или последнего) упражнения, и это считается одним подходом.\n\n"
                "Как делать суперсет:\n"
                "1.  Выполни первое упражнение.\n"
                "2.  Сразу переходи ко второму (и третьему, если есть).\n"
                "3.  После выполнения всех — отдых 1,5–2 минуты.\n"
                "4.  Повтори столько раз, сколько указано в программе.\n\n"
                "👉 «Суперсет» = упражнения подряд + один отдых = один подход.\n"
            ),
            "Ноги":(
                "🏋️‍♀️ Тренировка C (Ноги, 10–12 месяц)\n\n"
                "🔹 Упражнения по порядку\n"
                "1.  Фронтальные приседания со штангой / гирей у груди — 3 × 20-15-12\n"
                "2.  Суперсет: Разгибания ног в тренажёре — 3 × 15-15-15 + Приседания в гакк-машине — 3 × 15-15-15\n"
                "3.  Жим ногами узкой постановкой + медленный негатив (4 сек вниз) — 4 × 20-15-15-12\n"
                "4.  Суперсет: Румынская тяга со штангой — 4 × 20-15-15-12 + Сгибания ног в тренажёре — 4 × 15-15-12-12\n"
                "5.  Зашагивания на платформу вверх (гантели в руках) — 4 × 15-12-12-10 на каждую ногу\n\n"
                "📌 Основная нагрузка тренировки:\n"
                "• Квадрицепсы — фронтальные приседания, разгибания, гакк-приседы, жим ногами (узко).\n"
                "• Ягодицы — румынская тяга, гакк-приседания, выпады на платформу.\n"
                "• Бицепсы бедра — румынская тяга, сгибания ног лёжа.\n"
                "• Кор и стабилизаторы — активно работают во фронтальных приседаниях и выпадах.\n\n"
                "📌 Что такое суперсет:\n"
                "Суперсет — это когда два (или больше) упражнения выполняются подряд, без отдыха. Отдых даём только после второго (или последнего) упражнения, и это считается одним подходом.\n\n"
                "Как делать суперсет:\n"
                "1.  Выполни первое упражнение.\n"
                "2.  Сразу переходи ко второму (и третьему, если есть).\n"
                "3.  После выполнения всех — отдых 1,5–2 минуты.\n"
                "4.  Повтори столько раз, сколько указано в программе.\n\n"
                "👉 «Суперсет» = упражнения подряд + один отдых = один подход.\n"    
            ),
        },
    },
    "home": {
        "1": {
            "1": (
                "🏋️‍♀️ Тренировка 1 (Ягодицы, Бёдра, Спина) — домашняя версия\n\n"
                "🔹 Упражнения по порядку\n"
                "1.  Приседания со штангой/гантелями — 3×12-12-12\n"
                "2.  Ягодичный мостик со штангой/гантелями — 3×20-15-15\n"
                "3.  Тяга резинки сверху к груди (имитация вертикального блока) — 3×12-12-12\n"
                "4.  Разгибания ног с резинкой сидя — 3×15-15-15\n"
                "5.  Разведения ног с резинкой (сидя/лёжа) — 3×20-20-20\n"
                "6.  Подъём гантелей на бицепс стоя — 3×12-12-12\n\n"
                "📌 Основная нагрузка тренировки:\n"
                "• Ягодицы (главный акцент: мостик, присед, разведения).\n"
                "• Квадрицепсы (присед, разгибания).\n"
                "• Спина (тяга резинки сверху).\n"
                "• Бицепсы (подъём гантелей).\n\n"
            ),
            "2": (
                "🏋️‍♀️ Тренировка 2 (Ягодицы, Бёдра, Спина, Руки) — домашняя версия\n\n"
                "🔹 Упражнения по порядку\n"
                "1.  Румынская тяга со штангой/гантелями — 3×15-15-15\n"
                "2.  Приседания со штангой/гантелями — 3×15-15-15\n"
                "3.  Тяга гантелей в наклоне к поясу — 3×12-12-12\n"
                "4.  Жим гантелей сидя (плечи) — 3×12-12-12\n"
                "5.  Отжимания от скамьи на трицепс — 3×12-12-12\n\n"
                "📌 Основная нагрузка тренировки:\n"
                "• Ягодицы и задняя поверхность бедра (румынская тяга).\n"
                "• Квадрицепсы и ягодицы (приседания).\n"
                "• Средняя часть спины (тяга гантелей в наклоне).\n"
                "• Плечи (жим гантелей сидя).\n"
                "• Трицепсы (отжимания от скамьи).\n"
            ),
            "3": (
                "🏋️‍♀️ Тренировка 3 (Ягодицы, Бёдра, Спина, Плечи, Икры) — домашняя версия\n\n"
                "🔹 Упражнения по порядку\n"
                "1.  Зашагивания на платформу с гантелями — 3×15-15-15 на каждую ногу\n"
                "2.  Тяга резинки сверху к груди — 3×12-12-12\n"
                "3.  Сгибания ног лёжа с резинкой — 3×15-15-15\n"
                "4.  Разведения гантелей в стороны стоя — 3×15-15-15\n"
                "5.  Подъёмы на носки стоя с утяжелением — 3×20-20-20\n\n"
                "📌 Основная нагрузка тренировки:\n"
                "• Ягодицы и квадрицепсы (зашагивания).\n"
                "• Спина и бицепсы (тяга резинки сверху).\n"
                "• Задняя поверхность бедра (сгибания ног).\n"
                "• Плечи (разведения гантелей).\n"
                "• Икры (подъёмы на носки).\n"
            ),
            "4": (
                "🏋️‍♀️ Тренировка 4 (Ягодицы, Квадрицепс, Спина, Плечи) — домашняя версия\n\n"
                "🔹 Упражнения по порядку\n"
                "1.  Болгарские сплит-приседания с гантелями — 3×15-15-15 на каждую ногу\n"
                "2.  Ягодичный мостик со штангой/гантелями с паузой — 4×15-15-12-12 (удержание 2 сек)\n"
                "3.  Тяга резинки сверху к груди (широкий хват) — 3×15-15-15\n"
                "4.  Выпады назад со штангой/гантелями — 3×15-15-12 на каждую ногу\n"
                "5.  Обратные разведения с гантелями в наклоне — 3×12-12-12\n\n"
                "📌 Основная нагрузка тренировки:\n"
                "• Ягодицы (главный акцент: мостик, болгарские, выпады).\n"
                "• Квадрицепсы (болгарские, выпады).\n"
                "• Спина (тяга резинки сверху).\n"
                "• Плечи (задняя дельта через обратные разведения).\n"
            ),
            "5": (
               "🏋️‍♀️ Тренировка 5 (Ягодицы, Спина, Руки) — домашняя версия\n\n"
                "🔹 Упражнения по порядку\n"
                "1.  Румынская тяга на одной ноге с гантелями — 3×15-15-15 на каждую ногу\n"
                "2.  Тяга гантелей к поясу — 3×15-15-15\n"
                "3.  Сгибания рук «молот» с гантелями — 3×12-12-12\n"
                "4.  Разгибания рук с резинкой на трицепс — 3×12-12-12\n"
                "5.  Отведения ноги с резинкой на четвереньках — 3×15-15-15\n\n"
                "📌 Основная нагрузка тренировки:\n"
                "• Ягодицы и задняя поверхность бедра (румынская тяга на одной ноге, отведения ноги с резинкой).\n"
                "• Спина (тяга штанги к поясу).\n"
                "• Бицепсы и предплечья (сгибания «молот»).\n"
                "• Трицепсы (разгибания рук с резинкой).\n"
            ),
            "6": (
                "🏋️‍♀️ Тренировка 6 (Ягодицы, Спина, Бёдра, Икры) — домашняя версия\n\n"
                "🔹 Упражнения по порядку\n"
                "1.  Приседания «сумо» со штангой/гантелями — 3×15-15-15\n"
                "2.  Тяга резинки сверху к груди (широкий хват) — 3×15-15-15\n"
                "3.  Тяга резинки к поясу сидя — 3×15-15-15\n"
                "4.  Сгибания ног лёжа с резинкой — 3×12-12-12\n"
                "5.  Подъёмы на носки стоя с утяжелением — 3×20-20-20\n\n"
                "📌 Основная нагрузка тренировки:\n"
                "• Ягодицы и внутренняя поверхность бедра (приседания «сумо»).\n"
                "• Спина (тяга резинки сверху, тяга резинки к поясу).\n"
                "• Задняя поверхность бедра (сгибания ног).\n"
                "• Икры (подъёмы на носки).\n"
            ),
            "7": (
               "🏋️‍♀️ Тренировка 7 (Ягодицы, Спина, Грудь) — домашняя версия\n\n"
                "🔹 Упражнения по порядку\n"
                "1.  Приседания со штангой/гантелями — 3×15-15-15\n"
                "2.  Тяга гантели одной рукой в упоре — 3×15-15-15 на каждую руку\n"
                "3.  Отведения ноги с резинкой на четвереньках — 3×15-15-15\n"
                "4.  Ягодичный мостик со штангой/гантелями — 4×20-20-15-15\n"
                "5.  Тяга резинки сверху к груди (широкий хват) — 3×15-15-15\n"
                "6.  Отжимания с колен — 4×12-12-12\n\n"
                "📌 Основная нагрузка тренировки:\n"
                "• Ягодицы (главный акцент: приседания, мостик, отведения ноги).\n"
                "• Спина (тяга гантели одной рукой, тяга резинки сверху).\n"
                "• Грудь и трицепсы (отжимания с колен).\n"
            ),
            "8": (
               "🏋️‍♀️ Тренировка 8 (Ягодицы, Бёдра, Руки, Грудь) — домашняя версия\n\n"
                "🔹 Упражнения по порядку\n"
                "1.  Приседания с узкой постановкой ног со штангой/гантелями — 4×12-12-12-12\n"
                "2.  Сгибания ног лёжа с резинкой — 3×12-12-12\n"
                "3.  Румынская тяга со штангой/гантелями — 4×15-15-15\n"
                "4.  Сгибания рук на бицепс «21» (гантели) — 3 подхода\n"
                "5.  Французский жим с гантелью сидя — 3×15-15-15\n"
                "6.  Жим гантелей сидя (плечи) — 3×15-15-15\n\n"
                "📌 Основная нагрузка тренировки:\n"
                "• Ягодицы и задняя поверхность бедра (румынская тяга, сгибания ног).\n"
                "• Квадрицепсы (приседания с узкой постановкой).\n"
                "• Бицепсы (сгибания «21»).\n"
                "• Трицепсы (французский жим).\n"
                "• Плечи (жим гантелей сидя).\n"
            ),
            "9": (
                "🏋️‍♀️ Тренировка 9 (Спина, Ягодицы) — домашняя версия\n\n"
                "🔹 Упражнения по порядку\n"
                "1.  Пуловер с гантелью лёжа на стуле — 3×12-12-12\n"
                "2.  Тяга резинки к поясу сидя — 3×15-15-15\n"
                "3.  Обратные разведения с гантелями в наклоне — 3×12-12-12\n"
                "4.  Разведения ног с резинкой сидя — 3×15-15-15\n"
                "5.  Отведения ноги с резинкой на четвереньках — 3×15-15-15 на каждую ногу\n\n"
                "📌 Основная нагрузка тренировки:\n"
                "• Спина (пуловер, тяга к поясу, обратные разведения).\n"
                "• Ягодицы (разведения с резинкой, отведения ноги).\n"
                "• Плечи (задняя дельта через обратные разведения).\n"
            ),
            "10": (
                "🏋️‍♀️ Тренировка 10 (Ягодицы, Квадрицепсы, Спина, Икры) — домашняя версия\n\n"
                "🔹 Упражнения по порядку\n"
                "1.  Болгарские сплит-приседания с гантелями — 3×15-15-12 на каждую ногу\n"
                "2.  Ягодичный мостик со штангой/гантелями — 4×20-20-15-15\n"
                "3.  Тяга резинки узким хватом к груди — 3×15-15-15\n"
                "4.  Приседания со штангой/гантелями — 3×15-15-15\n"
                "5.  Подъёмы на носки стоя с утяжелением — 3×20-20-20\n\n"
                "📌 Основная нагрузка тренировки:\n"
                "• Ягодицы (главный акцент: болгарские, мостик, приседания).\n"
                "• Квадрицепсы (болгарские, приседания).\n"
                "• Спина (тяга резинки узким хватом).\n"
                "• Икры (подъёмы на носки).\n"
            ),
            "11": (
                "🏋️‍♀️ Тренировка 11 (Ягодицы, Задняя поверхность бедра, Руки, Грудь) — домашняя версия\n\n"
                "🔹 Упражнения по порядку\n"
                "1.  Сгибания ног лёжа с резинкой — 3×12-12-12\n"
                "2.  Румынская тяга со штангой/гантелями — 4×20-15-15-12\n"
                "3.  Выпады назад со штангой/гантелями — 3×12-12-12 на каждую ногу\n"
                "4.  Сгибания рук с супинацией (гантели) — 3×12-12-12\n"
                "5.  Разгибания рук с резинкой на трицепс — 3×15-15-15\n"
                "6.  Жим гантелей лёжа на полу — 3×12-12-12\n\n"
                "📌 Основная нагрузка тренировки:\n"
                "• Ягодицы и задняя поверхность бедра (сгибания ног, румынская тяга, выпады).\n"
                "• Квадрицепсы (выпады).\n"
                "• Бицепсы и трицепсы (сгибания и разгибания рук).\n"
                "• Грудные мышцы (жим гантелей).\n"
            ),
            "12": (
                "🏋️‍♀️ Тренировка 12 (Спина, Ягодицы, Плечи) — домашняя версия\n\n"
                "🔹 Упражнения по порядку\n"
                "1.  Тяга резинки сверху узким хватом — 4×15-15-15-15\n"
                "2.  Тяга штанги/гантелей к поясу — 3×15-15-15\n"
                "3.  Зашагивания на платформу с гантелями — 3×15-15-12 на каждую ногу\n"
                "4.  Разведения гантелей в стороны сидя (на стуле или наклонной поверхности) — 3×15-15-15\n"
                "5.  Обратные разведения с гантелями в наклоне — 3×12-12-12\n\n"
                "📌 Основная нагрузка тренировки:\n"
                "• Спина (тяга резинки сверху, тяга к поясу).\n"
                "• Ягодицы и квадрицепсы (зашагивания).\n"
                "• Плечи (разведения).\n"
            )
        },
        "2-3": {
            "Ягодицы": (
                "🏋️‍♀️ Тренировка А (Ягодицы, 2–3 месяц) — домашняя версия\n\n"
                "🔹 Упражнения по порядку\n"
                "1.  Ягодичный мостик со штангой/гантелями — 4×20-20-15-12\n"
                "2.  Болгарские сплит-приседания с гантелями — 4×15-15-12-12 на каждую ногу\n"
                "3.  Отведения ноги с резинкой на четвереньках — 4×15-15-15-15 на каждую ногу\n"
                "4.  Приседания «сумо» со штангой/гантелями — 4×15-15-12-12\n"
                "5.  Сгибания ног лёжа с резинкой — 3×15-15-15\n\n"
                "📌 Основная нагрузка тренировки:\n"
                "• Ягодицы (главный акцент: мостик, сплиты, отведения, сумо).\n"
                "• Задняя поверхность бедра (мостик, сумо, сгибания ног).\n"
                "• Квадрицепсы (сплит-приседания, приседания сумо).\n"
                "• Кор (баланс и стабилизация корпуса в сплитах и отведениях).\n"
            ),
            "Верх тела": (
                "🏋️‍ Тренировка Б (Верх тела, 2–3 месяц) — домашняя версия\n\n"
                "🔹 Упражнения по порядку\n"
                "1.  Тяга резинки сверху к груди (широкий хват) — 4×15-15-15-15\n"
                "2.  Тяга штанги/гантелей к поясу — 4×15-15-12-12\n"
                "3.  Разведения гантелей в стороны стоя — 3×12-12-12\n"
                "4.  Жим гантелей сидя — 3×15-15-15\n"
                "5.  Французский жим с гантелью сидя — 3×15-15-15\n"
                "6.  Сгибания рук со штангой/гантелями стоя — 3×12-12-12\n\n"
                "📌 Основная нагрузка тренировки:\n"
                "• Спина (тяги).\n"
                "• Плечи (жим, разведения).\n"
                "• Трицепсы (французский жим).\n"
                "• Бицепсы (сгибания рук, тяги).\n"
            ),
            "Ноги": (
                "🏋️‍ Тренировка C (Ноги, 2–3 месяц) — домашняя версия\n\n"
                "🔹 Упражнения по порядку\n"
                "1.  Приседания со штангой/гантелями — 4×20-15-15-12\n"
                "2.  Сгибания ног лёжа с резинкой — 3×15-15-15\n"
                "3.  Румынская тяга со штангой/гантелями — 4×15-15-12-12\n"
                "4.  Выпады с гантелями, шагая по комнате — 3×12-12-12 на каждую ногу\n"
                "5.  Подъёмы на носки стоя с утяжелением — 4×20-20-20-20\n\n"
                "📌 Основная нагрузка тренировки:\n"
                "• Ягодицы (главный акцент: присед, румынская тяга, выпады).\n"
                "• Задняя поверхность бедра (сгибания ног, румынка).\n"
                "• Квадрицепсы (присед, выпады).\n"
                "• Икры (подъёмы на носки).\n"
            ),
        },
        "4-5": {
            "Ягодицы": (
                "🏋️ Тренировка А (Ягодицы, 4–5 месяц) — домашняя версия\n\n"
                "🔹 Упражнения по порядку\n"
                "1.  Приседания со штангой/гантелями — 4×15-15-12-12\n"
                "2.  Ягодичный мостик со штангой/гантелями — 4×20-20-15-15\n"
                "3.  Выпады в диагональ с гантелями — 3×12-12-12 на каждую ногу\n"
                "4.  Становая тяга со штангой/гантелями — 3×12-12-12\n"
                "5.  Разведения ног с резинкой лёжа — 3×20-20-20 на каждую ногу\n"
                "6.  Румынская тяга со штангой/гантелями — 4×20-20-15-15\n\n"
                "📌 Основная нагрузка тренировки:\n"
                "• Ягодицы (главный акцент: приседания, мостик, выпады, становая, разведения, румынская тяга).\n"
                "• Задняя поверхность бедра (румынская тяга).\n"
                "• Внутренняя поверхность бедра (диагональные выпады).\n"
                "• Квадрицепсы (приседания, выпады).\n"
            ),
            "Верх тела": (
                "🏋️‍ Тренировка Б (Верх тела, 4–5 месяц) — домашняя версия\n\n"
                "🔹 Упражнения по порядку\n\n"
                "1.  Пуловер с гантелью или резинкой на стуле — 4×12-12-12-12\n"
                "2.  Тяга резинки к поясу сидя — 4×15-15-15-15\n"
                "3.  Тяга гантели в упоре на стул — 3×12-12-12 на каждую руку\n"
                "4.  Сгибания рук с супинацией (гантели) — 3×15-15-15\n"
                "5.  Разгибания рук с резинкой на трицепс — 3×15-15-15\n"
                "6.  Обратные разведения с гантелями в наклоне — 3×12-12-12\n\n"
                "📌 Основная нагрузка тренировки:\n"
                "• Спина (широчайшие, ромбовидные, трапеции — пуловер, тяга резинки, тяга гантели).\n"
                "• Плечи (задняя дельта — обратные разведения, стабилизация корпуса в тягах).\n"
                "• Бицепсы (сгибания рук, участвуют в тягательных движениях).\n"
                "• Трицепсы (разгибания рук).\n"
            ),
            "Ноги": (
                "🏋️‍♀️‍ Тренировка C (Ноги, 4–5 месяц) — домашняя версия\n\n"
                "🔹 Упражнения по порядку\n"
                "1.  Болгарские сплит-приседания с гантелями — 4×15-15-12-12 на каждую ногу\n"
                "2.  Разгибания ног с резинкой — 3×12-12-12\n"
                "3.  Сгибания ног лёжа с резинкой — 3×15-15-15\n"
                "4.  Подъёмы на носки стоя с утяжелением — 4×20-20-20-20\n"
                "5.  Подъём спины на коврике с задержкой — 3×15-15-15\n\n"
                "📌 Основная нагрузка тренировки:\n"
                "• Ягодицы (главный акцент: болгарские, подъём спины).\n"
                "• Квадрицепсы (разгибания, болгарские).\n"
                "• Задняя поверхность бедра (сгибания, статическая работа в подъёме спины).\n"
                "• Икры (подъёмы на носки).\n"
                "• Разгибатели спины (подъём корпуса).\n"
            ),
        },
        "6-7": {
            "Ягодицы": (
                "🏋️‍♀️‍ Тренировка А (Ягодицы, 6–7 месяц) — домашняя версия\n\n"
                "🔹 Упражнения по порядку\n"
                "1.  Суперсет: Отведения ноги с резинкой стоя + Приседания «сумо» со штангой/гантелями — 4×60 шагов (всего) и 4×15-15-12-12\n"
                "2.  Румынская тяга на одной ноге с гантелями — 3×15-15-15 на каждую ногу\n"
                "3.  Суперсет: Ягодичный мостик на коврике с резинкой + Ягодичный мостик со штангой — 4×20-20-20-20 и 4×20-20-15-15\n"
                "4.  Выпады назад с гантелями — 3×15-15-15 на каждую ногу\n"
                "5.  Суперсет: Разведения ног с резинкой сидя + Отведения ноги с резинкой на четвереньках — 3×20-20-20 и 3×15-15-15\n\n"
                "📌 Основная нагрузка тренировки:\n"
                "• Ягодицы (главный акцент — во всех упражнениях, с проработкой всех пучков).\n"
                "• Задняя поверхность бедра (румынская тяга).\n"
                "• Внутренняя поверхность бедра (присед сумо, разведения).\n"
                "• Квадрицепсы (приседания, выпады).\n"
                "• Кор и стабилизаторы (при односторонних упражнениях).\n\n"
                "📌 Что такое суперсет:\n"
                "Суперсет — это когда два (или больше) упражнения выполняются подряд, без отдыха. Отдых даём только после второго (или последнего) упражнения, и это считается одним подходом.\n\n"
                "Как делать суперсет:\n"
                "1.  Выполни первое упражнение.\n"
                "2.  Сразу переходи ко второму (и третьему, если есть).\n"
                "3.  После выполнения всех — отдых 1,5–2 минуты.\n"
                "4.  Повтори столько раз, сколько указано в программе.\n"
                "👉 «Суперсет» = упражнения подряд + один отдых = один подход.\n"
            ),
            "Верх тела": (
                "🏋️‍♀️‍ Тренировка Б (Верх тела, 6–7 месяц) — домашняя версия\n\n"
                "🔹 Упражнения по порядку\n"
                "1.  Суперсет: Тяга резинки сверху к груди (широкий хват) + Тяга резинки к поясу сидя — 4×20-15-15-15\n"
                "2.  Тяга штанги/гантелей к поясу в наклоне — 4×15-15-12-12\n"
                "3.  Подъём спины на коврике с задержкой — 3×15-15-15\n"
                "4.  Суперсет на плечи: Жим гантелей вверх стоя или сидя + Разведения гантелей в стороны стоя + Обратные разведения с гантелями в наклоне — 3 круга по 12-15 повторений\n"
                "5.  Суперсет: Сгибания рук на бицепс «21» (гантели/штанга) — 3 подхода + Разгибания рук с резинкой на трицепс — 3×15-15-15\n\n"
                "📌 Основная нагрузка тренировки:\n"
                "• Спина (широчайшие, ромбовидные, трапеции, разгибатели).\n"
                "• Плечи (передние, средние и задние дельты).\n"
                "• Руки (бицепсы, трицепсы).\n"
                "• Кор стабилизирует во всех упражнениях.\n\n"
                "📌 Что такое суперсет:\n"
                "Суперсет — это когда два (или больше) упражнения выполняются подряд, без отдыха. Отдых даём только после второго (или последнего) упражнения, и это считается одним подходом.\n\n"
                "Как делать суперсет:\n"
                "1.  Выполни первое упражнение.\n"
                "2.  Сразу переходи ко второму (и третьему, если есть).\n"
                "3.  После выполнения всех — отдых 1,5–2 минуты.\n"
                "4.  Повтори столько раз, сколько указано в программе.\n"
                "👉 «Суперсет» = упражнения подряд + один отдых = один подход.\n"
            ),
            "Ноги": (
                "🏋️‍♀️‍ Тренировка C (Ноги, 6–7 месяц) — домашняя версия\n\n"
                "🔹 Упражнения по порядку\n"
                "1.  Болгарские сплит-приседания с гантелями — 3×20-15-15 на каждую ногу\n"
                "2.  Приседания со штангой/гантелями (узкая постановка ног) — 4×20-20-15-15\n"
                "3.  Суперсет: Разгибания ног с резинкой сидя + Сгибания ног лёжа с резинкой — 3×12-12-12 и 3×15-15-15\n"
                "4.  Румынская тяга со штангой/гантелями — 4×20-15-15-12\n"
                "5.  Подъёмы на носки стоя с утяжелением — 4×20-20-20-20\n\n"
                "📌 Основная нагрузка тренировки:\n"
                "• Квадрицепсы (главный акцент — узкие приседания, разгибания ног).\n"
                "• Бицепсы бедра и ягодицы (румынская тяга, сгибания ног).\n"
                "• Икры (подъёмы на носки).\n"
                "• Кор и мышцы-стабилизаторы включаются во всех упражнениях.\n\n"
                "📌 Что такое суперсет:\n"
                "Суперсет — это когда два (или больше) упражнения выполняются подряд, без отдыха. Отдых даём только после второго (или последнего) упражнения, и это считается одним подходом.\n\n"
                "Как делать суперсет:\n"
                "1.  Выполни первое упражнение.\n"
                "2.  Сразу переходи ко второму (и третьему, если есть).\n"
                "3.  После выполнения всех — отдых 1,5–2 минуты.\n"
                "4.  Повтори столько раз, сколько указано в программе.\n"
                "👉 «Суперсет» = упражнения подряд + один отдых = один подход.\n"
            ),
        },
        "8-9": {
            "Ягодицы": (
                "🏋️‍ Тренировка А (Ягодицы, 8–9 месяц) — домашняя версия\n\n"
                "🔹 Упражнения по порядку\n"
                "1.  Присед на ягодицы на коврике — 3 подхода × 15-15-15\n"
                "2.  Ягодичный мостик со штангой (на диване/скамье) — 3 подхода × 30-25-20\n"
                "3.  Румынская тяга со штангой/гантелями — 4 подхода × 15-15-12-12\n"
                "4.  Выпады вперёд с гантелями (шагая по комнате) — 3 подхода × 30 шагов\n"
                "5.  Пожарный гидрант — 3 подхода × 15-15-15 на каждую ногу\n\n"
                "📌 Основная нагрузка тренировки:\n"
                "• Ягодицы (главный акцент — во всех упражнениях).\n"
                "• Задняя поверхность бедра (румынская тяга).\n"
                "• Внутренняя поверхность бедра (присед).\n"
                "• Квадрицепсы (выпады).\n"
                "• Средняя ягодичная и стабилизаторы (пожарный гидрант, выпады).\n"
            ),
            "Верх тела": (
                "🏋️‍Тренировка Б (Верх тела, 8–9 месяц) — домашняя версия\n\n"
                "🔹 Упражнения по порядку\n"
                "1.  Тяга резинки сверху к груди (широкий хват) — 4 подхода × 15-15-15-15\n"
                "2.  Тяга резинки к поясу сидя — 4 подхода × 15-15-15-15\n"
                "3.  Тяга штанги/гантелей к поясу — 3 подхода × 15-15-15\n"
                "4.  Обратные разведения с гантелями в наклоне — 3 подхода × 12-12-12\n"
                "5.  Суперсет на плечи: Разведения гантелей в стороны + Жим гантелей сидя — 3 подхода × 15-15-15 и 3 подхода × 20-15-12\n"
                "6.  Отжимания с колен с акцентом на трицепс — 3 подхода × 12-12-12\n"
                "7.  Сгибания рук со штангой/гантелями стоя — 3 подхода × 15-15-15\n\n"
                "📌 Основная нагрузка тренировки:\n"
                "• Спина (тяги резинки сверху/к поясу, тяга к поясу).\n"
                "• Плечи (средние и задние дельты — разведения; передние/средние — жим).\n"
                "• Руки: трицепс (отжимания), бицепс (сгибания стоя).\n"
                "• Кор стабилизирует корпус во всех базовых движениях.\n\n"
                "📌 Что такое суперсет:\n"
                "Суперсет — это когда два (или больше) упражнения выполняются подряд, без отдыха. Отдых даём только после второго (или последнего) упражнения, и это считается одним подходом.\n\n"
                "Как делать суперсет:\n"
                "1.  Выполни первое упражнение.\n"
                "2.  Сразу переходи ко второму (и третьему, если есть).\n"
                "3.  После выполнения всех — отдых 1,5–2 минуты.\n"
                "4.  Повтори столько раз, сколько указано в программе.\n"
                "👉 «Суперсет» = упражнения подряд + один отдых = один подход.\n"
            ),
            "Ноги": (
                "🏋️‍♀️‍ Тренировка C (Ноги, 8–9 месяц) — домашняя версия\n\n"
                "🔹 Упражнения по порядку\n"
                "1.  Суперсет: Приседания со штангой/гантелями (узкая постановка) + Разгибания ног с резинкой сидя — 4×12-12-12-12 и 4×12-12-12-12\n"
                "2.  Приседания на одной ноге к тумбе — 4×10-10-10-10 на каждую ногу\n"
                "3.  Сгибания ног лёжа с резинкой — 4×15-15-15-15\n"
                "4.  Румынская тяга со штангой/гантелями — 4×20-20-15-15\n\n"
                "📌 Основная нагрузка тренировки:\n"
                "• Квадрицепсы (главный акцент — приседания, разгибания, пистолеты).\n"
                "• Бицепсы бедра (сгибания ног, румынская тяга).\n"
                "• Ягодицы (румынская тяга).\n"
                "• Кор и стабилизаторы (приседания на одной ноге).\n\n"
                "📌 Что такое суперсет:\n"
                "Суперсет — это когда два (или больше) упражнения выполняются подряд, без отдыха. Отдых даём только после второго (или последнего) упражнения, и это считается одним подходом.\n\n"
                "Как делать суперсет:\n"
                "1.  Выполни первое упражнение.\n"
                "2.  Сразу переходи ко второму (и третьему, если есть).\n"
                "3.  После выполнения всех — отдых 1,5–2 минуты.\n"
                "4.  Повтори столько раз, сколько указано в программе.\n"
                "👉 «Суперсет» = упражнения подряд + один отдых = один подход.\n"
            ),
        },
        "10-12": {
            "Ягодицы": (
                "🏋️‍♀️‍ Тренировка А (Ягодицы, 10–12 месяц) — домашняя версия\n\n"
                "🔹 Упражнения по порядку\n"
                "1.  Ягодичный мостик со штангой (на диване/скамье) — 5 подходов с прогрессией веса 20-15-15-12-10\n"
                "2.  Болгарские сплит-приседания с гантелями (акцент на ягодицы) — 4 подхода с прогрессией веса 20-15-15-12\n"
                "3.  Приседания сумо с гантелью/штангой — 4 подхода с прогрессией веса 20-15-15-12\n"
                "4.  Суперсет: Разведения ног с резинкой сидя + Махи назад с резинкой в наклоне — 4×20-20-20-20 и 4×15-15-12-12\n\n"
                "📌 Основная нагрузка тренировки:\n"
                "• Ягодицы (главный акцент — мостик, болгарские, махи и отведения).\n"
                "• Бицепсы бедра (сумо-тяга).\n"
                "• Внутренняя поверхность бедра (сумо-тяга, отведения).\n"
                "• Квадрицепсы (болгарские приседания).\n"
                "• Стабилизаторы и мышцы кора (баланс в болгарских приседаниях).\n\n"
                "📌 Что такое суперсет:\n"
                "Суперсет — это когда два (или больше) упражнения выполняются подряд, без отдыха. Отдых даём только после второго (или последнего) упражнения, и это считается одним подходом.\n\n"
                "Как делать суперсет:\n"
                "1.  Выполни первое упражнение.\n"
                "2.  Сразу переходи ко второму (и третьему, если есть).\n"
                "3.  После выполнения всех — отдых 1,5–2 минуты.\n"
                "4.  Повтори столько раз, сколько указано в программе.\n"
                "👉 «Суперсет» = упражнения подряд + один отдых = один подход.\n"
            ),
            "Верх тела": (
                "🏋️‍ Тренировка B (Верх, 10–12 месяц) — домашняя версия\n\n"
                "🔹 Упражнения по порядку:\n"
                "1.  Пуловер с гателей или резинкой лёжа — 3×15-15-15\n"
                "2.  Тяга гантелей в наклоне — 3×15-15-12\n"
                "3.  Суперсет: Жим гантелей сидя + Разведения гантелей в стороны сидя — 3×15-15-12 и 3×15-15-15\n"
                "4.  Сведение рук («бабочка») с резинкой — 3×15-15-15\n"
                "5.  Суперсет: Сгибания рук с супинацией на бицепс + Французский жим с гантелью — 3×15-15-15 и 3×15-15-15\n\n"
                "📌 Основная нагрузка тренировки:\n"
                "• Спина (пуловер, тяга гантелей).\n"
                "• Плечи (жим гантелей, подъёмы в стороны).\n"
                "• Грудь (сведение рук, жим гантелей).\n"
                "• Руки: бицепс (подъём) и трицепс (французский жим).\n"
                "• Кор стабилизирует во всех упражнениях.\n\n"
                "📌 Что такое суперсет:\n"
                "Суперсет — это когда два (или больше) упражнения выполняются подряд, без отдыха. Отдых даём только после второго (или последнего) упражнения, и это считается одним подходом.\n\n"
                "Как делать суперсет:\n"
                "1.  Выполни первое упражнение.\n"
                "2.  Сразу переходи ко второму (и третьему, если есть).\n"
                "3.  После выполнения всех — отдых 1,5–2 минуты.\n"
                "4.  Повтори столько раз, сколько указано в программе.\n"
                "👉 «Суперсет» = упражнения подряд + один отдых = один подход.\n"
            ),
            "Ноги": (
                "🏋️‍Тренировка C (Ноги, 10–12 месяц) — домашняя версия\n\n"
                "🔹 Упражнения по порядку:\n"
                "1.  Фронтальные приседания с гантелью/штангой у груди — 4×20-20-15-15\n"
                "2.  Разгибания ног с резинкой сидя — 3×15-15-15\n"
                "3.  Приседания с узкой постановкой + медленный негатив (4 сек вниз) — 4×15-15-12-12\n"
                "4.  Суперсет: Румынская тяга со штангой/гантелями + Сгибания ног с резинкой лёжа — 4×20-15-15-12 и 4×15-15-15-15 \n"
                "5.  Зашагивания на платформу (стул/скамья) с гантелями — 3×20-15-15 на каждую ногу\n\n"
                "📌 Основная нагрузка тренировки:\n"
                "• Квадрицепсы — фронтальные приседания, разгибания, приседания с узкой постановкой.\n"
                "• Ягодицы — румынская тяга, приседания, зашагивания.\n"
                "• Бицепсы бедра — румынская тяга, сгибания ног.\n"
                "• Кор и стабилизаторы — активно включаются в приседаниях и зашагиваниях.\n\n"
                "📌 Что такое суперсет:\n"
                "Суперсет — это когда два (или больше) упражнения выполняются подряд, без отдыха. Отдых даём только после второго (или последнего) упражнения, и это считается одним подходом.\n\n"
                "Как делать суперсет:\n"
                "1.  Выполни первое упражнение.\n"
                "2.  Сразу переходи ко второму (и третьему, если есть).\n"
                "3.  После выполнения всех — отдых 1,5–2 минуты.\n"
                "4.  Повтори столько раз, сколько указано в программе.\n"
                "👉 «Суперсет» = упражнения подряд + один отдых = один подход.\n"
            ),
        },
    },
}

# ====== TERMS & PAY SUPPORT & DEV ======
async def cmd_terms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Условия использования и оплаты:\n\n"
        "- Подписка даёт доступ на 1 год ко всем тренировкам бота.\n"
        "- Оплата выполняется в Telegram Stars внутри приложения.\n"
        "- Возврат средств возможен в индивидуальном порядке по запросу, возврат возможен в течении 14 дней после покупки подписки.\n"
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

# ====== START ======
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
async def send_subscription_invoice(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    prices = [
        LabeledPrice(
            label="Годовая подписка CORPUS",
            amount=SUBSCRIPTION_PRICE_STARS,
        )
    ]

    await context.bot.send_invoice(
        chat_id=chat_id,
        title="Подписка CORPUS (1 год)",
        description="Разовый платёж за годовой доступ ко всем тренировкам бота.",
        payload=SUBSCRIPTION_PAYLOAD,
        provider_token="",
        currency="XTR",
        prices=prices,
        max_tip_amount=0,
    )


# ====== ОБРАБОТКА ПЛАТЕЖА STARS ======
async def precheckout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query

    if query.invoice_payload != SUBSCRIPTION_PAYLOAD:
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

    # Проверяем правильный payload
    if sp.currency == "XTR" and sp.invoice_payload == SUBSCRIPTION_PAYLOAD:
        sub = create_or_extend_subscription(user_id)
        start, end = sub["start"], sub["end"]

        await update.message.reply_text(
            "Оплата прошла успешно ✅\n"
            "Ваша подписка активирована.\n\n"
            f"Начало: {start.strftime('%d.%m.%Y')}\n"
            f"Окончание: {end.strftime('%d.%m.%Y')}\n\n"
            "Теперь Вам доступен полный набор тренировок.",
            reply_markup=kb_main(),
        )
    else:
        await update.message.reply_text(
            "Получен неизвестный платёж. Напишите в /paysupport."
        )

# ====== ОСНОВНОЙ ХЕНДЛЕР ТЕКСТА ======
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    user_id = update.effective_user.id
    has_sub = user_has_subscription(user_id)

    # возврат в меню
    if text.lower() in ["меню", "вернуться в меню", "/меню", "/menu", "/вернуться в меню"]:
        await start(update, context)
        return

    # Подписка
    if text == "✅Подписка":
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
                "Подписка даёт доступ ко всем тренировкам бота на 1 год.\n\n"
                "Чтобы оформить оплату через Telegram Stars, нажмите «Оформить подписку».",
                reply_markup=ReplyKeyboardMarkup(
                    [["Оформить подписку", "Вернуться в меню"]],
                    resize_keyboard=True,
                ),
                protect_content=True,
            )
        return

    if text == "Оформить подписку":
        await send_subscription_invoice(update, context)
        return

    # Правила
    if text == "⚠️Правила":
        await update.message.reply_text(
           "Условия использования и оплаты:\n\n"
            "- Подписка даёт доступ на 1 год ко всем тренировкам бота.\n"
            "- Оплата выполняется в Telegram Stars внутри приложения.\n"
            "- Возврат средств возможен в индивидуальном порядке по запросу, возврат возможен в течении 14 дней после покупки подписки.\n"
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
                "Чтобы открыть тренировки, сначала оформите подписку 💳\n\n"
                "Нажмите «Подписка» и следуй инструкциям.",
                reply_markup=ReplyKeyboardMarkup(
                    [["Подписка", "Меню"]],
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
            "Подробнее о питании Вы можете посмотреть в данной группе - ",
            reply_markup=ReplyKeyboardMarkup(
                [["Вернуться в меню"]],
                resize_keyboard=True,
            ),
            protect_content=True,
        )
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

        # описания для каждого блока месяцев
        month_descriptions = {
            "1": (
                "🟢 Месяц 1 — адаптация и обучение движению\n"
                "В этот месяц мы работаем по full body, чтобы включить всё тело и сформировать правильные двигательные навыки.\n"
                "Мы подготавливаем суставы, мышцы и нервную систему к регулярным нагрузкам, развиваем нейромышечные связи и учимся чувствовать целевые мышцы. "
                "Это безопасный и обязательный фундамент для дальнейшего прогресса.\n"
                "Выбирайте тренировку 👇"
            ),
            "2-3": (
                "🟡 Месяцы 2–3 — развитие базовой силы\n"
                "Переходим к сплиту ягодицы / верх / ноги. Нагрузка становится выше, а техника — стабильнее.\n"
                "Мы начинаем первое постепенное увеличение рабочих весов, снижаем повторения и развиваем силу в ключевых движениях. "
                "Этот этап закладывает структурную базу для роста мышц и дальнейших этапов.\n"
                "Выбирайте тренировку 👇"
            ),
            "4-5": (
                "🟠 Месяцы 4–5 — рост силы и мышечной массы\n"
                "Тренировки становятся плотнее и объёмнее. Благодаря ранее сформированной технике мы можем безопасно повышать нагрузки.\n"
                "На этом этапе активно растёт сила, увеличивается мышечная масса, тело начинает визуально меняться. "
                "Мы укрепляем фундамент и продвигаем рабочие веса вверх.\n"
                "Выбирайте тренировку 👇"
            ),
            "6-7": (
                "🔴 Месяцы 6–7 — повышение интенсивности и суперсеты\n"
                "Вы уже хорошо контролируете технику, поэтому увеличиваем интенсивность. Появляются суперсеты и более сложные варианты упражнений.\n"
                "Этот этап развивает выносливость, ускоряет метаболизм и улучшает качество выполнения движений в условиях усталости. "
                "Особенный акцент делаем на ягодицы.\n"
                "Выбирайте тренировку 👇"
            ),
            "8-9": (
                "🔵 Месяцы 8–9 — работа над формой и изоляцией\n"
                "После освоения техники и развития силы мы переходим к более точечной работе. Больше изоляции, контролируемый темп, внимание к слабым местам. "
                "Особенно прорабатываем руки, плечи и спину.\n"
                "Мы продолжаем прогрессировать в весах, но основной акцент — на качество выполнения упражнений и формирование рельефа.\n"
                "Выбирайте тренировку 👇"
            ),
            "10-12": (
                "🟣 Месяцы 10–12 — контроль движения и работа над рельефом\n"
                "На этом этапе тренировки становятся максимально осознанными. Мы детально контролируем амплитуду, темп и технику.\n"
                "Прогрессия весов остаётся, но главный фокус — чистое выполнение движений, акцент на нужных мышцах и формирование финального рельефа.\n"
                "Вы уже тренируетесь как опытный атлет.\n"
                "Выбирайте тренировку 👇"
            ),
        }

        # клавиатура: 1 месяц — цифры 1–12, остальные — категории (Ягодицы / Верх / Ноги)
        kb = kb_training_nums() if month_key == "1" else kb_training_abc()

        text_to_send = month_descriptions.get(month_key, "Выбирайте тренировку 👇")

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
    user_id = update.effective_user.id

    # 👉 Админ (из DEV_USER_IDS) тренируется без ограничения
    if user_id not in DEV_USER_IDS:
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
        msgs = await context.bot.send_media_group(chat_id=chat_id, media=media, protect_content=True)
        for m in msgs:
            messages_to_delete.append(m.message_id)
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
                when=86400,  # 24 часа
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