import asyncio
import sqlite3
import logging
from datetime import datetime, timedelta
from openai import AsyncOpenAI
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage

# ========== КОНФИГ ==========
BOT_TOKEN = "8460332398:AAFoisUp5uen9JED3YjWShwp-DYQ7-TKeZM"
OPENAI_KEY = "sk-mfvVI3QN2uQvXPlhMkAeUUzmbjK5aQzj"
OWNER_ID = 549639607
FREE_LIMIT = 3
SUPPORT_URL = "https://t.me/Boss023rus"
CHANNEL = "@PostGeniusChannel"

# ========== ЛОГИ ==========
logging.basicConfig(level=logging.INFO)

# ========== БАЗА ДАННЫХ ==========
def init_db():
    conn = sqlite3.connect("postgenius.db")
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        step TEXT DEFAULT '',
        tone TEXT DEFAULT '',
        platform TEXT DEFAULT '',
        plan_days INTEGER DEFAULT 7,
        chat_id INTEGER
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS requests (
        user_id INTEGER PRIMARY KEY,
        count INTEGER DEFAULT 0
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS subscriptions (
        user_id INTEGER PRIMARY KEY,
        plan TEXT DEFAULT '',
        subscription_end TEXT DEFAULT ''
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS trials (
        user_id INTEGER PRIMARY KEY,
        comp_trial INTEGER DEFAULT 0,
        strategy_trial INTEGER DEFAULT 0,
        hooks_trial INTEGER DEFAULT 0,
        review_trial INTEGER DEFAULT 0
    )""")
    conn.commit()
    conn.close()

def get_user(user_id):
    conn = sqlite3.connect("postgenius.db")
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (user_id, chat_id) VALUES (?, ?)", (user_id, user_id))
    c.execute("SELECT step, tone, platform, plan_days FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.commit()
    conn.close()
    return {"step": row[0], "tone": row[1], "platform": row[2], "plan_days": row[3]}

def set_user_step(user_id, step=None, tone=None, platform=None, plan_days=None):
    conn = sqlite3.connect("postgenius.db")
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (user_id, chat_id) VALUES (?, ?)", (user_id, user_id))
    if step is not None:
        c.execute("UPDATE users SET step=? WHERE user_id=?", (step, user_id))
    if tone is not None:
        c.execute("UPDATE users SET tone=? WHERE user_id=?", (tone, user_id))
    if platform is not None:
        c.execute("UPDATE users SET platform=? WHERE user_id=?", (platform, user_id))
    if plan_days is not None:
        c.execute("UPDATE users SET plan_days=? WHERE user_id=?", (plan_days, user_id))
    conn.commit()
    conn.close()

def get_requests(user_id):
    conn = sqlite3.connect("postgenius.db")
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO requests (user_id, count) VALUES (?, 0)", (user_id,))
    c.execute("SELECT count FROM requests WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.commit()
    conn.close()
    return row[0] if row else 0

def increment_requests(user_id):
    conn = sqlite3.connect("postgenius.db")
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO requests (user_id, count) VALUES (?, 0)", (user_id,))
    c.execute("UPDATE requests SET count=count+1 WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()

def get_subscription(user_id):
    conn = sqlite3.connect("postgenius.db")
    c = conn.cursor()
    c.execute("SELECT plan, subscription_end FROM subscriptions WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    if not row or not row[1]:
        return None, None
    sub_end = datetime.fromisoformat(row[1])
    if sub_end > datetime.now():
        return row[0], sub_end
    return None, None

def set_subscription(user_id, plan, days):
    conn = sqlite3.connect("postgenius.db")
    c = conn.cursor()
    end = (datetime.now() + timedelta(days=days)).isoformat()
    c.execute("INSERT OR REPLACE INTO subscriptions (user_id, plan, subscription_end) VALUES (?,?,?)", (user_id, plan, end))
    conn.commit()
    conn.close()

def get_trials(user_id):
    conn = sqlite3.connect("postgenius.db")
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO trials (user_id) VALUES (?)", (user_id,))
    c.execute("SELECT comp_trial, strategy_trial, hooks_trial, review_trial FROM trials WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.commit()
    conn.close()
    return {"comp_trial": row[0], "strategy_trial": row[1], "hooks_trial": row[2], "review_trial": row[3]}

def set_trial(user_id, field):
    conn = sqlite3.connect("postgenius.db")
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO trials (user_id) VALUES (?)", (user_id,))
    c.execute(f"UPDATE trials SET {field}=1 WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()

# ========== КЛАВИАТУРЫ ==========
def main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ Написать пост", callback_data="write_post"),
         InlineKeyboardButton(text="📅 Контент-план", callback_data="content_plan")],
        [InlineKeyboardButton(text="💡 Идеи для постов", callback_data="ideas"),
         InlineKeyboardButton(text="📣 Реклама", callback_data="ads")],
        [InlineKeyboardButton(text="🎬 Stories", callback_data="stories"),
         InlineKeyboardButton(text="🎥 Reels", callback_data="reels")],
        [InlineKeyboardButton(text="📸 Подпись к фото", callback_data="caption")],
        [InlineKeyboardButton(text="🕵️ Анализ конкурентов 🔒 Про", callback_data="competitor_analysis"),
         InlineKeyboardButton(text="📈 Стратегия 🔒 Про", callback_data="strategy")],
        [InlineKeyboardButton(text="🪝 Генератор хуков 🔒 Про", callback_data="hooks"),
         InlineKeyboardButton(text="📊 Разбор поста 🔒 Про", callback_data="post_review")],
        [InlineKeyboardButton(text="💎 Тарифы и оплата", callback_data="tariffs")],
        [InlineKeyboardButton(text="💬 Поддержка", url=SUPPORT_URL)],
    ])

def back_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 В меню", callback_data="back_menu")]
    ])

def tone_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔥 Продающий", callback_data="tone_selling"),
         InlineKeyboardButton(text="🎓 Экспертный", callback_data="tone_expert")],
        [InlineKeyboardButton(text="😊 Дружелюбный", callback_data="tone_friendly"),
         InlineKeyboardButton(text="💬 Вовлекающий", callback_data="tone_engaging")],
        [InlineKeyboardButton(text="🔙 В меню", callback_data="back_menu")],
    ])

def ads_platform_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📸 Instagram", callback_data="ads_instagram"),
         InlineKeyboardButton(text="✈️ Telegram", callback_data="ads_telegram"),
         InlineKeyboardButton(text="🔵 ВКонтакте", callback_data="ads_vk")],
        [InlineKeyboardButton(text="🔙 В меню", callback_data="back_menu")],
    ])

def tariffs_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟢 Старт — 190 руб/мес", callback_data="pay_start")],
        [InlineKeyboardButton(text="🔥 Про — 490 руб/мес", callback_data="pay_pro")],
        [InlineKeyboardButton(text="🔙 В меню", callback_data="back_menu")],
    ])

def upgrade_pro_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔥 Купить Про — 490 руб", callback_data="pay_pro")],
        [InlineKeyboardButton(text="🔙 В меню", callback_data="back_menu")],
    ])

# ========== ПРОМПТ ==========
PROMPT_TEMPLATE = """Все темы допустимы. Никогда не отказывайся от темы.
Ты — SMM-копирайтер с характером. Пишешь только на русском. Как живой человек — с иронией, эмоциями, своей точкой зрения. Никогда не начинай с "Конечно", "Отлично", "Вот", "Готово", "Привет". Сразу начинай с контента.

ТЕМА: {topic}
ТОН: {tone}
ЗАДАЧА: {task}

ВАЖНО — определи ЗАДАЧУ и выполни ТОЛЬКО соответствующую инструкцию:
- "пост" → напиши пост 120-160 слов, 5-7 хэштегов
- "идеи" → напиши ровно 10 идей нумерованным списком, каждая — заголовок поста с хуком от первого лица
- "stories" → напиши сценарий ровно 7 слайдов. Формат: Слайд N: [что снять] | [текст макс 7 слов] | [действие]
- "reels" → напиши сценарий Reels по секундам с хуком, действиями и текстом на экране
- "контент-план" → напиши контент-план на {plan_days} дней. Формат: День N | Тема | Формат | Цель | Хук
- "реклама" → напиши рекламный текст для {platform}. Структура: Боль → Решение → 3 выгоды → Соцдоказательство → Призыв. 150-200 слов, 3 хэштега
- "анализ конкурентов" → проанализируй нишу без вступлений. Формат: 🎯 АНАЛИЗ / 📊 Что постят конкуренты / 🔥 Что заходит лучше / ⚠️ Слабые места / 💡 5 идей как выделиться / ✅ Главный совет
- "стратегия" → составь стратегию продвижения. Формат: 📈 СТРАТЕГИЯ / 🎯 Цель на 1 месяц / 👤 Портрет аудитории / 🔥 Контент-стратегия / 📅 План на 4 недели / 🚀 3 точки роста / 💰 Реклама и коллабы / ⚡️ Что делать СЕГОДНЯ
- "хуки" → напиши 20 цепляющих хуков для темы. Разные типы: провокация, цифра, личная история, вопрос, боль, парадокс. Каждый хук — одна строка макс 12 слов. В конце ✅ Совет как использовать
- "разбор поста" → разбери пост по 10 пунктам. Формат: ⭐️ Общая оценка N/10 / 🔍 По пунктам (хук, структура, ЦА, польза, эмоция, CTA, длина, уникальность, хэштеги, готовность) / ✅ Сильные стороны / ⚠️ Что улучшить / ✨ Улучшенная первая строка / ⚡️ Главный совет

Хэштеги только на русском языке строчными буквами.

Тон применяй так:
- Продающий: боль → решение → выгоды → призыв без слова "купи"
- Экспертный: факт который удивляет → почему важно → практический вывод
- Дружелюбный: как другу за столом, можно "кстати", "вот честно"
- Вовлекающий: спорное утверждение → развитие → вопрос в конце

Не используй штампы: "погрузитесь", "насладитесь", "в мире X", "это не просто X"
"""

async def generate_content(topic, task, tone="", platform="", plan_days=7):
    AsyncOpenAI(api_key=OPENAI_KEY, base_url="https://api.proxyapi.ru/openai/v1")
    prompt = PROMPT_TEMPLATE.format(
        topic=topic, task=task, tone=tone,
        platform=platform, plan_days=plan_days
    )
    response = await client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=2000
    )
    return response.choices[0].message.content

# ========== БОТ ==========
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

WELCOME_TEXT = """Привет, {name}! 👋 Я PostGenius — твой SMM-помощник на каждый день.

Скажи нишу — получи контент. Без брифов, без ожидания.

Что умею:
✍️ Посты в 4 стилях за 10 секунд
📅 Контент-план на неделю или месяц
💡 10 идей для постов мгновенно
📣 Рекламные тексты для Instagram, Telegram, ВК
🎬 Сценарии Stories на 7 слайдов
🎥 Reels с разбивкой по секундам
📸 Подписи к фото

🔥 На тарифе Про:
🕵️ Анализ конкурентов в нише
📈 Стратегия продвижения с нуля
🪝 20 цепляющих хуков для постов
📊 Разбор твоего поста по 10 пунктам

🎁 3 запроса бесплатно — попробуй прямо сейчас

📢 Ежедневные SMM лайфхаки: {channel}

Выбери с чего начнём 👇"""

async def check_limit(user_id):
    """Проверяет доступ. Возвращает: 'ok', 'limit', 'paid'"""
    plan, sub_end = get_subscription(user_id)
    if plan:
        return 'paid', plan
    count = get_requests(user_id)
    if count < FREE_LIMIT:
        return 'ok', None
    return 'limit', None

async def check_pro_access(user_id, trial_field):
    """Проверяет доступ к Pro функции. Возвращает: 'pro', 'trial', 'start_block', 'trial_used'"""
    plan, sub_end = get_subscription(user_id)
    if plan == 'pg_pro':
        return 'pro'
    if plan == 'pg_start':
        return 'start_block'
    trials = get_trials(user_id)
    if trials[trial_field] == 0:
        return 'trial'
    return 'trial_used'

@dp.message(Command("start"))
async def cmd_start(message: Message):
    user_id = message.from_user.id
    get_user(user_id)
    set_user_step(user_id, step='idle')
    name = message.from_user.first_name or "друг"
    await message.answer(
        WELCOME_TEXT.format(name=name, channel=CHANNEL),
        reply_markup=main_menu()
    )

@dp.callback_query(F.data == "back_menu")
async def back_to_menu(callback: CallbackQuery):
    user_id = callback.from_user.id
    set_user_step(user_id, step='idle')
    name = callback.from_user.first_name or "друг"
    await callback.message.answer(
        WELCOME_TEXT.format(name=name, channel=CHANNEL),
        reply_markup=main_menu()
    )
    await callback.answer()

@dp.callback_query(F.data == "write_post")
async def write_post(callback: CallbackQuery):
    await callback.message.answer("Выбери тон для поста:", reply_markup=tone_menu())
    await callback.answer()

@dp.callback_query(F.data.startswith("tone_"))
async def select_tone(callback: CallbackQuery):
    user_id = callback.from_user.id
    tone_map = {
        "tone_selling": "продающий",
        "tone_expert": "экспертный",
        "tone_friendly": "дружелюбный",
        "tone_engaging": "вовлекающий"
    }
    tone = tone_map.get(callback.data, "дружелюбный")
    set_user_step(user_id, step='ждём тему', tone=tone)
    await callback.message.answer(
        f"Тон: {tone}\n\nВведи тему поста:",
        reply_markup=back_menu()
    )
    await callback.answer()

@dp.callback_query(F.data == "ideas")
async def ideas(callback: CallbackQuery):
    user_id = callback.from_user.id
    set_user_step(user_id, step='ждём нишу')
    await callback.message.answer(
        "💡 Идеи для постов\n\nВведи нишу:\n\nНапример: кофейня, фитнес, онлайн-курсы",
        reply_markup=back_menu()
    )
    await callback.answer()

@dp.callback_query(F.data == "stories")
async def stories(callback: CallbackQuery):
    user_id = callback.from_user.id
    set_user_step(user_id, step='ждём тему stories')
    await callback.message.answer(
        "🎬 Stories\n\nВведи тему для сценария:",
        reply_markup=back_menu()
    )
    await callback.answer()

@dp.callback_query(F.data == "reels")
async def reels(callback: CallbackQuery):
    user_id = callback.from_user.id
    set_user_step(user_id, step='ждём тему reels')
    await callback.message.answer(
        "🎥 Reels\n\nВведи тему для сценария:\n\nНапример: как я ускорил работу в 3 раза",
        reply_markup=back_menu()
    )
    await callback.answer()

@dp.callback_query(F.data == "caption")
async def caption(callback: CallbackQuery):
    user_id = callback.from_user.id
    set_user_step(user_id, step='ждём описание фото')
    await callback.message.answer(
        "📸 Подпись к фото\n\nОпиши фото или отправь его:",
        reply_markup=back_menu()
    )
    await callback.answer()

@dp.callback_query(F.data == "content_plan")
async def content_plan(callback: CallbackQuery):
    user_id = callback.from_user.id
    set_user_step(user_id, step='план 7', plan_days=7)
    await callback.message.answer(
        "📅 Контент-план\n\nВведи нишу для контент-плана на 7 дней:",
        reply_markup=back_menu()
    )
    await callback.answer()

@dp.callback_query(F.data == "ads")
async def ads(callback: CallbackQuery):
    await callback.message.answer("Выбери платформу для рекламы:", reply_markup=ads_platform_menu())
    await callback.answer()

@dp.callback_query(F.data.startswith("ads_"))
async def ads_platform(callback: CallbackQuery):
    user_id = callback.from_user.id
    platform_map = {"ads_instagram": "Instagram", "ads_telegram": "Telegram", "ads_vk": "ВКонтакте"}
    platform = platform_map.get(callback.data, "Instagram")
    set_user_step(user_id, step='ждём описание рекламы', platform=platform)
    await callback.message.answer(
        f"📣 Реклама для {platform}\n\nОпиши продукт или услугу:",
        reply_markup=back_menu()
    )
    await callback.answer()

@dp.callback_query(F.data == "competitor_analysis")
async def competitor_analysis(callback: CallbackQuery):
    user_id = callback.from_user.id
    access = await check_pro_access(user_id, 'comp_trial')
    if access == 'pro':
        set_user_step(user_id, step='ждём нишу конкурентов')
        await callback.message.answer(
            "🕵️ Анализ конкурентов\n\nВведи нишу и платформу:\n\nНапример: кофейня Instagram",
            reply_markup=back_menu()
        )
    elif access == 'trial':
        set_trial(user_id, 'comp_trial')
        set_user_step(user_id, step='ждём нишу конкурентов')
        await callback.message.answer(
            "🕵️ Анализ конкурентов\n\nВведи нишу и платформу:\n\nНапример: кофейня Instagram",
            reply_markup=back_menu()
        )
    elif access == 'start_block':
        await callback.message.answer(
            "🔒 Анализ конкурентов доступен только на тарифе Про.\n\nВаш тариф: 🟢 Старт\n\n🔥 Про — 490 руб / 1 месяц",
            reply_markup=upgrade_pro_menu()
        )
    else:
        await callback.message.answer(
            "🔒 Вы уже использовали бесплатную попытку анализа конкурентов.\n\n🔥 Про — 490 руб / 1 месяц\nВсе функции без ограничений",
            reply_markup=upgrade_pro_menu()
        )
    await callback.answer()

@dp.callback_query(F.data == "strategy")
async def strategy(callback: CallbackQuery):
    user_id = callback.from_user.id
    access = await check_pro_access(user_id, 'strategy_trial')
    if access in ('pro', 'trial'):
        if access == 'trial':
            set_trial(user_id, 'strategy_trial')
        set_user_step(user_id, step='ждём нишу стратегии')
        await callback.message.answer(
            "📈 Стратегия продвижения\n\nВведи нишу и платформу:\n\nНапример: кофейня Instagram",
            reply_markup=back_menu()
        )
    elif access == 'start_block':
        await callback.message.answer(
            "🔒 Стратегия продвижения доступна только на тарифе Про.\n\nВаш тариф: 🟢 Старт\n\n🔥 Про — 490 руб / 1 месяц",
            reply_markup=upgrade_pro_menu()
        )
    else:
        await callback.message.answer(
            "🔒 Вы уже использовали бесплатную попытку стратегии продвижения.\n\n🔥 Про — 490 руб / 1 месяц",
            reply_markup=upgrade_pro_menu()
        )
    await callback.answer()

@dp.callback_query(F.data == "hooks")
async def hooks(callback: CallbackQuery):
    user_id = callback.from_user.id
    access = await check_pro_access(user_id, 'hooks_trial')
    if access in ('pro', 'trial'):
        if access == 'trial':
            set_trial(user_id, 'hooks_trial')
        set_user_step(user_id, step='ждём тему хуков')
        await callback.message.answer(
            "🪝 Генератор хуков\n\nВведи тему поста:\n\nНапример: как выбрать кофе",
            reply_markup=back_menu()
        )
    elif access == 'start_block':
        await callback.message.answer(
            "🔒 Генератор хуков доступен только на тарифе Про.\n\nВаш тариф: 🟢 Старт\n\n🔥 Про — 490 руб / 1 месяц",
            reply_markup=upgrade_pro_menu()
        )
    else:
        await callback.message.answer(
            "🔒 Вы уже использовали бесплатную попытку генератора хуков.\n\n🔥 Про — 490 руб / 1 месяц",
            reply_markup=upgrade_pro_menu()
        )
    await callback.answer()

@dp.callback_query(F.data == "post_review")
async def post_review(callback: CallbackQuery):
    user_id = callback.from_user.id
    access = await check_pro_access(user_id, 'review_trial')
    if access in ('pro', 'trial'):
        if access == 'trial':
            set_trial(user_id, 'review_trial')
        set_user_step(user_id, step='ждём пост для разбора')
        await callback.message.answer(
            "📊 Разбор поста\n\nВставь свой пост — я разберу его по 10 пунктам:\n\n(просто скопируй текст и пришли)",
            reply_markup=back_menu()
        )
    elif access == 'start_block':
        await callback.message.answer(
            "🔒 Разбор поста доступен только на тарифе Про.\n\nВаш тариф: 🟢 Старт\n\n🔥 Про — 490 руб / 1 месяц",
            reply_markup=upgrade_pro_menu()
        )
    else:
        await callback.message.answer(
            "🔒 Вы уже использовали бесплатную попытку разбора поста.\n\n🔥 Про — 490 руб / 1 месяц",
            reply_markup=upgrade_pro_menu()
        )
    await callback.answer()

@dp.callback_query(F.data == "tariffs")
async def tariffs(callback: CallbackQuery):
    await callback.message.answer(
        "💎 Выбери тариф PostGenius\n\n"
        "🟢 Старт — 190 руб / 1 месяц\n"
        "Посты, идеи, контент-план, stories, reels, реклама\n\n"
        "🔥 Про — 490 руб / 1 месяц\n"
        "Все функции бота без ограничений\n\n"
        "🎁 3 запроса бесплатно для новых пользователей",
        reply_markup=tariffs_menu()
    )
    await callback.answer()

@dp.callback_query(F.data.in_({"pay_start", "pay_pro"}))
async def pay(callback: CallbackQuery):
    user_id = callback.from_user.id
    if callback.data == "pay_start":
        await callback.message.answer(
            "💳 Тариф «Старт» — 190 руб / 1 месяц\n\n"
            "Для оплаты напишите в поддержку:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💬 Написать в поддержку", url=SUPPORT_URL)],
                [InlineKeyboardButton(text="🔙 В меню", callback_data="back_menu")]
            ])
        )
    else:
        await callback.message.answer(
            "💳 Тариф «Про» — 490 руб / 1 месяц\n\n"
            "Для оплаты напишите в поддержку:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💬 Написать в поддержку", url=SUPPORT_URL)],
                [InlineKeyboardButton(text="🔙 В меню", callback_data="back_menu")]
            ])
        )
    await callback.answer()

@dp.message(F.text)
async def handle_text(message: Message):
    user_id = message.from_user.id
    text = message.text.strip()
    user = get_user(user_id)
    step = user.get('step', '')
    tone = user.get('tone', 'дружелюбный')
    platform = user.get('platform', 'Instagram')
    plan_days = user.get('plan_days', 7)

    # Pro шаги
    pro_steps = ['ждём нишу конкурентов', 'ждём нишу стратегии', 'ждём тему хуков', 'ждём пост для разбора']
    task_map = {
        'ждём нишу конкурентов': 'анализ конкурентов',
        'ждём нишу стратегии': 'стратегия',
        'ждём тему хуков': 'хуки',
        'ждём пост для разбора': 'разбор поста',
        'ждём тему': 'пост',
        'ждём нишу': 'идеи',
        'ждём тему stories': 'stories',
        'ждём тему reels': 'reels',
        'план 7': 'контент-план',
        'ждём описание рекламы': 'реклама',
        'ждём описание фото': 'пост',
    }

    if step in pro_steps:
        task = task_map[step]
        plan, _ = get_subscription(user_id)
        if not plan:
            pass
        set_user_step(user_id, step='idle')
        await message.answer("⏳ Генерирую...")
        try:
            result = await generate_content(text, task, tone, platform, plan_days)
            await message.answer(result, reply_markup=back_menu())
        except Exception as e:
            await message.answer(f"Ошибка: {e}", reply_markup=back_menu())
        return

    if step not in task_map or step in ('', 'idle', None):
        await message.answer(
            "Выбери действие из меню 👇",
            reply_markup=main_menu()
        )
        return

    # Проверяем лимит
    access, plan = await check_limit(user_id)
    if access == 'limit':
        await message.answer(
            "🚫 Лимит 3 бесплатных запросов исчерпан.\n\nВыбери тариф:\n🟢 Старт — 190 руб/мес\n🔥 Про — 490 руб/мес",
            reply_markup=tariffs_menu()
        )
        return

    task = task_map.get(step, 'пост')
    set_user_step(user_id, step='idle')

    await message.answer("⏳ Генерирую...")
    try:
        result = await generate_content(text, task, tone, platform, plan_days)
        if access == 'ok':
            increment_requests(user_id)
        await message.answer(result, reply_markup=back_menu())
    except Exception as e:
        await message.answer(f"Ошибка генерации: {e}", reply_markup=back_menu())

@dp.message(F.photo)
async def handle_photo(message: Message):
    user_id = message.from_user.id
    set_user_step(user_id, step='ждём описание фото')
    await message.answer(
        "📸 Фото получено!\n\nТеперь напиши о чём оно — я сделаю подпись:",
        reply_markup=back_menu()
    )

async def main():
    init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
