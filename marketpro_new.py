import asyncio
import logging
import sqlite3
import uuid
import io
import re
import json
import base64
import httpx
from datetime import datetime, timedelta
from openai import AsyncOpenAI
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
ANTHROPIC_KEY    = _env.get("ANTHROPIC_KEY", "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")
OWNER_ID         = int(_env.get("OWNER_ID", "549639607"))
SUPPORT_URL      = _env.get("SUPPORT_URL", "https://t.me/Boss023rus")
SPREADSHEET_ID   = _env.get("SPREADSHEET_ID", "")
CREDENTIALS_FILE = _env.get("CREDENTIALS_FILE", "/root/google_credentials.json")
YOOKASSA_SHOP_ID = _env.get("YOOKASSA_SHOP_ID", "1363324")
YOOKASSA_SECRET  = _env.get("YOOKASSA_SECRET", "")

Configuration.account_id = YOOKASSA_SHOP_ID
Configuration.secret_key  = YOOKASSA_SECRET

FREE_REQUESTS = 7   # бесплатных запросов
REF_BONUS_INVITER = 5  # бонус пригласившему
REF_BONUS_INVITED = 3  # бонус новому пользователю
SUB_PRICE = 690     # цена подписки

# ─── ИНИЦИАЛИЗАЦИЯ ──────────────────────────────────────────
bot    = Bot(token=BOT_TOKEN)
dp     = Dispatcher(storage=MemoryStorage())
openai = AsyncOpenAI(api_key=OPENAI_KEY)
claude = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
logging.basicConfig(level=logging.INFO)

# ─── FSM ────────────────────────────────────────────────────
class CardStates(StatesGroup):
    choose_platform = State()
    waiting_photo   = State()
    waiting_text    = State()

class ReviewStates(StatesGroup):
    waiting = State()

class AuditStates(StatesGroup):
    waiting = State()

class AuditLinkStates(StatesGroup):
    waiting = State()

class NicheStates(StatesGroup):
    waiting = State()

class CompetitorLinkStates(StatesGroup):
    waiting = State()

class AdStates(StatesGroup):
    waiting = State()

class AppealStates(StatesGroup):
    choose_type = State()
    waiting     = State()

class SupplyStates(StatesGroup):
    waiting_cost  = State()
    waiting_price = State()
    waiting_sales = State()
    waiting_stock = State()
    waiting_days  = State()

class UnitStates(StatesGroup):
    waiting_cost     = State()
    waiting_price    = State()
    waiting_category = State()
    waiting_scheme   = State()

class PromoStates(StatesGroup):
    waiting_price    = State()
    waiting_discount = State()
    waiting_cost     = State()

class SeoStates(StatesGroup):
    waiting = State()

class BroadcastStates(StatesGroup):
    waiting = State()

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
        sub_end TEXT DEFAULT '',
        paid_at TEXT DEFAULT ''
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
        bonus_requests INTEGER DEFAULT 0
    )""")
    conn.commit()
    conn.close()

def ensure_user(user_id, username="", first_name=""):
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (user_id,username,first_name,created_at) VALUES (?,?,?,?)",
              (user_id, username, first_name, datetime.now().isoformat()))
    c.execute("INSERT OR IGNORE INTO requests (user_id,count) VALUES (?,0)", (user_id,))
    c.execute("INSERT OR IGNORE INTO referrals (user_id,ref_by,ref_count,bonus_requests) VALUES (?,0,0,0)", (user_id,))
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
    paid = datetime.now().isoformat()
    conn.execute("INSERT OR REPLACE INTO subscriptions (user_id,plan,sub_end,paid_at) VALUES (?,?,?,?)",
                 (uid, plan, end, paid))
    conn.commit(); conn.close()

def is_paid(uid):
    plan, _ = get_sub(uid)
    return plan == "mp_sub"

def get_bonus_requests(uid):
    conn = sqlite3.connect(DB)
    r = conn.execute("SELECT bonus_requests FROM referrals WHERE user_id=?", (uid,)).fetchone()
    conn.close()
    return r[0] if r else 0

def add_bonus_requests(uid, amount):
    conn = sqlite3.connect(DB)
    conn.execute("INSERT OR IGNORE INTO referrals (user_id,ref_by,ref_count,bonus_requests) VALUES (?,0,0,0)", (uid,))
    conn.execute("UPDATE referrals SET bonus_requests=bonus_requests+? WHERE user_id=?", (amount, uid))
    conn.commit(); conn.close()

def can_use(uid):
    """Проверяет может ли пользователь использовать бот"""
    if is_paid(uid): return True
    used = get_requests(uid)
    bonus = get_bonus_requests(uid)
    return used < FREE_REQUESTS + bonus

def get_remaining(uid):
    """Остаток бесплатных запросов"""
    used = get_requests(uid)
    bonus = get_bonus_requests(uid)
    total = FREE_REQUESTS + bonus
    return max(0, total - used)

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

def get_ref_stats(uid):
    conn = sqlite3.connect(DB)
    r = conn.execute("SELECT ref_by,ref_count,bonus_requests FROM referrals WHERE user_id=?", (uid,)).fetchone()
    conn.close()
    return r if r else (0, 0, 0)

def set_ref_by(uid, ref_by):
    conn = sqlite3.connect(DB)
    conn.execute("UPDATE referrals SET ref_by=? WHERE user_id=? AND ref_by=0", (ref_by, uid))
    conn.commit(); conn.close()

def add_ref_count(ref_by):
    conn = sqlite3.connect(DB)
    conn.execute("UPDATE referrals SET ref_count=ref_count+1 WHERE user_id=?", (ref_by,))
    conn.commit(); conn.close()

def get_stats():
    conn = sqlite3.connect(DB)
    total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    subs  = conn.execute("SELECT COUNT(*) FROM subscriptions WHERE plan='mp_sub' AND sub_end>?",
                         (datetime.now().isoformat(),)).fetchone()[0]
    reqs  = conn.execute("SELECT SUM(count) FROM requests").fetchone()[0] or 0
    conn.close()
    return total, subs, reqs

# ─── GOOGLE SHEETS ───────────────────────────────────────────
def sheets_add_user(uid, username, first_name):
    try:
        if not SPREADSHEET_ID: return
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)
        gc = gspread.authorize(creds)
        spreadsheet = gc.open_by_key(SPREADSHEET_ID)

        # Ищем или создаём лист МаркетПРО
        try:
            sheet = spreadsheet.worksheet("МаркетПРО")
        except:
            sheet = spreadsheet.add_worksheet("МаркетПРО", rows=1000, cols=10)
            sheet.append_row(["ID","Username","Имя","Дата регистрации","Тариф","Запросов","Дата оплаты","Пригласил"])

        col = sheet.col_values(1)
        if str(uid) not in col:
            sheet.append_row([
                str(uid),
                f"@{username}" if username else "—",
                first_name or "—",
                datetime.now().strftime("%d.%m.%Y %H:%M"),
                "Бесплатный", 0, "—", "—"
            ])
    except Exception as e:
        logging.error(f"Sheets add_user error: {e}")

def sheets_update_sub(uid):
    try:
        if not SPREADSHEET_ID: return
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)
        gc = gspread.authorize(creds)
        sheet = gc.open_by_key(SPREADSHEET_ID).worksheet("МаркетПРО")
        col = sheet.col_values(1)
        if str(uid) in col:
            row = col.index(str(uid)) + 1
            sheet.update_cell(row, 5, "Подписка 690₽")
            sheet.update_cell(row, 7, datetime.now().strftime("%d.%m.%Y %H:%M"))
    except Exception as e:
        logging.error(f"Sheets update_sub error: {e}")

def sheets_update_requests(uid):
    try:
        if not SPREADSHEET_ID: return
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)
        gc = gspread.authorize(creds)
        sheet = gc.open_by_key(SPREADSHEET_ID).worksheet("МаркетПРО")
        col = sheet.col_values(1)
        if str(uid) in col:
            row = col.index(str(uid)) + 1
            sheet.update_cell(row, 6, get_requests(uid))
    except Exception as e:
        logging.error(f"Sheets update_requests error: {e}")

# ─── КЛАВИАТУРЫ ─────────────────────────────────────────────
def kb_main():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 Карточка товара", callback_data="sec_cards")],
        [InlineKeyboardButton(text="📊 Аудит и анализ", callback_data="sec_analytics")],
        [InlineKeyboardButton(text="🧮 Финансы и расчёты", callback_data="sec_finance")],
        [InlineKeyboardButton(text="⭐️ Работа с отзывами", callback_data="review_reply")],
        [InlineKeyboardButton(text="⚖️ Апелляции", callback_data="appeal")],
        [InlineKeyboardButton(text="👤 Мой профиль", callback_data="my_profile"),
         InlineKeyboardButton(text="💬 Поддержка", url=SUPPORT_URL)],
    ])

def kb_cards():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📸 Из фото товара", callback_data="make_card_photo")],
        [InlineKeyboardButton(text="✏️ Из текста / описания", callback_data="make_card_text")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_menu")],
    ])

def kb_analytics():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Аудит карточки по тексту", callback_data="audit_text")],
        [InlineKeyboardButton(text="🔗 Аудит по ссылке WB/Ozon", callback_data="audit_link")],
        [InlineKeyboardButton(text="🕵️ Анализ ниши", callback_data="niche_analysis")],
        [InlineKeyboardButton(text="🏆 Анализ конкурента по ссылке", callback_data="competitor_link")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_menu")],
    ])

def kb_finance():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧮 Юнит-экономика", callback_data="unit_eco")],
        [InlineKeyboardButton(text="📦 Калькулятор поставки", callback_data="supply_calc")],
        [InlineKeyboardButton(text="🎯 Расчёт акции WB/Ozon", callback_data="promo_calc")],
        [InlineKeyboardButton(text="📣 Рекламный текст", callback_data="ad_text")],
        [InlineKeyboardButton(text="🔑 SEO-заголовки (5 вариантов)", callback_data="seo_titles")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_menu")],
    ])

def kb_platform():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟣 Wildberries", callback_data="platform_wb"),
         InlineKeyboardButton(text="🔵 Ozon", callback_data="platform_ozon")],
        [InlineKeyboardButton(text="🟡 Авито", callback_data="platform_avito")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_menu")],
    ])

def kb_appeal_types():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Штраф от маркетплейса", callback_data="appeal_fine")],
        [InlineKeyboardButton(text="🚫 Блокировка карточки", callback_data="appeal_block")],
        [InlineKeyboardButton(text="📦 Претензия по поставке", callback_data="appeal_supply")],
        [InlineKeyboardButton(text="⭐️ Несправедливый отзыв", callback_data="appeal_review")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_menu")],
    ])

def kb_scheme():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="FBO (склад маркетплейса)", callback_data="scheme_fbo"),
         InlineKeyboardButton(text="FBS (свой склад)", callback_data="scheme_fbs")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_menu")],
    ])

def kb_back():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 В меню", callback_data="back_menu")]
    ])

def kb_subscribe():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Оформить подписку — 690 ₽/мес", callback_data="pay_sub")],
        [InlineKeyboardButton(text="👥 Пригласить друга и получить +5 запросов", callback_data="my_referral")],
        [InlineKeyboardButton(text="🔙 В меню", callback_data="back_menu")],
    ])

def kb_profile(uid):
    buttons = [
        [InlineKeyboardButton(text="📂 История запросов", callback_data="my_history")],
        [InlineKeyboardButton(text="👥 Реферальная программа", callback_data="my_referral")],
    ]
    if not is_paid(uid):
        buttons.append([InlineKeyboardButton(text="💳 Оформить подписку — 690 ₽/мес", callback_data="pay_sub")])
    buttons.append([InlineKeyboardButton(text="🔙 В меню", callback_data="back_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ─── ПРОВЕРКА ДОСТУПА ────────────────────────────────────────
async def check_access(uid, message_or_call):
    """Проверяет доступ и отправляет сообщение если лимит исчерпан. Возвращает True если доступ есть."""
    if can_use(uid):
        return True
    remaining = get_remaining(uid)
    text = (
        "⛔️ У тебя закончились бесплатные запросы.\n\n"
        f"Использовано: {get_requests(uid)} из {FREE_REQUESTS + get_bonus_requests(uid)}\n\n"
        "Оформи подписку — 690 ₽/мес и получи:\n"
        "✅ Безлимитные запросы\n"
        "✅ Все функции без ограничений\n"
        "✅ Приоритетный ответ\n\n"
        "Или пригласи друга и получи +5 бесплатных запросов 👥"
    )
    if isinstance(message_or_call, CallbackQuery):
        await message_or_call.message.answer(text, reply_markup=kb_subscribe())
        await message_or_call.answer()
    else:
        await message_or_call.answer(text, reply_markup=kb_subscribe())
    return False

# ─── GPT ФУНКЦИИ ────────────────────────────────────────────
PLATFORM_RULES = {
    "wb": """ПЛАТФОРМА: Wildberries
- Название: до 60 символов, начинай с ключа
- Описание: 500–2000 символов, структура: выгоды → характеристики → применение
- SEO-ключи: 20 штук через запятую
- Характеристики: бренд, категория, страна, состав/материал
- Запрещено: ссылки, слова "лучший/№1"
- Никакой markdown! Только чистый текст
Формат ответа строго:
📦 НАЗВАНИЕ:
[название]

📝 ОПИСАНИЕ:
[текст описания]

🔑 SEO-КЛЮЧИ:
[ключи через запятую]

📋 ХАРАКТЕРИСТИКИ:
[каждая с новой строки]""",

    "ozon": """ПЛАТФОРМА: Ozon
- Название: 50–200 символов, Тип+Бренд+характеристики
- Описание: 500–4000 символов
- SEO-ключи: 20 штук через запятую
- Никакой markdown! Только чистый текст
Формат ответа строго:
📦 НАЗВАНИЕ:
[название]

📝 ОПИСАНИЕ:
[текст]

🔑 SEO-КЛЮЧИ:
[ключи]

📋 ХАРАКТЕРИСТИКИ:
[характеристики]""",

    "avito": """ПЛАТФОРМА: Авито
- Заголовок: до 50 символов
- Описание: 300–2000 символов, боль → преимущества → призыв
- Ключи: 10–15 фраз
- Никакой markdown! Только чистый текст
Формат ответа строго:
📦 ЗАГОЛОВОК:
[заголовок]

📝 ОПИСАНИЕ:
[текст]

🔑 КЛЮЧЕВЫЕ СЛОВА:
[ключи]""",
}

PLATFORM_NAMES = {"wb": "Wildberries 🟣", "ozon": "Ozon 🔵", "avito": "Авито 🟡"}

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
    # Определяем тип
    media_type = "image/jpeg"
    if img_bytes[:4] == b'\x89PNG': media_type = "image/png"
    elif img_bytes[:4] == b'RIFF': media_type = "image/webp"
    resp = claude.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1500,
        system=system,
        messages=[{"role":"user","content":[
            {"type":"image","source":{"type":"base64","media_type":media_type,"data":b64}},
            {"type":"text","text":user_text}
        ]}]
    )
    return resp.content[0].text.strip()

async def transcribe(message: Message) -> str:
    fid = message.voice.file_id if message.voice else (message.audio.file_id if message.audio else None)
    if not fid: return ""
    f = await bot.get_file(fid)
    fb = await bot.download_file(f.file_path)
    audio = io.BytesIO(fb.read())
    audio.name = "voice.ogg"
    t = await openai.audio.transcriptions.create(model="whisper-1", file=audio, language="ru")
    return t.text.strip()

async def gen_card_text(platform, description):
    rules = PLATFORM_RULES.get(platform, PLATFORM_RULES["wb"])
    system = ("Ты эксперт по карточкам маркетплейсов. Пишешь только на русском. "
              "Никогда не начинай с 'Конечно', 'Отлично', 'Вот'. "
              "Сразу выдавай результат в нужном формате. Никакого markdown!\n\n" + rules)
    return await gpt(system, f"Создай карточку для товара:\n{description}")

async def gen_card_photo(platform, image_url):
    rules = PLATFORM_RULES.get(platform, PLATFORM_RULES["wb"])
    system = ("Ты эксперт по карточкам маркетплейсов. Пишешь только на русском. "
              "Определи тип товара на фото (не называй бренды), создай карточку. "
              "Никогда не начинай с 'Конечно', 'Отлично'. Никакого markdown!\n\n" + rules)
    return await claude_vision(system, "Определи товар и создай карточку.", image_url)

async def gen_review(text):
    system = ("Ты менеджер маркетплейса. Пишешь живые ответы на отзывы покупателей. "
              "На негатив — признаёшь проблему, предлагаешь решение, не оправдываешься. "
              "На позитив — благодаришь тепло и искренне. "
              "50–150 слов. Только русский язык. Без markdown.")
    return await gpt(system, f"Напиши ответ на отзыв покупателя:\n\n{text}")

async def gen_audit_text(text):
    system = "Ты эксперт по оптимизации карточек маркетплейсов WB и Ozon. Даёшь конкретные правки."
    prompt = (f"Проведи аудит карточки товара:\n\n{text}\n\n"
              "Оцени строго по пунктам:\n"
              "⭐️ Общая оценка: N/10\n\n"
              "📦 Название: [что хорошо и что исправить]\n\n"
              "📝 Описание: [что хорошо и что исправить]\n\n"
              "🔑 SEO-ключи: [есть ли, что добавить]\n\n"
              "📋 Характеристики: [полнота, что добавить]\n\n"
              "✅ Сильные стороны:\n\n"
              "⚠️ Главные проблемы:\n\n"
              "🚀 Топ-3 правки для роста продаж:\n"
              "1.\n2.\n3.")
    return await gpt(system, prompt, max_tokens=1500)

async def gen_audit_link(url):
    system = "Ты эксперт по маркетплейсам. Анализируешь карточки товаров и даёшь конкретные рекомендации."
    prompt = (f"Проанализируй карточку товара по ссылке: {url}\n\n"
              "Определи товар и проведи аудит:\n"
              "⭐️ Общая оценка: N/10\n\n"
              "📦 Название: [анализ]\n\n"
              "📝 Описание: [анализ]\n\n"
              "🔑 SEO: [анализ]\n\n"
              "✅ Сильные стороны:\n\n"
              "⚠️ Слабые места:\n\n"
              "🚀 Топ-3 правки:\n"
              "1.\n2.\n3.")
    return await gpt(system, prompt, max_tokens=1500)

async def gen_niche(niche):
    system = "Ты аналитик маркетплейсов. Даёшь практичные инсайты без воды."
    prompt = (f"Анализ ниши для маркетплейсов WB/Ozon: {niche}\n\n"
              "📊 Что продают лидеры ниши:\n\n"
              "🔑 Топ ключевые слова (15 штук):\n\n"
              "🔥 Что ценят покупатели:\n\n"
              "⚠️ Слабые места конкурентов:\n\n"
              "💡 5 идей как выделиться:\n\n"
              "✅ Главный совет для входа в нишу:")
    return await gpt(system, prompt, max_tokens=1500)

async def gen_competitor_link(url):
    system = "Ты аналитик маркетплейсов. Анализируешь карточки конкурентов и даёшь тактические советы."
    prompt = (f"Проанализируй карточку конкурента: {url}\n\n"
              "📦 Товар и его сильные стороны:\n\n"
              "🔑 Ключевые слова (10-15 штук):\n\n"
              "📝 Структура описания:\n\n"
              "✅ Что стоит взять себе:\n\n"
              "⚠️ Слабые места конкурента:\n\n"
              "💡 Как сделать лучше:")
    return await gpt(system, prompt, max_tokens=1500)

async def gen_ad(text):
    system = "Ты копирайтер для рекламы на маркетплейсах. Формула: боль→решение→выгоды→призыв. Без markdown."
    prompt = (f"Рекламный текст для товара:\n{text}\n\n"
              "🎯 ЗАГОЛОВОК (до 40 символов):\n\n"
              "📣 ТЕКСТ (100–180 слов):\n\n"
              "🏷️ СЛОГАН (до 20 символов):")
    return await gpt(system, prompt, max_tokens=800)

async def gen_seo_titles(product):
    system = "Ты SEO-эксперт по маркетплейсам WB и Ozon. Пишешь продающие заголовки с ключами."
    prompt = (f"Напиши 5 вариантов SEO-заголовков для товара: {product}\n\n"
              "Требования:\n"
              "- Каждый до 60 символов\n"
              "- Начинается с главного ключа\n"
              "- Содержит характеристику или выгоду\n"
              "- Без слов 'лучший', 'топ', '№1'\n\n"
              "Формат:\n"
              "1. [заголовок]\n"
              "2. [заголовок]\n"
              "3. [заголовок]\n"
              "4. [заголовок]\n"
              "5. [заголовок]")
    return await gpt(system, prompt, max_tokens=600)

async def gen_appeal(appeal_type, text):
    types = {
        "fine":   "штраф от маркетплейса",
        "block":  "блокировка карточки товара",
        "supply": "претензия по поставке",
        "review": "несправедливый отзыв покупателя",
    }
    system = ("Ты юрист и эксперт по работе с маркетплейсами WB и Ozon. "
              "Пишешь официальные апелляции. "
              "Тон: профессиональный, аргументированный, без эмоций.")
    prompt = (f"Напиши апелляцию по ситуации: {types.get(appeal_type, 'штраф')}\n\n"
              f"Детали: {text}\n\n"
              "📋 АПЕЛЛЯЦИЯ В СЛУЖБУ ПОДДЕРЖКИ:\n\n"
              "💡 ДОПОЛНИТЕЛЬНЫЕ АРГУМЕНТЫ:\n\n"
              "✅ СОВЕТ ЧТО СДЕЛАТЬ:")
    return await gpt(system, prompt, max_tokens=1200)

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
        f"🧮 ЮНИТ-ЭКОНОМИКА\n{'─'*30}\n"
        f"📦 Категория: {category}\n"
        f"💰 Себестоимость: {fmt(cost)} ₽\n"
        f"🏷 Цена продажи: {fmt(price)} ₽\n"
        f"📦 Схема: {'FBO' if scheme=='fbo' else 'FBS'}\n\n"
        f"🟣 WILDBERRIES\n"
        f"Комиссия {int(wb_comm*100)}%: -{fmt(price*wb_comm)} ₽\n"
        f"Логистика: -{fmt(wb_log)} ₽\n"
        f"Хранение: -{fmt(storage)} ₽\n"
        f"Прибыль: {fmt(wb_profit)} ₽ ({wb_margin:.1f}%)\n"
        f"{verdict(wb_margin)}\n\n"
        f"🔵 OZON\n"
        f"Комиссия {int(oz_comm*100)}%: -{fmt(price*oz_comm)} ₽\n"
        f"Логистика: -{fmt(oz_log)} ₽\n"
        f"Хранение: -{fmt(storage)} ₽\n"
        f"Прибыль: {fmt(oz_profit)} ₽ ({oz_margin:.1f}%)\n"
        f"{verdict(oz_margin)}\n\n"
        f"📊 Для дохода 50 000 ₽/мес нужно:\n"
        f"WB: {max(1,int(50000/wb_profit)) if wb_profit>0 else '∞'} продаж\n"
        f"Ozon: {max(1,int(50000/oz_profit)) if oz_profit>0 else '∞'} продаж\n\n"
        f"⚠️ Расчёт приблизительный. Комиссии могут отличаться."
    )

async def gen_supply(cost, price, sales_day, stock, days):
    demand = sales_day * days
    need = max(0, demand - stock)
    invest = need * cost
    revenue = demand * price
    profit = revenue - (demand * cost) - (demand * price * 0.17)
    return (
        f"📦 КАЛЬКУЛЯТОР ПОСТАВКИ\n{'─'*30}\n"
        f"📅 Период: {days} дней\n"
        f"📈 Продаж в день: {sales_day} шт\n"
        f"📦 Текущий остаток: {stock} шт\n\n"
        f"🎯 Прогноз спроса за период: {demand} шт\n"
        f"🚚 Нужно довезти: {need} шт\n"
        f"💰 Инвестиции в поставку: {invest:,.0f} ₽\n\n"
        f"📊 Прогноз выручки: {revenue:,.0f} ₽\n"
        f"💵 Прогноз прибыли: {profit:,.0f} ₽\n\n"
        f"✅ {'Поставка нужна — везите ' + str(need) + ' шт' if need > 0 else 'Остатка хватит на период — поставка не нужна'}"
    )

async def gen_promo_calc(price, discount_pct, cost):
    """Расчёт выгодности участия в акции маркетплейса"""
    sale_price = price * (1 - discount_pct/100)
    wb_comm = price * 0.17
    wb_comm_sale = sale_price * 0.17
    profit_normal = price - cost - wb_comm - max(50, price*0.05)
    profit_sale = sale_price - cost - wb_comm_sale - max(50, sale_price*0.05)
    margin_normal = profit_normal/price*100 if price>0 else 0
    margin_sale = profit_sale/sale_price*100 if sale_price>0 else 0

    verdict = ""
    if profit_sale > 0 and margin_sale >= 10:
        verdict = "✅ ВЫГОДНО участвовать — маржа сохраняется"
    elif profit_sale > 0 and margin_sale >= 5:
        verdict = "🟡 НЕЙТРАЛЬНО — маржа минимальная, но в плюсе"
    elif profit_sale > 0:
        verdict = "🔴 ОСТОРОЖНО — маржа очень низкая"
    else:
        verdict = "❌ НЕ УЧАСТВОВАТЬ — будет убыток"

    def fmt(v): return f"{v:,.0f}".replace(",", " ")
    return (
        f"🎯 РАСЧЁТ АКЦИИ МАРКЕТПЛЕЙСА\n{'─'*30}\n"
        f"💰 Обычная цена: {fmt(price)} ₽\n"
        f"🏷 Цена со скидкой {discount_pct}%: {fmt(sale_price)} ₽\n"
        f"📦 Себестоимость: {fmt(cost)} ₽\n\n"
        f"📊 БЕЗ АКЦИИ:\n"
        f"Прибыль с единицы: {fmt(profit_normal)} ₽ ({margin_normal:.1f}%)\n\n"
        f"📊 В АКЦИИ:\n"
        f"Прибыль с единицы: {fmt(profit_sale)} ₽ ({margin_sale:.1f}%)\n\n"
        f"📉 Потеря прибыли на единицу: {fmt(profit_normal - profit_sale)} ₽\n\n"
        f"{verdict}\n\n"
        f"💡 Совет: акция выгодна если у тебя высокий остаток на складе\n"
        f"или нужен буст позиции в выдаче."
    )

# ─── ОПЛАТА ─────────────────────────────────────────────────
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
                        set_sub(uid, "mp_sub", 30)
                        del_pending(pid)
                        sheets_update_sub(uid)
                        # Реферальный бонус пригласившему
                        ref_by, _, _ = get_ref_stats(uid)
                        if ref_by:
                            add_bonus_requests(ref_by, 5)
                            try:
                                await bot.send_message(ref_by,
                                    "🎉 Твой друг оформил подписку!\n\n"
                                    "💫 Тебе начислено +5 бесплатных запросов.",
                                    reply_markup=kb_main())
                            except: pass
                        await bot.send_message(uid,
                            "✅ Подписка активирована!\n\n"
                            "🚀 МаркетПРО — теперь все функции без ограничений на 30 дней.\n\n"
                            "Удачных продаж! 💪",
                            reply_markup=kb_main())
                    elif p.status == "canceled":
                        del_pending(pid)
                        await bot.send_message(uid, "❌ Платёж отменён.", reply_markup=kb_main())
                except Exception as e:
                    logging.error(f"payment check {pid}: {e}")
        except Exception as e:
            logging.error(f"payment loop: {e}")

# ─── ХЭНДЛЕРЫ ────────────────────────────────────────────────

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id
    username = message.from_user.username or ""
    first_name = message.from_user.first_name or ""
    ensure_user(uid, username, first_name)

    # Реферальная ссылка
    args = message.text.split()
    if len(args) > 1 and args[1].startswith("ref_"):
        try:
            ref_id = int(args[1].replace("ref_", ""))
            if ref_id != uid:
                set_ref_by(uid, ref_id)
                add_ref_count(ref_id)
                add_bonus_requests(uid, REF_BONUS_INVITED)
                add_bonus_requests(ref_id, REF_BONUS_INVITER)
                try:
                    await bot.send_message(ref_id,
                        f"🎉 По твоей ссылке зарегистрировался новый пользователь!\n\n"
                        f"💫 Тебе начислено +{REF_BONUS_INVITER} бесплатных запросов.")
                except: pass
        except: pass

    sheets_add_user(uid, username, first_name)
    remaining = get_remaining(uid)
    plan, sub_end = get_sub(uid)

    if plan:
        status = f"✅ Подписка активна до {sub_end.strftime('%d.%m.%Y')}"
    else:
        status = f"🆓 Бесплатных запросов: {remaining} из {FREE_REQUESTS + get_bonus_requests(uid)}"

    await message.answer(
        f"👋 Привет, {first_name}!\n\n"
        f"Я МаркетПРО — AI-помощник для продавцов на Wildberries, Ozon и Авито.\n\n"
        f"Что я умею:\n"
        f"📦 Создавать продающие карточки товаров\n"
        f"📊 Проводить аудит карточек по тексту или ссылке\n"
        f"🧮 Считать юнит-экономику и выгоду от акций\n"
        f"⭐️ Отвечать на отзывы покупателей\n"
        f"⚖️ Писать апелляции на штрафы и блокировки\n"
        f"🔑 Генерировать SEO-заголовки\n\n"
        f"{status}\n\n"
        f"Выбери что нужно 👇",
        reply_markup=kb_main()
    )

@dp.callback_query(F.data == "back_menu")
async def back_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    uid = call.from_user.id
    remaining = get_remaining(uid)
    plan, sub_end = get_sub(uid)
    if plan:
        status = f"✅ Подписка активна до {sub_end.strftime('%d.%m.%Y')}"
    else:
        status = f"🆓 Бесплатных запросов: {remaining}"
    await call.message.answer(
        f"Главное меню\n{status}",
        reply_markup=kb_main()
    )
    await call.answer()

# ─── КАРТОЧКА ТОВАРА ────────────────────────────────────────
@dp.callback_query(F.data == "sec_cards")
async def sec_cards(call: CallbackQuery):
    await call.message.answer(
        "📦 Карточка товара\n\n"
        "Создам продающее название, описание и SEO-ключи.\n"
        "Работает для Wildberries, Ozon и Авито.\n\n"
        "Выбери способ:",
        reply_markup=kb_cards()
    )
    await call.answer()

@dp.callback_query(F.data == "make_card_photo")
async def make_card_photo_start(call: CallbackQuery, state: FSMContext):
    if not await check_access(call.from_user.id, call): return
    await state.set_state(CardStates.choose_platform)
    await state.update_data(input_type="photo")
    await call.message.answer(
        "📸 Карточка из фото\n\n"
        "Сфотографируй товар или загрузи готовое фото.\n"
        "Я определю что это и создам карточку.\n\n"
        "Выбери платформу:",
        reply_markup=kb_platform()
    )
    await call.answer()

@dp.callback_query(F.data == "make_card_text")
async def make_card_text_start(call: CallbackQuery, state: FSMContext):
    if not await check_access(call.from_user.id, call): return
    await state.set_state(CardStates.choose_platform)
    await state.update_data(input_type="text")
    await call.message.answer(
        "✏️ Карточка из текста\n\n"
        "Опиши товар своими словами или голосом:\n"
        "— название\n"
        "— материал / состав\n"
        "— характеристики\n"
        "— для кого и зачем\n\n"
        "Выбери платформу:",
        reply_markup=kb_platform()
    )
    await call.answer()

@dp.callback_query(F.data.startswith("platform_"), CardStates.choose_platform)
async def platform_chosen(call: CallbackQuery, state: FSMContext):
    platform = call.data.replace("platform_", "")
    await state.update_data(platform=platform)
    data = await state.get_data()
    input_type = data.get("input_type", "text")
    plat_name = PLATFORM_NAMES.get(platform, platform)

    if input_type == "photo":
        await state.set_state(CardStates.waiting_photo)
        await call.message.answer(
            f"Платформа: {plat_name}\n\n"
            "📷 Отправь фото товара:",
            reply_markup=kb_back()
        )
    else:
        await state.set_state(CardStates.waiting_text)
        await call.message.answer(
            f"Платформа: {plat_name}\n\n"
            "✏️ Опиши товар (текстом или голосом):",
            reply_markup=kb_back()
        )
    await call.answer()

@dp.message(CardStates.waiting_photo, F.photo)
async def card_from_photo(message: Message, state: FSMContext):
    uid = message.from_user.id
    data = await state.get_data()
    platform = data.get("platform", "wb")
    await message.answer("⏳ Анализирую фото и создаю карточку...")
    try:
        photo = message.photo[-1]
        file = await bot.get_file(photo.file_id)
        image_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file.file_path}"
        result = await gen_card_photo(platform, image_url)
        inc_requests(uid)
        sheets_update_requests(uid)
        save_history(uid, "card_photo", result)
        remaining = get_remaining(uid)
        footer = f"\n\n💫 Осталось запросов: {remaining}" if not is_paid(uid) else ""
        await message.answer(result + footer, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Другой вариант", callback_data="make_card_photo")],
            [InlineKeyboardButton(text="🔙 В меню", callback_data="back_menu")],
        ]))
    except Exception as e:
        logging.error(f"card_photo error: {e}")
        await message.answer("❌ Ошибка. Попробуй ещё раз.", reply_markup=kb_back())
    await state.clear()

@dp.message(CardStates.waiting_text, F.text | F.voice | F.audio)
async def card_from_text(message: Message, state: FSMContext):
    uid = message.from_user.id
    text = message.text
    if message.voice or message.audio:
        await message.answer("🎙 Распознаю голос...")
        text = await transcribe(message)
        if not text:
            await message.answer("❌ Не удалось распознать.", reply_markup=kb_back()); return
    data = await state.get_data()
    platform = data.get("platform", "wb")
    await message.answer("⏳ Создаю карточку...")
    try:
        result = await gen_card_text(platform, text)
        inc_requests(uid)
        sheets_update_requests(uid)
        save_history(uid, "card_text", result)
        remaining = get_remaining(uid)
        footer = f"\n\n💫 Осталось запросов: {remaining}" if not is_paid(uid) else ""
        await message.answer(result + footer, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Другой вариант", callback_data="make_card_text")],
            [InlineKeyboardButton(text="🔙 В меню", callback_data="back_menu")],
        ]))
    except Exception as e:
        logging.error(f"card_text error: {e}")
        await message.answer("❌ Ошибка. Попробуй ещё раз.", reply_markup=kb_back())
    await state.clear()

# ─── АУДИТ И АНАЛИЗ ─────────────────────────────────────────
@dp.callback_query(F.data == "sec_analytics")
async def sec_analytics(call: CallbackQuery):
    await call.message.answer(
        "📊 Аудит и анализ\n\n"
        "Проверю карточку, найду слабые места и дам конкретные правки для роста продаж.\n\n"
        "Выбери действие:",
        reply_markup=kb_analytics()
    )
    await call.answer()

@dp.callback_query(F.data == "audit_text")
async def audit_text_start(call: CallbackQuery, state: FSMContext):
    if not await check_access(call.from_user.id, call): return
    await state.set_state(AuditStates.waiting)
    await call.message.answer(
        "🔍 Аудит карточки по тексту\n\n"
        "Вставь текст своей карточки (название + описание + характеристики).\n"
        "Я оценю её по 10-балльной шкале и дам конкретные правки.",
        reply_markup=kb_back()
    )
    await call.answer()

@dp.message(AuditStates.waiting, F.text | F.voice | F.audio)
async def audit_text_process(message: Message, state: FSMContext):
    uid = message.from_user.id
    text = message.text
    if message.voice or message.audio:
        await message.answer("🎙 Распознаю...")
        text = await transcribe(message)
        if not text:
            await message.answer("❌ Не удалось распознать.", reply_markup=kb_back()); return
    await message.answer("⏳ Анализирую карточку...")
    try:
        result = await gen_audit_text(text)
        inc_requests(uid)
        sheets_update_requests(uid)
        save_history(uid, "audit", result)
        remaining = get_remaining(uid)
        footer = f"\n\n💫 Осталось запросов: {remaining}" if not is_paid(uid) else ""
        await message.answer(result + footer, reply_markup=kb_back())
    except Exception as e:
        logging.error(f"audit error: {e}")
        await message.answer("❌ Ошибка. Попробуй ещё раз.", reply_markup=kb_back())
    await state.clear()

@dp.callback_query(F.data == "audit_link")
async def audit_link_start(call: CallbackQuery, state: FSMContext):
    if not await check_access(call.from_user.id, call): return
    await state.set_state(AuditLinkStates.waiting)
    await call.message.answer(
        "🔗 Аудит по ссылке\n\n"
        "Вставь ссылку на карточку товара с Wildberries или Ozon.\n\n"
        "Пример:\n"
        "https://www.wildberries.ru/catalog/123456/detail.aspx\n"
        "https://www.ozon.ru/product/123456/",
        reply_markup=kb_back()
    )
    await call.answer()

@dp.message(AuditLinkStates.waiting, F.text)
async def audit_link_process(message: Message, state: FSMContext):
    uid = message.from_user.id
    url = message.text.strip()
    if "wildberries.ru" not in url and "ozon.ru" not in url:
        await message.answer(
            "⚠️ Это не похоже на ссылку WB или Ozon.\n\nОтправь ссылку вида:\nhttps://www.wildberries.ru/catalog/...",
            reply_markup=kb_back()
        ); return
    await message.answer("⏳ Анализирую карточку по ссылке...")
    try:
        result = await gen_audit_link(url)
        inc_requests(uid)
        sheets_update_requests(uid)
        save_history(uid, "audit_link", result)
        remaining = get_remaining(uid)
        footer = f"\n\n💫 Осталось запросов: {remaining}" if not is_paid(uid) else ""
        await message.answer(result + footer, reply_markup=kb_back())
    except Exception as e:
        logging.error(f"audit_link error: {e}")
        await message.answer("❌ Ошибка. Попробуй ещё раз.", reply_markup=kb_back())
    await state.clear()

@dp.callback_query(F.data == "niche_analysis")
async def niche_start(call: CallbackQuery, state: FSMContext):
    if not await check_access(call.from_user.id, call): return
    await state.set_state(NicheStates.waiting)
    await call.message.answer(
        "🕵️ Анализ ниши\n\n"
        "Напиши название ниши или категории товара.\n\n"
        "Примеры: 'кофемолки', 'детские игрушки до 3 лет', 'спортивные носки'",
        reply_markup=kb_back()
    )
    await call.answer()

@dp.message(NicheStates.waiting, F.text | F.voice | F.audio)
async def niche_process(message: Message, state: FSMContext):
    uid = message.from_user.id
    text = message.text
    if message.voice or message.audio:
        await message.answer("🎙 Распознаю...")
        text = await transcribe(message)
        if not text:
            await message.answer("❌ Не удалось распознать.", reply_markup=kb_back()); return
    await message.answer("⏳ Анализирую нишу...")
    try:
        result = await gen_niche(text)
        inc_requests(uid)
        sheets_update_requests(uid)
        save_history(uid, "niche", result)
        remaining = get_remaining(uid)
        footer = f"\n\n💫 Осталось запросов: {remaining}" if not is_paid(uid) else ""
        await message.answer(result + footer, reply_markup=kb_back())
    except Exception as e:
        logging.error(f"niche error: {e}")
        await message.answer("❌ Ошибка. Попробуй ещё раз.", reply_markup=kb_back())
    await state.clear()

@dp.callback_query(F.data == "competitor_link")
async def competitor_link_start(call: CallbackQuery, state: FSMContext):
    if not await check_access(call.from_user.id, call): return
    await state.set_state(CompetitorLinkStates.waiting)
    await call.message.answer(
        "🏆 Анализ конкурента по ссылке\n\n"
        "Вставь ссылку на карточку конкурента с WB или Ozon.\n"
        "Я найду его сильные и слабые стороны и подскажу как сделать лучше.",
        reply_markup=kb_back()
    )
    await call.answer()

@dp.message(CompetitorLinkStates.waiting, F.text)
async def competitor_link_process(message: Message, state: FSMContext):
    uid = message.from_user.id
    url = message.text.strip()
    await message.answer("⏳ Анализирую конкурента...")
    try:
        result = await gen_competitor_link(url)
        inc_requests(uid)
        sheets_update_requests(uid)
        save_history(uid, "competitor", result)
        remaining = get_remaining(uid)
        footer = f"\n\n💫 Осталось запросов: {remaining}" if not is_paid(uid) else ""
        await message.answer(result + footer, reply_markup=kb_back())
    except Exception as e:
        logging.error(f"competitor error: {e}")
        await message.answer("❌ Ошибка. Попробуй ещё раз.", reply_markup=kb_back())
    await state.clear()

# ─── ФИНАНСЫ ─────────────────────────────────────────────────
@dp.callback_query(F.data == "sec_finance")
async def sec_finance(call: CallbackQuery):
    await call.message.answer(
        "🧮 Финансы и расчёты\n\n"
        "Помогу посчитать экономику, оценить поставку и решить участвовать ли в акции.\n\n"
        "Выбери инструмент:",
        reply_markup=kb_finance()
    )
    await call.answer()

@dp.callback_query(F.data == "unit_eco")
async def unit_eco_start(call: CallbackQuery, state: FSMContext):
    if not await check_access(call.from_user.id, call): return
    await state.set_state(UnitStates.waiting_cost)
    await call.message.answer(
        "🧮 Юнит-экономика\n\n"
        "Рассчитаю прибыль на Wildberries и Ozon.\n\n"
        "Шаг 1/4. Введи себестоимость товара (в рублях):",
        reply_markup=kb_back()
    )
    await call.answer()

@dp.message(UnitStates.waiting_cost, F.text)
async def unit_cost(message: Message, state: FSMContext):
    try:
        cost = float(message.text.replace(",",".").replace(" ",""))
        await state.update_data(cost=cost)
        await state.set_state(UnitStates.waiting_price)
        await message.answer("Шаг 2/4. Введи цену продажи (в рублях):", reply_markup=kb_back())
    except: await message.answer("Введи число, например: 350")

@dp.message(UnitStates.waiting_price, F.text)
async def unit_price(message: Message, state: FSMContext):
    try:
        price = float(message.text.replace(",",".").replace(" ",""))
        await state.update_data(price=price)
        await state.set_state(UnitStates.waiting_category)
        await message.answer(
            "Шаг 3/4. Введи категорию товара:\n\n"
            "одежда, обувь, электроника, косметика,\n"
            "товары для дома, игрушки, спорт, продукты",
            reply_markup=kb_back()
        )
    except: await message.answer("Введи число, например: 1200")

@dp.message(UnitStates.waiting_category, F.text)
async def unit_category(message: Message, state: FSMContext):
    await state.update_data(category=message.text.strip())
    await state.set_state(UnitStates.waiting_scheme)
    await message.answer("Шаг 4/4. Выбери схему работы:", reply_markup=kb_scheme())

@dp.callback_query(F.data.startswith("scheme_"), UnitStates.waiting_scheme)
async def unit_scheme(call: CallbackQuery, state: FSMContext):
    scheme = call.data.replace("scheme_", "")
    await state.update_data(scheme=scheme)
    data = await state.get_data()
    uid = call.from_user.id
    await call.message.answer("⏳ Считаю...")
    try:
        result = await gen_unit_eco(data["cost"], data["price"], data["category"], scheme)
        inc_requests(uid)
        sheets_update_requests(uid)
        save_history(uid, "unit_eco", result)
        remaining = get_remaining(uid)
        footer = f"\n\n💫 Осталось запросов: {remaining}" if not is_paid(uid) else ""
        await call.message.answer(result + footer, reply_markup=kb_back())
    except Exception as e:
        logging.error(f"unit_eco error: {e}")
        await call.message.answer("❌ Ошибка расчёта.", reply_markup=kb_back())
    await state.clear()
    await call.answer()

@dp.callback_query(F.data == "supply_calc")
async def supply_start(call: CallbackQuery, state: FSMContext):
    if not await check_access(call.from_user.id, call): return
    await state.set_state(SupplyStates.waiting_cost)
    await call.message.answer(
        "📦 Калькулятор поставки\n\n"
        "Рассчитаю сколько товара нужно везти и во что это обойдётся.\n\n"
        "Шаг 1/5. Себестоимость единицы товара (₽):",
        reply_markup=kb_back()
    )
    await call.answer()

@dp.message(SupplyStates.waiting_cost, F.text)
async def supply_cost(message: Message, state: FSMContext):
    try:
        v = float(message.text.replace(",",".").replace(" ",""))
        await state.update_data(cost=v)
        await state.set_state(SupplyStates.waiting_price)
        await message.answer("Шаг 2/5. Цена продажи (₽):", reply_markup=kb_back())
    except: await message.answer("Введи число")

@dp.message(SupplyStates.waiting_price, F.text)
async def supply_price(message: Message, state: FSMContext):
    try:
        v = float(message.text.replace(",",".").replace(" ",""))
        await state.update_data(price=v)
        await state.set_state(SupplyStates.waiting_sales)
        await message.answer("Шаг 3/5. Сколько продаж в день (штук):", reply_markup=kb_back())
    except: await message.answer("Введи число")

@dp.message(SupplyStates.waiting_sales, F.text)
async def supply_sales(message: Message, state: FSMContext):
    try:
        v = float(message.text.replace(",",".").replace(" ",""))
        await state.update_data(sales_day=v)
        await state.set_state(SupplyStates.waiting_stock)
        await message.answer("Шаг 4/5. Текущий остаток на складе (штук):", reply_markup=kb_back())
    except: await message.answer("Введи число")

@dp.message(SupplyStates.waiting_stock, F.text)
async def supply_stock(message: Message, state: FSMContext):
    try:
        v = float(message.text.replace(",",".").replace(" ",""))
        await state.update_data(stock=v)
        await state.set_state(SupplyStates.waiting_days)
        await message.answer("Шаг 5/5. На сколько дней планируешь поставку:", reply_markup=kb_back())
    except: await message.answer("Введи число")

@dp.message(SupplyStates.waiting_days, F.text)
async def supply_days(message: Message, state: FSMContext):
    uid = message.from_user.id
    try:
        v = int(message.text.replace(" ",""))
        data = await state.get_data()
        result = await gen_supply(data["cost"], data["price"], data["sales_day"], data["stock"], v)
        inc_requests(uid)
        sheets_update_requests(uid)
        save_history(uid, "supply", result)
        remaining = get_remaining(uid)
        footer = f"\n\n💫 Осталось запросов: {remaining}" if not is_paid(uid) else ""
        await message.answer(result + footer, reply_markup=kb_back())
        await state.clear()
    except: await message.answer("Введи число дней")

@dp.callback_query(F.data == "promo_calc")
async def promo_start(call: CallbackQuery, state: FSMContext):
    if not await check_access(call.from_user.id, call): return
    await state.set_state(PromoStates.waiting_price)
    await call.message.answer(
        "🎯 Расчёт акции маркетплейса\n\n"
        "Помогу понять выгодно ли участвовать в акции WB или Ozon.\n\n"
        "Шаг 1/3. Текущая цена товара (₽):",
        reply_markup=kb_back()
    )
    await call.answer()

@dp.message(PromoStates.waiting_price, F.text)
async def promo_price(message: Message, state: FSMContext):
    try:
        v = float(message.text.replace(",",".").replace(" ",""))
        await state.update_data(price=v)
        await state.set_state(PromoStates.waiting_discount)
        await message.answer("Шаг 2/3. Размер скидки по акции (%):", reply_markup=kb_back())
    except: await message.answer("Введи число")

@dp.message(PromoStates.waiting_discount, F.text)
async def promo_discount(message: Message, state: FSMContext):
    try:
        v = float(message.text.replace("%","").replace(",",".").replace(" ",""))
        await state.update_data(discount=v)
        await state.set_state(PromoStates.waiting_cost)
        await message.answer("Шаг 3/3. Себестоимость товара (₽):", reply_markup=kb_back())
    except: await message.answer("Введи число")

@dp.message(PromoStates.waiting_cost, F.text)
async def promo_cost(message: Message, state: FSMContext):
    uid = message.from_user.id
    try:
        v = float(message.text.replace(",",".").replace(" ",""))
        data = await state.get_data()
        result = await gen_promo_calc(data["price"], data["discount"], v)
        inc_requests(uid)
        sheets_update_requests(uid)
        save_history(uid, "promo", result)
        remaining = get_remaining(uid)
        footer = f"\n\n💫 Осталось запросов: {remaining}" if not is_paid(uid) else ""
        await message.answer(result + footer, reply_markup=kb_back())
        await state.clear()
    except: await message.answer("Введи число")

@dp.callback_query(F.data == "ad_text")
async def ad_start(call: CallbackQuery, state: FSMContext):
    if not await check_access(call.from_user.id, call): return
    await state.set_state(AdStates.waiting)
    await call.message.answer(
        "📣 Рекламный текст\n\n"
        "Напиши описание товара, я создам продающий рекламный текст.\n"
        "Формат: заголовок + текст + слоган.",
        reply_markup=kb_back()
    )
    await call.answer()

@dp.message(AdStates.waiting, F.text | F.voice | F.audio)
async def ad_process(message: Message, state: FSMContext):
    uid = message.from_user.id
    text = message.text
    if message.voice or message.audio:
        await message.answer("🎙 Распознаю...")
        text = await transcribe(message)
        if not text:
            await message.answer("❌ Не удалось распознать.", reply_markup=kb_back()); return
    await message.answer("⏳ Создаю рекламный текст...")
    try:
        result = await gen_ad(text)
        inc_requests(uid)
        sheets_update_requests(uid)
        save_history(uid, "ad", result)
        remaining = get_remaining(uid)
        footer = f"\n\n💫 Осталось запросов: {remaining}" if not is_paid(uid) else ""
        await message.answer(result + footer, reply_markup=kb_back())
    except Exception as e:
        logging.error(f"ad error: {e}")
        await message.answer("❌ Ошибка.", reply_markup=kb_back())
    await state.clear()

@dp.callback_query(F.data == "seo_titles")
async def seo_start(call: CallbackQuery, state: FSMContext):
    if not await check_access(call.from_user.id, call): return
    await state.set_state(SeoStates.waiting)
    await call.message.answer(
        "🔑 SEO-заголовки\n\n"
        "Напиши название товара — я создам 5 вариантов SEO-заголовков с ключами.\n\n"
        "Пример: 'кофемолка электрическая'\nили: 'носки мужские хлопок'",
        reply_markup=kb_back()
    )
    await call.answer()

@dp.message(SeoStates.waiting, F.text | F.voice | F.audio)
async def seo_process(message: Message, state: FSMContext):
    uid = message.from_user.id
    text = message.text
    if message.voice or message.audio:
        await message.answer("🎙 Распознаю...")
        text = await transcribe(message)
        if not text:
            await message.answer("❌ Не удалось распознать.", reply_markup=kb_back()); return
    await message.answer("⏳ Генерирую SEO-заголовки...")
    try:
        result = await gen_seo_titles(text)
        inc_requests(uid)
        sheets_update_requests(uid)
        save_history(uid, "seo", result)
        remaining = get_remaining(uid)
        footer = f"\n\n💫 Осталось запросов: {remaining}" if not is_paid(uid) else ""
        await message.answer(
            f"🔑 SEO-заголовки для: {text}\n\n{result}" + footer,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Ещё варианты", callback_data="seo_titles")],
                [InlineKeyboardButton(text="🔙 В меню", callback_data="back_menu")],
            ])
        )
    except Exception as e:
        logging.error(f"seo error: {e}")
        await message.answer("❌ Ошибка.", reply_markup=kb_back())
    await state.clear()

# ─── ОТЗЫВЫ ─────────────────────────────────────────────────
@dp.callback_query(F.data == "review_reply")
async def review_start(call: CallbackQuery, state: FSMContext):
    if not await check_access(call.from_user.id, call): return
    await state.set_state(ReviewStates.waiting)
    await call.message.answer(
        "⭐️ Ответ на отзыв\n\n"
        "Вставь текст отзыва покупателя — я напишу профессиональный ответ.\n\n"
        "Работает для негативных и позитивных отзывов.",
        reply_markup=kb_back()
    )
    await call.answer()

@dp.message(ReviewStates.waiting, F.text | F.voice | F.audio)
async def review_process(message: Message, state: FSMContext):
    uid = message.from_user.id
    text = message.text
    if message.voice or message.audio:
        await message.answer("🎙 Распознаю...")
        text = await transcribe(message)
        if not text:
            await message.answer("❌ Не удалось распознать.", reply_markup=kb_back()); return
    await message.answer("⏳ Составляю ответ...")
    try:
        result = await gen_review(text)
        inc_requests(uid)
        sheets_update_requests(uid)
        save_history(uid, "review", result)
        remaining = get_remaining(uid)
        footer = f"\n\n💫 Осталось запросов: {remaining}" if not is_paid(uid) else ""
        await message.answer(
            f"⭐️ Ответ на отзыв:\n\n{result}" + footer,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Другой вариант", callback_data="review_reply")],
                [InlineKeyboardButton(text="🔙 В меню", callback_data="back_menu")],
            ])
        )
    except Exception as e:
        logging.error(f"review error: {e}")
        await message.answer("❌ Ошибка.", reply_markup=kb_back())
    await state.clear()

# ─── АПЕЛЛЯЦИИ ──────────────────────────────────────────────
@dp.callback_query(F.data == "appeal")
async def appeal_start(call: CallbackQuery, state: FSMContext):
    if not await check_access(call.from_user.id, call): return
    await state.set_state(AppealStates.choose_type)
    await call.message.answer(
        "⚖️ Апелляция к маркетплейсу\n\n"
        "Напишу официальную апелляцию на штраф, блокировку или несправедливый отзыв.\n\n"
        "Выбери тип ситуации:",
        reply_markup=kb_appeal_types()
    )
    await call.answer()

@dp.callback_query(F.data.startswith("appeal_"), AppealStates.choose_type)
async def appeal_type_chosen(call: CallbackQuery, state: FSMContext):
    appeal_type = call.data.replace("appeal_", "")
    await state.update_data(appeal_type=appeal_type)
    await state.set_state(AppealStates.waiting)
    types_text = {
        "fine":   "штраф от маркетплейса",
        "block":  "блокировка карточки",
        "supply": "претензия по поставке",
        "review": "несправедливый отзыв",
    }
    await call.message.answer(
        f"Тип: {types_text.get(appeal_type, 'апелляция')}\n\n"
        "Опиши ситуацию подробно:\n"
        "— что произошло\n"
        "— когда\n"
        "— какая сумма/какой товар\n"
        "— что ты считаешь несправедливым",
        reply_markup=kb_back()
    )
    await call.answer()

@dp.message(AppealStates.waiting, F.text | F.voice | F.audio)
async def appeal_process(message: Message, state: FSMContext):
    uid = message.from_user.id
    text = message.text
    if message.voice or message.audio:
        await message.answer("🎙 Распознаю...")
        text = await transcribe(message)
        if not text:
            await message.answer("❌ Не удалось распознать.", reply_markup=kb_back()); return
    data = await state.get_data()
    appeal_type = data.get("appeal_type", "fine")
    await message.answer("⏳ Составляю апелляцию...")
    try:
        result = await gen_appeal(appeal_type, text)
        inc_requests(uid)
        sheets_update_requests(uid)
        save_history(uid, "appeal", result)
        remaining = get_remaining(uid)
        footer = f"\n\n💫 Осталось запросов: {remaining}" if not is_paid(uid) else ""
        await message.answer(result + footer, reply_markup=kb_back())
    except Exception as e:
        logging.error(f"appeal error: {e}")
        await message.answer("❌ Ошибка.", reply_markup=kb_back())
    await state.clear()

# ─── ПРОФИЛЬ ─────────────────────────────────────────────────
@dp.callback_query(F.data == "my_profile")
async def my_profile(call: CallbackQuery):
    uid = call.from_user.id
    plan, sub_end = get_sub(uid)
    used = get_requests(uid)
    bonus = get_bonus_requests(uid)
    _, ref_count, _ = get_ref_stats(uid)

    if plan:
        sub_status = f"✅ Подписка активна до {sub_end.strftime('%d.%m.%Y')}"
    else:
        remaining = get_remaining(uid)
        sub_status = f"🆓 Бесплатный план\nОсталось запросов: {remaining} из {FREE_REQUESTS + bonus}"

    await call.message.answer(
        f"👤 Мой профиль\n\n"
        f"{sub_status}\n\n"
        f"📊 Всего запросов сделано: {used}\n"
        f"👥 Приглашено друзей: {ref_count}\n"
        f"🎁 Бонусных запросов: {bonus}\n",
        reply_markup=kb_profile(uid)
    )
    await call.answer()

@dp.callback_query(F.data == "my_history")
async def my_history(call: CallbackQuery):
    uid = call.from_user.id
    history = get_history(uid, 5)
    if not history:
        await call.message.answer("📂 История пуста — сделай первый запрос!", reply_markup=kb_back())
        await call.answer(); return

    names = {
        "card_photo": "📸 Карточка из фото",
        "card_text":  "✏️ Карточка из текста",
        "audit":      "🔍 Аудит карточки",
        "audit_link": "🔗 Аудит по ссылке",
        "niche":      "🕵️ Анализ ниши",
        "competitor": "🏆 Анализ конкурента",
        "review":     "⭐️ Ответ на отзыв",
        "appeal":     "⚖️ Апелляция",
        "unit_eco":   "🧮 Юнит-экономика",
        "supply":     "📦 Поставка",
        "promo":      "🎯 Расчёт акции",
        "ad":         "📣 Рекламный текст",
        "seo":        "🔑 SEO-заголовки",
    }

    text = "📂 Последние 5 запросов:\n\n"
    for feature, result, created_at in history:
        dt = datetime.fromisoformat(created_at).strftime("%d.%m %H:%M")
        fname = names.get(feature, feature)
        text += f"• {fname} — {dt}\n"

    await call.message.answer(text, reply_markup=kb_back())
    await call.answer()

@dp.callback_query(F.data == "my_referral")
async def my_referral(call: CallbackQuery):
    uid = call.from_user.id
    _, ref_count, bonus = get_ref_stats(uid)
    ref_link = f"https://t.me/PostGeniusHelperBot?start=ref_{uid}"
    await call.message.answer(
        "👥 Реферальная программа\n\n"
        f"Твоя ссылка:\n{ref_link}\n\n"
        f"За каждого приглашённого:\n"
        f"• Тебе: +{REF_BONUS_INVITER} бесплатных запросов\n"
        f"• Другу: +{REF_BONUS_INVITED} бесплатных запросов\n"
        f"• Если друг купит подписку: ещё +5 запросов тебе\n\n"
        f"📊 Твоя статистика:\n"
        f"👤 Приглашено: {ref_count} чел.\n"
        f"🎁 Бонусных запросов получено: {bonus}\n\n"
        "Поделись ссылкой с коллегами-продавцами!",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="📤 Поделиться ссылкой",
                switch_inline_query=f"Попробуй МаркетПРО — AI-помощник для WB и Ozon! {ref_link}"
            )],
            [InlineKeyboardButton(text="🔙 В меню", callback_data="back_menu")],
        ])
    )
    await call.answer()

# ─── ОПЛАТА ─────────────────────────────────────────────────
@dp.callback_query(F.data == "pay_sub")
async def pay_sub(call: CallbackQuery):
    uid = call.from_user.id
    await call.answer()
    try:
        p = await create_payment(uid, SUB_PRICE, "mp_sub", "Подписка МаркетПРО на 30 дней")
        save_pending(p.id, uid, "mp_sub", SUB_PRICE)
        await call.message.answer(
            f"💳 Оплата подписки — {SUB_PRICE} ₽\n\n"
            "✅ Безлимитные запросы на 30 дней\n"
            "✅ Все функции без ограничений\n\n"
            "После оплаты подписка активируется автоматически.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"💳 Оплатить {SUB_PRICE} ₽", url=p.confirmation.confirmation_url)],
                [InlineKeyboardButton(text="🔙 В меню", callback_data="back_menu")],
            ])
        )
    except Exception as e:
        logging.error(f"payment error: {e}")
        await call.message.answer(f"❌ Ошибка оплаты. Напиши в поддержку: {SUPPORT_URL}")

# ─── ADMIN ───────────────────────────────────────────────────
@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    if message.from_user.id != OWNER_ID: return
    total, subs, reqs = get_stats()
    await message.answer(
        f"📊 МаркетПРО статистика\n\n"
        f"👤 Пользователей: {total}\n"
        f"💳 Подписок: {subs}\n"
        f"⚡️ Всего запросов: {reqs}\n"
        f"💰 Доход/мес: {subs * SUB_PRICE} ₽"
    )

@dp.message(Command("give"))
async def cmd_give(message: Message):
    if message.from_user.id != OWNER_ID: return
    parts = message.text.split()
    if len(parts) < 3:
        await message.answer("Формат: /give USER_ID sub\nили: /give USER_ID bonus N")
        return
    uid = int(parts[1])
    if parts[2] == "sub":
        set_sub(uid, "mp_sub", 30)
        await message.answer(f"✅ Подписка выдана пользователю {uid}")
    elif parts[2] == "bonus":
        n = int(parts[3]) if len(parts) > 3 else 10
        add_bonus_requests(uid, n)
        await message.answer(f"✅ +{n} бонусных запросов пользователю {uid}")

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

# ─── FALLBACK ────────────────────────────────────────────────
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
            "📷 Хочешь создать карточку из этого фото?\n\n"
            "Нажми 📦 Карточка товара → 📸 Из фото товара",
            reply_markup=kb_main()
        )

@dp.message(F.voice | F.audio)
async def fallback_voice(message: Message, state: FSMContext):
    current = await state.get_state()
    if current is None:
        await message.answer(
            "🎙 Голосовые работают внутри функций!\n\n"
            "Например: нажми 📦 Карточка товара → ✏️ Из текста и отправь голосовое.",
            reply_markup=kb_main()
        )

# ─── ЗАПУСК ──────────────────────────────────────────────────
async def main():
    init_db()
    asyncio.create_task(check_payments_loop())
    logging.info("МаркетПРО запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
