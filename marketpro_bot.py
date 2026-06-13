import asyncio
import logging
import sqlite3
import uuid
import io
import re
import json
import textwrap
import base64
import httpx
from datetime import datetime, timedelta
from openai import AsyncOpenAI
from PIL import Image, ImageDraw, ImageFont
import anthropic
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup,
    InlineKeyboardButton, BufferedInputFile
)
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from yookassa import Configuration, Payment
import gspread
from google.oauth2.service_account import Credentials

# ─── ЗАГРУЗКА КЛЮЧЕЙ ────────────────────────────────────────
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
        logging.warning(f"env load error: {e}")
    return env

_env = load_env()

# ─── КОНСТАНТЫ ──────────────────────────────────────────────
BOT_TOKEN        = _env.get("BOT_TOKEN", "")
OPENAI_KEY       = _env.get("OPENAI_KEY", "")
ANTHROPIC_KEY    = "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"
OWNER_ID         = int(_env.get("OWNER_ID", "549639607"))
SUPPORT_URL      = _env.get("SUPPORT_URL", "https://t.me/Boss023rus")
SPREADSHEET_ID   = _env.get("SPREADSHEET_ID", "")
CREDENTIALS_FILE = _env.get("CREDENTIALS_FILE", "/root/google_credentials.json")

YOOKASSA_SHOP_ID = _env.get("YOOKASSA_SHOP_ID", "1363324")
YOOKASSA_SECRET  = _env.get("YOOKASSA_SECRET", "")
Configuration.account_id = YOOKASSA_SHOP_ID
Configuration.secret_key  = YOOKASSA_SECRET

FREE_REQUESTS   = 5
FREE_DALLE      = 1
DALLE_COST      = 4  # рублей за одну DALL-E генерацию

# ─── ИНИЦИАЛИЗАЦИЯ ──────────────────────────────────────────
bot    = Bot(token=BOT_TOKEN)
dp     = Dispatcher(storage=MemoryStorage())
openai = AsyncOpenAI(api_key=OPENAI_KEY)
claude = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
logging.basicConfig(level=logging.INFO)

# ─── FSM ────────────────────────────────────────────────────
class OnboardStates(StatesGroup):
    step1 = State()
    step2 = State()
    step3 = State()

class CardStates(StatesGroup):
    choose_platform = State()
    choose_input    = State()
    waiting_photo   = State()
    waiting_text    = State()

class ReviewStates(StatesGroup):
    waiting = State()

class AuditStates(StatesGroup):
    waiting = State()

class CompetitorStates(StatesGroup):
    waiting_niche = State()

class CompetitorLinkStates(StatesGroup):
    waiting_link = State()

class AdStates(StatesGroup):
    waiting = State()

class AppealStates(StatesGroup):
    choose_type = State()
    waiting     = State()

class SupplyStates(StatesGroup):
    waiting_cost     = State()
    waiting_price    = State()
    waiting_sales    = State()
    waiting_stock    = State()
    waiting_days     = State()

class UnitStates(StatesGroup):
    waiting_cost     = State()
    waiting_price    = State()
    waiting_category = State()
    waiting_scheme   = State()

class DalleStates(StatesGroup):
    choose_type  = State()
    waiting_desc = State()

class DallePhotoStates(StatesGroup):
    choose_type  = State()
    waiting_photo = State()

class BroadcastStates(StatesGroup):
    waiting = State()

class SlideSetStates(StatesGroup):
    choose_platform = State()
    waiting_desc    = State()

# ─── БАЗА ДАННЫХ ────────────────────────────────────────────
DB = "/root/marketpro.db"

def init_db():
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT, first_name TEXT,
        created_at TEXT, onboarded INTEGER DEFAULT 0
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
    c.execute("""CREATE TABLE IF NOT EXISTS credits (
        user_id INTEGER PRIMARY KEY,
        dalle_credits INTEGER DEFAULT 0
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS pending_payments (
        payment_id TEXT PRIMARY KEY,
        user_id INTEGER, plan TEXT, amount INTEGER,
        created_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, feature TEXT,
        result TEXT, created_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS referrals (
        user_id INTEGER PRIMARY KEY,
        ref_by INTEGER DEFAULT 0,
        ref_count INTEGER DEFAULT 0,
        ref_earned INTEGER DEFAULT 0
    )""")
    conn.commit()
    conn.close()

def ensure_user(user_id, username="", first_name=""):
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (user_id,username,first_name,created_at) VALUES (?,?,?,?)",
              (user_id, username, first_name, datetime.now().isoformat()))
    c.execute("INSERT OR IGNORE INTO requests (user_id,count) VALUES (?,0)", (user_id,))
    c.execute("INSERT OR IGNORE INTO credits (user_id,dalle_credits) VALUES (?,0)", (user_id,))
    conn.commit()
    conn.close()

def get_requests(uid):
    conn = sqlite3.connect(DB)
    r = conn.execute("SELECT count FROM requests WHERE user_id=?", (uid,)).fetchone()
    conn.close()
    return r[0] if r else 0

def inc_requests(uid):
    conn = sqlite3.connect(DB)
    conn.execute("UPDATE requests SET count=count+1 WHERE user_id=?", (uid,))
    conn.commit(); conn.close()

def get_sub(uid):
    conn = sqlite3.connect(DB)
    r = conn.execute("SELECT plan,sub_end FROM subscriptions WHERE user_id=?", (uid,)).fetchone()
    conn.close()
    if not r or not r[1]: return None, None
    end = datetime.fromisoformat(r[1])
    if end > datetime.now(): return r[0], end
    return None, None

def set_sub(uid, plan, days=30):
    conn = sqlite3.connect(DB)
    end = (datetime.now() + timedelta(days=days)).isoformat()
    conn.execute("INSERT OR REPLACE INTO subscriptions (user_id,plan,sub_end) VALUES (?,?,?)", (uid,plan,end))
    conn.commit(); conn.close()

def get_dalle(uid):
    conn = sqlite3.connect(DB)
    r = conn.execute("SELECT dalle_credits FROM credits WHERE user_id=?", (uid,)).fetchone()
    conn.close()
    return r[0] if r else 0

def add_dalle(uid, amount):
    conn = sqlite3.connect(DB)
    conn.execute("INSERT OR IGNORE INTO credits (user_id,dalle_credits) VALUES (?,0)", (uid,))
    conn.execute("UPDATE credits SET dalle_credits=dalle_credits+? WHERE user_id=?", (amount, uid))
    conn.commit(); conn.close()

def use_dalle(uid):
    d = get_dalle(uid)
    if d <= 0: return False
    conn = sqlite3.connect(DB)
    conn.execute("UPDATE credits SET dalle_credits=dalle_credits-1 WHERE user_id=?", (uid,))
    conn.commit(); conn.close()
    return True

def get_onboarded(uid):
    conn = sqlite3.connect(DB)
    r = conn.execute("SELECT onboarded FROM users WHERE user_id=?", (uid,)).fetchone()
    conn.close()
    return r[0] if r else 0

def set_onboarded(uid):
    conn = sqlite3.connect(DB)
    conn.execute("UPDATE users SET onboarded=1 WHERE user_id=?", (uid,))
    conn.commit(); conn.close()

def save_pending(pid, uid, plan, amount):
    conn = sqlite3.connect(DB)
    conn.execute("INSERT INTO pending_payments (payment_id,user_id,plan,amount,created_at) VALUES (?,?,?,?,?)",
                 (pid, uid, plan, amount, datetime.now().isoformat()))
    conn.commit(); conn.close()

def get_pending():
    conn = sqlite3.connect(DB)
    r = conn.execute("SELECT payment_id,user_id,plan,amount FROM pending_payments").fetchall()
    conn.close()
    return r

def del_pending(pid):
    conn = sqlite3.connect(DB)
    conn.execute("DELETE FROM pending_payments WHERE payment_id=?", (pid,))
    conn.commit(); conn.close()

def save_history(uid, feature, result):
    conn = sqlite3.connect(DB)
    conn.execute("INSERT INTO history (user_id,feature,result,created_at) VALUES (?,?,?,?)",
                 (uid, feature, result[:3000], datetime.now().isoformat()))
    conn.commit(); conn.close()

def get_history(uid, limit=5):
    conn = sqlite3.connect(DB)
    r = conn.execute("SELECT feature,result,created_at FROM history WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
                     (uid, limit)).fetchall()
    conn.close()
    return r

def get_all_users():
    conn = sqlite3.connect(DB)
    r = conn.execute("SELECT user_id FROM users").fetchall()
    conn.close()
    return [x[0] for x in r]

def init_referral(uid):
    conn = sqlite3.connect(DB)
    conn.execute("INSERT OR IGNORE INTO referrals (user_id,ref_by,ref_count,ref_earned) VALUES (?,0,0,0)", (uid,))
    conn.commit(); conn.close()

def set_ref_by(uid, ref_by):
    init_referral(uid)
    conn = sqlite3.connect(DB)
    conn.execute("UPDATE referrals SET ref_by=? WHERE user_id=? AND ref_by=0", (ref_by, uid))
    conn.commit(); conn.close()

def add_ref_count(ref_by):
    conn = sqlite3.connect(DB)
    conn.execute("UPDATE referrals SET ref_count=ref_count+1 WHERE user_id=?", (ref_by,))
    conn.commit(); conn.close()

def add_ref_bonus(ref_by, amount):
    conn = sqlite3.connect(DB)
    conn.execute("UPDATE referrals SET ref_earned=ref_earned+? WHERE user_id=?", (amount, ref_by))
    conn.commit(); conn.close()

def get_ref_stats(uid):
    init_referral(uid)
    conn = sqlite3.connect(DB)
    r = conn.execute("SELECT ref_by,ref_count,ref_earned FROM referrals WHERE user_id=?", (uid,)).fetchone()
    conn.close()
    return r if r else (0, 0, 0)

def get_stats():
    conn = sqlite3.connect(DB)
    total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    starts = conn.execute("SELECT COUNT(*) FROM subscriptions WHERE plan='mp_start' AND sub_end>?",
                          (datetime.now().isoformat(),)).fetchone()[0]
    pros = conn.execute("SELECT COUNT(*) FROM subscriptions WHERE plan='mp_pro' AND sub_end>?",
                        (datetime.now().isoformat(),)).fetchone()[0]
    reqs = conn.execute("SELECT SUM(count) FROM requests").fetchone()[0] or 0
    conn.close()
    return total, starts, pros, reqs

# ─── ДОСТУП ─────────────────────────────────────────────────
def access_level(uid):
    plan, _ = get_sub(uid)
    if plan == "mp_pro":   return "pro"
    if plan == "mp_start": return "start"
    if get_requests(uid) < FREE_REQUESTS: return "free"
    return "limit"

def is_pro(uid):   return get_sub(uid)[0] == "mp_pro"
def is_paid(uid):  return get_sub(uid)[0] in ("mp_start","mp_pro")

# ─── GOOGLE SHEETS ───────────────────────────────────────────
def sheets_add(uid, username, first_name):
    try:
        if not SPREADSHEET_ID: return
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)
        gc = gspread.authorize(creds)
        sheet = gc.open_by_key(SPREADSHEET_ID).sheet1
        col = sheet.col_values(1)
        if not col or col[0] != "ID":
            sheet.insert_row(["ID","Username","Имя","Дата","Тариф"], 1)
            col = sheet.col_values(1)
        if str(uid) not in col:
            sheet.append_row([str(uid), f"@{username}" if username else "—",
                              first_name or "—", datetime.now().strftime("%d.%m.%Y %H:%M"), "Бесплатный"])
    except Exception as e:
        logging.error(f"Sheets error: {e}")

# ─── КЛАВИАТУРЫ ─────────────────────────────────────────────
def kb_main(uid=None):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 Карточка товара", callback_data="sec_cards")],
        [InlineKeyboardButton(text="🖼 Инфографика DALL-E", callback_data="sec_dalle")],
        [InlineKeyboardButton(text="📊 Аналитика", callback_data="sec_analytics")],
        [InlineKeyboardButton(text="⚙️ Инструменты", callback_data="sec_tools")],
        [InlineKeyboardButton(text="👤 Профиль", callback_data="my_profile"),
         InlineKeyboardButton(text="📂 История", callback_data="my_history")],
        [InlineKeyboardButton(text="💎 Тарифы", callback_data="tariffs"),
         InlineKeyboardButton(text="💬 Поддержка", url=SUPPORT_URL)],
    ])

def kb_cards():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📸 Карточка из фото", callback_data="make_card_photo")],
        [InlineKeyboardButton(text="✏️ Карточка из текста", callback_data="make_card_text")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_menu")],
    ])

def kb_analytics():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Аудит карточки", callback_data="audit_card")],
        [InlineKeyboardButton(text="🕵️ Анализ ниши", callback_data="competitors")],
        [InlineKeyboardButton(text="🔗 Анализ конкурента по ссылке 🔒 Про", callback_data="competitor_link")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_menu")],
    ])

def kb_tools():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧮 Юнит-экономика", callback_data="unit_eco")],
        [InlineKeyboardButton(text="📦 Калькулятор поставки 🔒 Про", callback_data="supply_calc")],
        [InlineKeyboardButton(text="⭐️ Ответ на отзыв", callback_data="review_reply")],
        [InlineKeyboardButton(text="📣 Рекламный текст 🔒 Старт", callback_data="ad_text")],
        [InlineKeyboardButton(text="⚖️ Апелляция к МП 🔒 Про", callback_data="appeal")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_menu")],
    ])

def kb_platform():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟣 Wildberries", callback_data="platform_wb"),
         InlineKeyboardButton(text="🔵 Ozon", callback_data="platform_ozon")],
        [InlineKeyboardButton(text="🟡 Авито", callback_data="platform_avito"),
         InlineKeyboardButton(text="✅ Все 3 🔒 Старт", callback_data="platform_all")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_menu")],
    ])

def kb_dalle_types():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏷 Обложка товара", callback_data="dalle_cover"),
         InlineKeyboardButton(text="✅ Преимущества", callback_data="dalle_benefits")],
        [InlineKeyboardButton(text="🔄 До / После", callback_data="dalle_before_after"),
         InlineKeyboardButton(text="💡 Проблема→Решение", callback_data="dalle_problem")],
        [InlineKeyboardButton(text="📋 Как использовать", callback_data="dalle_howto"),
         InlineKeyboardButton(text="⚔️ Vs конкурента", callback_data="dalle_compare")],
        [InlineKeyboardButton(text="📦 Комплектация", callback_data="dalle_kit")],
        [InlineKeyboardButton(text="📸 Фото → Инфографика (2 кредита)", callback_data="dalle_from_photo")],
        [InlineKeyboardButton(text="📚 Набор слайдов (3-5 карточек)", callback_data="dalle_slideset")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_menu")],
    ])

def kb_appeal_types():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Штраф WB/Ozon", callback_data="appeal_fine")],
        [InlineKeyboardButton(text="🚫 Блокировка карточки", callback_data="appeal_block")],
        [InlineKeyboardButton(text="📦 Претензия по поставке", callback_data="appeal_supply")],
        [InlineKeyboardButton(text="⭐️ Несправедливый отзыв", callback_data="appeal_review")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_menu")],
    ])

def kb_scheme():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="FBO (склад МП)", callback_data="scheme_fbo"),
         InlineKeyboardButton(text="FBS (свой склад)", callback_data="scheme_fbs")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_menu")],
    ])

def kb_back():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 В меню", callback_data="back_menu")]
    ])

def kb_upgrade_start():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟢 Купить Старт — 490 ₽", callback_data="pay_start")],
        [InlineKeyboardButton(text="🔥 Купить Про — 990 ₽", callback_data="pay_pro")],
        [InlineKeyboardButton(text="🔙 В меню", callback_data="back_menu")],
    ])

def kb_upgrade_pro():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔥 Купить Про — 990 ₽", callback_data="pay_pro")],
        [InlineKeyboardButton(text="🔙 В меню", callback_data="back_menu")],
    ])

def kb_tariffs():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟢 Старт — 490 ₽/мес", callback_data="pay_start")],
        [InlineKeyboardButton(text="🔥 Про — 990 ₽/мес", callback_data="pay_pro")],
        [InlineKeyboardButton(text="💎 DALL-E: 10 ген — 149 ₽", callback_data="pay_dalle_10")],
        [InlineKeyboardButton(text="💎 DALL-E: 30 ген — 349 ₽", callback_data="pay_dalle_30")],
        [InlineKeyboardButton(text="💎 DALL-E: 100 ген — 990 ₽", callback_data="pay_dalle_100")],
        [InlineKeyboardButton(text="🔙 В меню", callback_data="back_menu")],
    ])

# ─── GPT ФУНКЦИИ ────────────────────────────────────────────
PLATFORM_RULES = {
    "wb": """ПЛАТФОРМА: Wildberries
- Название: до 60 символов, начинай с ключа
- Описание: 500–2000 символов, структура: выгоды → характеристики → применение
- SEO-ключи: 20 штук через запятую
- Характеристики: бренд, категория, страна, состав/материал
- Запрещено: ссылки, слова "лучший/№1"
- Никакой markdown разметки! Никаких ##, **, *, ---, только чистый текст
Формат ответа строго:
📦 НАЗВАНИЕ:
[название без кавычек и спецсимволов]

📝 ОПИСАНИЕ:
[чистый текст описания, абзацы через пустую строку, без markdown]

🔑 SEO-КЛЮЧИ:
[ключи через запятую]

📋 ХАРАКТЕРИСТИКИ:
[каждая характеристика с новой строки без маркеров markdown]""",
    "ozon": """ПЛАТФОРМА: Ozon
- Название: 50–200 символов, Тип+Бренд+характеристики
- Описание: 500–4000 символов, абзацы с подзаголовками в виде обычного текста
- SEO-ключи: 20 штук через запятую
- Характеристики: бренд, страна, гарантия, комплектация
- Никакой markdown разметки! Никаких ##, **, *, ---, только чистый текст
Формат ответа строго:
📦 НАЗВАНИЕ:
[название без кавычек]

📝 ОПИСАНИЕ:
[чистый текст, подзаголовки просто заглавными буквами или с двоеточием, абзацы через пустую строку]

🔑 SEO-КЛЮЧИ:
[ключи через запятую]

📋 ХАРАКТЕРИСТИКИ:
[каждая с новой строки, без маркеров]""",
    "avito": """ПЛАТФОРМА: Авито
- Заголовок: до 50 символов, конкретный
- Описание: 300–2000 символов, боль → преимущества → призыв к действию
- Ключи: 10–15 фраз которые ищут на Авито
- Никакой markdown разметки! Никаких ##, **, *, ---, только чистый текст
Формат ответа строго:
📦 ЗАГОЛОВОК:
[заголовок без кавычек]

📝 ОПИСАНИЕ:
[чистый живой текст, абзацы через пустую строку]

🔑 КЛЮЧЕВЫЕ СЛОВА:
[ключи через запятую]""",
}

PLATFORM_NAMES = {"wb": "Wildberries", "ozon": "Ozon", "avito": "Авито"}

async def gpt(system, user_text, model="gpt-4o", max_tokens=1500):
    resp = await openai.chat.completions.create(
        model=model,
        messages=[{"role":"system","content":system},{"role":"user","content":user_text}],
        max_tokens=max_tokens
    )
    return resp.choices[0].message.content.strip()

async def claude_vision(system, user_text, image_url):
    async with httpx.AsyncClient() as hc:
        r = await hc.get(image_url)
        img_bytes = r.content
    b64 = base64.b64encode(img_bytes).decode()
    resp = claude.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1500,
        system=system,
        messages=[{"role":"user","content":[
            {"type":"image","source":{"type":"base64","media_type":"image/jpeg","data":b64}},
            {"type":"text","text":user_text}
        ]}]
    )
    return resp.content[0].text.strip()

async def gen_card_text(platform, description):
    rules = PLATFORM_RULES.get(platform, PLATFORM_RULES["wb"])
    system = ("Ты эксперт по карточкам маркетплейсов. Пишешь только на русском. "
              "Никогда не начинай с 'Конечно', 'Отлично', 'Вот'. "
              "Сразу выдавай результат в нужном формате.\n\n" + rules)
    return await gpt(system, f"Создай карточку:\n{description}")

async def gen_card_photo(platform, image_url):
    rules = PLATFORM_RULES.get(platform, PLATFORM_RULES["wb"])
    system = ("Ты эксперт по карточкам маркетплейсов. Пишешь только на русском. "
              "Никогда не начинай с 'Конечно', 'Отлично', 'Вот'. "
              "Определи тип товара (не называй бренды), создай карточку.\n\n" + rules)
    return await claude_vision(system, "Определи товар и создай карточку.", image_url)

async def gen_review(text):
    system = ("Ты менеджер маркетплейса. Пишешь живые ответы на отзывы. "
              "На негатив — признаёшь проблему, предлагаешь решение. "
              "На позитив — благодаришь с теплом. 50–150 слов. Только русский.")
    return await gpt(system, f"Ответ на отзыв:\n{text}")

async def gen_audit(text):
    system = "Ты эксперт по оптимизации карточек маркетплейсов. Даёшь конкретные правки."
    prompt = (f"Проведи аудит карточки:\n{text}\n\n"
              "Оцени по пунктам:\n"
              "⭐️ Оценка: N/10\n"
              "📦 Название: [оценка + правки]\n"
              "📝 Описание: [оценка + правки]\n"
              "🔑 SEO-ключи: [оценка + рекомендации]\n"
              "📋 Характеристики: [полнота + что добавить]\n"
              "✅ Сильные стороны:\n"
              "⚠️ Главные проблемы:\n"
              "🚀 Топ-3 правки для результата:")
    return await gpt(system, prompt, max_tokens=1200)

async def gen_competitors(niche):
    system = "Ты аналитик маркетплейсов. Даёшь практичные инсайты без воды."
    prompt = (f"Анализ ниши: {niche}\n\n"
              "📊 Что продают лидеры:\n"
              "🔑 Ключевые слова топа (15 штук):\n"
              "🔥 Что ценят покупатели:\n"
              "⚠️ Слабые места конкурентов:\n"
              "💡 5 идей как выделиться:\n"
              "✅ Главный совет для входа в нишу:")
    return await gpt(system, prompt, max_tokens=1200)

async def gen_competitor_link(url):
    system = "Ты аналитик маркетплейсов. Анализируешь карточки конкурентов и даёшь тактические советы."
    prompt = (f"Проанализируй карточку товара по этой ссылке: {url}\n\n"
              "Если ссылка на WB или Ozon — определи товар и проведи анализ:\n"
              "📦 Название конкурента и его сильные стороны:\n"
              "🔑 Ключевые слова которые он использует (10-15 штук):\n"
              "📝 Структура описания:\n"
              "✅ Что стоит взять себе:\n"
              "⚠️ Слабые места конкурента:\n"
              "💡 Как сделать лучше:")
    return await gpt(system, prompt, max_tokens=1200)

async def gen_ad(text):
    system = "Ты копирайтер для рекламы на маркетплейсах. Формула: боль→решение→выгоды→призыв."
    prompt = (f"Рекламный текст для товара:\n{text}\n\n"
              "🎯 ЗАГОЛОВОК (до 40 символов):\n"
              "📣 ТЕКСТ (100–180 слов):\n"
              "🏷️ СЛОГАН (до 20 символов):")
    return await gpt(system, prompt, max_tokens=800)

async def gen_appeal(appeal_type, text):
    types = {
        "fine": "штраф от маркетплейса",
        "block": "блокировка карточки",
        "supply": "претензия по поставке",
        "review": "несправедливый отзыв покупателя",
    }
    system = ("Ты юрист и эксперт по работе с маркетплейсами. "
              "Пишешь официальные апелляции для WB и Ozon. "
              "Тон: профессиональный, аргументированный, без эмоций.")
    prompt = (f"Напиши апелляцию по ситуации: {types.get(appeal_type, 'штраф')}\n\n"
              f"Детали ситуации: {text}\n\n"
              "Формат:\n"
              "📋 АПЕЛЛЯЦИЯ В СЛУЖБУ ПОДДЕРЖКИ:\n"
              "[официальный текст апелляции]\n\n"
              "💡 ДОПОЛНИТЕЛЬНЫЕ АРГУМЕНТЫ:\n"
              "[что ещё можно добавить]\n\n"
              "✅ СОВЕТ:\n"
              "[что сделать для решения вопроса]")
    return await gpt(system, prompt, max_tokens=1000)

async def gen_unit_eco(cost, price, category, scheme):
    wb_comm = {"одежда":0.25,"обувь":0.25,"электроника":0.10,"косметика":0.20,
               "товары для дома":0.17,"игрушки":0.17,"спорт":0.17,"продукты":0.15}.get(category.lower(), 0.17)
    oz_comm = {"одежда":0.50,"обувь":0.45,"электроника":0.08,"косметика":0.30,
               "товары для дома":0.25,"игрушки":0.20,"спорт":0.20,"продукты":0.18}.get(category.lower(), 0.20)
    wb_log = max(50, price*0.05) if scheme=="fbo" else max(80, price*0.07)
    oz_log = max(70, price*0.06) if scheme=="fbo" else max(100, price*0.08)
    storage = price * 0.01
    wb_profit = price - cost - price*wb_comm - wb_log - storage
    oz_profit = price - cost - price*oz_comm - oz_log - storage
    wb_margin = wb_profit/price*100 if price>0 else 0
    oz_margin = oz_profit/price*100 if price>0 else 0
    def verdict(m):
        if m>=30: return "✅ Отличная маржа"
        if m>=15: return "🟡 Приемлемо"
        if m>=0:  return "🔴 Слабая маржа"
        return "❌ Убыток"
    def fmt(v): return f"{v:,.0f}".replace(",", " ")
    return (
        f"🧮 ЮНИТ-ЭКОНОМИКА\n{'─'*28}\n"
        f"📦 Категория: {category}\n"
        f"💰 Себестоимость: {fmt(cost)} ₽\n"
        f"🏷 Цена: {fmt(price)} ₽\n"
        f"📦 Схема: {'FBO' if scheme=='fbo' else 'FBS'}\n\n"
        f"🟣 WILDBERRIES\n"
        f"Комиссия {int(wb_comm*100)}%: -{fmt(price*wb_comm)} ₽\n"
        f"Логистика: -{fmt(wb_log)} ₽\n"
        f"Прибыль: {fmt(wb_profit)} ₽ ({wb_margin:.1f}%)\n"
        f"{verdict(wb_margin)}\n\n"
        f"🔵 OZON\n"
        f"Комиссия {int(oz_comm*100)}%: -{fmt(price*oz_comm)} ₽\n"
        f"Логистика: -{fmt(oz_log)} ₽\n"
        f"Прибыль: {fmt(oz_profit)} ₽ ({oz_margin:.1f}%)\n"
        f"{verdict(oz_margin)}\n\n"
        f"📊 Для 50 000 ₽/мес нужно продать:\n"
        f"WB: {max(1,int(50000/wb_profit)) if wb_profit>0 else '∞'} шт\n"
        f"Ozon: {max(1,int(50000/oz_profit)) if oz_profit>0 else '∞'} шт\n\n"
        f"⚠️ Расчёт приблизительный."
    )

async def gen_supply(cost, price, sales_day, stock, days):
    demand = sales_day * days
    need = max(0, demand - stock)
    invest = need * cost
    revenue = demand * price
    profit = revenue - (demand * cost) - (demand * cost * 0.17)
    return (
        f"📦 КАЛЬКУЛЯТОР ПОСТАВКИ\n{'─'*28}\n"
        f"📅 Период: {days} дней\n"
        f"📈 Продаж в день: {sales_day} шт\n"
        f"📦 Текущий остаток: {stock} шт\n\n"
        f"🎯 Прогноз спроса: {demand} шт\n"
        f"🚚 Нужно довезти: {need} шт\n"
        f"💰 Инвестиции в поставку: {need*cost:,.0f} ₽\n\n"
        f"📊 Прогноз выручки: {revenue:,.0f} ₽\n"
        f"💵 Прогноз прибыли (17% комиссия): {profit:,.0f} ₽\n\n"
        f"✅ {'Поставка нужна — везите ' + str(need) + ' шт' if need > 0 else 'Остатка хватит на период'}"
    )

# ─── DALL-E ИНФОГРАФИКА ─────────────────────────────────────
DALLE_TYPES = {
    "cover":        ("Обложка товара", "главное фото с заголовком и ключевыми характеристиками"),
    "benefits":     ("Преимущества", "карточка с 3-5 выгодами товара и иконками"),
    "before_after": ("До / После", "визуальное сравнение эффекта до и после применения"),
    "problem":      ("Проблема→Решение", "боль покупателя слева и решение товаром справа"),
    "howto":        ("Как использовать", "пошаговая инструкция из 3 шагов"),
    "compare":      ("Vs конкурента", "сравнительная таблица наш товар vs обычный"),
    "kit":          ("Комплектация", "все элементы комплекта с количеством"),
}

async def gen_dalle(dalle_type, product_desc, platform="wb"):
    platform_colors = {
        "wb":    "purple and white color scheme, Wildberries marketplace style",
        "ozon":  "blue and white color scheme, Ozon marketplace style",
        "avito": "green and white color scheme, Avito marketplace style",
    }
    colors = platform_colors.get(platform, platform_colors["wb"])
    type_name, type_desc = DALLE_TYPES.get(dalle_type, ("Обложка", "карточка товара"))

    prompts = {
        "cover": (
            f"Professional product card for Russian marketplace. {colors}. "
            f"Clean white background. Product: {product_desc}. "
            f"Large product title at top in bold Russian text. "
            f"3-4 key characteristics listed below with checkmarks. "
            f"Price badge in corner. Modern flat design. No real logos. "
            f"Size 900x1200px vertical format. High quality commercial photography style."
        ),
        "benefits": (
            f"Benefits infographic card for Russian marketplace. {colors}. "
            f"Product: {product_desc}. "
            f"Header: ПРЕИМУЩЕСТВА in bold. "
            f"4 benefits listed vertically with colorful icons and short text. "
            f"Clean minimal design. White background. Vertical format 900x1200px."
        ),
        "before_after": (
            f"Before/After comparison card for marketplace. {colors}. "
            f"Product: {product_desc}. "
            f"Left side labeled ДО with problem visualization. "
            f"Right side labeled ПОСЛЕ with solution/result. "
            f"Arrow between sides. Clean design. 900x1200px."
        ),
        "problem": (
            f"Problem-Solution infographic for Russian marketplace. {colors}. "
            f"Product: {product_desc}. "
            f"Top half: ПРОБЛЕМА section with pain point illustration. "
            f"Bottom half: РЕШЕНИЕ section with product benefit. "
            f"Bold Russian text. Clean design. 900x1200px."
        ),
        "howto": (
            f"How-to use instruction card for marketplace. {colors}. "
            f"Product: {product_desc}. "
            f"Title: КАК ИСПОЛЬЗОВАТЬ. "
            f"3 numbered steps with simple icons and short Russian text. "
            f"Clean step-by-step layout. 900x1200px."
        ),
        "compare": (
            f"Comparison table infographic for marketplace. {colors}. "
            f"Product: {product_desc}. "
            f"Two columns: НАШ ТОВАР vs ОБЫЧНЫЙ. "
            f"4-5 comparison parameters with checkmarks and crosses. "
            f"Our product column highlighted. 900x1200px."
        ),
        "kit": (
            f"Kit contents card for marketplace. {colors}. "
            f"Product: {product_desc}. "
            f"Title: В КОМПЛЕКТЕ. "
            f"All included items numbered and labeled in Russian. "
            f"Clean product layout on white background. 900x1200px."
        ),
    }

    prompt = prompts.get(dalle_type, prompts["cover"])
    WORKER_URL = "https://noisy-wildflower-187e.demo23rus.workers.dev"
    try:
        async with httpx.AsyncClient(timeout=90) as hc:
            r = await hc.post(WORKER_URL, json={
                "prompt": prompt,
                "image_size": "portrait_4_3",
                "num_inference_steps": 4,
                "num_images": 1,
            })
            data = r.json()
        img_url = data["images"][0]["url"]
        return img_url
    except Exception as e:
        raise Exception(f"Ошибка генерации изображения: {e}")

# ─── ТРАНСКРИПЦИЯ ГОЛОСА ────────────────────────────────────
async def transcribe(message: Message) -> str:
    fid = message.voice.file_id if message.voice else (message.audio.file_id if message.audio else None)
    if not fid: return ""
    f = await bot.get_file(fid)
    fb = await bot.download_file(f.file_path)
    audio = io.BytesIO(fb.read())
    audio.name = "voice.ogg"
    t = await openai.audio.transcriptions.create(model="whisper-1", file=audio, language="ru")
    return t.text.strip()

# ─── СОЗДАНИЕ ОПЛАТЫ ────────────────────────────────────────
async def create_payment(uid, amount, plan, description):
    p = Payment.create({
        "amount": {"value": f"{amount}.00", "currency": "RUB"},
        "confirmation": {"type": "redirect", "return_url": "https://t.me/PostGeniusHelperBot"},
        "capture": True,
        "description": f"{description} — {uid}",
        "receipt": {
            "customer": {"email": "client@marketpro.ru"},
            "items": [{"description": description, "quantity": "1.00",
                       "amount": {"value": f"{amount}.00", "currency": "RUB"},
                       "vat_code": 1, "payment_subject": "service", "payment_mode": "full_payment"}]
        },
        "metadata": {"user_id": uid, "plan": plan}
    }, str(uuid.uuid4()))
    return p

async def check_payments_loop():
    while True:
        await asyncio.sleep(15)
        try:
            for pid, uid, plan, amount in get_pending():
                try:
                    p = Payment.find_one(pid)
                    if p.status == "succeeded":
                        if plan.startswith("dalle_"):
                            credits = {"dalle_10":10,"dalle_30":30,"dalle_100":100}.get(plan,10)
                            add_dalle(uid, credits)
                            del_pending(pid)
                            await bot.send_message(uid,
                                f"✅ Оплата прошла!\n\n💎 DALL-E кредиты: +{credits} генераций\n"
                                f"Баланс: {get_dalle(uid)} ген", reply_markup=kb_main())
                        else:
                            # Добавляем бонусные кредиты при подписке
                            bonus = 5 if plan=="mp_start" else 20
                            set_sub(uid, plan, 30)
                            add_dalle(uid, bonus)
                            del_pending(pid)
                            name = "🟢 Старт" if plan=="mp_start" else "🔥 Про"
                            await bot.send_message(uid,
                                f"✅ Оплата прошла!\n\n{name} активирован на 30 дней.\n"
                                f"💎 Бонус: +{bonus} DALL-E генераций\n\nУдачных продаж! 🚀",
                                reply_markup=kb_main())
                    elif p.status == "canceled":
                        del_pending(pid)
                        await bot.send_message(uid, "❌ Платёж отменён.", reply_markup=kb_main())
                except Exception as e:
                    logging.error(f"payment check {pid}: {e}")
        except Exception as e:
            logging.error(f"payment loop: {e}")

# ─── ХЕНДЛЕРЫ: СТАРТ И ОНБОРДИНГ ────────────────────────────
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    u = message.from_user
    ensure_user(u.id, u.username or "", u.first_name or "")
    asyncio.create_task(asyncio.to_thread(sheets_add, u.id, u.username or "", u.first_name or ""))

    # Обработка реферальной ссылки
    args = message.text.split()
    if len(args) > 1 and args[1].startswith("ref_"):
        try:
            ref_by = int(args[1].replace("ref_",""))
            if ref_by != u.id:
                set_ref_by(u.id, ref_by)
                # Бонус тому кто пригласил
                add_dalle(ref_by, 3)
                add_ref_count(ref_by)
                add_ref_bonus(ref_by, 3)
                try:
                    await bot.send_message(ref_by,
                        "🎉 По твоей ссылке зарегистрировался новый пользователь!\n"
                        "💎 +3 DALL-E кредита на твой счёт!")
                except: pass
                # Бонус новому пользователю
                add_dalle(u.id, 2)
        except: pass

    if not get_onboarded(u.id):
        await state.set_state(OnboardStates.step1)
        await message.answer(
            f"👋 Привет, {u.first_name or 'друг'}!\n\n"
            f"Я МаркетПро — твой AI-помощник для работы на маркетплейсах.\n\n"
            f"Давай быстро покажу что умею 👇\n\n"
            f"📦 <b>Карточки товаров</b> — из фото или текста для WB, Ozon и Авито\n"
            f"🖼 <b>Инфографика DALL-E</b> — профессиональные карточки как у дизайнера\n"
            f"🧮 <b>Юнит-экономика</b> — считаю прибыль с учётом комиссий МП\n"
            f"⭐️ <b>Ответы на отзывы</b> — грамотно и без шаблонов\n"
            f"🔍 <b>Аудит карточки</b> — нахожу что мешает продавать\n"
            f"⚖️ <b>Апелляции к МП</b> — официальные письма при штрафах\n\n"
            f"<b>Тебе дарю бесплатно:</b>\n"
            f"✅ 5 текстовых запросов\n"
            f"🖼 1 DALL-E генерацию\n\n"
            f"Нажми кнопку чтобы начать 👇",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🚀 Начать работу!", callback_data="onboard_done")]
            ])
        )
        add_dalle(u.id, FREE_DALLE)
    else:
        await show_main(message, u.id)

@dp.callback_query(F.data == "onboard_done")
async def onboard_done(call: CallbackQuery, state: FSMContext):
    await state.clear()
    set_onboarded(call.from_user.id)
    await call.message.answer(
        "Отлично! Выбирай с чего начнём 👇",
        reply_markup=kb_main()
    )
    await call.answer()

async def show_main(message: Message, uid: int):
    plan, sub_end = get_sub(uid)
    dalle = get_dalle(uid)
    if plan:
        name = "🟢 Старт" if plan=="mp_start" else "🔥 Про"
        status = f"Тариф: {name} до {sub_end.strftime('%d.%m.%Y')}"
    else:
        count = get_requests(uid)
        status = f"Бесплатных запросов: {max(0, FREE_REQUESTS-count)}/{FREE_REQUESTS}"
    await message.answer(
        f"МаркетПро 🚀\n{status}\n💎 DALL-E кредиты: {dalle}",
        reply_markup=kb_main()
    )

@dp.callback_query(F.data == "back_menu")
async def back_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    uid = call.from_user.id
    plan, sub_end = get_sub(uid)
    dalle = get_dalle(uid)
    if plan:
        name = "🟢 Старт" if plan=="mp_start" else "🔥 Про"
        status = f"Тариф: {name} до {sub_end.strftime('%d.%m.%Y')}"
    else:
        count = get_requests(uid)
        status = f"Бесплатных запросов: {max(0, FREE_REQUESTS-count)}/{FREE_REQUESTS}"
    await call.message.answer(
        f"МаркетПро 🚀\n{status}\n💎 DALL-E кредиты: {dalle}",
        reply_markup=kb_main()
    )
    await call.answer()

# ─── РАЗДЕЛЫ МЕНЮ ────────────────────────────────────────────
@dp.callback_query(F.data == "sec_cards")
async def sec_cards(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.answer("📦 Карточки товаров\n\nВыбери способ создания:", reply_markup=kb_cards())
    await call.answer()

@dp.callback_query(F.data == "sec_analytics")
async def sec_analytics(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.answer("📊 Аналитика\n\nВыбери инструмент:", reply_markup=kb_analytics())
    await call.answer()

@dp.callback_query(F.data == "sec_tools")
async def sec_tools(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.answer("⚙️ Инструменты\n\nВыбери:", reply_markup=kb_tools())
    await call.answer()

# ─── ПРОФИЛЬ ─────────────────────────────────────────────────
@dp.callback_query(F.data == "my_profile")
async def my_profile(call: CallbackQuery):
    uid = call.from_user.id
    plan, sub_end = get_sub(uid)
    dalle = get_dalle(uid)
    reqs = get_requests(uid)
    if plan:
        name = "🟢 Старт" if plan=="mp_start" else "🔥 Про"
        plan_line = f"{name} до {sub_end.strftime('%d.%m.%Y')}"
    else:
        plan_line = f"Бесплатный ({max(0, FREE_REQUESTS-reqs)}/{FREE_REQUESTS} запросов)"
    await call.message.answer(
        f"👤 Профиль\n\n"
        f"ID: {uid}\n"
        f"Тариф: {plan_line}\n"
        f"💎 DALL-E кредиты: {dalle} генераций\n"
        f"📊 Всего запросов: {reqs}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👥 Пригласить друга", callback_data="referral")],
            [InlineKeyboardButton(text="💎 Купить DALL-E кредиты", callback_data="tariffs")],
            [InlineKeyboardButton(text="🔙 В меню", callback_data="back_menu")],
        ])
    )
    await call.answer()

# ─── ИСТОРИЯ ─────────────────────────────────────────────────
@dp.callback_query(F.data == "my_history")
async def my_history(call: CallbackQuery):
    uid = call.from_user.id
    if not is_paid(uid):
        await call.message.answer("🔒 История доступна с тарифа Старт.", reply_markup=kb_upgrade_start())
        await call.answer(); return
    rows = get_history(uid, 5)
    if not rows:
        await call.message.answer("История пуста. Создай первую карточку!", reply_markup=kb_back())
        await call.answer(); return
    feature_names = {
        "card_text":"Карточка из текста","card_photo":"Карточка из фото",
        "review":"Ответ на отзыв","audit":"Аудит","competitors":"Анализ ниши",
        "competitor_link":"Анализ конкурента","ad_text":"Рекламный текст",
        "appeal":"Апелляция","unit_eco":"Юнит-экономика","supply":"Поставка",
        "dalle":"DALL-E инфографика",
    }
    text = "📂 Последние 5 результатов:\n\n"
    for feature, result, created_at in rows:
        dt = datetime.fromisoformat(created_at).strftime("%d.%m %H:%M")
        fname = feature_names.get(feature, feature)
        text += f"📌 {fname} | {dt}\n{result[:150]}...\n\n"
    await call.message.answer(text, reply_markup=kb_back())
    await call.answer()

# ─── КАРТОЧКИ ────────────────────────────────────────────────
@dp.callback_query(F.data == "make_card_photo")
async def make_card_photo(call: CallbackQuery, state: FSMContext):
    if access_level(call.from_user.id) == "limit":
        await call.message.answer("🚫 Лимит бесплатных запросов исчерпан.", reply_markup=kb_upgrade_start())
        await call.answer(); return
    await state.clear()
    await state.set_state(CardStates.choose_platform)
    await state.update_data(input_type="photo")
    await call.message.answer("📸 Карточка из фото\n\nВыбери платформу:", reply_markup=kb_platform())
    await call.answer()

@dp.callback_query(F.data == "make_card_text")
async def make_card_text(call: CallbackQuery, state: FSMContext):
    if access_level(call.from_user.id) == "limit":
        await call.message.answer("🚫 Лимит бесплатных запросов исчерпан.", reply_markup=kb_upgrade_start())
        await call.answer(); return
    await state.clear()
    await state.set_state(CardStates.choose_platform)
    await state.update_data(input_type="text")
    await call.message.answer("✏️ Карточка из текста\n\nВыбери платформу:", reply_markup=kb_platform())
    await call.answer()

@dp.callback_query(F.data.startswith("platform_"), CardStates.choose_platform)
async def choose_platform(call: CallbackQuery, state: FSMContext):
    platform = call.data.replace("platform_", "")
    if platform == "all" and not is_paid(call.from_user.id):
        await call.message.answer("🔒 Все 3 платформы доступны в тарифе Старт.", reply_markup=kb_upgrade_start())
        await call.answer(); return
    data = await state.get_data()
    input_type = data.get("input_type", "text")
    await state.update_data(platform=platform)
    if input_type == "photo":
        await state.set_state(CardStates.waiting_photo)
        label = "все 3 платформы" if platform=="all" else PLATFORM_NAMES.get(platform, platform)
        await call.message.answer(
            f"Платформа: {label}\n\n📷 Отправь фото товара — определю что это и создам карточку.",
            reply_markup=kb_back()
        )
    else:
        await state.set_state(CardStates.waiting_text)
        label = "все 3 платформы" if platform=="all" else PLATFORM_NAMES.get(platform, platform)
        await call.message.answer(
            f"Платформа: {label}\n\n✏️ Опиши товар своими словами.\n\n"
            "Чем подробнее — тем лучше карточка:\n"
            "— что за товар\n— материал/состав\n— размер/цвет\n— для кого/зачем",
            reply_markup=kb_back()
        )
    await call.answer()

async def process_card(message, state, platform, input_type):
    uid = message.from_user.id
    await message.answer("⏳ Создаю карточку...")
    try:
        if platform == "all":
            results = []
            for pl in ["wb","ozon","avito"]:
                if input_type == "photo":
                    photo = message.photo[-1]
                    f = await bot.get_file(photo.file_id)
                    url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{f.file_path}"
                    r = await gen_card_photo(pl, url)
                else:
                    r = await gen_card_text(pl, message.text)
                save_history(uid, "card_photo" if input_type=="photo" else "card_text", r)
                results.append(f"{'─'*28}\n{PLATFORM_NAMES[pl].upper()}\n{'─'*28}\n{r}")
            result = "\n\n".join(results)
            for chunk in [result[i:i+4000] for i in range(0, len(result), 4000)]:
                await message.answer(chunk, reply_markup=kb_back())
        else:
            if input_type == "photo":
                photo = message.photo[-1]
                f = await bot.get_file(photo.file_id)
                url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{f.file_path}"
                result = await gen_card_photo(platform, url)
            else:
                result = await gen_card_text(platform, message.text)
            save_history(uid, "card_photo" if input_type=="photo" else "card_text", result)
            await message.answer(result, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Другой вариант", callback_data=f"regen_{platform}_{input_type}")],
                [InlineKeyboardButton(text="🔙 В меню", callback_data="back_menu")],
            ]))
            await state.update_data(last_desc=message.text if input_type=="text" else "", platform=platform)
        if access_level(uid) == "free":
            inc_requests(uid)
        await state.clear()
    except Exception as e:
        logging.error(f"Card error: {e}")
        await message.answer("❌ Ошибка генерации. Попробуй ещё раз.", reply_markup=kb_back())

@dp.message(CardStates.waiting_photo, F.photo)
async def card_photo(message: Message, state: FSMContext):
    data = await state.get_data()
    await process_card(message, state, data.get("platform","wb"), "photo")

@dp.message(CardStates.waiting_text, F.text)
async def card_text(message: Message, state: FSMContext):
    data = await state.get_data()
    await process_card(message, state, data.get("platform","wb"), "text")

@dp.message(CardStates.waiting_text, F.voice | F.audio)
async def card_voice(message: Message, state: FSMContext):
    await message.answer("🎙 Распознаю голосовое...")
    text = await transcribe(message)
    if not text:
        await message.answer("❌ Не удалось распознать. Напиши текстом.", reply_markup=kb_back()); return
    await message.answer(f"📝 Распознано: {text}\n\n⏳ Создаю карточку...")
    data = await state.get_data()
    platform = data.get("platform","wb")
    uid = message.from_user.id
    try:
        result = await gen_card_text(platform, text)
        save_history(uid, "card_text", result)
        if access_level(uid) == "free": inc_requests(uid)
        await message.answer(result, reply_markup=kb_back())
        await state.clear()
    except Exception as e:
        logging.error(f"Voice card: {e}")
        await message.answer("❌ Ошибка.", reply_markup=kb_back())

@dp.callback_query(F.data.startswith("regen_"))
async def regen(call: CallbackQuery, state: FSMContext):
    parts = call.data.split("_")
    platform = parts[1]
    input_type = parts[2] if len(parts)>2 else "text"
    data = await state.get_data()
    last = data.get("last_desc","")
    if not last:
        await call.message.answer("Для перегенерации опиши товар заново.", reply_markup=kb_back())
        await call.answer(); return
    await call.message.answer("⏳ Генерирую другой вариант...")
    try:
        result = await gen_card_text(platform, last)
        save_history(call.from_user.id, "card_text", result)
        await call.message.answer(result, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Другой вариант", callback_data=f"regen_{platform}_{input_type}")],
            [InlineKeyboardButton(text="🔙 В меню", callback_data="back_menu")],
        ]))
    except Exception as e:
        await call.message.answer("❌ Ошибка.", reply_markup=kb_back())
    await call.answer()

# ─── ОТВЕТ НА ОТЗЫВ ──────────────────────────────────────────
@dp.callback_query(F.data == "review_reply")
async def review_start(call: CallbackQuery, state: FSMContext):
    if access_level(call.from_user.id) == "limit":
        await call.message.answer("🚫 Лимит запросов исчерпан.", reply_markup=kb_upgrade_start())
        await call.answer(); return
    await state.clear()
    await state.set_state(ReviewStates.waiting)
    await call.message.answer(
        "⭐️ Ответ на отзыв\n\nВставь текст отзыва покупателя — напишу грамотный живой ответ.\n\n"
        "Работает с любым отзывом: негативным, позитивным, нейтральным.",
        reply_markup=kb_back()
    )
    await call.answer()

@dp.message(ReviewStates.waiting, F.text | F.voice | F.audio)
async def review_process(message: Message, state: FSMContext):
    text = message.text
    if message.voice or message.audio:
        await message.answer("🎙 Распознаю...")
        text = await transcribe(message)
        if not text:
            await message.answer("❌ Не удалось распознать.", reply_markup=kb_back()); return
    await message.answer("⏳ Пишу ответ...")
    try:
        result = await gen_review(text)
        save_history(message.from_user.id, "review", result)
        if access_level(message.from_user.id) == "free": inc_requests(message.from_user.id)
        await message.answer(f"⭐️ Готовый ответ:\n\n{result}", reply_markup=kb_back())
        await state.clear()
    except Exception as e:
        await message.answer("❌ Ошибка.", reply_markup=kb_back())

# ─── АУДИТ ───────────────────────────────────────────────────
@dp.callback_query(F.data == "audit_card")
async def audit_start(call: CallbackQuery, state: FSMContext):
    if not is_paid(call.from_user.id):
        await call.message.answer("🔒 Аудит доступен с тарифа Старт.", reply_markup=kb_upgrade_start())
        await call.answer(); return
    await state.clear()
    await state.set_state(AuditStates.waiting)
    await call.message.answer(
        "🔍 Аудит карточки\n\nВставь текст своей карточки (название + описание + характеристики).\n\n"
        "Дам оценку и конкретные правки.",
        reply_markup=kb_back()
    )
    await call.answer()

@dp.message(AuditStates.waiting, F.text | F.voice | F.audio)
async def audit_process(message: Message, state: FSMContext):
    text = message.text
    if message.voice or message.audio:
        await message.answer("🎙 Распознаю...")
        text = await transcribe(message)
        if not text:
            await message.answer("❌ Не удалось распознать.", reply_markup=kb_back()); return
    await message.answer("⏳ Провожу аудит...")
    try:
        result = await gen_audit(text)
        save_history(message.from_user.id, "audit", result)
        await message.answer(result, reply_markup=kb_back())
        await state.clear()
    except Exception as e:
        await message.answer("❌ Ошибка.", reply_markup=kb_back())

# ─── АНАЛИЗ НИШИ ─────────────────────────────────────────────
@dp.callback_query(F.data == "competitors")
async def competitors_start(call: CallbackQuery, state: FSMContext):
    if not is_paid(call.from_user.id):
        await call.message.answer("🔒 Анализ ниши доступен с тарифа Старт.", reply_markup=kb_upgrade_start())
        await call.answer(); return
    await state.clear()
    await state.set_state(CompetitorStates.waiting_niche)
    await call.message.answer(
        "🕵️ Анализ ниши\n\nНапиши нишу для анализа.\n\nНапример: силиконовые формы для выпечки, мужские носки",
        reply_markup=kb_back()
    )
    await call.answer()

@dp.message(CompetitorStates.waiting_niche, F.text | F.voice | F.audio)
async def competitors_process(message: Message, state: FSMContext):
    text = message.text
    if message.voice or message.audio:
        await message.answer("🎙 Распознаю...")
        text = await transcribe(message)
        if not text:
            await message.answer("❌ Не удалось распознать.", reply_markup=kb_back()); return
    await message.answer("⏳ Анализирую нишу...")
    try:
        result = await gen_competitors(text)
        save_history(message.from_user.id, "competitors", result)
        await message.answer(result, reply_markup=kb_back())
        await state.clear()
    except Exception as e:
        await message.answer("❌ Ошибка.", reply_markup=kb_back())

# ─── АНАЛИЗ КОНКУРЕНТА ПО ССЫЛКЕ ─────────────────────────────
@dp.callback_query(F.data == "competitor_link")
async def competitor_link_start(call: CallbackQuery, state: FSMContext):
    if not is_pro(call.from_user.id):
        await call.message.answer("🔒 Анализ конкурента по ссылке доступен в тарифе Про.", reply_markup=kb_upgrade_pro())
        await call.answer(); return
    await state.clear()
    await state.set_state(CompetitorLinkStates.waiting_link)
    await call.message.answer(
        "🔗 Анализ конкурента по ссылке\n\n"
        "Отправь ссылку на карточку товара конкурента (WB или Ozon).\n\n"
        "Пример: https://www.wildberries.ru/catalog/12345678/detail.aspx",
        reply_markup=kb_back()
    )
    await call.answer()

@dp.message(CompetitorLinkStates.waiting_link, F.text)
async def competitor_link_process(message: Message, state: FSMContext):
    await message.answer("⏳ Анализирую карточку конкурента...")
    try:
        result = await gen_competitor_link(message.text)
        save_history(message.from_user.id, "competitor_link", result)
        await message.answer(result, reply_markup=kb_back())
        await state.clear()
    except Exception as e:
        await message.answer("❌ Ошибка.", reply_markup=kb_back())

# ─── РЕКЛАМНЫЙ ТЕКСТ ─────────────────────────────────────────
@dp.callback_query(F.data == "ad_text")
async def ad_start(call: CallbackQuery, state: FSMContext):
    if not is_paid(call.from_user.id):
        await call.message.answer("🔒 Рекламный текст доступен с тарифа Старт.", reply_markup=kb_upgrade_start())
        await call.answer(); return
    await state.clear()
    await state.set_state(AdStates.waiting)
    await call.message.answer(
        "📣 Рекламный текст\n\nОпиши товар — напишу продающий текст для внутренней рекламы на МП.\n\n"
        "Название, главные преимущества, для кого.",
        reply_markup=kb_back()
    )
    await call.answer()

@dp.message(AdStates.waiting, F.text | F.voice | F.audio)
async def ad_process(message: Message, state: FSMContext):
    text = message.text
    if message.voice or message.audio:
        await message.answer("🎙 Распознаю...")
        text = await transcribe(message)
        if not text:
            await message.answer("❌ Не удалось распознать.", reply_markup=kb_back()); return
    await message.answer("⏳ Создаю рекламный текст...")
    try:
        result = await gen_ad(text)
        save_history(message.from_user.id, "ad_text", result)
        await message.answer(result, reply_markup=kb_back())
        await state.clear()
    except Exception as e:
        await message.answer("❌ Ошибка.", reply_markup=kb_back())

# ─── АПЕЛЛЯЦИЯ ───────────────────────────────────────────────
@dp.callback_query(F.data == "appeal")
async def appeal_start(call: CallbackQuery, state: FSMContext):
    if not is_pro(call.from_user.id):
        await call.message.answer(
            "🔒 Апелляции к маркетплейсам доступны в тарифе Про.\n\n"
            "Пишу официальные письма при штрафах, блокировках и претензиях от WB и Ozon.",
            reply_markup=kb_upgrade_pro()
        )
        await call.answer(); return
    await state.clear()
    await state.set_state(AppealStates.choose_type)
    await call.message.answer("⚖️ Апелляция к маркетплейсу\n\nВыбери тип обращения:", reply_markup=kb_appeal_types())
    await call.answer()

@dp.callback_query(F.data.startswith("appeal_"), AppealStates.choose_type)
async def appeal_type(call: CallbackQuery, state: FSMContext):
    atype = call.data.replace("appeal_", "")
    await state.update_data(appeal_type=atype)
    await state.set_state(AppealStates.waiting)
    types = {"fine":"штрафе","block":"блокировке карточки","supply":"претензии по поставке","review":"несправедливом отзыве"}
    await call.message.answer(
        f"Опиши ситуацию с {types.get(atype,'штрафе')} подробно:\n\n"
        "— что произошло\n— когда\n— какая сумма или товар\n— что ты считаешь несправедливым",
        reply_markup=kb_back()
    )
    await call.answer()

@dp.message(AppealStates.waiting, F.text | F.voice | F.audio)
async def appeal_process(message: Message, state: FSMContext):
    text = message.text
    if message.voice or message.audio:
        await message.answer("🎙 Распознаю...")
        text = await transcribe(message)
        if not text:
            await message.answer("❌ Не удалось распознать.", reply_markup=kb_back()); return
    data = await state.get_data()
    atype = data.get("appeal_type","fine")
    await message.answer("⏳ Составляю апелляцию...")
    try:
        result = await gen_appeal(atype, text)
        save_history(message.from_user.id, "appeal", result)
        await message.answer(result, reply_markup=kb_back())
        await state.clear()
    except Exception as e:
        await message.answer("❌ Ошибка.", reply_markup=kb_back())

# ─── ЮНИТ-ЭКОНОМИКА ──────────────────────────────────────────
@dp.callback_query(F.data == "unit_eco")
async def unit_start(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(UnitStates.waiting_cost)
    await call.message.answer(
        "🧮 Юнит-экономика\n\nШаг 1/4 — Себестоимость товара (закупка + доставка до склада) в рублях:\n\nНапример: 350",
        reply_markup=kb_back()
    )
    await call.answer()

@dp.message(UnitStates.waiting_cost, F.text)
async def unit_cost(message: Message, state: FSMContext):
    try:
        cost = float(message.text.strip().replace(",",".").replace(" ",""))
        await state.update_data(cost=cost)
        await state.set_state(UnitStates.waiting_price)
        await message.answer(f"✅ Себестоимость: {cost:,.0f} ₽\n\nШаг 2/4 — Цена продажи для покупателя:")
    except: await message.answer("Введи число. Например: 350")

@dp.message(UnitStates.waiting_price, F.text)
async def unit_price(message: Message, state: FSMContext):
    try:
        price = float(message.text.strip().replace(",",".").replace(" ",""))
        await state.update_data(price=price)
        await state.set_state(UnitStates.waiting_category)
        await message.answer(
            f"✅ Цена: {price:,.0f} ₽\n\nШаг 3/4 — Категория товара:\n\n"
            "Одежда / Обувь / Электроника / Косметика / Товары для дома / Игрушки / Спорт / Продукты / Другое"
        )
    except: await message.answer("Введи число. Например: 990")

@dp.message(UnitStates.waiting_category, F.text)
async def unit_category(message: Message, state: FSMContext):
    await state.update_data(category=message.text.strip())
    await state.set_state(UnitStates.waiting_scheme)
    await message.answer(f"✅ Категория: {message.text.strip()}\n\nШаг 4/4 — Схема работы:", reply_markup=kb_scheme())

@dp.callback_query(F.data.startswith("scheme_"), UnitStates.waiting_scheme)
async def unit_scheme(call: CallbackQuery, state: FSMContext):
    scheme = call.data.replace("scheme_","")
    data = await state.get_data()
    await state.clear()
    await call.message.answer("⏳ Считаю...")
    try:
        result = await gen_unit_eco(data["cost"], data["price"], data["category"], scheme)
        save_history(call.from_user.id, "unit_eco", result)
        await call.message.answer(result, reply_markup=kb_back())
    except Exception as e:
        await call.message.answer("❌ Ошибка.", reply_markup=kb_back())
    await call.answer()

# ─── КАЛЬКУЛЯТОР ПОСТАВКИ ────────────────────────────────────
@dp.callback_query(F.data == "supply_calc")
async def supply_start(call: CallbackQuery, state: FSMContext):
    if not is_pro(call.from_user.id):
        await call.message.answer("🔒 Калькулятор поставки доступен в тарифе Про.", reply_markup=kb_upgrade_pro())
        await call.answer(); return
    await state.clear()
    await state.set_state(SupplyStates.waiting_cost)
    await call.message.answer(
        "📦 Калькулятор поставки\n\nШаг 1/5 — Себестоимость единицы товара (₽):\n\nНапример: 250",
        reply_markup=kb_back()
    )
    await call.answer()

@dp.message(SupplyStates.waiting_cost, F.text)
async def supply_cost(message: Message, state: FSMContext):
    try:
        v = float(message.text.strip().replace(",",".").replace(" ",""))
        await state.update_data(cost=v)
        await state.set_state(SupplyStates.waiting_price)
        await message.answer(f"✅ Себестоимость: {v:,.0f} ₽\n\nШаг 2/5 — Цена продажи (₽):")
    except: await message.answer("Введи число.")

@dp.message(SupplyStates.waiting_price, F.text)
async def supply_price(message: Message, state: FSMContext):
    try:
        v = float(message.text.strip().replace(",",".").replace(" ",""))
        await state.update_data(price=v)
        await state.set_state(SupplyStates.waiting_sales)
        await message.answer(f"✅ Цена: {v:,.0f} ₽\n\nШаг 3/5 — Среднее количество продаж в день (шт):")
    except: await message.answer("Введи число.")

@dp.message(SupplyStates.waiting_sales, F.text)
async def supply_sales(message: Message, state: FSMContext):
    try:
        v = float(message.text.strip().replace(",",".").replace(" ",""))
        await state.update_data(sales_day=v)
        await state.set_state(SupplyStates.waiting_stock)
        await message.answer(f"✅ Продаж в день: {v} шт\n\nШаг 4/5 — Текущий остаток на складе (шт):")
    except: await message.answer("Введи число.")

@dp.message(SupplyStates.waiting_stock, F.text)
async def supply_stock(message: Message, state: FSMContext):
    try:
        v = float(message.text.strip().replace(",",".").replace(" ",""))
        await state.update_data(stock=v)
        await state.set_state(SupplyStates.waiting_days)
        await message.answer(f"✅ Остаток: {v} шт\n\nШаг 5/5 — На сколько дней планируем поставку (обычно 30-60):")
    except: await message.answer("Введи число.")

@dp.message(SupplyStates.waiting_days, F.text)
async def supply_days(message: Message, state: FSMContext):
    try:
        v = float(message.text.strip().replace(",",".").replace(" ",""))
        data = await state.get_data()
        await state.clear()
        await message.answer("⏳ Считаю...")
        result = await gen_supply(data["cost"], data["price"], data["sales_day"], data["stock"], v)
        save_history(message.from_user.id, "supply", result)
        await message.answer(result, reply_markup=kb_back())
    except: await message.answer("Введи число.")

# ─── DALL-E ИНФОГРАФИКА ──────────────────────────────────────
@dp.callback_query(F.data == "sec_dalle")
async def sec_dalle(call: CallbackQuery, state: FSMContext):
    await state.clear()
    uid = call.from_user.id
    dalle = get_dalle(uid)
    if not is_paid(uid):
        await call.message.answer(
            "🖼 Инфографика DALL-E\n\n"
            "Профессиональные карточки товаров как у топовых продавцов на WB и Ozon.\n\n"
            f"💎 Твой баланс: {dalle} генераций\n\n"
            "Для использования нужен тариф Старт или Про.\n"
            "При покупке тарифа — бонусные генерации в подарок!",
            reply_markup=kb_upgrade_start()
        )
        await call.answer(); return
    await state.set_state(DalleStates.choose_type)
    await call.message.answer(
        f"🖼 Инфографика DALL-E\n\n💎 Баланс: {dalle} генераций\n\nВыбери тип карточки:",
        reply_markup=kb_dalle_types()
    )
    await call.answer()

@dp.callback_query(F.data.startswith("dalle_"), DalleStates.choose_type)
async def dalle_choose(call: CallbackQuery, state: FSMContext):
    if call.data in ("dalle_from_photo", "dalle_slideset"):
        await state.clear()
        if call.data == "dalle_from_photo":
            await dalle_from_photo_start(call, state)
        else:
            await slideset_start(call, state)
        return
    dtype = call.data.replace("dalle_","")
    uid = call.from_user.id
    if get_dalle(uid) <= 0:
        await call.message.answer(
            "💎 DALL-E кредиты закончились.\n\nКупи кредиты чтобы продолжить:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="10 ген — 149 ₽", callback_data="pay_dalle_10")],
                [InlineKeyboardButton(text="30 ген — 349 ₽", callback_data="pay_dalle_30")],
                [InlineKeyboardButton(text="100 ген — 990 ₽", callback_data="pay_dalle_100")],
                [InlineKeyboardButton(text="🔙 В меню", callback_data="back_menu")],
            ])
        )
        await call.answer(); return
    type_name = DALLE_TYPES.get(dtype, ("Карточка",""))[0]
    await state.update_data(dalle_type=dtype)
    await state.set_state(DalleStates.waiting_desc)
    await call.message.answer(
        f"🖼 {type_name}\n\n"
        f"💎 Будет списана 1 генерация (баланс: {get_dalle(uid)})\n\n"
        f"Опиши товар подробно:\n"
        f"— что за товар\n— материал/состав\n— ключевые характеристики\n— для кого\n\n"
        f"Чем подробнее — тем лучше результат!",
        reply_markup=kb_back()
    )
    await call.answer()

@dp.message(DalleStates.waiting_desc, F.text | F.voice | F.audio)
async def dalle_generate(message: Message, state: FSMContext):
    text = message.text
    if message.voice or message.audio:
        await message.answer("🎙 Распознаю...")
        text = await transcribe(message)
        if not text:
            await message.answer("❌ Не удалось распознать.", reply_markup=kb_back()); return
    uid = message.from_user.id
    data = await state.get_data()
    dtype = data.get("dalle_type","cover")
    if not use_dalle(uid):
        await message.answer("💎 DALL-E кредиты закончились.", reply_markup=kb_tariffs())
        return
    await message.answer("⏳ Генерирую инфографику через DALL-E... это займёт 15-30 секунд")
    try:
        img_url = await gen_dalle(dtype, text)
        type_name = DALLE_TYPES.get(dtype, ("Карточка",""))[0]
        dalle_left = get_dalle(uid)
        save_history(uid, "dalle", f"{type_name}: {text[:100]}")
        async with httpx.AsyncClient() as hc:
            r = await hc.get(img_url)
            img_bytes = r.content
        photo_file = BufferedInputFile(img_bytes, filename="infographic.jpg")
        await message.answer_photo(
            photo_file,
            caption=f"🖼 {type_name} готова!\n\n💎 Осталось генераций: {dalle_left}\n\nСохрани и загружай в карточку товара.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Другой вариант (1 кредит)", callback_data=f"dalle_regen_{dtype}")],
                [InlineKeyboardButton(text="🖼 Другой тип карточки", callback_data="sec_dalle")],
                [InlineKeyboardButton(text="🔙 В меню", callback_data="back_menu")],
            ])
        )
        await state.update_data(last_dalle_desc=text)
    except Exception as e:
        logging.error(f"DALL-E error: {e}")
        add_dalle(uid, 1)  # Возвращаем кредит при ошибке
        await message.answer("❌ Ошибка генерации. Кредит возвращён. Попробуй ещё раз.", reply_markup=kb_back())
    await state.clear()

@dp.callback_query(F.data.startswith("dalle_regen_"))
async def dalle_regen(call: CallbackQuery, state: FSMContext):
    dtype = call.data.replace("dalle_regen_","")
    uid = call.from_user.id
    if get_dalle(uid) <= 0:
        await call.message.answer("💎 DALL-E кредиты закончились.", reply_markup=kb_tariffs())
        await call.answer(); return
    data = await state.get_data()
    last = data.get("last_dalle_desc","")
    if not last:
        await state.set_state(DalleStates.waiting_desc)
        await state.update_data(dalle_type=dtype)
        await call.message.answer("Опиши товар заново:", reply_markup=kb_back())
        await call.answer(); return
    if not use_dalle(uid):
        await call.message.answer("💎 Кредиты закончились.", reply_markup=kb_tariffs())
        await call.answer(); return
    await call.message.answer("⏳ Генерирую другой вариант...")
    try:
        img_url = await gen_dalle(dtype, last)
        dalle_left = get_dalle(uid)
        async with httpx.AsyncClient() as hc:
            r = await hc.get(img_url)
            img_bytes = r.content
        photo_file = BufferedInputFile(img_bytes, filename="infographic.jpg")
        type_name = DALLE_TYPES.get(dtype, ("Карточка",""))[0]
        await call.message.answer_photo(
            photo_file,
            caption=f"🖼 {type_name} — другой вариант!\n\n💎 Осталось: {dalle_left} генераций",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Ещё вариант (1 кредит)", callback_data=f"dalle_regen_{dtype}")],
                [InlineKeyboardButton(text="🔙 В меню", callback_data="back_menu")],
            ])
        )
    except Exception as e:
        add_dalle(uid, 1)
        await call.message.answer("❌ Ошибка. Кредит возвращён.", reply_markup=kb_back())
    await call.answer()

# ─── ТАРИФЫ И ОПЛАТА ─────────────────────────────────────────
@dp.callback_query(F.data == "tariffs")
async def tariffs(call: CallbackQuery):
    uid = call.from_user.id
    plan, sub_end = get_sub(uid)
    dalle = get_dalle(uid)
    count = get_requests(uid)
    if plan:
        name = "🟢 Старт" if plan=="mp_start" else "🔥 Про"
        status = f"Твой тариф: {name} до {sub_end.strftime('%d.%m.%Y')}"
    else:
        status = f"Использовано запросов: {count}/{FREE_REQUESTS}"
    await call.message.answer(
        f"💎 Тарифы МаркетПро\n\n{status}\n💎 DALL-E кредиты: {dalle}\n\n"
        f"🆓 Бесплатно:\n• {FREE_REQUESTS} текстовых запросов\n• 1 DALL-E генерация\n\n"
        f"🟢 Старт — 490 ₽/мес:\n"
        f"• Безлимит: карточки, SEO, юнит-экономика, отзывы, реклама\n"
        f"• Аудит карточки\n• Анализ ниши\n• История запросов\n"
        f"• +5 DALL-E генераций в подарок\n\n"
        f"🔥 Про — 990 ₽/мес:\n"
        f"• Всё из Старта\n• Апелляции к маркетплейсам\n"
        f"• Анализ конкурента по ссылке\n• Калькулятор поставки\n"
        f"• +20 DALL-E генераций в подарок\n\n"
        f"💎 DALL-E кредиты (докупка):\n"
        f"• 10 генераций — 149 ₽\n• 30 генераций — 349 ₽\n• 100 генераций — 990 ₽\n"
        f"• Кредиты не сгорают!\n",
        reply_markup=kb_tariffs()
    )
    await call.answer()

@dp.callback_query(F.data.startswith("pay_"))
async def pay(call: CallbackQuery):
    uid = call.from_user.id
    plans = {
        "pay_start":     (490,  "mp_start",    "Тариф Старт МаркетПро 30 дней"),
        "pay_pro":       (990,  "mp_pro",       "Тариф Про МаркетПро 30 дней"),
        "pay_dalle_10":  (149,  "dalle_10",     "DALL-E 10 генераций МаркетПро"),
        "pay_dalle_30":  (349,  "dalle_30",     "DALL-E 30 генераций МаркетПро"),
        "pay_dalle_100": (990,  "dalle_100",    "DALL-E 100 генераций МаркетПро"),
    }
    if call.data not in plans:
        await call.answer(); return
    amount, plan, desc = plans[call.data]
    await call.answer()
    try:
        p = await create_payment(uid, amount, plan, desc)
        save_pending(p.id, uid, plan, amount)
        await call.message.answer(
            f"💳 Оплата {amount} ₽\n\n{desc}\n\nНажми кнопку для оплаты.\nПодписка активируется автоматически! ✅",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"💳 Оплатить {amount} ₽", url=p.confirmation.confirmation_url)],
                [InlineKeyboardButton(text="🔙 В меню", callback_data="back_menu")],
            ])
        )
    except Exception as e:
        logging.error(f"Payment error: {e}")
        await call.message.answer(f"❌ Ошибка оплаты. Напиши в поддержку: {SUPPORT_URL}")

# ─── ADMIN КОМАНДЫ ────────────────────────────────────────────
@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    if message.from_user.id != OWNER_ID: return
    total, starts, pros, reqs = get_stats()
    await message.answer(
        f"📊 МаркетПро статистика\n\n"
        f"👤 Пользователей: {total}\n"
        f"🟢 Старт: {starts}\n🔥 Про: {pros}\n"
        f"⚡️ Запросов: {reqs}\n"
        f"💰 Доход/мес: {starts*490+pros*990} ₽"
    )

@dp.message(Command("give"))
async def cmd_give(message: Message):
    if message.from_user.id != OWNER_ID: return
    parts = message.text.split()
    if len(parts) < 3:
        await message.answer("Формат: /give USER_ID plan (mp_start|mp_pro)\nили: /give USER_ID dalle N")
        return
    uid = int(parts[1])
    if parts[2] == "dalle":
        n = int(parts[3]) if len(parts)>3 else 10
        add_dalle(uid, n)
        await message.answer(f"✅ +{n} DALL-E кредитов пользователю {uid}")
    else:
        set_sub(uid, parts[2], 30)
        await message.answer(f"✅ Тариф {parts[2]} выдан пользователю {uid}")

@dp.message(Command("broadcast"))
async def cmd_broadcast(message: Message, state: FSMContext):
    if message.from_user.id != OWNER_ID: return
    await state.set_state(BroadcastStates.waiting)
    await message.answer("Введи текст рассылки:")

@dp.message(BroadcastStates.waiting, F.text)
async def broadcast_send(message: Message, state: FSMContext):
    if message.from_user.id != OWNER_ID: return
    await state.clear()
    users = get_all_users()
    ok = 0
    for uid in users:
        try:
            await bot.send_message(uid, message.text)
            ok += 1
            await asyncio.sleep(0.05)
        except: pass
    await message.answer(f"✅ Рассылка отправлена {ok}/{len(users)} пользователям")



# ─── ФОТО → DALL-E ИНФОГРАФИКА ───────────────────────────────

@dp.callback_query(F.data == "dalle_from_photo")
async def dalle_from_photo_start(call: CallbackQuery, state: FSMContext):
    uid = call.from_user.id
    if not is_paid(uid):
        await call.message.answer("🔒 Фото → Инфографика доступна с тарифа Старт.", reply_markup=kb_upgrade_start())
        await call.answer(); return
    dalle = get_dalle(uid)
    if dalle < 2:
        await call.message.answer(
            f"💎 Для генерации из фото нужно 2 DALL-E кредита.\n\nТвой баланс: {dalle} кредитов.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="10 ген — 149 ₽", callback_data="pay_dalle_10")],
                [InlineKeyboardButton(text="30 ген — 349 ₽", callback_data="pay_dalle_30")],
                [InlineKeyboardButton(text="🔙 В меню", callback_data="back_menu")],
            ])
        )
        await call.answer(); return
    await state.clear()
    await state.set_state(DallePhotoStates.choose_type)
    await call.message.answer(
        "📸 Фото → Инфографика DALL-E\n\n"
        "Загружаешь фото товара → я определяю что это → DALL-E создаёт профессиональную карточку.\n\n"
        f"💎 Стоимость: 2 кредита (баланс: {dalle})\n\n"
        "Выбери тип карточки:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🏷 Обложка товара", callback_data="dphoto_cover"),
             InlineKeyboardButton(text="✅ Преимущества", callback_data="dphoto_benefits")],
            [InlineKeyboardButton(text="🔄 До / После", callback_data="dphoto_before_after"),
             InlineKeyboardButton(text="💡 Проблема→Решение", callback_data="dphoto_problem")],
            [InlineKeyboardButton(text="📋 Как использовать", callback_data="dphoto_howto"),
             InlineKeyboardButton(text="📦 Комплектация", callback_data="dphoto_kit")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="sec_dalle")],
        ])
    )
    await call.answer()

@dp.callback_query(F.data.startswith("dphoto_"), DallePhotoStates.choose_type)
async def dalle_photo_choose_type(call: CallbackQuery, state: FSMContext):
    dtype = call.data.replace("dphoto_", "")
    await state.update_data(dalle_type=dtype)
    await state.set_state(DallePhotoStates.waiting_photo)
    type_name = DALLE_TYPES.get(dtype, ("Карточка", ""))[0]
    await call.message.answer(
        f"Тип: {type_name}\n\n"
        "📷 Отправь фото товара — определю что это и создам профессиональную карточку.",
        reply_markup=kb_back()
    )
    await call.answer()

@dp.message(DallePhotoStates.waiting_photo, F.photo)
async def dalle_from_photo_generate(message: Message, state: FSMContext):
    uid = message.from_user.id
    data = await state.get_data()
    dtype = data.get("dalle_type", "cover")

    if get_dalle(uid) < 2:
        await message.answer("💎 Недостаточно кредитов. Нужно 2.", reply_markup=kb_tariffs())
        return

    await message.answer("⏳ Анализирую фото... потом генерирую карточку. 20-40 секунд.")

    try:
        # Шаг 1 — Claude анализирует фото
        photo = message.photo[-1]
        file = await bot.get_file(photo.file_id)
        image_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file.file_path}"

        system = (
            "Ты эксперт по маркетплейсам. Анализируй фото товара. "
            "Определи тип товара (не называй бренды). "
            "Верни ТОЛЬКО JSON без markdown:\n"
            '{"name":"название до 40 символов","benefits":["выгода1","выгода2","выгода3","выгода4"],"material":"материал или состав","use":"для кого и зачем","features":"ключевые характеристики через запятую"}'
        )
        raw = await claude_vision(system, "Определи товар и заполни JSON.", image_url)
        raw = re.sub(r"```json|```", "", raw).strip()
        product_data = json.loads(raw)

        # Формируем описание для DALL-E
        desc = (
            f"{product_data.get('name', 'товар')}. "
            f"Материал: {product_data.get('material', '')}. "
            f"Применение: {product_data.get('use', '')}. "
            f"Характеристики: {product_data.get('features', '')}. "
            f"Преимущества: {', '.join(product_data.get('benefits', []))}"
        )

        # Шаг 2 — списываем 2 кредита и генерируем через DALL-E
        use_dalle(uid)
        use_dalle(uid)

        img_url = await gen_dalle(dtype, desc)
        type_name = DALLE_TYPES.get(dtype, ("Карточка", ""))[0]
        dalle_left = get_dalle(uid)

        async with httpx.AsyncClient() as hc:
            r = await hc.get(img_url)
            img_bytes = r.content

        save_history(uid, "dalle", f"Фото→{type_name}: {product_data.get('name','')}")
        photo_file = BufferedInputFile(img_bytes, filename="infographic.jpg")
        await message.answer_photo(
            photo_file,
            caption=(
                f"🖼 {type_name} готова!\n\n"
                f"📦 {product_data.get('name', '')}\n\n"
                f"💎 Остаток кредитов: {dalle_left}\n\n"
                "Сохрани и загружай в карточку товара!"
            ),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Другой тип (2 кредита)", callback_data="dalle_from_photo")],
                [InlineKeyboardButton(text="📸 Другое фото", callback_data="dalle_from_photo")],
                [InlineKeyboardButton(text="🔙 В меню", callback_data="back_menu")],
            ])
        )
        await state.clear()

    except json.JSONDecodeError:
        # Если Claude вернул не JSON — используем текстовое описание
        add_dalle(uid, 1)  # Возвращаем 1 кредит (1 уже потрачен на Claude)
        await message.answer(
            "⚠️ Не удалось точно распознать товар. Попробуй описать его текстом через обычную DALL-E генерацию.",
            reply_markup=kb_back()
        )
    except Exception as e:
        logging.error(f"DALL-E from photo error: {e}")
        add_dalle(uid, 2)  # Возвращаем оба кредита при ошибке
        await message.answer("❌ Ошибка генерации. Оба кредита возвращены.", reply_markup=kb_back())


# ─── РЕФЕРАЛКА ───────────────────────────────────────────────

@dp.callback_query(F.data == "referral")
async def referral_info(call: CallbackQuery):
    uid = call.from_user.id
    ref_by, ref_count, ref_earned = get_ref_stats(uid)
    ref_link = f"https://t.me/PostGeniusHelperBot?start=ref_{uid}"
    await call.message.answer(
        "👥 Реферальная программа\n\n"
        f"Твоя ссылка:\n{ref_link}\n\n"
        "За каждого приглашённого:\n"
        "💎 +3 DALL-E кредита тебе\n"
        "💎 +2 DALL-E кредита другу\n\n"
        "Если приглашённый купит тариф:\n"
        "🟢 Старт — ещё +5 кредитов тебе\n"
        "🔥 Про — ещё +10 кредитов тебе\n\n"
        f"📊 Твоя статистика:\n"
        f"👤 Приглашено: {ref_count} чел.\n"
        f"💎 Заработано кредитов: {ref_earned}\n\n"
        "Поделись ссылкой с коллегами-продавцами!",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📤 Поделиться ссылкой", switch_inline_query=f"Попробуй МаркетПро — AI-помощник для WB и Ozon! {ref_link}")],
            [InlineKeyboardButton(text="🔙 В меню", callback_data="back_menu")],
        ])
    )
    await call.answer()

# ─── НАБОР СЛАЙДОВ DALL-E ────────────────────────────────────

@dp.callback_query(F.data == "dalle_slideset")
async def slideset_start(call: CallbackQuery, state: FSMContext):
    uid = call.from_user.id
    if not is_paid(uid):
        await call.message.answer("🔒 Набор слайдов доступен с тарифа Старт.", reply_markup=kb_upgrade_start())
        await call.answer(); return
    dalle = get_dalle(uid)
    if dalle < 5:
        await call.message.answer(
            f"💎 Для набора слайдов нужно 5 DALL-E кредитов.\n\n"
            f"Твой баланс: {dalle} кредитов.\n\n"
            "Купи кредиты чтобы продолжить:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="10 ген — 149 ₽", callback_data="pay_dalle_10")],
                [InlineKeyboardButton(text="30 ген — 349 ₽", callback_data="pay_dalle_30")],
                [InlineKeyboardButton(text="🔙 В меню", callback_data="back_menu")],
            ])
        )
        await call.answer(); return
    await state.clear()
    await state.set_state(SlideSetStates.choose_platform)
    await call.message.answer(
        "📚 Набор слайдов\n\n"
        "Генерирую 5 карточек в едином стиле:\n"
        "1️⃣ Обложка товара\n"
        "2️⃣ Преимущества\n"
        "3️⃣ Как использовать\n"
        "4️⃣ До / После\n"
        "5️⃣ Комплектация\n\n"
        f"💎 Стоимость: 5 кредитов (баланс: {dalle})\n\n"
        "Выбери платформу:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🟣 Wildberries", callback_data="slideset_wb"),
             InlineKeyboardButton(text="🔵 Ozon", callback_data="slideset_ozon")],
            [InlineKeyboardButton(text="🟡 Авито", callback_data="slideset_avito")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="back_menu")],
        ])
    )
    await call.answer()

@dp.callback_query(F.data.startswith("slideset_"), SlideSetStates.choose_platform)
async def slideset_platform(call: CallbackQuery, state: FSMContext):
    platform = call.data.replace("slideset_", "")
    await state.update_data(platform=platform)
    await state.set_state(SlideSetStates.waiting_desc)
    label = PLATFORM_NAMES.get(platform, platform)
    await call.message.answer(
        f"Платформа: {label}\n\n"
        "✏️ Опиши товар подробно:\n\n"
        "— что за товар\n"
        "— материал / состав\n"
        "— ключевые характеристики\n"
        "— для кого\n"
        "— главные преимущества\n\n"
        "Чем подробнее — тем лучше все 5 карточек!",
        reply_markup=kb_back()
    )
    await call.answer()

@dp.message(SlideSetStates.waiting_desc, F.text | F.voice | F.audio)
async def slideset_generate(message: Message, state: FSMContext):
    text = message.text
    if message.voice or message.audio:
        await message.answer("🎙 Распознаю...")
        text = await transcribe(message)
        if not text:
            await message.answer("❌ Не удалось распознать.", reply_markup=kb_back()); return

    uid = message.from_user.id
    data = await state.get_data()
    platform = data.get("platform", "wb")

    if get_dalle(uid) < 5:
        await message.answer("💎 Недостаточно кредитов для набора слайдов (нужно 5).", reply_markup=kb_tariffs())
        return

    await message.answer(
        "⏳ Генерирую набор из 5 слайдов...\n\n"
        "Это займёт 1-2 минуты. Отправлю все карточки по очереди 🎨"
    )

    slide_types = ["cover", "benefits", "howto", "before_after", "kit"]
    slide_names = {
        "cover": "1️⃣ Обложка товара",
        "benefits": "2️⃣ Преимущества",
        "howto": "3️⃣ Как использовать",
        "before_after": "4️⃣ До / После",
        "kit": "5️⃣ Комплектация",
    }

    success_count = 0
    for dtype in slide_types:
        if get_dalle(uid) <= 0:
            await message.answer("💎 Кредиты закончились во время генерации. Часть слайдов не создана.")
            break
        try:
            use_dalle(uid)
            img_url = await gen_dalle(dtype, text, platform)
            async with httpx.AsyncClient() as hc:
                r = await hc.get(img_url)
                img_bytes = r.content
            photo_file = BufferedInputFile(img_bytes, filename=f"slide_{dtype}.jpg")
            await message.answer_photo(
                photo_file,
                caption=f"{slide_names[dtype]}\n💎 Осталось кредитов: {get_dalle(uid)}"
            )
            success_count += 1
            await asyncio.sleep(1)
        except Exception as e:
            logging.error(f"Slideset {dtype} error: {e}")
            add_dalle(uid, 1)  # Возвращаем кредит при ошибке
            await message.answer(f"⚠️ Слайд {slide_names[dtype]} не удалось создать — кредит возвращён.")

    save_history(uid, "dalle", f"Набор слайдов: {text[:100]}")
    await message.answer(
        f"✅ Готово! Создано {success_count}/5 слайдов.\n\n"
        f"💎 Остаток кредитов: {get_dalle(uid)}\n\n"
        "Сохрани все фото и загружай в карточку товара!",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📚 Ещё набор", callback_data="dalle_slideset")],
            [InlineKeyboardButton(text="🔙 В меню", callback_data="back_menu")],
        ])
    )
    await state.clear()


# ─── ЗАГЛУШКИ ────────────────────────────────────────────────
@dp.message(F.text)
async def fallback_text(message: Message, state: FSMContext):
    current = await state.get_state()
    if current is None:
        await message.answer("Выбери действие из меню 👇", reply_markup=kb_main())

@dp.message(F.photo)
async def fallback_photo(message: Message, state: FSMContext):
    current = await state.get_state()
    if current is None:
        await message.answer(
            "📷 Хочешь создать карточку из фото?\n\nНажми '📦 Карточка товара' в меню:",
            reply_markup=kb_main()
        )

@dp.message(F.voice | F.audio)
async def fallback_voice(message: Message, state: FSMContext):
    current = await state.get_state()
    if current is None:
        await message.answer(
            "🎙 Голосовые работают внутри функций!\n\nНапример: выбери '📦 Карточка из текста' и отправь голосовое.",
            reply_markup=kb_main()
        )

# ─── ЗАПУСК ──────────────────────────────────────────────────
async def main():
    init_db()
    asyncio.create_task(check_payments_loop())
    logging.info("МаркетПро запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
