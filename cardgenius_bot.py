import asyncio
import logging
import sqlite3
import uuid
import io
import textwrap
from datetime import datetime, timedelta
from openai import AsyncOpenAI
from PIL import Image, ImageDraw, ImageFont
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from yookassa import Configuration, Payment
import gspread
from google.oauth2.service_account import Credentials

# ─── ЗАГРУЗКА КЛЮЧЕЙ ─────────────────────────────────────────
def load_env(path="/root/.env_card"):
    env = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
    except Exception as e:
        logging.warning(f"Не удалось загрузить {path}: {e}")
    return env

_env = load_env()

# ─── НАСТРОЙКИ ───────────────────────────────────────────────
BOT_TOKEN        = _env.get("BOT_TOKEN", "ВСТАВЬ_ТОКЕН")
OPENAI_KEY       = _env.get("OPENAI_KEY", "ВСТАВЬ_КЛЮЧ")
OWNER_ID         = int(_env.get("OWNER_ID", "549639607"))
SUPPORT_URL      = _env.get("SUPPORT_URL", "https://t.me/Boss023rus")
SPREADSHEET_ID   = _env.get("SPREADSHEET_ID", "")
CREDENTIALS_FILE = _env.get("CREDENTIALS_FILE", "/root/google_credentials.json")

FREE_LIMIT = 5

# ─── ЮКАССА ──────────────────────────────────────────────────
YOOKASSA_SHOP_ID = _env.get("YOOKASSA_SHOP_ID", "1363324")
YOOKASSA_SECRET  = _env.get("YOOKASSA_SECRET", "")
Configuration.account_id = YOOKASSA_SHOP_ID
Configuration.secret_key  = YOOKASSA_SECRET

# ─── ИНИЦИАЛИЗАЦИЯ ───────────────────────────────────────────
bot    = Bot(token=BOT_TOKEN)
dp     = Dispatcher(storage=MemoryStorage())
client = AsyncOpenAI(api_key=OPENAI_KEY)
logging.basicConfig(level=logging.INFO)

ANTHROPIC_KEY = "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"

# ─── FSM СОСТОЯНИЯ ───────────────────────────────────────────
class CardStates(StatesGroup):
    choose_platform  = State()
    waiting_photo    = State()
    waiting_text     = State()

class ReviewStates(StatesGroup):
    waiting_review   = State()

class AuditStates(StatesGroup):
    waiting_card     = State()

class CompetitorStates(StatesGroup):
    waiting_niche    = State()

class UnitStates(StatesGroup):
    waiting_cost     = State()
    waiting_price    = State()
    waiting_category = State()
    waiting_scheme   = State()

class AdStates(StatesGroup):
    waiting_product  = State()

class InfographicStates(StatesGroup):
    choose_platform  = State()
    choose_mode      = State()
    waiting_photo    = State()
    manual_name      = State()
    manual_price     = State()
    manual_benefits  = State()
    manual_tag       = State()
    manual_photo     = State()

# ─── БАЗА ДАННЫХ ─────────────────────────────────────────────
DB = "/root/cardgenius.db"

def init_db():
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        created_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS requests (
        user_id INTEGER PRIMARY KEY,
        count INTEGER DEFAULT 0
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS subscriptions (
        user_id INTEGER PRIMARY KEY,
        plan TEXT DEFAULT '',
        sub_end TEXT DEFAULT ''
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS pending_payments (
        payment_id TEXT PRIMARY KEY,
        user_id INTEGER,
        plan TEXT,
        created_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        platform TEXT,
        feature TEXT,
        result TEXT,
        created_at TEXT
    )""")
    conn.commit()
    conn.close()

def ensure_user(user_id, username="", first_name=""):
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (user_id, username, first_name, created_at) VALUES (?,?,?,?)",
              (user_id, username, first_name, datetime.now().isoformat()))
    c.execute("INSERT OR IGNORE INTO requests (user_id, count) VALUES (?,0)", (user_id,))
    conn.commit()
    conn.close()

def get_requests(user_id):
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT count FROM requests WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else 0

def increment_requests(user_id):
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("UPDATE requests SET count=count+1 WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()

def get_subscription(user_id):
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT plan, sub_end FROM subscriptions WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    if not row or not row[1]:
        return None, None
    sub_end = datetime.fromisoformat(row[1])
    if sub_end > datetime.now():
        return row[0], sub_end
    return None, None

def set_subscription(user_id, plan, days=30):
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    end = (datetime.now() + timedelta(days=days)).isoformat()
    c.execute("INSERT OR REPLACE INTO subscriptions (user_id, plan, sub_end) VALUES (?,?,?)",
              (user_id, plan, end))
    conn.commit()
    conn.close()

def save_pending(payment_id, user_id, plan):
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("INSERT INTO pending_payments (payment_id, user_id, plan, created_at) VALUES (?,?,?,?)",
              (payment_id, user_id, plan, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def get_pending():
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT payment_id, user_id, plan FROM pending_payments")
    rows = c.fetchall()
    conn.close()
    return rows

def delete_pending(payment_id):
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("DELETE FROM pending_payments WHERE payment_id=?", (payment_id,))
    conn.commit()
    conn.close()

def save_history(user_id, platform, feature, result):
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("INSERT INTO history (user_id, platform, feature, result, created_at) VALUES (?,?,?,?,?)",
              (user_id, platform, feature, result[:3000], datetime.now().isoformat()))
    conn.commit()
    conn.close()

def get_history(user_id, limit=10):
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT platform, feature, result, created_at FROM history WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
              (user_id, limit))
    rows = c.fetchall()
    conn.close()
    return rows

def get_all_users():
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT user_id FROM users")
    rows = c.fetchall()
    conn.close()
    return [r[0] for r in rows]

def get_stats():
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users")
    total = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM subscriptions WHERE plan='card_start' AND sub_end > ?", (datetime.now().isoformat(),))
    starts = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM subscriptions WHERE plan='card_pro' AND sub_end > ?", (datetime.now().isoformat(),))
    pros = c.fetchone()[0]
    c.execute("SELECT SUM(count) FROM requests")
    reqs = c.fetchone()[0] or 0
    conn.close()
    return total, starts, pros, reqs

# ─── GOOGLE SHEETS ────────────────────────────────────────────
def sheets_add_user(user_id, username, first_name):
    try:
        if not SPREADSHEET_ID:
            return
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)
        gc = gspread.authorize(creds)
        sheet = gc.open_by_key(SPREADSHEET_ID).sheet1
        col = sheet.col_values(1)
        if not col or col[0] != "ID":
            sheet.insert_row(["ID", "Username", "Имя", "Дата", "Тариф"], 1)
            col = sheet.col_values(1)
        if str(user_id) not in col:
            sheet.append_row([str(user_id), f"@{username}" if username else "—",
                              first_name or "—", datetime.now().strftime("%d.%m.%Y %H:%M"), "Бесплатный"])
    except Exception as e:
        logging.error(f"Sheets user error: {e}")

def sheets_update_sub(user_id, plan):
    try:
        if not SPREADSHEET_ID:
            return
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)
        gc = gspread.authorize(creds)
        sheet = gc.open_by_key(SPREADSHEET_ID).sheet1
        col = sheet.col_values(1)
        if str(user_id) in col:
            row = col.index(str(user_id)) + 1
            plan_name = "Старт" if plan == "card_start" else "Про"
            sheet.update_cell(row, 5, plan_name)
    except Exception as e:
        logging.error(f"Sheets sub error: {e}")

# ─── ДОСТУП И ЛИМИТЫ ─────────────────────────────────────────
def check_access(user_id):
    """Возвращает ('pro'|'start'|'free'|'limit'), plan"""
    plan, sub_end = get_subscription(user_id)
    if plan == "card_pro":
        return "pro", plan
    if plan == "card_start":
        return "start", plan
    count = get_requests(user_id)
    if count < FREE_LIMIT:
        return "free", None
    return "limit", None

def is_pro(user_id):
    plan, _ = get_subscription(user_id)
    return plan == "card_pro"

def is_paid(user_id):
    plan, _ = get_subscription(user_id)
    return plan in ("card_start", "card_pro")

# ─── КЛАВИАТУРЫ ──────────────────────────────────────────────
def kb_main():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 Создать карточку", callback_data="make_card")],
        [InlineKeyboardButton(text="🖼 Инфографика из фото 🔒 Про", callback_data="make_infographic")],
        [InlineKeyboardButton(text="🧮 Юнит-экономика", callback_data="unit_eco"),
         InlineKeyboardButton(text="⭐️ Ответ на отзыв", callback_data="review_reply")],
        [InlineKeyboardButton(text="🔍 Аудит карточки 🔒 Про", callback_data="audit_card"),
         InlineKeyboardButton(text="🕵️ Конкуренты 🔒 Про", callback_data="competitors")],
        [InlineKeyboardButton(text="📣 Рекламный текст 🔒 Про", callback_data="ad_text"),
         InlineKeyboardButton(text="📂 История", callback_data="my_history")],
        [InlineKeyboardButton(text="💎 Тарифы", callback_data="tariffs"),
         InlineKeyboardButton(text="💬 Поддержка", url=SUPPORT_URL)],
    ])

def kb_back():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 В меню", callback_data="back_menu")]
    ])

def kb_platform(prefix="card"):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟣 Wildberries", callback_data=f"{prefix}_wb"),
         InlineKeyboardButton(text="🔵 Ozon", callback_data=f"{prefix}_ozon")],
        [InlineKeyboardButton(text="🟡 Авито", callback_data=f"{prefix}_avito"),
         InlineKeyboardButton(text="✅ Все 3 сразу 🔒 Старт", callback_data=f"{prefix}_all")],
        [InlineKeyboardButton(text="🏠 В меню", callback_data="back_menu")],
    ])

def kb_card_input():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📷 Отправить фото", callback_data="input_photo"),
         InlineKeyboardButton(text="✏️ Описать текстом", callback_data="input_text")],
        [InlineKeyboardButton(text="🏠 В меню", callback_data="back_menu")],
    ])

def kb_tariffs():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟢 Старт — 190 ₽/мес", callback_data="pay_start")],
        [InlineKeyboardButton(text="🔥 Про — 390 ₽/мес", callback_data="pay_pro")],
        [InlineKeyboardButton(text="🏠 В меню", callback_data="back_menu")],
    ])

def kb_upgrade_start():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟢 Купить Старт — 190 ₽", callback_data="pay_start")],
        [InlineKeyboardButton(text="🔥 Купить Про — 390 ₽", callback_data="pay_pro")],
        [InlineKeyboardButton(text="🏠 В меню", callback_data="back_menu")],
    ])

def kb_upgrade_pro():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔥 Купить Про — 390 ₽", callback_data="pay_pro")],
        [InlineKeyboardButton(text="🏠 В меню", callback_data="back_menu")],
    ])

def kb_regen(platform, feature):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Другой вариант", callback_data=f"regen_{feature}_{platform}")],
        [InlineKeyboardButton(text="📂 Сохранить в историю", callback_data="already_saved"),
         InlineKeyboardButton(text="🏠 В меню", callback_data="back_menu")],
    ])

def kb_scheme():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="FBO (склад МП)", callback_data="scheme_fbo"),
         InlineKeyboardButton(text="FBS (свой склад)", callback_data="scheme_fbs")],
        [InlineKeyboardButton(text="🏠 В меню", callback_data="back_menu")],
    ])

def kb_infographic_platform():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟣 Wildberries", callback_data="infographic_wb"),
         InlineKeyboardButton(text="🔵 Ozon", callback_data="infographic_ozon")],
        [InlineKeyboardButton(text="🟡 Авито", callback_data="infographic_avito")],
        [InlineKeyboardButton(text="🏠 В меню", callback_data="back_menu")],
    ])

def kb_infographic_mode():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤖 Авто — ИИ сам всё придумает", callback_data="infmode_auto")],
        [InlineKeyboardButton(text="✏️ Вручную — я укажу текст сам", callback_data="infmode_manual")],
        [InlineKeyboardButton(text="🏠 В меню", callback_data="back_menu")],
    ])

# ─── ПРОМПТЫ ─────────────────────────────────────────────────
PLATFORM_RULES = {
    "wb": """ПЛАТФОРМА: Wildberries
Правила:
- Название: до 60 символов, начинать с главного ключа, без оценочных слов
- Описание: 500–2000 символов, ключи вписать естественно, структура: выгоды → характеристики → применение
- SEO-ключи: 20 штук, высоко- и среднечастотные, через запятую
- Характеристики: бренд, категория, страна, состав/материал, размер (если применимо)
- Запрещено: ссылки, контакты, сравнения с конкурентами, слова "лучший/№1"
Формат ответа строго:
📦 НАЗВАНИЕ:
[название]

📝 ОПИСАНИЕ:
[описание]

🔑 SEO-КЛЮЧИ:
[ключи через запятую]

📋 ХАРАКТЕРИСТИКИ:
[список характеристик]""",

    "ozon": """ПЛАТФОРМА: Ozon
Правила:
- Название: 50–200 символов, формула: Тип + Бренд + Модель + ключевые характеристики
- Описание: rich-текст, 500–4000 символов, абзацы с подзаголовками, акцент на выгоды
- SEO-ключи: 20 штук релевантных фраз
- Характеристики: бренд, страна, гарантия, комплектация, ключевые атрибуты категории
- Запрещено: ссылки, призывы связаться, обещания типа "доставим за час"
Формат ответа строго:
📦 НАЗВАНИЕ:
[название]

📝 ОПИСАНИЕ:
[описание с абзацами]

🔑 SEO-КЛЮЧИ:
[ключи через запятую]

📋 ХАРАКТЕРИСТИКИ:
[список характеристик]""",

    "avito": """ПЛАТФОРМА: Авито
Правила:
- Заголовок: до 50 символов, конкретный, с главным ключом
- Описание: 300–2000 символов, структура: что это → преимущества → характеристики → призыв к действию
- Ключи: 10–15 фраз которые люди вводят в поиске Авито
- Стиль: живой, как человек пишет человеку, без канцелярщины
- Финал: чёткий призыв ("Пишите в чат", "Звоните", "Самовывоз/доставка")
Формат ответа строго:
📦 ЗАГОЛОВОК:
[заголовок]

📝 ОПИСАНИЕ:
[описание]

🔑 КЛЮЧЕВЫЕ СЛОВА:
[ключи через запятую]"""
}

PLATFORM_NAMES = {"wb": "Wildberries", "ozon": "Ozon", "avito": "Авито"}

async def gpt(system, user_text, model="gpt-4o", max_tokens=1500):
    resp = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user_text}
        ],
        max_tokens=max_tokens
    )
    return resp.choices[0].message.content.strip()

async def gpt_vision(system, user_text, image_url, max_tokens=1500):
    import base64, httpx, anthropic
    async with httpx.AsyncClient() as hc:
        r = await hc.get(image_url)
        img_bytes = r.content
    b64 = base64.b64encode(img_bytes).decode()
    aclient = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    resp = aclient.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=max_tokens,
        system=system,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": b64
                    }
                },
                {"type": "text", "text": user_text}
            ]
        }]
    )
    return resp.content[0].text.strip()

async def generate_card_from_text(platform, description):
    rules = PLATFORM_RULES.get(platform, PLATFORM_RULES["wb"])
    system = (
        "Ты эксперт по созданию продающих карточек товаров для маркетплейсов. "
        "Пишешь только на русском. Никогда не начинай с вводных слов типа 'Конечно', 'Отлично', 'Вот'. "
        "Сразу выдаёшь результат в нужном формате.\n\n" + rules
    )
    return await gpt(system, f"Создай карточку для этого товара:\n{description}")

async def generate_card_from_photo(platform, image_url):
    rules = PLATFORM_RULES.get(platform, PLATFORM_RULES["wb"])
    system = (
        "Ты эксперт по созданию продающих карточек товаров для маркетплейсов. "
        "Пишешь только на русском. Никогда не начинай с вводных слов типа 'Конечно', 'Отлично', 'Вот'. "
        "Сразу выдаёшь результат в нужном формате.\n\n" + rules
    )
    user_prompt = (
        "На фото изображён товар. Опиши его тип, назначение, характеристики и создай полную карточку. "
        "Не упоминай бренды и логотипы. Сосредоточься на типе товара и его свойствах."
    )
    return await gpt_vision(system, user_prompt, image_url)

async def generate_review_reply(review_text):
    system = (
        "Ты менеджер маркетплейса, пишешь ответы на отзывы покупателей. "
        "Тон: вежливый, живой, не шаблонный. "
        "На негатив — признаёшь проблему, извиняешься, предлагаешь решение, не оправдываешься. "
        "На позитив — благодаришь с теплом, приглашаешь снова. "
        "На нейтральный — благодаришь, уточняешь что можно улучшить. "
        "Длина ответа: 50–150 слов. Никаких шаблонных фраз типа 'Уважаемый покупатель'. "
        "Не упоминай личные данные покупателя. Пиши только на русском."
    )
    return await gpt(system, f"Напиши ответ на этот отзыв:\n\n{review_text}")

async def generate_audit(card_text):
    system = (
        "Ты эксперт по оптимизации карточек товаров на маркетплейсах WB, Ozon, Авито. "
        "Проводишь детальный аудит карточки и даёшь конкретные правки."
    )
    prompt = (
        f"Проведи аудит этой карточки товара:\n\n{card_text}\n\n"
        "Оцени по пунктам:\n"
        "⭐️ Общая оценка: N/10\n"
        "📦 Название: [оценка + что изменить]\n"
        "📝 Описание: [оценка + что изменить]\n"
        "🔑 SEO-ключи: [есть/нет + рекомендации]\n"
        "📋 Характеристики: [полнота + что добавить]\n"
        "✅ Сильные стороны: [список]\n"
        "⚠️ Главные проблемы: [список]\n"
        "🚀 Топ-3 правки которые дадут результат прямо сейчас:"
    )
    return await gpt(system, prompt, max_tokens=1200)

async def generate_competitors(niche):
    system = (
        "Ты аналитик маркетплейсов. Анализируешь конкурентную среду в нишах WB и Ozon. "
        "Даёшь практичные инсайты без воды."
    )
    prompt = (
        f"Проанализируй нишу: {niche}\n\n"
        "Дай анализ по формату:\n"
        "🎯 АНАЛИЗ НИШИ: {niche}\n\n"
        "📊 Что продают лидеры:\n[что типично в топе]\n\n"
        "🔑 Ключевые слова которые используют:\n[10–15 ключей которые скорее всего в топе]\n\n"
        "🔥 Что заходит покупателям:\n[триггеры покупки, что ценят]\n\n"
        "⚠️ Слабые места конкурентов:\n[где можно выиграть]\n\n"
        "💡 5 идей как выделиться:\n[конкретные УТП]\n\n"
        "✅ Главный совет для входа в нишу:"
    )
    return await gpt(system, prompt, max_tokens=1200)

async def generate_ad_text(product_desc):
    system = (
        "Ты копирайтер для внутренней рекламы на маркетплейсах. "
        "Пишешь продающие тексты по формуле: боль → решение → выгоды → призыв. "
        "Коротко, цепко, без воды. Русский язык."
    )
    prompt = (
        f"Напиши рекламный текст для товара:\n{product_desc}\n\n"
        "Формат:\n"
        "🎯 ЗАГОЛОВОК (до 40 символов):\n[цепляющий заголовок]\n\n"
        "📣 ТЕКСТ ОБЪЯВЛЕНИЯ (100–180 слов):\n"
        "[боль покупателя → как товар решает → 3 конкретных выгоды → призыв к действию]\n\n"
        "🏷️ КОРОТКИЙ СЛОГАН (до 20 символов):\n[слоган для баннера]"
    )
    return await gpt(system, prompt, max_tokens=800)

async def calculate_unit_eco(cost, price, category, scheme):
    # Комиссии по категориям WB (приблизительные актуальные)
    wb_commissions = {
        "одежда": 0.25, "обувь": 0.25, "аксессуары": 0.20,
        "электроника": 0.10, "бытовая техника": 0.10,
        "косметика": 0.20, "товары для дома": 0.17,
        "игрушки": 0.17, "спорт": 0.17, "книги": 0.17,
        "продукты": 0.15, "другое": 0.17
    }
    ozon_commissions = {
        "одежда": 0.50, "обувь": 0.45, "аксессуары": 0.35,
        "электроника": 0.08, "бытовая техника": 0.08,
        "косметика": 0.30, "товары для дома": 0.25,
        "игрушки": 0.20, "спорт": 0.20, "книги": 0.15,
        "продукты": 0.18, "другое": 0.20
    }

    cat_lower = category.lower()
    wb_comm = wb_commissions.get(cat_lower, 0.17)
    oz_comm = ozon_commissions.get(cat_lower, 0.20)

    # Логистика WB (средняя по FBO)
    wb_logistics_fbo = max(50, price * 0.05)
    wb_logistics_fbs = max(80, price * 0.07)
    wb_logistics = wb_logistics_fbo if scheme == "fbo" else wb_logistics_fbs

    # Логистика Ozon (средняя по FBO)
    oz_logistics_fbo = max(70, price * 0.06)
    oz_logistics_fbs = max(100, price * 0.08)
    oz_logistics = oz_logistics_fbo if scheme == "fbo" else oz_logistics_fbs

    # Хранение (примерно)
    storage = price * 0.01

    # WB расчёт
    wb_comm_rub = price * wb_comm
    wb_profit = price - cost - wb_comm_rub - wb_logistics - storage
    wb_margin = (wb_profit / price * 100) if price > 0 else 0

    # Ozon расчёт
    oz_comm_rub = price * oz_comm
    oz_profit = price - cost - oz_comm_rub - oz_logistics - storage
    oz_margin = (oz_profit / price * 100) if price > 0 else 0

    def fmt(v):
        return f"{v:,.0f}".replace(",", " ")

    def verdict(profit, margin):
        if margin >= 30:
            return "✅ Отличная маржинальность"
        elif margin >= 15:
            return "🟡 Приемлемо, но есть риски"
        elif margin >= 0:
            return "🔴 Слабая маржа, пересмотри цену или затраты"
        else:
            return "❌ Убыток! Торговать нельзя"

    text = (
        f"🧮 ЮНИТ-ЭКОНОМИКА\n"
        f"{'─'*30}\n\n"
        f"📦 Товар: {category}\n"
        f"💰 Себестоимость: {fmt(cost)} ₽\n"
        f"🏷️ Цена продажи: {fmt(price)} ₽\n"
        f"📦 Схема: {'FBO (склад МП)' if scheme == 'fbo' else 'FBS (свой склад)'}\n\n"
        f"{'─'*30}\n"
        f"🟣 WILDBERRIES\n"
        f"Комиссия ({int(wb_comm*100)}%): -{fmt(wb_comm_rub)} ₽\n"
        f"Логистика: -{fmt(wb_logistics)} ₽\n"
        f"Хранение: ~-{fmt(storage)} ₽\n"
        f"Прибыль с единицы: {fmt(wb_profit)} ₽\n"
        f"Маржинальность: {wb_margin:.1f}%\n"
        f"{verdict(wb_profit, wb_margin)}\n\n"
        f"🔵 OZON\n"
        f"Комиссия ({int(oz_comm*100)}%): -{fmt(oz_comm_rub)} ₽\n"
        f"Логистика: -{fmt(oz_logistics)} ₽\n"
        f"Хранение: ~-{fmt(storage)} ₽\n"
        f"Прибыль с единицы: {fmt(oz_profit)} ₽\n"
        f"Маржинальность: {oz_margin:.1f}%\n"
        f"{verdict(oz_profit, oz_margin)}\n\n"
        f"{'─'*30}\n"
        f"📊 Чтобы зарабатывать 50 000 ₽/мес:\n"
        f"WB: надо продавать {max(1, int(50000/wb_profit)) if wb_profit > 0 else '∞'} шт/мес\n"
        f"Ozon: надо продавать {max(1, int(50000/oz_profit)) if oz_profit > 0 else '∞'} шт/мес\n\n"
        f"⚠️ Расчёт приблизительный. Точные тарифы — в личном кабинете МП."
    )
    return text

# ─── ХЕНДЛЕРЫ ────────────────────────────────────────────────

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    user = message.from_user
    ensure_user(user.id, user.username or "", user.first_name or "")
    asyncio.create_task(asyncio.to_thread(
        sheets_add_user, user.id, user.username or "", user.first_name or ""
    ))
    name = user.first_name or "друг"
    plan, sub_end = get_subscription(user.id)
    if plan:
        plan_str = "🟢 Старт" if plan == "card_start" else "🔥 Про"
        plan_line = f"Твой тариф: {plan_str} (до {sub_end.strftime('%d.%m.%Y')})"
    else:
        count = get_requests(user.id)
        plan_line = f"Бесплатных запросов осталось: {max(0, FREE_LIMIT - count)} из {FREE_LIMIT}"

    await message.answer(
        f"Привет, {name}! 👋\n\n"
        f"Я КарточкаПро — бот для создания продающих карточек товаров.\n\n"
        f"Что умею:\n"
        f"📦 Карточки для WB, Ozon и Авито — из фото или текста\n"
        f"🧮 Юнит-экономика — считаю прибыль с учётом комиссий\n"
        f"⭐️ Ответы на отзывы — грамотно и без шаблонов\n"
        f"🔍 Аудит карточки — нахожу что мешает продавать\n"
        f"🕵️ Анализ конкурентов — ключи и тактики лидеров\n"
        f"📣 Рекламный текст — продающий оффер для объявлений\n\n"
        f"{plan_line}\n\n"
        f"Выбирай с чего начнём 👇",
        reply_markup=kb_main()
    )

@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    if message.from_user.id != OWNER_ID:
        return
    total, starts, pros, reqs = get_stats()
    await message.answer(
        f"📊 Статистика КарточкаПро\n\n"
        f"👤 Всего пользователей: {total}\n"
        f"🟢 Тариф Старт: {starts}\n"
        f"🔥 Тариф Про: {pros}\n"
        f"⚡️ Всего запросов: {reqs}\n"
        f"💰 Доход/мес (est.): {starts*190 + pros*390} ₽"
    )

@dp.message(Command("give"))
async def cmd_give(message: Message):
    if message.from_user.id != OWNER_ID:
        return
    parts = message.text.split()
    if len(parts) != 3:
        await message.answer("Формат: /give USER_ID plan (card_start|card_pro)")
        return
    uid, plan = int(parts[1]), parts[2]
    set_subscription(uid, plan, 30)
    await message.answer(f"✅ Тариф {plan} выдан пользователю {uid} на 30 дней")

# ─── ГЛАВНОЕ МЕНЮ ────────────────────────────────────────────

@dp.callback_query(F.data == "back_menu")
async def back_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    name = call.from_user.first_name or "друг"
    plan, sub_end = get_subscription(call.from_user.id)
    if plan:
        plan_str = "🟢 Старт" if plan == "card_start" else "🔥 Про"
        plan_line = f"Твой тариф: {plan_str}"
    else:
        count = get_requests(call.from_user.id)
        plan_line = f"Бесплатных запросов: {max(0, FREE_LIMIT - count)} из {FREE_LIMIT}"
    await call.message.answer(
        f"Главное меню, {name} 👇\n{plan_line}",
        reply_markup=kb_main()
    )
    await call.answer()

# ─── СОЗДАНИЕ КАРТОЧКИ ───────────────────────────────────────

@dp.callback_query(F.data == "make_card")
async def make_card(call: CallbackQuery, state: FSMContext):
    await state.clear()
    access, _ = check_access(call.from_user.id)
    if access == "limit":
        await call.message.answer(
            f"🚫 Бесплатные запросы закончились ({FREE_LIMIT} шт.)\n\n"
            "Выбери тариф чтобы продолжить:",
            reply_markup=kb_upgrade_start()
        )
        await call.answer()
        return
    await call.message.answer(
        "📦 Создание карточки\n\nВыбери платформу:",
        reply_markup=kb_platform("card")
    )
    await call.answer()

@dp.callback_query(F.data.startswith("card_"))
async def card_platform(call: CallbackQuery, state: FSMContext):
    platform = call.data.replace("card_", "")
    if platform == "all" and not is_paid(call.from_user.id):
        await call.message.answer(
            "🔒 Карточки сразу для всех 3 платформ доступны в тарифе Старт и выше.\n\n"
            "На бесплатном — выбери одну платформу.",
            reply_markup=kb_upgrade_start()
        )
        await call.answer()
        return
    await state.update_data(platform=platform)
    await state.set_state(CardStates.choose_platform)
    await call.message.answer(
        f"Платформа: {'все 3 сразу' if platform == 'all' else PLATFORM_NAMES.get(platform, platform)}\n\n"
        "Как хочешь описать товар?",
        reply_markup=kb_card_input()
    )
    await call.answer()

@dp.callback_query(F.data == "input_photo", CardStates.choose_platform)
async def input_photo(call: CallbackQuery, state: FSMContext):
    await state.set_state(CardStates.waiting_photo)
    await call.message.answer(
        "📷 Отправь фото товара — я определю что это и создам карточку.",
        reply_markup=kb_back()
    )
    await call.answer()

@dp.callback_query(F.data == "input_text", CardStates.choose_platform)
async def input_text_btn(call: CallbackQuery, state: FSMContext):
    await state.set_state(CardStates.waiting_text)
    await call.message.answer(
        "✏️ Опиши товар своими словами.\n\n"
        "Чем подробнее — тем лучше карточка. Можно написать:\n"
        "— что за товар\n"
        "— материал / состав\n"
        "— размеры / цвет\n"
        "— для кого / зачем\n"
        "— любые особенности",
        reply_markup=kb_back()
    )
    await call.answer()

@dp.message(CardStates.waiting_photo, F.photo)
async def process_photo(message: Message, state: FSMContext):
    data = await state.get_data()
    platform = data.get("platform", "wb")
    user_id = message.from_user.id

    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    image_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file.file_path}"

    await message.answer("⏳ Анализирую фото и создаю карточку...")

    try:
        if platform == "all":
            results = []
            for pl in ["wb", "ozon", "avito"]:
                r = await generate_card_from_photo(pl, image_url)
                results.append(f"{'─'*30}\n{PLATFORM_NAMES[pl].upper()}\n{'─'*30}\n{r}")
                save_history(user_id, pl, "card_photo", r)
            result = "\n\n".join(results)
            await message.answer(result[:4000], reply_markup=kb_back())
            if len(result) > 4000:
                await message.answer(result[4000:8000], reply_markup=kb_back())
        else:
            result = await generate_card_from_photo(platform, image_url)
            save_history(user_id, platform, "card_photo", result)
            await message.answer(result, reply_markup=kb_regen(platform, "photo"))

        access, _ = check_access(user_id)
        if access == "free":
            increment_requests(user_id)

        await state.clear()
    except Exception as e:
        logging.error(f"Photo card error: {e}")
        await message.answer("❌ Ошибка генерации. Попробуй ещё раз.", reply_markup=kb_back())

@dp.message(CardStates.waiting_text, F.text)
async def process_text(message: Message, state: FSMContext):
    data = await state.get_data()
    platform = data.get("platform", "wb")
    user_id = message.from_user.id

    await message.answer("⏳ Создаю карточку...")

    try:
        if platform == "all":
            results = []
            for pl in ["wb", "ozon", "avito"]:
                r = await generate_card_from_text(pl, message.text)
                results.append(f"{'─'*30}\n{PLATFORM_NAMES[pl].upper()}\n{'─'*30}\n{r}")
                save_history(user_id, pl, "card_text", r)
            result = "\n\n".join(results)
            await message.answer(result[:4000], reply_markup=kb_back())
            if len(result) > 4000:
                await message.answer(result[4000:8000], reply_markup=kb_back())
        else:
            result = await generate_card_from_text(platform, message.text)
            save_history(user_id, platform, "card_text", result)
            await message.answer(result, reply_markup=kb_regen(platform, "text"))

        access, _ = check_access(user_id)
        if access == "free":
            increment_requests(user_id)

        await state.update_data(last_desc=message.text, platform=platform)
    except Exception as e:
        logging.error(f"Text card error: {e}")
        await message.answer("❌ Ошибка генерации. Попробуй ещё раз.", reply_markup=kb_back())

@dp.callback_query(F.data.startswith("regen_"))
async def regen_card(call: CallbackQuery, state: FSMContext):
    parts = call.data.split("_")
    feature = parts[1]
    platform = parts[2] if len(parts) > 2 else "wb"
    data = await state.get_data()
    last_desc = data.get("last_desc", "")

    if not last_desc:
        await call.message.answer(
            "Для перегенерации отправь описание товара заново.",
            reply_markup=kb_back()
        )
        await call.answer()
        return

    await call.message.answer("⏳ Генерирую другой вариант...")
    try:
        result = await generate_card_from_text(platform, last_desc)
        save_history(call.from_user.id, platform, "card_regen", result)
        await call.message.answer(result, reply_markup=kb_regen(platform, feature))
    except Exception as e:
        await call.message.answer("❌ Ошибка. Попробуй ещё раз.", reply_markup=kb_back())
    await call.answer()

@dp.callback_query(F.data == "already_saved")
async def already_saved(call: CallbackQuery):
    await call.answer("✅ Карточка уже сохранена в историю!", show_alert=False)

# ─── ЮНИТ-ЭКОНОМИКА ─────────────────────────────────────────

@dp.callback_query(F.data == "unit_eco")
async def unit_eco_start(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(UnitStates.waiting_cost)
    await call.message.answer(
        "🧮 Юнит-экономика\n\n"
        "Посчитаю твою прибыль с учётом комиссий WB и Ozon, логистики и хранения.\n\n"
        "Шаг 1/4 — Введи себестоимость товара (закупочная цена + доставка до склада) в рублях:\n\n"
        "Например: 350",
        reply_markup=kb_back()
    )
    await call.answer()

@dp.message(UnitStates.waiting_cost, F.text)
async def unit_cost(message: Message, state: FSMContext):
    try:
        cost = float(message.text.strip().replace(",", ".").replace(" ", ""))
        await state.update_data(cost=cost)
        await state.set_state(UnitStates.waiting_price)
        await message.answer(
            f"✅ Себестоимость: {cost:,.0f} ₽\n\n"
            "Шаг 2/4 — Введи цену продажи для покупателя в рублях:\n\n"
            "Например: 990",
            reply_markup=kb_back()
        )
    except ValueError:
        await message.answer("Введи число. Например: 350")

@dp.message(UnitStates.waiting_price, F.text)
async def unit_price(message: Message, state: FSMContext):
    try:
        price = float(message.text.strip().replace(",", ".").replace(" ", ""))
        await state.update_data(price=price)
        await state.set_state(UnitStates.waiting_category)
        await message.answer(
            f"✅ Цена продажи: {price:,.0f} ₽\n\n"
            "Шаг 3/4 — Напиши категорию товара:\n\n"
            "Одежда / Обувь / Электроника / Бытовая техника / Косметика / "
            "Товары для дома / Игрушки / Спорт / Продукты / Другое",
            reply_markup=kb_back()
        )
    except ValueError:
        await message.answer("Введи число. Например: 990")

@dp.message(UnitStates.waiting_category, F.text)
async def unit_category(message: Message, state: FSMContext):
    await state.update_data(category=message.text.strip())
    await state.set_state(UnitStates.waiting_scheme)
    await message.answer(
        f"✅ Категория: {message.text.strip()}\n\n"
        "Шаг 4/4 — Выбери схему работы:",
        reply_markup=kb_scheme()
    )

@dp.callback_query(F.data.startswith("scheme_"), UnitStates.waiting_scheme)
async def unit_scheme(call: CallbackQuery, state: FSMContext):
    scheme = call.data.replace("scheme_", "")
    data = await state.get_data()
    await state.clear()

    await call.message.answer("⏳ Считаю...")
    try:
        result = await calculate_unit_eco(
            data["cost"], data["price"], data["category"], scheme
        )
        save_history(call.from_user.id, "all", "unit_eco", result)
        await call.message.answer(result, reply_markup=kb_back())
    except Exception as e:
        logging.error(f"Unit eco error: {e}")
        await call.message.answer("❌ Ошибка расчёта. Попробуй ещё раз.", reply_markup=kb_back())
    await call.answer()

# ─── ОТВЕТЫ НА ОТЗЫВЫ ────────────────────────────────────────

@dp.callback_query(F.data == "review_reply")
async def review_start(call: CallbackQuery, state: FSMContext):
    access, _ = check_access(call.from_user.id)
    if access == "limit":
        await call.message.answer(
            "🚫 Бесплатные запросы закончились.",
            reply_markup=kb_upgrade_start()
        )
        await call.answer()
        return
    await state.clear()
    await state.set_state(ReviewStates.waiting_review)
    await call.message.answer(
        "⭐️ Ответ на отзыв\n\n"
        "Вставь текст отзыва покупателя — я напишу грамотный живой ответ.\n\n"
        "Работает с любым отзывом: негативным, позитивным, нейтральным.",
        reply_markup=kb_back()
    )
    await call.answer()

@dp.message(ReviewStates.waiting_review, F.text)
async def process_review(message: Message, state: FSMContext):
    user_id = message.from_user.id
    await message.answer("⏳ Пишу ответ...")
    try:
        result = await generate_review_reply(message.text)
        save_history(user_id, "—", "review", result)
        access, _ = check_access(user_id)
        if access == "free":
            increment_requests(user_id)
        await message.answer(
            f"⭐️ Готовый ответ:\n\n{result}",
            reply_markup=kb_back()
        )
        await state.clear()
    except Exception as e:
        logging.error(f"Review error: {e}")
        await message.answer("❌ Ошибка. Попробуй ещё раз.", reply_markup=kb_back())

# ─── АУДИТ КАРТОЧКИ (ПРО) ────────────────────────────────────

@dp.callback_query(F.data == "audit_card")
async def audit_start(call: CallbackQuery, state: FSMContext):
    if not is_paid(call.from_user.id):
        await call.message.answer(
            "🔒 Аудит карточки доступен с тарифа Старт.\n\n"
            "Бот разберёт твою текущую карточку по пунктам и даст конкретные правки.",
            reply_markup=kb_upgrade_start()
        )
        await call.answer()
        return
    await state.clear()
    await state.set_state(AuditStates.waiting_card)
    await call.message.answer(
        "🔍 Аудит карточки\n\n"
        "Вставь текст своей текущей карточки товара (название + описание + характеристики).\n\n"
        "Дам оценку по 10 пунктам и скажу что конкретно исправить.",
        reply_markup=kb_back()
    )
    await call.answer()

@dp.message(AuditStates.waiting_card, F.text)
async def process_audit(message: Message, state: FSMContext):
    user_id = message.from_user.id
    await message.answer("⏳ Провожу аудит...")
    try:
        result = await generate_audit(message.text)
        save_history(user_id, "—", "audit", result)
        await message.answer(result, reply_markup=kb_back())
        await state.clear()
    except Exception as e:
        logging.error(f"Audit error: {e}")
        await message.answer("❌ Ошибка. Попробуй ещё раз.", reply_markup=kb_back())

# ─── АНАЛИЗ КОНКУРЕНТОВ (ПРО) ────────────────────────────────

@dp.callback_query(F.data == "competitors")
async def competitors_start(call: CallbackQuery, state: FSMContext):
    if not is_pro(call.from_user.id):
        await call.message.answer(
            "🔒 Анализ конкурентов доступен в тарифе Про.\n\n"
            "Получишь: ключевые слова лидеров, слабые места конкурентов и 5 идей как выделиться.",
            reply_markup=kb_upgrade_pro()
        )
        await call.answer()
        return
    await state.clear()
    await state.set_state(CompetitorStates.waiting_niche)
    await call.message.answer(
        "🕵️ Анализ конкурентов\n\n"
        "Напиши нишу для анализа.\n\n"
        "Например: силиконовые формы для выпечки, мужские носки, детские конструкторы",
        reply_markup=kb_back()
    )
    await call.answer()

@dp.message(CompetitorStates.waiting_niche, F.text)
async def process_competitors(message: Message, state: FSMContext):
    user_id = message.from_user.id
    await message.answer("⏳ Анализирую нишу...")
    try:
        result = await generate_competitors(message.text)
        save_history(user_id, "WB+Ozon", "competitors", result)
        await message.answer(result, reply_markup=kb_back())
        await state.clear()
    except Exception as e:
        logging.error(f"Competitors error: {e}")
        await message.answer("❌ Ошибка. Попробуй ещё раз.", reply_markup=kb_back())

# ─── РЕКЛАМНЫЙ ТЕКСТ (ПРО) ───────────────────────────────────

@dp.callback_query(F.data == "ad_text")
async def ad_start(call: CallbackQuery, state: FSMContext):
    if not is_pro(call.from_user.id):
        await call.message.answer(
            "🔒 Рекламный текст доступен в тарифе Про.\n\n"
            "Пишу продающий оффер для внутренней рекламы на WB и Ozon.",
            reply_markup=kb_upgrade_pro()
        )
        await call.answer()
        return
    await state.clear()
    await state.set_state(AdStates.waiting_product)
    await call.message.answer(
        "📣 Рекламный текст\n\n"
        "Опиши товар — напишу продающий текст для внутренней рекламы на маркетплейсах.\n\n"
        "Что написать: название товара, главные преимущества, для кого он.",
        reply_markup=kb_back()
    )
    await call.answer()

@dp.message(AdStates.waiting_product, F.text)
async def process_ad(message: Message, state: FSMContext):
    user_id = message.from_user.id
    await message.answer("⏳ Создаю рекламный текст...")
    try:
        result = await generate_ad_text(message.text)
        save_history(user_id, "—", "ad_text", result)
        await message.answer(result, reply_markup=kb_back())
        await state.clear()
    except Exception as e:
        logging.error(f"Ad text error: {e}")
        await message.answer("❌ Ошибка. Попробуй ещё раз.", reply_markup=kb_back())

# ─── ИСТОРИЯ ─────────────────────────────────────────────────

@dp.callback_query(F.data == "my_history")
async def my_history(call: CallbackQuery):
    user_id = call.from_user.id
    if not is_paid(user_id):
        await call.message.answer(
            "🔒 История доступна в тарифе Старт и выше.",
            reply_markup=kb_upgrade_start()
        )
        await call.answer()
        return
    rows = get_history(user_id, limit=5)
    if not rows:
        await call.message.answer("История пуста. Создай первую карточку!", reply_markup=kb_back())
        await call.answer()
        return
    feature_names = {
        "card_photo": "Карточка из фото",
        "card_text": "Карточка из текста",
        "card_regen": "Перегенерация",
        "unit_eco": "Юнит-экономика",
        "review": "Ответ на отзыв",
        "audit": "Аудит карточки",
        "competitors": "Анализ конкурентов",
        "ad_text": "Рекламный текст",
    }
    text = "📂 Последние 5 результатов:\n\n"
    for platform, feature, result, created_at in rows:
        dt = datetime.fromisoformat(created_at).strftime("%d.%m %H:%M")
        fname = feature_names.get(feature, feature)
        text += f"📌 {fname} | {platform} | {dt}\n"
        text += f"{result[:200]}...\n\n"
    await call.message.answer(text, reply_markup=kb_back())
    await call.answer()

# ─── ТАРИФЫ И ОПЛАТА ─────────────────────────────────────────

@dp.callback_query(F.data == "tariffs")
async def tariffs(call: CallbackQuery):
    user_id = call.from_user.id
    plan, sub_end = get_subscription(user_id)
    count = get_requests(user_id)

    if plan == "card_pro":
        status = f"Твой тариф: 🔥 Про (до {sub_end.strftime('%d.%m.%Y')})"
    elif plan == "card_start":
        status = f"Твой тариф: 🟢 Старт (до {sub_end.strftime('%d.%m.%Y')})"
    else:
        status = f"Использовано бесплатных: {count} из {FREE_LIMIT}"

    await call.message.answer(
        f"💎 Тарифы КарточкаПро\n\n"
        f"{status}\n\n"
        f"🆓 Бесплатно:\n"
        f"• {FREE_LIMIT} запросов на старте\n"
        f"• Карточки для одной платформы\n\n"
        f"🟢 Старт — 190 ₽/мес:\n"
        f"• Безлимит карточек\n"
        f"• Все 3 платформы сразу (WB + Ozon + Авито)\n"
        f"• Юнит-экономика без ограничений\n"
        f"• Ответы на отзывы\n"
        f"• Аудит карточки\n"
        f"• История последних 5 результатов\n\n"
        f"🔥 Про — 390 ₽/мес:\n"
        f"• Всё из Старта\n"
        f"• Анализ конкурентов\n"
        f"• Рекламный текст для МП\n"
        f"• Приоритетная поддержка\n",
        reply_markup=kb_tariffs()
    )
    await call.answer()

async def create_payment(user_id, amount, plan, description):
    payment = Payment.create({
        "amount": {"value": f"{amount}.00", "currency": "RUB"},
        "confirmation": {"type": "redirect", "return_url": "https://t.me/PostGeniusHelperBot"},
        "capture": True,
        "description": f"{description} — пользователь {user_id}",
        "receipt": {
            "customer": {"email": "client@cardgenius.ru"},
            "items": [{
                "description": description,
                "quantity": "1.00",
                "amount": {"value": f"{amount}.00", "currency": "RUB"},
                "vat_code": 1,
                "payment_subject": "service",
                "payment_mode": "full_payment"
            }]
        },
        "metadata": {"user_id": user_id, "plan": plan}
    }, str(uuid.uuid4()))
    return payment

@dp.callback_query(F.data == "pay_start")
async def pay_start(call: CallbackQuery):
    user_id = call.from_user.id
    await call.answer()
    try:
        payment = await create_payment(user_id, 190, "card_start", "Тариф Старт КарточкаПро 30 дней")
        save_pending(payment.id, user_id, "card_start")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Оплатить 190 ₽", url=payment.confirmation.confirmation_url)],
            [InlineKeyboardButton(text="🏠 В меню", callback_data="back_menu")]
        ])
        await call.message.answer(
            "🟢 Тариф Старт — 190 ₽ / 30 дней\n\n"
            "Нажми кнопку ниже для оплаты.\n"
            "Подписка активируется автоматически в течение 15 секунд! ✅",
            reply_markup=kb
        )
    except Exception as e:
        logging.error(f"Payment error: {e}")
        await call.message.answer(f"❌ Ошибка оплаты. Обратись в поддержку: {SUPPORT_URL}")

@dp.callback_query(F.data == "pay_pro")
async def pay_pro(call: CallbackQuery):
    user_id = call.from_user.id
    await call.answer()
    try:
        payment = await create_payment(user_id, 390, "card_pro", "Тариф Про КарточкаПро 30 дней")
        save_pending(payment.id, user_id, "card_pro")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Оплатить 390 ₽", url=payment.confirmation.confirmation_url)],
            [InlineKeyboardButton(text="🏠 В меню", callback_data="back_menu")]
        ])
        await call.message.answer(
            "🔥 Тариф Про — 390 ₽ / 30 дней\n\n"
            "Нажми кнопку ниже для оплаты.\n"
            "Подписка активируется автоматически в течение 15 секунд! ✅",
            reply_markup=kb
        )
    except Exception as e:
        logging.error(f"Payment error: {e}")
        await call.message.answer(f"❌ Ошибка оплаты. Обратись в поддержку: {SUPPORT_URL}")

# ─── ПРОВЕРКА ПЛАТЕЖЕЙ ───────────────────────────────────────

async def check_payments_loop():
    while True:
        await asyncio.sleep(15)
        try:
            for payment_id, user_id, plan in get_pending():
                try:
                    payment = Payment.find_one(payment_id)
                    if payment.status == "succeeded":
                        set_subscription(user_id, plan, 30)
                        delete_pending(payment_id)
                        asyncio.create_task(asyncio.to_thread(sheets_update_sub, user_id, plan))
                        plan_name = "🟢 Старт" if plan == "card_start" else "🔥 Про"
                        await bot.send_message(
                            user_id,
                            f"✅ Оплата прошла!\n\n"
                            f"Тариф {plan_name} активирован на 30 дней. Удачных продаж! 🚀",
                            reply_markup=kb_main()
                        )
                    elif payment.status == "canceled":
                        delete_pending(payment_id)
                        await bot.send_message(
                            user_id,
                            "❌ Платёж отменён. Попробуй снова через меню.",
                            reply_markup=kb_main()
                        )
                except Exception as e:
                    logging.error(f"Payment check {payment_id}: {e}")
        except Exception as e:
            logging.error(f"Payment loop error: {e}")


# ─── ИНФОГРАФИКА ─────────────────────────────────────────────

# Шрифты — пробуем системные, падаем на встроенный Pillow
def get_font(size, bold=False):
    candidates_bold = [
        "/root/fonts/Roboto-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    ]
    candidates_reg = [
        "/root/fonts/Roboto-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    ]
    import os
    candidates = candidates_bold if bold else candidates_reg
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except:
                continue
    return ImageFont.load_default()

PLATFORM_STYLES = {
    "wb":    {"header": (103, 0, 255),  "panel": (240, 235, 255), "label": "Wildberries"},
    "ozon":  {"header": (0, 91, 255),   "panel": (230, 240, 255), "label": "Ozon"},
    "avito": {"header": (0, 155, 119),  "panel": (230, 248, 242), "label": "Авито"},
}

async def get_infographic_data(image_url: str, platform: str) -> dict:
    platform_label = PLATFORM_STYLES[platform]["label"]
    prompt = (
        "Ты помощник по созданию карточек товаров. На фото изображён товар. "
        "Определи тип товара и его назначение (не упоминай бренды). "
        "Также определи в какой части фото расположен основной объект товара. "
        "Верни ТОЛЬКО JSON без markdown, без пояснений.\n"
        "Платформа: " + platform_label + "\n"
        'Формат JSON строго:\n'
        '{\n'
        '  "name": "краткое название товара до 40 символов",\n'
        '  "price": "примерная цена с рублевым знаком или пустая строка",\n'
        '  "benefits": ["выгода 1 до 28 символов", "выгода 2", "выгода 3", "выгода 4"],\n'
        '  "tag": "короткий слоган до 25 символов",\n'
        '  "product_side": "left или right или center — где находится основной товар на фото"\n'
        '}'
    )
    import base64, httpx, anthropic, json, re
    async with httpx.AsyncClient() as hc:
        r = await hc.get(image_url)
        img_bytes = r.content
    b64 = base64.b64encode(img_bytes).decode()
    aclient = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    resp = aclient.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=400,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": b64
                    }
                },
                {"type": "text", "text": prompt}
            ]
        }]
    )
    raw = resp.content[0].text.strip()
    raw = re.sub(r"```json|```", "", raw).strip()
    return json.loads(raw)

SIZE = 1000

def draw_infographic(img_bytes: bytes, data: dict, platform: str) -> bytes:
    STYLES = {
        "wb":    {"accent": (138, 43, 226), "accent2": (180, 100, 255), "label": "Wildberries"},
        "ozon":  {"accent": (0, 100, 255),  "accent2": (60, 150, 255),  "label": "Ozon"},
        "avito": {"accent": (0, 168, 132),  "accent2": (0, 210, 165),   "label": "Авито"},
    }
    style = STYLES[platform]
    ac  = style["accent"]
    ac2 = style["accent2"]

    # Определяем с какой стороны товар — панель ставим напротив
    product_side = data.get("product_side", "right").lower()
    if product_side == "left":
        panel_on_right = True   # товар слева — панель справа
    elif product_side == "right":
        panel_on_right = False  # товар справа — панель слева
    else:
        panel_on_right = False  # центр — панель слева

    product = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
    w, h = product.size
    side = min(w, h)
    left_crop = (w - side) // 2
    top_crop  = (h - side) // 2
    product = product.crop((left_crop, top_crop, left_crop + side, top_crop + side))
    product = product.resize((SIZE, SIZE), Image.LANCZOS)

    canvas = Image.new("RGBA", (SIZE, SIZE), (0,0,0,0))
    canvas.paste(product, (0, 0))

    # Тёмный градиент снизу
    overlay = Image.new("RGBA", (SIZE, SIZE), (0,0,0,0))
    draw_ov = ImageDraw.Draw(overlay)
    for y in range(SIZE):
        if y > SIZE * 0.6:
            alpha = min(210, int((y - SIZE * 0.6) / (SIZE * 0.4) * 210))
            draw_ov.line([(0, y), (SIZE, y)], fill=(10, 10, 20, alpha))
    canvas = Image.alpha_composite(canvas, overlay)

    # Боковая панель — с нужной стороны
    panel_w = 340
    side_panel = Image.new("RGBA", (SIZE, SIZE), (0,0,0,0))
    draw_sp = ImageDraw.Draw(side_panel)
    if panel_on_right:
        px = SIZE - panel_w
        draw_sp.rectangle([(px, 0), (SIZE, SIZE - 130)], fill=(10, 10, 20, 210))
        # Акцентная вертикальная линия
        draw_sp.rectangle([(px, 0), (px + 4, SIZE - 130)], fill=(*ac, 255))
    else:
        px = 0
        draw_sp.rectangle([(0, 0), (panel_w, SIZE - 130)], fill=(10, 10, 20, 210))
        # Акцентная вертикальная линия
        draw_sp.rectangle([(panel_w - 4, 0), (panel_w, SIZE - 130)], fill=(*ac, 255))
    canvas = Image.alpha_composite(canvas, side_panel)

    draw = ImageDraw.Draw(canvas)

    fnt_big   = get_font(46, bold=True)
    fnt_med   = get_font(24, bold=True)
    fnt_small = get_font(20, bold=False)
    fnt_label = get_font(16, bold=True)

    # Верхняя акцентная полоса
    draw.rectangle([(0, 0), (SIZE, 5)], fill=ac)

    # Текст панели
    tx = px + 16 if panel_on_right else 16

    # Название платформы
    draw.text((tx, 18), style["label"].upper(), font=fnt_label, fill=ac2)

    # Название товара
    name = data.get("name", "Товар")
    y_cur = 44
    for line in textwrap.wrap(name, 18)[:2]:
        draw.text((tx, y_cur), line, font=fnt_med, fill="white")
        y_cur += 32

    # Разделитель
    draw.rectangle([(tx, y_cur + 8), (tx + 30, y_cur + 10)], fill=ac)
    draw.rectangle([(tx + 34, y_cur + 8), (tx + panel_w - 20, y_cur + 10)], fill=(70, 70, 90))
    y_cur += 26

    # Преимущества
    benefits = data.get("benefits", [])[:4]
    for i, b in enumerate(benefits):
        # Номерной бейдж
        draw.rectangle([(tx, y_cur), (tx + 26, y_cur + 26)], fill=ac)
        draw.text((tx + 6, y_cur + 4), str(i+1), font=fnt_small, fill="white")
        # Текст
        lines = textwrap.wrap(b[:34], 16)[:2]
        for li, ln in enumerate(lines):
            draw.text((tx + 34, y_cur + li * 21), ln, font=fnt_small, fill=(210, 210, 225))
        y_cur += max(50, 21 * len(lines) + 14)

    # Нижняя тёмная полоса на всю ширину
    draw.rectangle([(0, SIZE - 128), (SIZE, SIZE)], fill=(10, 10, 20, 235))
    draw.rectangle([(0, SIZE - 132), (SIZE, SIZE - 128)], fill=ac)

    price = data.get("price", "")
    tag   = data.get("tag", "")
    if price:
        draw.text((28, SIZE - 112), price, font=fnt_big, fill="white")
        if tag:
            draw.text((28, SIZE - 52), tag, font=fnt_med, fill=ac2)
    elif tag:
        draw.text((28, SIZE - 100), tag, font=fnt_big, fill="white")

    # Бейдж платформы справа внизу
    badge_w = 175
    draw.rectangle([(SIZE - badge_w - 16, SIZE - 48), (SIZE - 16, SIZE - 16)], fill=ac)
    draw.text((SIZE - badge_w - 4, SIZE - 44), style["label"], font=fnt_med, fill="white")

    out = io.BytesIO()
    canvas.convert("RGB").save(out, format="JPEG", quality=93)
    out.seek(0)
    return out.read()



# ─── ИНФОГРАФИКА ХЕНДЛЕРЫ ────────────────────────────────────

@dp.callback_query(F.data == "make_infographic")
async def make_infographic_start(call: CallbackQuery, state: FSMContext):
    if not is_pro(call.from_user.id):
        await call.message.answer(
            "🔒 Инфографика из фото доступна в тарифе Про.\n\n"
            "Бот накладывает продающие подписи прямо на фото товара — как в топе WB и Ozon.",
            reply_markup=kb_upgrade_pro()
        )
        await call.answer()
        return
    await state.clear()
    await state.set_state(InfographicStates.choose_platform)
    await call.message.answer(
        "🖼 Инфографика из фото\n\nВыбери платформу:",
        reply_markup=kb_infographic_platform()
    )
    await call.answer()

@dp.callback_query(F.data.startswith("infographic_"), InfographicStates.choose_platform)
async def infographic_choose_platform(call: CallbackQuery, state: FSMContext):
    platform = call.data.replace("infographic_", "")
    await state.update_data(platform=platform)
    await state.set_state(InfographicStates.choose_mode)
    label = PLATFORM_STYLES[platform]["label"]
    await call.message.answer(
        "Платформа: " + label + "\n\nКак заполним текст на инфографике?",
        reply_markup=kb_infographic_mode()
    )
    await call.answer()

@dp.callback_query(F.data == "infmode_auto", InfographicStates.choose_mode)
async def infographic_mode_auto(call: CallbackQuery, state: FSMContext):
    await state.update_data(mode="auto")
    await state.set_state(InfographicStates.waiting_photo)
    await call.message.answer(
        "🤖 Авто режим\n\n📷 Отправь фото товара — ИИ сам определит название, преимущества и слоган.",
        reply_markup=kb_back()
    )
    await call.answer()

@dp.message(InfographicStates.waiting_photo, F.photo)
async def process_infographic_auto(message: Message, state: FSMContext):
    data = await state.get_data()
    platform = data.get("platform", "wb")
    user_id = message.from_user.id
    await message.answer("⏳ Анализирую фото и создаю инфографику...")
    try:
        photo = message.photo[-1]
        file = await bot.get_file(photo.file_id)
        image_url = "https://api.telegram.org/file/bot" + BOT_TOKEN + "/" + file.file_path
        file_bytes = await bot.download_file(file.file_path)
        img_bytes = file_bytes.read()
        product_data = await get_infographic_data(image_url, platform)
        product_data.pop("price", None)
        result_bytes = await asyncio.to_thread(draw_infographic, img_bytes, product_data, platform)
        label = PLATFORM_STYLES[platform]["label"]
        caption = "🖼 Инфографика для " + label + " готова!\n\n📦 " + product_data.get("name", "") + "\n\nСохрани фото и загружай в карточку товара."
        save_history(user_id, platform, "infographic", product_data.get("name", ""))
        photo_file = BufferedInputFile(result_bytes, filename="infographic.jpg")
        await message.answer_photo(photo_file, caption=caption, reply_markup=kb_main())
        await state.clear()
    except Exception as e:
        logging.error("Infographic auto error: " + str(e))
        await message.answer("❌ Ошибка. Попробуй ещё раз или отправь другое фото.", reply_markup=kb_back())

@dp.callback_query(F.data == "infmode_manual", InfographicStates.choose_mode)
async def infographic_mode_manual(call: CallbackQuery, state: FSMContext):
    await state.update_data(mode="manual", manual_name="", manual_price="", manual_benefits=[], manual_tag="")
    await state.set_state(InfographicStates.manual_name)
    await call.message.answer(
        "✏️ Шаг 1/4 — Название товара\n\nВведи название для инфографики (до 40 символов)\nили нажми кнопку — ИИ придумает сам.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🤖 Придумать за меня", callback_data="infai_name")],
            [InlineKeyboardButton(text="🏠 В меню", callback_data="back_menu")],
        ])
    )
    await call.answer()

@dp.message(InfographicStates.manual_name, F.text)
async def manual_name_text(message: Message, state: FSMContext):
    await state.update_data(manual_name=message.text[:40])
    await state.set_state(InfographicStates.manual_price)
    await message.answer(
        "💰 Шаг 2/4 — Цена\n\nВведи цену (например: 1 990 ₽)\nили пропусти — цена не будет на инфографике.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⏭ Не указывать цену", callback_data="infskip_price")],
            [InlineKeyboardButton(text="🏠 В меню", callback_data="back_menu")],
        ])
    )

@dp.callback_query(F.data == "infai_name", InfographicStates.manual_name)
async def manual_name_ai(call: CallbackQuery, state: FSMContext):
    await state.update_data(manual_name="__ai__")
    await state.set_state(InfographicStates.manual_price)
    await call.message.answer(
        "💰 Шаг 2/4 — Цена\n\nВведи цену (например: 1 990 ₽)\nили пропусти — цена не будет на инфографике.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⏭ Не указывать цену", callback_data="infskip_price")],
            [InlineKeyboardButton(text="🏠 В меню", callback_data="back_menu")],
        ])
    )
    await call.answer()

@dp.message(InfographicStates.manual_price, F.text)
async def manual_price_text(message: Message, state: FSMContext):
    await state.update_data(manual_price=message.text[:20])
    await state.set_state(InfographicStates.manual_benefits)
    await message.answer(
        "✅ Шаг 3/4 — Преимущества\n\nВведи 4 преимущества — каждое с новой строки.\nНапример:\nВодонепроницаемый корпус\nРаботает от батарейки\nТихий мотор\nЧехол в комплекте\n\nИли нажми — ИИ придумает сам.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🤖 Придумать за меня", callback_data="infai_benefits")],
            [InlineKeyboardButton(text="🏠 В меню", callback_data="back_menu")],
        ])
    )

@dp.callback_query(F.data == "infskip_price", InfographicStates.manual_price)
async def manual_price_skip(call: CallbackQuery, state: FSMContext):
    await state.update_data(manual_price="")
    await state.set_state(InfographicStates.manual_benefits)
    await call.message.answer(
        "✅ Шаг 3/4 — Преимущества\n\nВведи 4 преимущества — каждое с новой строки.\nНапример:\nВодонепроницаемый корпус\nРаботает от батарейки\nТихий мотор\nЧехол в комплекте\n\nИли нажми — ИИ придумает сам.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🤖 Придумать за меня", callback_data="infai_benefits")],
            [InlineKeyboardButton(text="🏠 В меню", callback_data="back_menu")],
        ])
    )
    await call.answer()

@dp.message(InfographicStates.manual_benefits, F.text)
async def manual_benefits_text(message: Message, state: FSMContext):
    lines = [l.strip() for l in message.text.split("\n") if l.strip()][:4]
    await state.update_data(manual_benefits=lines)
    await state.set_state(InfographicStates.manual_tag)
    await message.answer(
        "🏷 Шаг 4/4 — Слоган\n\nВведи короткий слоган (до 25 символов)\nнапример: «Уход без усилий»\n\nИли выбери:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🤖 Придумать за меня", callback_data="infai_tag")],
            [InlineKeyboardButton(text="⏭ Не указывать", callback_data="infskip_tag")],
            [InlineKeyboardButton(text="🏠 В меню", callback_data="back_menu")],
        ])
    )

@dp.callback_query(F.data == "infai_benefits", InfographicStates.manual_benefits)
async def manual_benefits_ai(call: CallbackQuery, state: FSMContext):
    await state.update_data(manual_benefits=["__ai__"])
    await state.set_state(InfographicStates.manual_tag)
    await call.message.answer(
        "🏷 Шаг 4/4 — Слоган\n\nВведи короткий слоган (до 25 символов)\nнапример: «Уход без усилий»\n\nИли выбери:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🤖 Придумать за меня", callback_data="infai_tag")],
            [InlineKeyboardButton(text="⏭ Не указывать", callback_data="infskip_tag")],
            [InlineKeyboardButton(text="🏠 В меню", callback_data="back_menu")],
        ])
    )
    await call.answer()

@dp.message(InfographicStates.manual_tag, F.text)
async def manual_tag_text(message: Message, state: FSMContext):
    await state.update_data(manual_tag=message.text[:25])
    await state.set_state(InfographicStates.manual_photo)
    await message.answer("📷 Отлично! Теперь отправь фото товара — наложу всё на изображение.", reply_markup=kb_back())

@dp.callback_query(F.data == "infai_tag", InfographicStates.manual_tag)
async def manual_tag_ai(call: CallbackQuery, state: FSMContext):
    await state.update_data(manual_tag="__ai__")
    await state.set_state(InfographicStates.manual_photo)
    await call.message.answer("📷 Отлично! Теперь отправь фото товара — наложу всё на изображение.", reply_markup=kb_back())
    await call.answer()

@dp.callback_query(F.data == "infskip_tag", InfographicStates.manual_tag)
async def manual_tag_skip(call: CallbackQuery, state: FSMContext):
    await state.update_data(manual_tag="")
    await state.set_state(InfographicStates.manual_photo)
    await call.message.answer("📷 Отлично! Теперь отправь фото товара — наложу всё на изображение.", reply_markup=kb_back())
    await call.answer()

@dp.message(InfographicStates.manual_photo, F.photo)
async def process_infographic_manual(message: Message, state: FSMContext):
    data = await state.get_data()
    platform = data.get("platform", "wb")
    user_id = message.from_user.id
    await message.answer("⏳ Создаю инфографику...")
    try:
        photo = message.photo[-1]
        file = await bot.get_file(photo.file_id)
        image_url = "https://api.telegram.org/file/bot" + BOT_TOKEN + "/" + file.file_path
        file_bytes = await bot.download_file(file.file_path)
        img_bytes = file_bytes.read()
        need_ai = (
            data.get("manual_name") == "__ai__" or
            data.get("manual_benefits") == ["__ai__"] or
            data.get("manual_tag") == "__ai__"
        )
        ai_data = await get_infographic_data(image_url, platform) if need_ai else {}
        name = data.get("manual_name") if data.get("manual_name") != "__ai__" else ai_data.get("name", "")
        price = data.get("manual_price", "")
        benefits = data.get("manual_benefits") if data.get("manual_benefits") != ["__ai__"] else ai_data.get("benefits", [])
        tag = data.get("manual_tag") if data.get("manual_tag") not in ("__ai__",) else ai_data.get("tag", "")
        if data.get("manual_tag") == "":
            tag = ""
        product_data = {
            "name": name,
            "price": price,
            "benefits": benefits,
            "tag": tag,
            "product_side": ai_data.get("product_side", "right"),
        }
        result_bytes = await asyncio.to_thread(draw_infographic, img_bytes, product_data, platform)
        label = PLATFORM_STYLES[platform]["label"]
        price_line = "💰 " + price + "\n" if price else ""
        caption = "🖼 Инфографика для " + label + " готова!\n\n📦 " + name + "\n" + price_line + "\nСохрани фото и загружай в карточку товара."
        save_history(user_id, platform, "infographic", name)
        photo_file = BufferedInputFile(result_bytes, filename="infographic.jpg")
        await message.answer_photo(photo_file, caption=caption, reply_markup=kb_main())
        await state.clear()
    except Exception as e:
        logging.error("Infographic manual error: " + str(e))
        await message.answer("❌ Ошибка. Попробуй ещё раз.", reply_markup=kb_back())


# ─── ГОЛОСОВЫЕ СООБЩЕНИЯ ─────────────────────────────────────

async def transcribe_voice(message: Message) -> str:
    """Скачивает голосовое/аудио и транскрибирует через Whisper"""
    if message.voice:
        file_id = message.voice.file_id
    elif message.audio:
        file_id = message.audio.file_id
    else:
        return ""
    file = await bot.get_file(file_id)
    file_bytes = await bot.download_file(file.file_path)
    audio_data = io.BytesIO(file_bytes.read())
    audio_data.name = "voice.ogg"
    transcript = await client.audio.transcriptions.create(
        model="whisper-1",
        file=audio_data,
        language="ru"
    )
    return transcript.text.strip()

@dp.message(CardStates.waiting_text, F.voice | F.audio)
async def voice_card_text(message: Message, state: FSMContext):
    await message.answer("🎙 Распознаю голосовое...")
    text = await transcribe_voice(message)
    if not text:
        await message.answer("❌ Не удалось распознать. Попробуй ещё раз или напиши текстом.")
        return
    await message.answer(f"📝 Распознано: {text}\n\n⏳ Создаю карточку...")
    data = await state.get_data()
    platform = data.get("platform", "wb")
    user_id = message.from_user.id
    try:
        if platform == "all":
            results = []
            for pl in ["wb", "ozon", "avito"]:
                r = await generate_card_from_text(pl, text)
                results.append(f"{'─'*30}\n{PLATFORM_NAMES[pl].upper()}\n{'─'*30}\n{r}")
                save_history(user_id, pl, "card_text", r)
            result = "\n\n".join(results)
            await message.answer(result[:4000], reply_markup=kb_back())
            if len(result) > 4000:
                await message.answer(result[4000:8000], reply_markup=kb_back())
        else:
            result = await generate_card_from_text(platform, text)
            save_history(user_id, platform, "card_text", result)
            await message.answer(result, reply_markup=kb_regen(platform, "text"))
        access, _ = check_access(user_id)
        if access == "free":
            increment_requests(user_id)
        await state.update_data(last_desc=text, platform=platform)
    except Exception as e:
        logging.error(f"Voice card error: {e}")
        await message.answer("❌ Ошибка генерации. Попробуй ещё раз.", reply_markup=kb_back())

@dp.message(ReviewStates.waiting_review, F.voice | F.audio)
async def voice_review(message: Message, state: FSMContext):
    await message.answer("🎙 Распознаю голосовое...")
    text = await transcribe_voice(message)
    if not text:
        await message.answer("❌ Не удалось распознать. Попробуй ещё раз или напиши текстом.")
        return
    await message.answer(f"📝 Распознано: {text}\n\n⏳ Пишу ответ...")
    user_id = message.from_user.id
    try:
        result = await generate_review_reply(text)
        save_history(user_id, "—", "review", result)
        access, _ = check_access(user_id)
        if access == "free":
            increment_requests(user_id)
        await message.answer(f"⭐️ Готовый ответ:\n\n{result}", reply_markup=kb_back())
        await state.clear()
    except Exception as e:
        logging.error(f"Voice review error: {e}")
        await message.answer("❌ Ошибка. Попробуй ещё раз.", reply_markup=kb_back())

@dp.message(AuditStates.waiting_card, F.voice | F.audio)
async def voice_audit(message: Message, state: FSMContext):
    await message.answer("🎙 Распознаю голосовое...")
    text = await transcribe_voice(message)
    if not text:
        await message.answer("❌ Не удалось распознать. Попробуй ещё раз или напиши текстом.")
        return
    await message.answer(f"📝 Распознано: {text}\n\n⏳ Провожу аудит...")
    user_id = message.from_user.id
    try:
        result = await generate_audit(text)
        save_history(user_id, "—", "audit", result)
        await message.answer(result, reply_markup=kb_back())
        await state.clear()
    except Exception as e:
        logging.error(f"Voice audit error: {e}")
        await message.answer("❌ Ошибка. Попробуй ещё раз.", reply_markup=kb_back())

@dp.message(CompetitorStates.waiting_niche, F.voice | F.audio)
async def voice_competitors(message: Message, state: FSMContext):
    await message.answer("🎙 Распознаю голосовое...")
    text = await transcribe_voice(message)
    if not text:
        await message.answer("❌ Не удалось распознать. Попробуй ещё раз или напиши текстом.")
        return
    await message.answer(f"📝 Распознано: {text}\n\n⏳ Анализирую нишу...")
    user_id = message.from_user.id
    try:
        result = await generate_competitors(text)
        save_history(user_id, "WB+Ozon", "competitors", result)
        await message.answer(result, reply_markup=kb_back())
        await state.clear()
    except Exception as e:
        logging.error(f"Voice competitors error: {e}")
        await message.answer("❌ Ошибка. Попробуй ещё раз.", reply_markup=kb_back())

@dp.message(AdStates.waiting_product, F.voice | F.audio)
async def voice_ad(message: Message, state: FSMContext):
    await message.answer("🎙 Распознаю голосовое...")
    text = await transcribe_voice(message)
    if not text:
        await message.answer("❌ Не удалось распознать. Попробуй ещё раз или напиши текстом.")
        return
    await message.answer(f"📝 Распознано: {text}\n\n⏳ Создаю рекламный текст...")
    user_id = message.from_user.id
    try:
        result = await generate_ad_text(text)
        save_history(user_id, "—", "ad_text", result)
        await message.answer(result, reply_markup=kb_back())
        await state.clear()
    except Exception as e:
        logging.error(f"Voice ad error: {e}")
        await message.answer("❌ Ошибка. Попробуй ещё раз.", reply_markup=kb_back())

# ─── ЗАГЛУШКИ ДЛЯ НЕОБРАБОТАННЫХ СООБЩЕНИЙ ──────────────────

@dp.message(F.text)
async def fallback_text(message: Message, state: FSMContext):
    current = await state.get_state()
    if current is None:
        await message.answer(
            "Выбери действие из меню 👇",
            reply_markup=kb_main()
        )

@dp.message(F.photo)
async def fallback_photo(message: Message, state: FSMContext):
    current = await state.get_state()
    if current is None:
        await message.answer(
            "📷 Хочешь создать карточку из этого фото?\n\nНажми '📦 Создать карточку' в меню:",
            reply_markup=kb_main()
        )

@dp.message(F.voice | F.audio)
async def fallback_voice(message: Message, state: FSMContext):
    current = await state.get_state()
    if current is None:
        await message.answer(
            "🎙 Хочешь описать товар голосом?\n\nНажми '📦 Создать карточку' → выбери платформу → и отправь голосовое!",
            reply_markup=kb_main()
        )

# ─── ЗАПУСК ──────────────────────────────────────────────────

async def main():
    init_db()
    asyncio.create_task(check_payments_loop())
    logging.info("КарточкаПро запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
