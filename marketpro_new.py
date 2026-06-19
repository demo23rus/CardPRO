#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
МаркетПРО Premium — коммерческий Telegram AI-помощник селлера.

Главные возможности:
- Карточка товара 360° из текста или фото
- Сохранённые товары и Brand Kit
- Аудит карточки по тексту, ссылке и скриншотам
- Анализ конкурента и AI-стратегия ниши
- Юнит-экономика PRO, финансовый доктор, акции и поставки
- Ответы и пакетный анализ отзывов
- Апелляции PRO
- Подключение WB/Ozon Seller API (без имитации данных)
- AI-директор и ежедневные отчёты
- Тарифы, платежи, лимиты, рефералы, промокоды
- История, экспорт, админ-статистика и рассылки

Один файл специально сохранён для простого развёртывания. Для масштабирования
его можно без изменения бизнес-логики разнести на модули.
"""

from __future__ import annotations

import asyncio
import base64
import csv
import hashlib
import html
import io
import json
import logging
import math
import os
import re
import secrets
import sqlite3
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import urlparse

import aiosqlite
import httpx
from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.client.default import DefaultBotProperties
from openai import AsyncOpenAI

try:
    from aiogram.fsm.storage.redis import RedisStorage
except Exception:  # optional dependency
    RedisStorage = None

try:
    from cryptography.fernet import Fernet
except Exception:  # optional dependency
    Fernet = None

try:
    from yookassa import Configuration, Payment
except Exception:  # optional dependency
    Configuration = Payment = None

try:
    import anthropic
except Exception:  # optional dependency
    anthropic = None


# ============================================================================
# CONFIG
# ============================================================================

def load_env_file(path: str) -> dict[str, str]:
    data: dict[str, str] = {}
    p = Path(path)
    if not p.exists():
        return data
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip().strip('"').strip("'")
    return data


FILE_ENV = load_env_file(os.getenv("MARKETPRO_ENV", "/root/.env_card"))


def env(name: str, default: str = "") -> str:
    return os.getenv(name, FILE_ENV.get(name, default))


def env_int(name: str, default: int) -> int:
    try:
        return int(env(name, str(default)))
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(env(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    bot_token: str = env("BOT_TOKEN")
    bot_username: str = env("BOT_USERNAME", "PostGeniusHelperBot").lstrip("@")
    owner_id: int = env_int("OWNER_ID", 549639607)
    support_url: str = env("SUPPORT_URL", "https://t.me/Boss023rus")
    db_path: str = env("DB_PATH", "/root/marketpro_premium.db")
    redis_url: str = env("REDIS_URL")

    openai_key: str = env("OPENAI_KEY")
    openai_text_model: str = env("OPENAI_TEXT_MODEL", "gpt-4o-mini")
    openai_vision_model: str = env("OPENAI_VISION_MODEL", "gpt-4o")
    openai_image_model: str = env("OPENAI_IMAGE_MODEL", "gpt-image-1")
    anthropic_key: str = env("ANTHROPIC_KEY")
    anthropic_model: str = env("ANTHROPIC_MODEL", "claude-sonnet-4-5")

    yookassa_shop_id: str = env("YOOKASSA_SHOP_ID")
    yookassa_secret: str = env("YOOKASSA_SECRET")
    payment_return_url: str = env("PAYMENT_RETURN_URL", "https://t.me/PostGeniusHelperBot")

    encryption_key: str = env("ENCRYPTION_KEY")
    timezone_offset: int = env_int("TZ_OFFSET", 3)
    request_timeout: int = env_int("HTTP_TIMEOUT", 35)
    max_input_chars: int = env_int("MAX_INPUT_CHARS", 18000)
    max_photo_mb: int = env_int("MAX_PHOTO_MB", 15)
    fair_use_daily: int = env_int("FAIR_USE_DAILY", 250)
    image_daily: int = env_int("IMAGE_DAILY", 10)
    free_credits: int = env_int("FREE_CREDITS", 12)
    referral_inviter: int = env_int("REF_BONUS_INVITER", 7)
    referral_invited: int = env_int("REF_BONUS_INVITED", 5)


CFG = Config()
LOCAL_TZ = timezone(timedelta(hours=CFG.timezone_offset))

PLANS: dict[str, dict[str, Any]] = {
    "free": {"name": "Знакомство", "price": 0, "days": 0, "credits": CFG.free_credits, "products": 2, "images": 1, "shops": 0},
    "start": {"name": "Старт", "price": 490, "days": 30, "credits": 80, "products": 7, "images": 5, "shops": 0},
    "pro": {"name": "ПРО", "price": 990, "days": 30, "credits": 350, "products": 40, "images": 20, "shops": 1},
    "business": {"name": "Бизнес", "price": 1990, "days": 30, "credits": 1200, "products": 250, "images": 80, "shops": 4},
}

FEATURE_COST = {
    "card360_text": 3,
    "card360_photo": 5,
    "audit": 2,
    "audit_link": 3,
    "competitor": 3,
    "niche": 3,
    "review": 1,
    "review_batch": 3,
    "appeal": 2,
    "finance": 1,
    "doctor": 2,
    "supply": 1,
    "image": 8,
    "director": 3,
}

logging.basicConfig(
    level=getattr(logging, env("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("marketpro")

if Payment and CFG.yookassa_shop_id and CFG.yookassa_secret:
    Configuration.account_id = CFG.yookassa_shop_id
    Configuration.secret_key = CFG.yookassa_secret


# ============================================================================
# HELPERS
# ============================================================================

def now() -> datetime:
    return datetime.now(LOCAL_TZ)


def iso_now() -> str:
    return now().isoformat()


def parse_dt(value: str | None) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        return dt if dt.tzinfo else dt.replace(tzinfo=LOCAL_TZ)
    except ValueError:
        return None


def safe_float(value: str, minimum: float = 0, maximum: float = 10**9) -> float:
    cleaned = re.sub(r"[^0-9,.-]", "", value).replace(",", ".")
    number = float(cleaned)
    if not minimum <= number <= maximum:
        raise ValueError(f"Значение должно быть от {minimum:g} до {maximum:g}")
    return number


def safe_int(value: str, minimum: int = 0, maximum: int = 10**9) -> int:
    return int(safe_float(value, minimum, maximum))


def money(value: float) -> str:
    return f"{value:,.0f}".replace(",", " ") + " ₽"


def pct(value: float) -> str:
    return f"{value:.1f}%"


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def chunks(text: str, size: int = 3900) -> list[str]:
    if len(text) <= size:
        return [text]
    parts: list[str] = []
    current = ""
    for paragraph in text.split("\n"):
        candidate = current + ("\n" if current else "") + paragraph
        if len(candidate) <= size:
            current = candidate
            continue
        if current:
            parts.append(current)
        while len(paragraph) > size:
            parts.append(paragraph[:size])
            paragraph = paragraph[size:]
        current = paragraph
    if current:
        parts.append(current)
    return parts


def clean_ai_text(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json|markdown|text)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def extract_json(text: str) -> dict[str, Any]:
    clean = clean_ai_text(text)
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", clean, re.S)
        if not match:
            raise
        return json.loads(match.group(0))


def redact_token(token: str) -> str:
    if len(token) <= 8:
        return "••••••••"
    return token[:4] + "••••" + token[-4:]


def normalize_url(url: str) -> str:
    url = url.strip()
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Некорректная ссылка")
    return url


def marketplace_from_url(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if "wildberries" in host or "wb.ru" in host:
        return "wb"
    if "ozon" in host:
        return "ozon"
    if "avito" in host:
        return "avito"
    return "unknown"


def extract_article(url: str, marketplace: str) -> Optional[str]:
    patterns = {
        "wb": [r"/catalog/(\d+)", r"(?:nm|article|id)[=/](\d+)"],
        "ozon": [r"-(\d{6,})(?:/|\?|$)", r"/product/[^/?]*?(\d{6,})"],
    }
    for pattern in patterns.get(marketplace, []):
        m = re.search(pattern, url, re.I)
        if m:
            return m.group(1)
    return None


class TokenCipher:
    def __init__(self, key: str):
        self._fernet = None
        if key and Fernet:
            try:
                raw = key.encode()
                if len(raw) != 44:
                    raw = base64.urlsafe_b64encode(hashlib.sha256(raw).digest())
                self._fernet = Fernet(raw)
            except Exception as exc:
                log.error("Encryption init failed: %s", exc)

    def encrypt(self, value: str) -> str:
        if not value:
            return ""
        if self._fernet:
            return self._fernet.encrypt(value.encode()).decode()
        # Fallback obfuscation is not cryptographic; production should set ENCRYPTION_KEY.
        return "plain:" + base64.urlsafe_b64encode(value.encode()).decode()

    def decrypt(self, value: str) -> str:
        if not value:
            return ""
        try:
            if self._fernet and not value.startswith("plain:"):
                return self._fernet.decrypt(value.encode()).decode()
            if value.startswith("plain:"):
                return base64.urlsafe_b64decode(value[6:].encode()).decode()
        except Exception:
            return ""
        return value


CIPHER = TokenCipher(CFG.encryption_key)


# ============================================================================
# DATABASE
# ============================================================================

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT NOT NULL DEFAULT '',
    first_name TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    onboarded INTEGER NOT NULL DEFAULT 0,
    experience TEXT NOT NULL DEFAULT 'newbie',
    primary_marketplace TEXT NOT NULL DEFAULT 'wb',
    is_blocked INTEGER NOT NULL DEFAULT 0,
    ref_by INTEGER NOT NULL DEFAULT 0,
    referral_rewarded INTEGER NOT NULL DEFAULT 0,
    daily_report_hour INTEGER NOT NULL DEFAULT 9,
    notifications_enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS balances (
    user_id INTEGER PRIMARY KEY,
    credits INTEGER NOT NULL DEFAULT 0,
    image_credits INTEGER NOT NULL DEFAULT 0,
    lifetime_used INTEGER NOT NULL DEFAULT 0,
    daily_used INTEGER NOT NULL DEFAULT 0,
    daily_date TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS subscriptions (
    user_id INTEGER PRIMARY KEY,
    plan TEXT NOT NULL DEFAULT 'free',
    started_at TEXT NOT NULL DEFAULT '',
    expires_at TEXT NOT NULL DEFAULT '',
    auto_renew INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    marketplace TEXT NOT NULL DEFAULT 'wb',
    article TEXT NOT NULL DEFAULT '',
    brand TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    audience TEXT NOT NULL DEFAULT '',
    benefits TEXT NOT NULL DEFAULT '',
    characteristics TEXT NOT NULL DEFAULT '',
    keywords TEXT NOT NULL DEFAULT '',
    source_text TEXT NOT NULL DEFAULT '',
    current_content TEXT NOT NULL DEFAULT '',
    cost REAL NOT NULL DEFAULT 0,
    price REAL NOT NULL DEFAULT 0,
    length_cm REAL NOT NULL DEFAULT 0,
    width_cm REAL NOT NULL DEFAULT 0,
    height_cm REAL NOT NULL DEFAULT 0,
    weight_kg REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS brand_kits (
    user_id INTEGER PRIMARY KEY,
    brand_name TEXT NOT NULL DEFAULT '',
    tone TEXT NOT NULL DEFAULT 'профессиональный и дружелюбный',
    colors TEXT NOT NULL DEFAULT '',
    fonts TEXT NOT NULL DEFAULT '',
    visual_style TEXT NOT NULL DEFAULT '',
    target_audience TEXT NOT NULL DEFAULT '',
    forbidden_phrases TEXT NOT NULL DEFAULT '',
    logo_file_id TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    product_id INTEGER,
    feature TEXT NOT NULL,
    input_text TEXT NOT NULL DEFAULT '',
    result TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE,
    FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS marketplace_connections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    marketplace TEXT NOT NULL,
    name TEXT NOT NULL,
    client_id TEXT NOT NULL DEFAULT '',
    token_encrypted TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    last_sync_at TEXT NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(user_id, marketplace, name),
    FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS imported_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    product_id INTEGER,
    source TEXT NOT NULL,
    period_start TEXT NOT NULL DEFAULT '',
    period_end TEXT NOT NULL DEFAULT '',
    metrics_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE,
    FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS payments (
    payment_id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    plan TEXT NOT NULL,
    amount REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS referral_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invited_user_id INTEGER NOT NULL UNIQUE,
    inviter_user_id INTEGER NOT NULL,
    registered_rewarded INTEGER NOT NULL DEFAULT 0,
    paid_rewarded INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS promo_codes (
    code TEXT PRIMARY KEY,
    bonus_credits INTEGER NOT NULL DEFAULT 0,
    discount_percent INTEGER NOT NULL DEFAULT 0,
    max_uses INTEGER NOT NULL DEFAULT 0,
    uses INTEGER NOT NULL DEFAULT 0,
    expires_at TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS promo_uses (
    user_id INTEGER NOT NULL,
    code TEXT NOT NULL,
    used_at TEXT NOT NULL,
    PRIMARY KEY(user_id, code)
);

CREATE TABLE IF NOT EXISTS recommendations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    product_id INTEGER,
    severity TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    estimated_effect REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'new',
    created_at TEXT NOT NULL,
    resolved_at TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_history_user ON history(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_products_user ON products(user_id, status);
CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status);
"""


class Database:
    def __init__(self, path: str):
        self.path = path
        self._write_lock = asyncio.Lock()

    async def init(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as db:
            await db.executescript(SCHEMA)
            await db.commit()

    @asynccontextmanager
    async def connect(self):
        db = await aiosqlite.connect(self.path)
        db.row_factory = aiosqlite.Row
        try:
            yield db
        finally:
            await db.close()

    async def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        async with self._write_lock, self.connect() as db:
            cur = await db.execute(sql, tuple(params))
            await db.commit()
            return cur.lastrowid

    async def fetchone(self, sql: str, params: Iterable[Any] = ()) -> Optional[dict[str, Any]]:
        async with self.connect() as db:
            cur = await db.execute(sql, tuple(params))
            row = await cur.fetchone()
            return dict(row) if row else None

    async def fetchall(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        async with self.connect() as db:
            cur = await db.execute(sql, tuple(params))
            rows = await cur.fetchall()
            return [dict(row) for row in rows]

    async def transaction(self, operations: list[tuple[str, tuple[Any, ...]]]) -> None:
        async with self._write_lock, self.connect() as db:
            try:
                await db.execute("BEGIN IMMEDIATE")
                for sql, params in operations:
                    await db.execute(sql, params)
                await db.commit()
            except Exception:
                await db.rollback()
                raise


DB = Database(CFG.db_path)


async def ensure_user(user_id: int, username: str = "", first_name: str = "") -> None:
    stamp = iso_now()
    await DB.transaction([
        ("""INSERT INTO users(user_id,username,first_name,created_at,last_seen_at)
             VALUES(?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET
             username=excluded.username, first_name=excluded.first_name,
             last_seen_at=excluded.last_seen_at""", (user_id, username, first_name, stamp, stamp)),
        ("INSERT OR IGNORE INTO balances(user_id,credits,image_credits,daily_date) VALUES(?,?,?,?)",
         (user_id, CFG.free_credits, 1, now().date().isoformat())),
        ("INSERT OR IGNORE INTO subscriptions(user_id,plan) VALUES(?, 'free')", (user_id,)),
    ])


async def user_plan(user_id: int) -> tuple[str, Optional[datetime]]:
    row = await DB.fetchone("SELECT plan,expires_at FROM subscriptions WHERE user_id=?", (user_id,))
    if not row:
        return "free", None
    plan = row["plan"] or "free"
    expires = parse_dt(row["expires_at"])
    if plan != "free" and (not expires or expires <= now()):
        await DB.execute("UPDATE subscriptions SET plan='free',expires_at='' WHERE user_id=?", (user_id,))
        return "free", None
    return plan, expires


async def activate_plan(user_id: int, plan: str) -> None:
    if plan not in PLANS or plan == "free":
        raise ValueError("Неизвестный тариф")
    old_plan, old_end = await user_plan(user_id)
    start = now()
    base = old_end if old_end and old_end > start else start
    end = base + timedelta(days=PLANS[plan]["days"])
    await DB.transaction([
        ("""INSERT INTO subscriptions(user_id,plan,started_at,expires_at)
             VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET
             plan=excluded.plan, started_at=excluded.started_at, expires_at=excluded.expires_at""",
         (user_id, plan, start.isoformat(), end.isoformat())),
        ("UPDATE balances SET credits=credits+?, image_credits=image_credits+? WHERE user_id=?",
         (PLANS[plan]["credits"], PLANS[plan]["images"], user_id)),
    ])


async def balance(user_id: int) -> dict[str, Any]:
    today = now().date().isoformat()
    row = await DB.fetchone("SELECT * FROM balances WHERE user_id=?", (user_id,))
    if not row:
        await ensure_user(user_id)
        row = await DB.fetchone("SELECT * FROM balances WHERE user_id=?", (user_id,))
    if row and row["daily_date"] != today:
        await DB.execute("UPDATE balances SET daily_used=0,daily_date=? WHERE user_id=?", (today, user_id))
        row["daily_used"] = 0
        row["daily_date"] = today
    return row or {}


async def charge(user_id: int, feature: str, image: bool = False) -> tuple[bool, str]:
    cost = FEATURE_COST.get(feature, 1)
    bal = await balance(user_id)
    user = await DB.fetchone("SELECT is_blocked FROM users WHERE user_id=?", (user_id,))
    if user and user["is_blocked"]:
        return False, "Ваш доступ временно ограничен. Напишите в поддержку."
    if bal.get("daily_used", 0) >= CFG.fair_use_daily:
        return False, "Достигнут дневной fair-use лимит. Он защищает сервис от автоматической перегрузки."
    if image:
        if bal.get("image_credits", 0) <= 0:
            return False, "Закончились кредиты изображений. Пополните тариф или пакет изображений."
        await DB.execute(
            "UPDATE balances SET image_credits=image_credits-1,daily_used=daily_used+1,lifetime_used=lifetime_used+1 WHERE user_id=?",
            (user_id,),
        )
        return True, ""
    if bal.get("credits", 0) < cost:
        return False, f"Для операции нужно {cost} кредитов, доступно {bal.get('credits', 0)}."
    await DB.execute(
        "UPDATE balances SET credits=credits-?,daily_used=daily_used+1,lifetime_used=lifetime_used+1 WHERE user_id=?",
        (cost, user_id),
    )
    return True, ""


async def refund(user_id: int, feature: str, image: bool = False) -> None:
    if image:
        await DB.execute("UPDATE balances SET image_credits=image_credits+1 WHERE user_id=?", (user_id,))
    else:
        await DB.execute("UPDATE balances SET credits=credits+? WHERE user_id=?", (FEATURE_COST.get(feature, 1), user_id))


async def save_history(user_id: int, feature: str, input_text: str, result: str,
                       product_id: int | None = None, metadata: dict[str, Any] | None = None) -> int:
    return await DB.execute(
        "INSERT INTO history(user_id,product_id,feature,input_text,result,metadata,created_at) VALUES(?,?,?,?,?,?,?)",
        (user_id, product_id, feature, input_text[:12000], result[:50000], json.dumps(metadata or {}, ensure_ascii=False), iso_now()),
    )


# ============================================================================
# AI SERVICE
# ============================================================================

class AIService:
    def __init__(self):
        self.openai = AsyncOpenAI(api_key=CFG.openai_key) if CFG.openai_key else None
        self.anthropic = anthropic.AsyncAnthropic(api_key=CFG.anthropic_key) if anthropic and CFG.anthropic_key else None

    async def text(self, system: str, user: str, *, model: str | None = None,
                   max_tokens: int = 2200, temperature: float = 0.35) -> str:
        if not self.openai:
            raise RuntimeError("OPENAI_KEY не настроен")
        user = user[:CFG.max_input_chars]
        response = await self.openai.chat.completions.create(
            model=model or CFG.openai_text_model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return clean_ai_text(response.choices[0].message.content or "")

    async def json(self, system: str, user: str, *, max_tokens: int = 2600) -> dict[str, Any]:
        strict = system + "\nВерни только валидный JSON без markdown и пояснений."
        raw = await self.text(strict, user, max_tokens=max_tokens, temperature=0.2)
        return extract_json(raw)

    async def vision(self, system: str, prompt: str, image_bytes: bytes, mime: str = "image/jpeg") -> str:
        if not self.openai:
            raise RuntimeError("OPENAI_KEY не настроен")
        b64 = base64.b64encode(image_bytes).decode()
        response = await self.openai.chat.completions.create(
            model=CFG.openai_vision_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                ]},
            ],
            max_tokens=2800,
            temperature=0.25,
        )
        return clean_ai_text(response.choices[0].message.content or "")

    async def transcribe(self, audio_bytes: bytes, filename: str = "voice.ogg") -> str:
        if not self.openai:
            raise RuntimeError("OPENAI_KEY не настроен")
        bio = io.BytesIO(audio_bytes)
        bio.name = filename
        result = await self.openai.audio.transcriptions.create(model="whisper-1", file=bio, language="ru")
        return result.text.strip()

    async def image(self, prompt: str, size: str = "1024x1024") -> bytes:
        if not self.openai:
            raise RuntimeError("OPENAI_KEY не настроен")
        response = await self.openai.images.generate(
            model=CFG.openai_image_model,
            prompt=prompt[:4000],
            size=size,
            n=1,
        )
        item = response.data[0]
        if getattr(item, "b64_json", None):
            return base64.b64decode(item.b64_json)
        if getattr(item, "url", None):
            async with httpx.AsyncClient(timeout=60) as client:
                r = await client.get(item.url)
                r.raise_for_status()
                return r.content
        raise RuntimeError("Сервис изображений не вернул файл")


AI = AIService()

CARD_SYSTEM = """Ты ведущий e-commerce стратег по Wildberries, Ozon и Авито.
Создавай коммерчески сильный результат без выдумывания характеристик. Если данных нет,
помечай их как «нужно уточнить». Не обещай гарантированный рост. Пиши по-русски,
конкретно, без вступлений и без фраз «конечно/вот результат». Учитывай правила площадки,
читабельность, естественное SEO и соответствие фактам."""

AUDIT_SYSTEM = """Ты строгий e-commerce аудитор. Отделяй фактические данные от гипотез.
Никогда не утверждай, что видел страницу или метрики, если они не переданы. Диагностируй
проблемы по этапам воронки: показы → CTR → карточка → корзина → заказ → выкуп → возврат.
Каждой рекомендации назначай приоритет, сложность, ожидаемый эффект и уровень уверенности."""

FINANCE_SYSTEM = """Ты финансовый аналитик маркетплейсов. Не подменяй расчёты общими советами.
Явно показывай допущения, риски и чувствительность результата. Не выдавай приблизительные
тарифы за официальные. Все выводы формулируй как управленческие рекомендации."""


async def brand_context(user_id: int) -> str:
    row = await DB.fetchone("SELECT * FROM brand_kits WHERE user_id=?", (user_id,))
    if not row:
        return "Brand Kit не заполнен."
    return (
        f"Бренд: {row['brand_name'] or 'не указан'}; тон: {row['tone']}; цвета: {row['colors']}; "
        f"стиль: {row['visual_style']}; аудитория: {row['target_audience']}; "
        f"запрещённые фразы: {row['forbidden_phrases']}"
    )


async def generate_card360(user_id: int, platform: str, product_data: str) -> str:
    brand = await brand_context(user_id)
    limits = {
        "wb": "название желательно до 60 символов; без внешних ссылок и неподтверждённых превосходных степеней",
        "ozon": "название по схеме тип + бренд + ключевые характеристики; подготовь Rich Content",
        "avito": "заголовок до 50 символов; описание с выгодами, доверием и призывом",
    }.get(platform, "соблюдай общие требования маркетплейса")
    prompt = f"""Площадка: {platform.upper()}. Правила: {limits}.
Brand Kit: {brand}
Данные товара:
{product_data}

Создай полный пакет «Карточка 360°»:
1. Проверка входных данных и список того, что нельзя выдумывать.
2. Целевая аудитория: 3 сегмента, боли, мотивы и возражения.
3. Позиционирование и УТП.
4. Пять SEO-названий с пометкой стратегии каждого; выбери лучший и объясни.
5. Финальное описание площадки.
6. Характеристики: заполненные и список «нужно уточнить».
7. SEO-кластеры: высокочастотные, средние, низкие и смысловые.
8. FAQ из 7 вопросов.
9. Структура 8 слайдов инфографики с точным текстом и задачей каждого слайда.
10. Концепция главного изображения и две идеи A/B-теста.
11. Три промта для AI-фотосессии с обязательным сохранением формы и маркировки товара.
12. Rich Content / расширенный контент.
13. Три рекламных объявления.
14. Сценарий видео 20–30 секунд.
15. План запуска на 14 дней.
16. Контроль качества: риски, переспам, неподтверждённые заявления, лимиты символов.
"""
    return await AI.text(CARD_SYSTEM, prompt, max_tokens=4200)


async def audit_content(source: str, context: str = "") -> str:
    prompt = f"""Материал для аудита:
{source}

Дополнительный контекст/метрики:
{context or 'не переданы'}

Подготовь:
- что является фактом, а что гипотезой;
- оценку 0–100 и по 10-балльной шкале: название, SEO, описание, характеристики, оффер,
  визуал, доверие, цена, отзывы, соответствие аудитории;
- диагноз по воронке;
- 5 критических проблем;
- быстрые правки на 30 минут;
- план улучшения на 7 дней;
- готовые варианты улучшенного названия, первого экрана и описания;
- 3 A/B-теста;
- таблицу рекомендаций: действие / приоритет / сложность / ожидаемый эффект / уверенность.
"""
    return await AI.text(AUDIT_SYSTEM, prompt, max_tokens=3200)


# ============================================================================
# MARKETPLACE DATA ADAPTERS
# ============================================================================

class MarketplaceError(RuntimeError):
    pass


class MarketplaceClient:
    def __init__(self):
        self.timeout = httpx.Timeout(CFG.request_timeout)
        self.headers = {"User-Agent": "Mozilla/5.0 MarketPRO/2.0", "Accept-Language": "ru-RU,ru;q=0.9"}

    async def fetch_public_card(self, url: str) -> dict[str, Any]:
        url = normalize_url(url)
        marketplace = marketplace_from_url(url)
        article = extract_article(url, marketplace)
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True, headers=self.headers) as client:
            r = await client.get(url)
            if r.status_code >= 400:
                raise MarketplaceError(f"Страница вернула HTTP {r.status_code}")
            text = r.text[:2_000_000]
        data: dict[str, Any] = {"marketplace": marketplace, "url": str(r.url), "article": article or ""}
        # JSON-LD is the most stable public source when available.
        blocks = re.findall(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', text, re.S | re.I)
        for block in blocks:
            try:
                parsed = json.loads(html.unescape(block.strip()))
                candidates = parsed if isinstance(parsed, list) else [parsed]
                for item in candidates:
                    if isinstance(item, dict) and item.get("@type") in {"Product", "IndividualProduct"}:
                        data.update({
                            "name": item.get("name", ""),
                            "description": item.get("description", ""),
                            "brand": item.get("brand", {}).get("name", "") if isinstance(item.get("brand"), dict) else item.get("brand", ""),
                            "image": item.get("image", ""),
                            "sku": item.get("sku", ""),
                            "rating": (item.get("aggregateRating") or {}).get("ratingValue", ""),
                            "reviews_count": (item.get("aggregateRating") or {}).get("reviewCount", ""),
                        })
                        offers = item.get("offers") or {}
                        if isinstance(offers, list):
                            offers = offers[0] if offers else {}
                        data["price"] = offers.get("price", "") if isinstance(offers, dict) else ""
            except Exception:
                continue
        if not data.get("name"):
            def meta(prop: str) -> str:
                patterns = [
                    rf'<meta[^>]+property=["\']{re.escape(prop)}["\'][^>]+content=["\'](.*?)["\']',
                    rf'<meta[^>]+content=["\'](.*?)["\'][^>]+property=["\']{re.escape(prop)}["\']',
                    rf'<meta[^>]+name=["\']{re.escape(prop)}["\'][^>]+content=["\'](.*?)["\']',
                ]
                for p in patterns:
                    m = re.search(p, text, re.I | re.S)
                    if m:
                        return html.unescape(m.group(1)).strip()
                return ""
            data.update({
                "name": meta("og:title") or meta("twitter:title"),
                "description": meta("og:description") or meta("description"),
                "image": meta("og:image"),
                "price": meta("product:price:amount"),
            })
        if not data.get("name") and not data.get("description"):
            raise MarketplaceError("Площадка не отдала публичные данные карточки. Используйте скриншоты или Seller API.")
        return data

    async def wb_validate(self, token: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.get("https://common-api.wildberries.ru/ping", headers={"Authorization": token})
            if r.status_code not in {200, 204}:
                raise MarketplaceError(f"WB API: HTTP {r.status_code}: {r.text[:300]}")
            return {"ok": True}

    async def wb_cards(self, token: str, limit: int = 100) -> list[dict[str, Any]]:
        payload = {"settings": {"cursor": {"limit": min(limit, 100)}, "filter": {"withPhoto": -1}}}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(
                "https://content-api.wildberries.ru/content/v2/get/cards/list",
                headers={"Authorization": token, "Content-Type": "application/json"}, json=payload,
            )
            if r.status_code != 200:
                raise MarketplaceError(f"WB cards: HTTP {r.status_code}: {r.text[:400]}")
            body = r.json()
            return body.get("cards", []) if isinstance(body, dict) else []

    async def ozon_validate(self, client_id: str, token: str) -> dict[str, Any]:
        headers = {"Client-Id": client_id, "Api-Key": token, "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post("https://api-seller.ozon.ru/v3/product/info/list", headers=headers, json={"product_id": [], "offer_id": [], "sku": []})
            if r.status_code != 200:
                raise MarketplaceError(f"Ozon API: HTTP {r.status_code}: {r.text[:300]}")
            return {"ok": True}

    async def ozon_products(self, client_id: str, token: str, limit: int = 100) -> list[dict[str, Any]]:
        headers = {"Client-Id": client_id, "Api-Key": token, "Content-Type": "application/json"}
        payload = {"filter": {"visibility": "ALL"}, "last_id": "", "limit": min(limit, 100)}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post("https://api-seller.ozon.ru/v3/product/list", headers=headers, json=payload)
            if r.status_code != 200:
                raise MarketplaceError(f"Ozon products: HTTP {r.status_code}: {r.text[:400]}")
            return ((r.json().get("result") or {}).get("items") or [])


MP = MarketplaceClient()


def card_data_text(data: dict[str, Any]) -> str:
    labels = {
        "marketplace": "Площадка", "article": "Артикул", "name": "Название", "description": "Описание",
        "brand": "Бренд", "price": "Цена", "rating": "Рейтинг", "reviews_count": "Отзывов", "url": "Ссылка",
    }
    lines = []
    for key, label in labels.items():
        value = data.get(key)
        if value not in (None, "", [], {}):
            lines.append(f"{label}: {value}")
    return "\n".join(lines)


# ============================================================================
# FINANCE ENGINE
# ============================================================================

@dataclass
class UnitEconomicsInput:
    price: float
    cost: float
    commission_pct: float
    logistics: float
    last_mile: float
    storage: float
    acquiring_pct: float
    tax_pct: float
    ads_pct: float
    buyout_pct: float
    return_cost: float
    packaging: float
    other: float


@dataclass
class UnitEconomicsResult:
    revenue_per_order: float
    expected_revenue: float
    commission: float
    acquiring: float
    tax: float
    ads: float
    expected_returns_cost: float
    total_cost: float
    profit: float
    margin_pct: float
    roi_pct: float
    break_even_price: float
    max_discount_pct: float
    max_ads_pct: float


def calculate_unit(inp: UnitEconomicsInput) -> UnitEconomicsResult:
    if inp.price <= 0 or inp.cost < 0:
        raise ValueError("Цена должна быть больше нуля, себестоимость — неотрицательной")
    buyout = clamp(inp.buyout_pct / 100, 0.01, 1)
    expected_revenue = inp.price * buyout
    commission = inp.price * inp.commission_pct / 100 * buyout
    acquiring = inp.price * inp.acquiring_pct / 100 * buyout
    tax = inp.price * inp.tax_pct / 100 * buyout
    ads = inp.price * inp.ads_pct / 100 * buyout
    expected_returns_cost = (1 - buyout) * inp.return_cost
    fixed = inp.cost + inp.logistics + inp.last_mile + inp.storage + inp.packaging + inp.other + expected_returns_cost
    total = fixed + commission + acquiring + tax + ads
    profit = expected_revenue - total
    margin = profit / expected_revenue * 100 if expected_revenue else -100
    roi = profit / max(inp.cost + inp.packaging, 1) * 100
    variable_rate = buyout * (1 - (inp.commission_pct + inp.acquiring_pct + inp.tax_pct + inp.ads_pct) / 100)
    break_even = fixed / variable_rate if variable_rate > 0 else math.inf
    max_discount = clamp((inp.price - break_even) / inp.price * 100, 0, 100) if math.isfinite(break_even) else 0
    base_without_ads = expected_revenue - (total - ads)
    max_ads_pct = clamp(base_without_ads / (inp.price * buyout) * 100, 0, 100) if inp.price * buyout else 0
    return UnitEconomicsResult(inp.price, expected_revenue, commission, acquiring, tax, ads,
                               expected_returns_cost, total, profit, margin, roi,
                               break_even, max_discount, max_ads_pct)


def scenario(inp: UnitEconomicsInput, price_factor: float, logistics_factor: float,
             buyout_delta: float, ads_delta: float) -> UnitEconomicsResult:
    adjusted = UnitEconomicsInput(
        price=inp.price * price_factor,
        cost=inp.cost,
        commission_pct=inp.commission_pct,
        logistics=inp.logistics * logistics_factor,
        last_mile=inp.last_mile * logistics_factor,
        storage=inp.storage * logistics_factor,
        acquiring_pct=inp.acquiring_pct,
        tax_pct=inp.tax_pct,
        ads_pct=max(0, inp.ads_pct + ads_delta),
        buyout_pct=clamp(inp.buyout_pct + buyout_delta, 1, 100),
        return_cost=inp.return_cost,
        packaging=inp.packaging,
        other=inp.other,
    )
    return calculate_unit(adjusted)


def render_unit(inp: UnitEconomicsInput) -> str:
    base = calculate_unit(inp)
    pessimistic = scenario(inp, 0.95, 1.2, -8, 3)
    optimistic = scenario(inp, 1.03, 0.95, 5, -2)
    verdict = "✅ здоровая экономика" if base.margin_pct >= 20 else "🟡 экономика требует контроля" if base.margin_pct >= 8 else "🔴 высокий риск убытка"
    return f"""🧮 ЮНИТ-ЭКОНОМИКА PRO

Цена: {money(inp.price)}
Себестоимость: {money(inp.cost)}
Выкуп: {pct(inp.buyout_pct)}

ОЖИДАЕМЫЙ РЕЗУЛЬТАТ НА ОДИН ЗАКАЗ
Выручка с учётом выкупа: {money(base.expected_revenue)}
Комиссия: −{money(base.commission)}
Логистика + последняя миля: −{money(inp.logistics + inp.last_mile)}
Хранение: −{money(inp.storage)}
Эквайринг: −{money(base.acquiring)}
Налог: −{money(base.tax)}
Реклама: −{money(base.ads)}
Возвраты: −{money(base.expected_returns_cost)}
Упаковка и прочее: −{money(inp.packaging + inp.other)}

Чистая прибыль: {money(base.profit)}
Маржинальность: {pct(base.margin_pct)}
ROI на товар и упаковку: {pct(base.roi_pct)}
Вердикт: {verdict}

БЕЗОПАСНЫЕ ГРАНИЦЫ
Точка безубыточности по цене: {money(base.break_even_price) if math.isfinite(base.break_even_price) else 'не рассчитывается'}
Максимальная скидка до нулевой прибыли: {pct(base.max_discount_pct)}
Максимальный ДРР до нулевой прибыли: {pct(base.max_ads_pct)}

СЦЕНАРИИ
🔴 Пессимистичный: {money(pessimistic.profit)} / маржа {pct(pessimistic.margin_pct)}
🟡 Базовый: {money(base.profit)} / маржа {pct(base.margin_pct)}
🟢 Оптимистичный: {money(optimistic.profit)} / маржа {pct(optimistic.margin_pct)}

⚠️ Комиссии и тарифы введены пользователем. Сверяйте их с актуальным кабинетом маркетплейса."""


def calculate_supply(sales_day: float, stock: int, in_transit: int, lead_days: int,
                     coverage_days: int, safety_days: int, growth_pct: float,
                     cost: float, buyout_pct: float) -> str:
    adjusted_sales = sales_day * (1 + growth_pct / 100)
    available = stock + in_transit
    days_left = available / adjusted_sales if adjusted_sales > 0 else math.inf
    target = math.ceil(adjusted_sales * (lead_days + coverage_days + safety_days) / max(buyout_pct / 100, 0.01))
    order_qty = max(0, target - available)
    order_date = now().date() + timedelta(days=max(0, math.floor(days_left - lead_days - safety_days))) if math.isfinite(days_left) else None
    risk = "🔴 риск out-of-stock" if days_left <= lead_days + safety_days else "🟡 готовьте поставку" if days_left <= lead_days + coverage_days else "🟢 запас достаточен"
    return f"""📦 ПЛАН ПОСТАВКИ PRO

Скорректированный спрос: {adjusted_sales:.1f} шт./день
Доступно с учётом товара в пути: {available} шт.
Запаса хватит: {'∞' if not math.isfinite(days_left) else f'{days_left:.1f} дней'}
Статус: {risk}

Целевой запас: {target} шт.
Рекомендуемый заказ: {order_qty} шт.
Инвестиции: {money(order_qty * cost)}
Рекомендуемая дата заказа: {order_date.strftime('%d.%m.%Y') if order_date else 'не требуется'}

Расчёт учитывает срок поставки, страховой запас, рост спроса и процент выкупа."""


# ============================================================================
# BOT UI
# ============================================================================

class Onboarding(StatesGroup):
    experience = State()
    marketplace = State()


class Card360(StatesGroup):
    platform = State()
    input_type = State()
    waiting_text = State()
    waiting_photo = State()
    save_name = State()


class ProductFlow(StatesGroup):
    add_name = State()
    add_details = State()
    edit_field = State()


class BrandFlow(StatesGroup):
    waiting = State()


class AuditFlow(StatesGroup):
    mode = State()
    waiting_text = State()
    waiting_link = State()
    waiting_photo = State()


class CompetitorFlow(StatesGroup):
    waiting_link = State()


class NicheFlow(StatesGroup):
    waiting = State()


class ReviewFlow(StatesGroup):
    mode = State()
    waiting = State()


class AppealFlow(StatesGroup):
    type = State()
    waiting = State()


class UnitFlow(StatesGroup):
    waiting = State()


class DoctorFlow(StatesGroup):
    waiting = State()


class SupplyFlow(StatesGroup):
    waiting = State()


class ImageFlow(StatesGroup):
    waiting_prompt = State()
    waiting_photo = State()


class ConnectFlow(StatesGroup):
    marketplace = State()
    waiting_wb = State()
    waiting_ozon = State()


class PromoFlow(StatesGroup):
    waiting = State()


class PromoCodeFlow(StatesGroup):
    waiting = State()


class BroadcastFlow(StatesGroup):
    waiting = State()


class ImportFlow(StatesGroup):
    waiting = State()


def kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    buttons = []
    for row in rows:
        buttons.append([
            InlineKeyboardButton(text=text, url=value[4:]) if value.startswith("url:")
            else InlineKeyboardButton(text=text, callback_data=value)
            for text, value in row
        ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def main_kb() -> InlineKeyboardMarkup:
    return kb([
        [("🚀 Карточка 360°", "card360"), ("📦 Мои товары", "products")],
        [("🎨 Визуальная студия", "visual"), ("📊 Аудит и рост", "analytics")],
        [("💰 Финансы", "finance"), ("💬 Отзывы", "reviews")],
        [("🛡 Апелляции", "appeals"), ("🤖 AI-директор", "director")],
        [("🔌 Кабинеты WB/Ozon", "connections"), ("👤 Профиль", "profile")],
        [("💳 Тарифы", "tariffs"), ("💬 Поддержка", f"url:{CFG.support_url}")],
    ])


def back_kb(target: str = "home") -> InlineKeyboardMarkup:
    return kb([[("⬅️ Назад", target), ("🏠 Меню", "home")]])


def card_platform_kb() -> InlineKeyboardMarkup:
    return kb([
        [("🟣 Wildberries", "cplat_wb"), ("🔵 Ozon", "cplat_ozon")],
        [("🟡 Авито", "cplat_avito")],
        [("🏠 Меню", "home")],
    ])


async def answer_long(message: Message, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
    for index, part in enumerate(chunks(text)):
        await message.answer(part, reply_markup=reply_markup if index == len(chunks(text)) - 1 else None)


async def edit_or_answer(call: CallbackQuery, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
    try:
        await call.message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest:
        await call.message.answer(text, reply_markup=reply_markup)
    await call.answer()


async def get_message_input(message: Message, bot: Bot) -> str:
    if message.text:
        return message.text.strip()
    if message.voice or message.audio:
        media = message.voice or message.audio
        file = await bot.get_file(media.file_id)
        bio = await bot.download_file(file.file_path)
        return await AI.transcribe(bio.read(), getattr(media, "file_name", None) or "voice.ogg")
    return ""


async def get_photo_bytes(message: Message, bot: Bot) -> tuple[bytes, str]:
    if not message.photo:
        raise ValueError("Фото не найдено")
    photo = message.photo[-1]
    if photo.file_size and photo.file_size > CFG.max_photo_mb * 1024 * 1024:
        raise ValueError(f"Максимальный размер фото — {CFG.max_photo_mb} МБ")
    file = await bot.get_file(photo.file_id)
    bio = await bot.download_file(file.file_path)
    return bio.read(), "image/jpeg"


async def access_or_paywall(call_or_message: CallbackQuery | Message, feature: str, image: bool = False) -> bool:
    uid = call_or_message.from_user.id
    ok, reason = await charge(uid, feature, image=image)
    if ok:
        return True
    text = f"⛔️ {reason}\n\nВыберите тариф или активируйте промокод."
    markup = kb([[("💳 Посмотреть тарифы", "tariffs"), ("🎁 Промокод", "promo")], [("🏠 Меню", "home")]])
    if isinstance(call_or_message, CallbackQuery):
        await call_or_message.message.answer(text, reply_markup=markup)
        await call_or_message.answer()
    else:
        await call_or_message.answer(text, reply_markup=markup)
    return False


router = Router()


@router.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    u = message.from_user
    await ensure_user(u.id, u.username or "", u.first_name or "")
    user = await DB.fetchone("SELECT onboarded FROM users WHERE user_id=?", (u.id,))

    # Atomic referral reward: unique invited_user_id prevents repeated bonuses.
    args = (message.text or "").split(maxsplit=1)
    if len(args) == 2 and args[1].startswith("ref_"):
        try:
            inviter = int(args[1][4:])
            if inviter != u.id and await DB.fetchone("SELECT user_id FROM users WHERE user_id=?", (inviter,)):
                try:
                    await DB.transaction([
                        ("INSERT INTO referral_events(invited_user_id,inviter_user_id,registered_rewarded,created_at) VALUES(?,?,1,?)",
                         (u.id, inviter, iso_now())),
                        ("UPDATE users SET ref_by=?,referral_rewarded=1 WHERE user_id=? AND referral_rewarded=0", (inviter, u.id)),
                        ("UPDATE balances SET credits=credits+? WHERE user_id=?", (CFG.referral_invited, u.id)),
                        ("UPDATE balances SET credits=credits+? WHERE user_id=?", (CFG.referral_inviter, inviter)),
                    ])
                    try:
                        await message.bot.send_message(inviter, f"🎉 Новый пользователь по вашей ссылке. Начислено +{CFG.referral_inviter} кредитов.")
                    except Exception:
                        pass
                except sqlite3.IntegrityError:
                    pass
        except ValueError:
            pass

    if not user or not user["onboarded"]:
        await state.set_state(Onboarding.experience)
        await message.answer(
            "👋 Добро пожаловать в МаркетПРО Premium.\n\n"
            "Я не просто генерирую тексты: сохраняю товары, диагностирую воронку, считаю прибыль "
            "и формирую план действий.\n\nКакой у вас опыт?",
            reply_markup=kb([[("🌱 Начинаю", "exp_newbie"), ("📈 Уже продаю", "exp_seller")], [("🏢 Команда/агентство", "exp_agency")]]),
        )
        return
    await show_home(message)


@router.callback_query(F.data.startswith("exp_"), Onboarding.experience)
async def onboarding_experience(call: CallbackQuery, state: FSMContext):
    experience = call.data[4:]
    await state.update_data(experience=experience)
    await state.set_state(Onboarding.marketplace)
    await edit_or_answer(call, "Основная площадка:", kb([[("🟣 WB", "onb_wb"), ("🔵 Ozon", "onb_ozon")], [("🟡 Авито", "onb_avito"), ("🌐 Несколько", "onb_multi")]]))


@router.callback_query(F.data.startswith("onb_"), Onboarding.marketplace)
async def onboarding_marketplace(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await DB.execute("UPDATE users SET onboarded=1,experience=?,primary_marketplace=? WHERE user_id=?",
                     (data.get("experience", "newbie"), call.data[4:], call.from_user.id))
    await state.clear()
    await call.message.answer("✅ Профиль настроен. Для первого результата рекомендую «Карточка 360°».", reply_markup=main_kb())
    await call.answer()


async def show_home(message: Message):
    plan, expiry = await user_plan(message.from_user.id)
    bal = await balance(message.from_user.id)
    products = await DB.fetchone("SELECT COUNT(*) AS c FROM products WHERE user_id=? AND status='active'", (message.from_user.id,))
    status = f"{PLANS[plan]['name']}" + (f" до {expiry.strftime('%d.%m.%Y')}" if expiry else "")
    await message.answer(
        f"🏠 МАРКЕТПРО PREMIUM\n\nТариф: {status}\nКредиты: {bal.get('credits', 0)} · Изображения: {bal.get('image_credits', 0)}\n"
        f"Товаров: {(products or {}).get('c', 0)}\n\nВыберите задачу:",
        reply_markup=main_kb(),
    )


@router.callback_query(F.data == "home")
async def home_cb(call: CallbackQuery, state: FSMContext):
    await state.clear()
    plan, expiry = await user_plan(call.from_user.id)
    bal = await balance(call.from_user.id)
    await edit_or_answer(call, f"🏠 МАРКЕТПРО PREMIUM\n\nТариф: {PLANS[plan]['name']}\nКредиты: {bal.get('credits',0)} · Изображения: {bal.get('image_credits',0)}", main_kb())


# --- Card 360 ---------------------------------------------------------------
@router.callback_query(F.data == "card360")
async def card360_start(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(Card360.platform)
    await edit_or_answer(call, "🚀 КАРТОЧКА 360°\n\nСоздам контент, SEO, инфографику, Rich Content, рекламу и план запуска.\nВыберите площадку:", card_platform_kb())


@router.callback_query(F.data.startswith("cplat_"), Card360.platform)
async def card360_platform(call: CallbackQuery, state: FSMContext):
    await state.update_data(platform=call.data[6:])
    await state.set_state(Card360.input_type)
    await edit_or_answer(call, "Откуда взять данные о товаре?", kb([[("✍️ Текст/голос", "ctype_text"), ("📷 Фото", "ctype_photo")], [("🏠 Меню", "home")]]))


@router.callback_query(F.data == "ctype_text", Card360.input_type)
async def card360_text_start(call: CallbackQuery, state: FSMContext):
    if not await access_or_paywall(call, "card360_text"):
        return
    await state.update_data(charged_feature="card360_text")
    await state.set_state(Card360.waiting_text)
    await edit_or_answer(call, "Опишите товар: название, материал, размер, комплект, аудитория, преимущества, цена. Можно голосом.\n\nНеизвестные характеристики бот не станет выдумывать.", back_kb("card360"))


@router.message(Card360.waiting_text, F.text | F.voice | F.audio)
async def card360_text_process(message: Message, state: FSMContext):
    data = await state.get_data()
    try:
        text = await get_message_input(message, message.bot)
        if len(text) < 20:
            raise ValueError("Добавьте больше данных о товаре — минимум 20 символов")
        progress = await message.answer("⏳ Собираю стратегию, SEO и контент карточки...")
        result = await generate_card360(message.from_user.id, data.get("platform", "wb"), text)
        hid = await save_history(message.from_user.id, "card360", text, result, metadata={"platform": data.get("platform")})
        await progress.delete()
        await answer_long(message, result, kb([[("💾 Сохранить как товар", f"savehist_{hid}"), ("📄 Экспорт TXT", f"export_{hid}")], [("🔄 Новая карточка", "card360"), ("🏠 Меню", "home")]]))
    except Exception as exc:
        await refund(message.from_user.id, data.get("charged_feature", "card360_text"))
        log.exception("card360 text")
        await message.answer(f"❌ Не удалось создать карточку: {exc}", reply_markup=back_kb("card360"))
    await state.clear()


@router.callback_query(F.data == "ctype_photo", Card360.input_type)
async def card360_photo_start(call: CallbackQuery, state: FSMContext):
    if not await access_or_paywall(call, "card360_photo"):
        return
    await state.update_data(charged_feature="card360_photo")
    await state.set_state(Card360.waiting_photo)
    await edit_or_answer(call, "Отправьте чёткое фото товара. После распознавания я создам карточку 360° без выдумывания скрытых характеристик.", back_kb("card360"))


@router.message(Card360.waiting_photo, F.photo)
async def card360_photo_process(message: Message, state: FSMContext):
    data = await state.get_data()
    try:
        image, mime = await get_photo_bytes(message, message.bot)
        progress = await message.answer("⏳ Анализирую товар на фото...")
        recognized = await AI.vision(
            "Ты товаровед. Определи только видимые характеристики товара. Не угадывай бренд, материал и комплектность. Если это не товар, ответь НЕ_ТОВАР.",
            "Опиши товар максимально точно для последующего создания карточки маркетплейса. Отдельно перечисли неизвестные данные.", image, mime,
        )
        if "НЕ_ТОВАР" in recognized.upper():
            raise ValueError("На фото не удалось распознать товар")
        result = await generate_card360(message.from_user.id, data.get("platform", "wb"), recognized)
        hid = await save_history(message.from_user.id, "card360_photo", recognized, result, metadata={"platform": data.get("platform")})
        await progress.delete()
        await answer_long(message, result, kb([[("💾 Сохранить товар", f"savehist_{hid}"), ("📄 Экспорт", f"export_{hid}")], [("🏠 Меню", "home")]]))
    except Exception as exc:
        await refund(message.from_user.id, data.get("charged_feature", "card360_photo"))
        log.exception("card360 photo")
        await message.answer(f"❌ {exc}", reply_markup=back_kb("card360"))
    await state.clear()


@router.callback_query(F.data.startswith("savehist_"))
async def save_history_as_product(call: CallbackQuery, state: FSMContext):
    hid = int(call.data.split("_", 1)[1])
    row = await DB.fetchone("SELECT * FROM history WHERE id=? AND user_id=?", (hid, call.from_user.id))
    if not row:
        await call.answer("Результат не найден", show_alert=True)
        return
    plan, _ = await user_plan(call.from_user.id)
    count = await DB.fetchone("SELECT COUNT(*) AS c FROM products WHERE user_id=? AND status='active'", (call.from_user.id,))
    if (count or {}).get("c", 0) >= PLANS[plan]["products"]:
        await call.answer("Достигнут лимит товаров тарифа", show_alert=True)
        return
    await state.update_data(history_id=hid)
    await state.set_state(Card360.save_name)
    await call.message.answer("Введите короткое название товара для раздела «Мои товары»: ", reply_markup=back_kb("products"))
    await call.answer()


@router.message(Card360.save_name, F.text)
async def save_product_name(message: Message, state: FSMContext):
    data = await state.get_data()
    row = await DB.fetchone("SELECT * FROM history WHERE id=? AND user_id=?", (data["history_id"], message.from_user.id))
    if not row:
        await message.answer("Результат не найден")
        await state.clear(); return
    meta = json.loads(row["metadata"] or "{}")
    pid = await DB.execute(
        """INSERT INTO products(user_id,name,marketplace,source_text,current_content,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?)""",
        (message.from_user.id, message.text[:120], meta.get("platform", "wb"), row["input_text"], row["result"], iso_now(), iso_now()),
    )
    await DB.execute("UPDATE history SET product_id=? WHERE id=?", (pid, row["id"]))
    await state.clear()
    await message.answer("✅ Товар сохранён. Теперь все будущие результаты можно связывать с его паспортом.", reply_markup=back_kb("products"))


# --- Products and brand ------------------------------------------------------
@router.callback_query(F.data == "products")
async def products_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    rows = await DB.fetchall("SELECT id,name,marketplace,price,cost FROM products WHERE user_id=? AND status='active' ORDER BY updated_at DESC LIMIT 20", (call.from_user.id,))
    buttons = [[(f"{p['name']} · {p['marketplace'].upper()}", f"product_{p['id']}")] for p in rows]
    buttons += [[("➕ Добавить вручную", "product_add"), ("🎨 Brand Kit", "brandkit")], [("🏠 Меню", "home")]]
    text = "📦 МОИ ТОВАРЫ\n\n" + ("Выберите товар:" if rows else "Товаров пока нет. Создайте карточку 360° или добавьте товар вручную.")
    await edit_or_answer(call, text, kb(buttons))


@router.callback_query(F.data.startswith("product_") & ~F.data.in_({"product_add"}))
async def product_view(call: CallbackQuery):
    try:
        pid = int(call.data.split("_")[1])
    except ValueError:
        return
    p = await DB.fetchone("SELECT * FROM products WHERE id=? AND user_id=?", (pid, call.from_user.id))
    if not p:
        await call.answer("Товар не найден", show_alert=True); return
    text = f"""📦 {p['name']}
Площадка: {p['marketplace'].upper()}
Артикул: {p['article'] or '—'}
Категория: {p['category'] or '—'}
Цена: {money(p['price']) if p['price'] else '—'}
Себестоимость: {money(p['cost']) if p['cost'] else '—'}
Обновлён: {parse_dt(p['updated_at']).strftime('%d.%m.%Y') if parse_dt(p['updated_at']) else '—'}"""
    await edit_or_answer(call, text, kb([[("🔍 Аудит", f"paudit_{pid}"), ("🧮 Экономика", f"pfinance_{pid}")], [("📄 Контент", f"pcontent_{pid}"), ("🗄 Архивировать", f"parchive_{pid}")], [("⬅️ Товары", "products"), ("🏠 Меню", "home")]]))


@router.callback_query(F.data.startswith("pcontent_"))
async def product_content(call: CallbackQuery):
    pid = int(call.data.split("_")[1])
    p = await DB.fetchone("SELECT current_content FROM products WHERE id=? AND user_id=?", (pid, call.from_user.id))
    if not p:
        await call.answer("Не найдено", show_alert=True); return
    await call.answer()
    await answer_long(call.message, p["current_content"] or "Контент ещё не создан.", back_kb("products"))


@router.callback_query(F.data.startswith("parchive_"))
async def product_archive(call: CallbackQuery):
    pid = int(call.data.split("_")[1])
    await DB.execute("UPDATE products SET status='archived',updated_at=? WHERE id=? AND user_id=?", (iso_now(), pid, call.from_user.id))
    await call.answer("Товар архивирован")
    await edit_or_answer(call, "✅ Товар перемещён в архив.", back_kb("products"))


@router.callback_query(F.data == "product_add")
async def product_add(call: CallbackQuery, state: FSMContext):
    await state.set_state(ProductFlow.add_name)
    await edit_or_answer(call, "Введите название товара:", back_kb("products"))


@router.message(ProductFlow.add_name, F.text)
async def product_add_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text[:120])
    await state.set_state(ProductFlow.add_details)
    await message.answer("Одним сообщением укажите площадку, артикул, категорию, цену, себестоимость, характеристики и преимущества.")


@router.message(ProductFlow.add_details, F.text)
async def product_add_details(message: Message, state: FSMContext):
    data = await state.get_data()
    await DB.execute("INSERT INTO products(user_id,name,source_text,created_at,updated_at) VALUES(?,?,?,?,?)",
                     (message.from_user.id, data["name"], message.text[:12000], iso_now(), iso_now()))
    await state.clear()
    await message.answer("✅ Паспорт товара создан. Детали можно использовать в аудитах и генерациях.", reply_markup=back_kb("products"))


@router.callback_query(F.data == "brandkit")
async def brandkit_start(call: CallbackQuery, state: FSMContext):
    current = await DB.fetchone("SELECT * FROM brand_kits WHERE user_id=?", (call.from_user.id,))
    preview = ""
    if current:
        preview = f"\n\nТекущий бренд: {current['brand_name'] or '—'}\nТон: {current['tone']}\nСтиль: {current['visual_style'] or '—'}"
    await state.set_state(BrandFlow.waiting)
    await edit_or_answer(call, "🎨 BRAND KIT" + preview + "\n\nОтправьте одним сообщением: название бренда; тон; цвета; шрифты; визуальный стиль; аудитория; запрещённые фразы.", back_kb("products"))


@router.message(BrandFlow.waiting, F.text)
async def brandkit_save(message: Message, state: FSMContext):
    prompt = f"Разбери описание Brand Kit в JSON с ключами brand_name,tone,colors,fonts,visual_style,target_audience,forbidden_phrases:\n{message.text}"
    try:
        data = await AI.json("Ты бренд-стратег. Не добавляй факты, которых нет.", prompt, max_tokens=700)
    except Exception:
        data = {"brand_name": "", "tone": "профессиональный", "colors": "", "fonts": "", "visual_style": message.text, "target_audience": "", "forbidden_phrases": ""}
    await DB.execute("""INSERT INTO brand_kits(user_id,brand_name,tone,colors,fonts,visual_style,target_audience,forbidden_phrases,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET brand_name=excluded.brand_name,tone=excluded.tone,
        colors=excluded.colors,fonts=excluded.fonts,visual_style=excluded.visual_style,target_audience=excluded.target_audience,
        forbidden_phrases=excluded.forbidden_phrases,updated_at=excluded.updated_at""",
        (message.from_user.id, data.get("brand_name", ""), data.get("tone", ""), data.get("colors", ""), data.get("fonts", ""), data.get("visual_style", ""), data.get("target_audience", ""), data.get("forbidden_phrases", ""), iso_now()))
    await state.clear()
    await message.answer("✅ Brand Kit сохранён и будет применяться в карточках, ответах и визуальных промтах.", reply_markup=back_kb("products"))


# --- Analytics --------------------------------------------------------------
@router.callback_query(F.data == "analytics")
async def analytics_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await edit_or_answer(call, "📊 АУДИТ И РОСТ\n\nФакты и AI-гипотезы всегда разделяются.", kb([[("📝 Аудит текста", "audit_text"), ("🔗 Аудит ссылки", "audit_link")], [("📷 Аудит скриншота", "audit_photo"), ("🏆 Конкурент", "competitor")], [("🔭 Стратегия ниши", "niche"), ("📥 Импорт отчёта", "import_report")], [("🏠 Меню", "home")]]))


@router.callback_query(F.data == "audit_text")
async def audit_text_start(call: CallbackQuery, state: FSMContext):
    if not await access_or_paywall(call, "audit"): return
    await state.update_data(charged_feature="audit")
    await state.set_state(AuditFlow.waiting_text)
    await edit_or_answer(call, "Вставьте название, описание, характеристики и при наличии метрики воронки.", back_kb("analytics"))


@router.message(AuditFlow.waiting_text, F.text | F.voice | F.audio)
async def audit_text_process(message: Message, state: FSMContext):
    data = await state.get_data()
    try:
        text = await get_message_input(message, message.bot)
        result = await audit_content(text)
        hid = await save_history(message.from_user.id, "audit_text", text, result)
        await answer_long(message, result, kb([[("📄 Экспорт", f"export_{hid}"), ("🏠 Меню", "home")]]))
    except Exception as exc:
        await refund(message.from_user.id, data.get("charged_feature", "audit"))
        await message.answer(f"❌ Ошибка аудита: {exc}", reply_markup=back_kb("analytics"))
    await state.clear()


@router.callback_query(F.data == "audit_link")
async def audit_link_start(call: CallbackQuery, state: FSMContext):
    if not await access_or_paywall(call, "audit_link"): return
    await state.update_data(charged_feature="audit_link")
    await state.set_state(AuditFlow.waiting_link)
    await edit_or_answer(call, "Отправьте ссылку WB, Ozon или Авито. Сначала бот загрузит реальные публичные данные; если площадка их скрывает, попросит скриншоты.", back_kb("analytics"))


@router.message(AuditFlow.waiting_link, F.text)
async def audit_link_process(message: Message, state: FSMContext):
    data = await state.get_data()
    try:
        card = await MP.fetch_public_card(message.text)
        source = card_data_text(card)
        await message.answer("✅ Получены реальные публичные данные:\n\n" + source[:1500])
        result = await audit_content(source, "Метрики кабинета не переданы; выводы по воронке являются гипотезами.")
        hid = await save_history(message.from_user.id, "audit_link", message.text, result, metadata=card)
        await answer_long(message, result, kb([[("📄 Экспорт", f"export_{hid}"), ("🏠 Меню", "home")]]))
    except Exception as exc:
        await refund(message.from_user.id, data.get("charged_feature", "audit_link"))
        await message.answer(f"⚠️ Не удалось достоверно получить карточку: {exc}\n\nОтправьте скриншоты через «Аудит скриншота» — бот не будет выдумывать содержимое ссылки.", reply_markup=back_kb("analytics"))
    await state.clear()


@router.callback_query(F.data == "audit_photo")
async def audit_photo_start(call: CallbackQuery, state: FSMContext):
    if not await access_or_paywall(call, "audit"): return
    await state.update_data(charged_feature="audit")
    await state.set_state(AuditFlow.waiting_photo)
    await edit_or_answer(call, "Отправьте скриншот карточки, рекламы, аналитики или штрафа.", back_kb("analytics"))


@router.message(AuditFlow.waiting_photo, F.photo)
async def audit_photo_process(message: Message, state: FSMContext):
    data = await state.get_data()
    try:
        image, mime = await get_photo_bytes(message, message.bot)
        result = await AI.vision(AUDIT_SYSTEM, "Разбери этот скриншот. Сначала перечисли, что точно видно; затем проблемы, риски, приоритетные действия и данные, которых не хватает.", image, mime)
        hid = await save_history(message.from_user.id, "audit_screenshot", "screenshot", result)
        await answer_long(message, result, kb([[("📄 Экспорт", f"export_{hid}"), ("🏠 Меню", "home")]]))
    except Exception as exc:
        await refund(message.from_user.id, data.get("charged_feature", "audit"))
        await message.answer(f"❌ {exc}", reply_markup=back_kb("analytics"))
    await state.clear()


@router.callback_query(F.data == "competitor")
async def competitor_start(call: CallbackQuery, state: FSMContext):
    if not await access_or_paywall(call, "competitor"): return
    await state.update_data(charged_feature="competitor")
    await state.set_state(CompetitorFlow.waiting_link)
    await edit_or_answer(call, "Отправьте ссылку конкурента. Анализ будет основан только на реально полученных данных.", back_kb("analytics"))


@router.message(CompetitorFlow.waiting_link, F.text)
async def competitor_process(message: Message, state: FSMContext):
    data = await state.get_data()
    try:
        card = await MP.fetch_public_card(message.text)
        source = card_data_text(card)
        prompt = f"""Данные конкурента:
{source}

Сделай анализ: позиционирование, оффер, SEO, доверие, цена, сильные и слабые стороны,
что нельзя копировать, как отстроиться, новая концепция продукта, таблица «конкурент / мы»,
5 A/B-тестов. Не выдумывай данные, которых нет."""
        result = await AI.text(AUDIT_SYSTEM, prompt, max_tokens=2600)
        hid = await save_history(message.from_user.id, "competitor", message.text, result, metadata=card)
        await answer_long(message, result, kb([[("📄 Экспорт", f"export_{hid}"), ("🏠 Меню", "home")]]))
    except Exception as exc:
        await refund(message.from_user.id, data.get("charged_feature", "competitor"))
        await message.answer(f"⚠️ {exc}\nДля достоверного анализа используйте скриншоты.", reply_markup=back_kb("analytics"))
    await state.clear()


@router.callback_query(F.data == "niche")
async def niche_start(call: CallbackQuery, state: FSMContext):
    if not await access_or_paywall(call, "niche"): return
    await state.update_data(charged_feature="niche")
    await state.set_state(NicheFlow.waiting)
    await edit_or_answer(call, "Введите нишу, площадку, бюджет и ваш опыт. Это AI-стратегия; фактические объёмы продаж появятся только после импорта данных.", back_kb("analytics"))


@router.message(NicheFlow.waiting, F.text | F.voice | F.audio)
async def niche_process(message: Message, state: FSMContext):
    data = await state.get_data()
    try:
        text = await get_message_input(message, message.bot)
        result = await AI.text(CARD_SYSTEM, f"""Подготовь AI-стратегию ниши: {text}
Отдели гипотезы от фактов. Дай сегменты, Jobs-to-be-Done, ценовые уровни, продуктовые идеи,
комплектации, риски, сезонность как гипотезу, ключевые кластеры, отстройку, MVP,
план проверки спроса, бюджетный сценарий запуска и список данных, которые нужно собрать.""", max_tokens=3000)
        hid = await save_history(message.from_user.id, "niche_strategy", text, result)
        await answer_long(message, result, kb([[("📄 Экспорт", f"export_{hid}"), ("🏠 Меню", "home")]]))
    except Exception as exc:
        await refund(message.from_user.id, data.get("charged_feature", "niche"))
        await message.answer(f"❌ {exc}")
    await state.clear()


@router.callback_query(F.data == "import_report")
async def import_report_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(ImportFlow.waiting)
    await edit_or_answer(call, "Отправьте CSV с заголовками. Бот сохранит данные и подготовит управленческий разбор. Максимум 2 МБ.", back_kb("analytics"))


@router.message(ImportFlow.waiting, F.document)
async def import_report_process(message: Message, state: FSMContext):
    doc = message.document
    if not doc.file_name.lower().endswith(".csv"):
        await message.answer("Поддерживается CSV. XLSX экспортируйте в CSV."); return
    if doc.file_size and doc.file_size > 2 * 1024 * 1024:
        await message.answer("Файл больше 2 МБ."); return
    try:
        file = await message.bot.get_file(doc.file_id)
        bio = await message.bot.download_file(file.file_path)
        raw = bio.read()
        decoded = None
        for enc in ("utf-8-sig", "cp1251", "utf-8"):
            try:
                decoded = raw.decode(enc); break
            except UnicodeDecodeError:
                continue
        if decoded is None:
            raise ValueError("Не удалось определить кодировку")
        sample = decoded[:5000]
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        rows = list(csv.DictReader(io.StringIO(decoded), dialect=dialect))[:500]
        if not rows:
            raise ValueError("CSV пуст")
        compact = json.dumps(rows[:80], ensure_ascii=False)
        result = await AI.text(AUDIT_SYSTEM, f"Проанализируй строки отчёта. Найди аномалии, потери, лидеров, риски остатков и 10 действий. Не путай корреляцию с причиной.\n{compact}", max_tokens=2600)
        await DB.execute("INSERT INTO imported_metrics(user_id,source,metrics_json,created_at) VALUES(?,?,?,?)", (message.from_user.id, doc.file_name, json.dumps(rows, ensure_ascii=False), iso_now()))
        hid = await save_history(message.from_user.id, "import_analysis", doc.file_name, result)
        await answer_long(message, result, kb([[("📄 Экспорт", f"export_{hid}"), ("🏠 Меню", "home")]]))
    except Exception as exc:
        await message.answer(f"❌ Ошибка импорта: {exc}", reply_markup=back_kb("analytics"))
    await state.clear()


# --- Visual -----------------------------------------------------------------
@router.callback_query(F.data == "visual")
async def visual_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await edit_or_answer(call, "🎨 ВИЗУАЛЬНАЯ СТУДИЯ\n\nГенерация использует отдельные image-кредиты.", kb([[("🖼 Создать концепт", "image_generate"), ("🔍 Аудит обложки", "audit_photo")], [("🧩 Сценарий 8 слайдов", "card360"), ("🎨 Brand Kit", "brandkit")], [("🏠 Меню", "home")]]))


@router.callback_query(F.data == "image_generate")
async def image_generate_start(call: CallbackQuery, state: FSMContext):
    if not await access_or_paywall(call, "image", image=True): return
    await state.update_data(image_charged=True)
    await state.set_state(ImageFlow.waiting_prompt)
    await edit_or_answer(call, "Опишите товар и нужный кадр. Укажите формат: 1:1, 3:4 или 4:5; фон; аудиторию; текст на изображении.\n\nДля точного сохранения конкретного товара используйте специализированный image-to-image API — текстовая генерация может изменить детали упаковки.", back_kb("visual"))


@router.message(ImageFlow.waiting_prompt, F.text)
async def image_generate_process(message: Message, state: FSMContext):
    try:
        brand = await brand_context(message.from_user.id)
        prompt = f"Профессиональная карточка товара для маркетплейса. {message.text}. Brand Kit: {brand}. Чистая коммерческая композиция, высокая читаемость, без ложных наград и логотипов маркетплейса."
        progress = await message.answer("⏳ Создаю визуальный концепт...")
        image = await AI.image(prompt)
        await progress.delete()
        await message.answer_photo(BufferedInputFile(image, filename="marketpro_visual.png"), caption="✅ Визуальный концепт. Перед публикацией проверьте форму товара, упаковку, текст и маркировку.", reply_markup=back_kb("visual"))
    except Exception as exc:
        await refund(message.from_user.id, "image", image=True)
        await message.answer(f"❌ Генерация изображения недоступна: {exc}", reply_markup=back_kb("visual"))
    await state.clear()


# --- Finance ----------------------------------------------------------------
@router.callback_query(F.data == "finance")
async def finance_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await edit_or_answer(call, "💰 ФИНАНСЫ\n\nТарифы и комиссии вводятся пользователем — бот не выдаёт устаревшие проценты за актуальные.", kb([[("🧮 Юнит-экономика PRO", "unit"), ("🩺 Финансовый доктор", "doctor")], [("📦 План поставки", "supply"), ("🎯 Проверка акции", "promo_calc")], [("🏠 Меню", "home")]]))


UNIT_TEMPLATE = """Отправьте 14 чисел через точку с запятой:
цена; себестоимость; комиссия%; логистика; последняя миля; хранение; эквайринг%; налог%; ДРР%; выкуп%; стоимость возврата; упаковка; прочие расходы; продажи в месяц

Пример:
1990; 600; 20; 120; 40; 15; 1.5; 6; 12; 82; 100; 35; 20; 150"""


@router.callback_query(F.data == "unit")
async def unit_start(call: CallbackQuery, state: FSMContext):
    if not await access_or_paywall(call, "finance"): return
    await state.update_data(charged_feature="finance")
    await state.set_state(UnitFlow.waiting)
    await edit_or_answer(call, UNIT_TEMPLATE, back_kb("finance"))


@router.message(UnitFlow.waiting, F.text)
async def unit_process(message: Message, state: FSMContext):
    data = await state.get_data()
    try:
        values = [safe_float(x.strip()) for x in message.text.split(";")]
        if len(values) != 14:
            raise ValueError("Нужно ровно 14 значений")
        inp = UnitEconomicsInput(*values[:13])
        result = render_unit(inp)
        monthly = calculate_unit(inp).profit * values[13]
        result += f"\n\nПРОГНОЗ ЗА МЕСЯЦ\nПри {values[13]:.0f} заказах: {money(monthly)}"
        hid = await save_history(message.from_user.id, "unit_economics", message.text, result)
        await message.answer(result, reply_markup=kb([[("📄 Экспорт", f"export_{hid}"), ("🏠 Меню", "home")]]))
    except Exception as exc:
        await refund(message.from_user.id, data.get("charged_feature", "finance"))
        await message.answer(f"❌ {exc}\n\n{UNIT_TEMPLATE}", reply_markup=back_kb("finance"))
    await state.clear()


@router.callback_query(F.data == "doctor")
async def doctor_start(call: CallbackQuery, state: FSMContext):
    if not await access_or_paywall(call, "doctor"): return
    await state.update_data(charged_feature="doctor")
    await state.set_state(DoctorFlow.waiting)
    await edit_or_answer(call, "Опишите финансовую ситуацию: выручка, продажи, себестоимость, комиссии, логистика, реклама, возвраты, налоги, акции, остатки и цель прибыли.", back_kb("finance"))


@router.message(DoctorFlow.waiting, F.text | F.voice | F.audio)
async def doctor_process(message: Message, state: FSMContext):
    data = await state.get_data()
    try:
        text = await get_message_input(message, message.bot)
        result = await AI.text(FINANCE_SYSTEM, f"""Проведи финансовую диагностику:
{text}

Найди, где теряются деньги; посчитай то, что можно посчитать; обозначь недостающие данные;
оцени максимальный ДРР, безопасную цену, влияние возвратов и акций; сформируй три сценария;
дай 5 действий по приоритету и ожидаемому эффекту в рублях. Не выдумывай тарифы.""", max_tokens=2600)
        hid = await save_history(message.from_user.id, "financial_doctor", text, result)
        await answer_long(message, result, kb([[("📄 Экспорт", f"export_{hid}"), ("🏠 Меню", "home")]]))
    except Exception as exc:
        await refund(message.from_user.id, data.get("charged_feature", "doctor"))
        await message.answer(f"❌ {exc}")
    await state.clear()


@router.callback_query(F.data == "supply")
async def supply_start(call: CallbackQuery, state: FSMContext):
    if not await access_or_paywall(call, "supply"): return
    await state.update_data(charged_feature="supply")
    await state.set_state(SupplyFlow.waiting)
    await edit_or_answer(call, "Отправьте через ;\nпродаж/день; остаток; в пути; срок поставки дней; покрытие дней; страховой запас дней; рост%; себестоимость; выкуп%\n\nПример: 8; 70; 20; 14; 30; 7; 10; 450; 85", back_kb("finance"))


@router.message(SupplyFlow.waiting, F.text)
async def supply_process(message: Message, state: FSMContext):
    data = await state.get_data()
    try:
        v = [safe_float(x) for x in message.text.split(";")]
        if len(v) != 9: raise ValueError("Нужно 9 значений")
        result = calculate_supply(v[0], int(v[1]), int(v[2]), int(v[3]), int(v[4]), int(v[5]), v[6], v[7], v[8])
        hid = await save_history(message.from_user.id, "supply_plan", message.text, result)
        await message.answer(result, reply_markup=kb([[("📄 Экспорт", f"export_{hid}"), ("🏠 Меню", "home")]]))
    except Exception as exc:
        await refund(message.from_user.id, data.get("charged_feature", "supply"))
        await message.answer(f"❌ {exc}", reply_markup=back_kb("finance"))
    await state.clear()


@router.callback_query(F.data == "promo_calc")
async def promo_calc_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(PromoFlow.waiting)
    await edit_or_answer(call, "Отправьте: текущая цена; скидка%; чистая прибыль без акции; ожидаемый рост продаж%\nПример: 1990; 20; 420; 35", back_kb("finance"))


@router.message(PromoFlow.waiting, F.text)
async def promo_calc_process(message: Message, state: FSMContext):
    try:
        price, discount, profit, growth = [safe_float(x) for x in message.text.split(";")]
        new_price = price * (1 - discount / 100)
        lost = price - new_price
        new_profit = profit - lost
        volume_factor = 1 + growth / 100
        indexed = new_profit * volume_factor
        verdict = "✅ акция может увеличить общую прибыль" if indexed > profit else "❌ прогнозируемый рост не компенсирует скидку"
        result = f"🎯 ПРОВЕРКА АКЦИИ\n\nЦена в акции: {money(new_price)}\nПрибыль с единицы: {money(new_profit)}\nИндекс прибыли при росте продаж: {money(indexed)} против {money(profit)}\n\n{verdict}\n\n⚠️ Упрощённая проверка. Для точного решения используйте Юнит-экономику PRO с новой ценой."
        await message.answer(result, reply_markup=back_kb("finance"))
    except Exception as exc:
        await message.answer(f"❌ {exc}")
    await state.clear()


# --- Reviews and appeals -----------------------------------------------------
@router.callback_query(F.data == "reviews")
async def reviews_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await edit_or_answer(call, "💬 ОТЗЫВЫ И ПОКУПАТЕЛИ", kb([[("⭐ Ответ на отзыв", "review_one"), ("📊 Анализ массива", "review_batch")], [("❓ FAQ из вопросов", "review_faq"), ("🏠 Меню", "home")]]))


@router.callback_query(F.data.in_({"review_one", "review_batch", "review_faq"}))
async def review_start(call: CallbackQuery, state: FSMContext):
    feature = "review_batch" if call.data != "review_one" else "review"
    if not await access_or_paywall(call, feature): return
    await state.update_data(mode=call.data, charged_feature=feature)
    await state.set_state(ReviewFlow.waiting)
    prompt = "Вставьте отзыв и при необходимости контекст заказа." if call.data == "review_one" else "Вставьте отзывы/вопросы, каждый с новой строки. Можно до 18 000 символов."
    await edit_or_answer(call, prompt, back_kb("reviews"))


@router.message(ReviewFlow.waiting, F.text | F.voice | F.audio)
async def review_process(message: Message, state: FSMContext):
    data = await state.get_data()
    try:
        text = await get_message_input(message, message.bot)
        brand = await brand_context(message.from_user.id)
        if data["mode"] == "review_one":
            prompt = f"Отзыв: {text}\nBrand Kit: {brand}\nДай 3 ответа: короткий, тёплый, официальный. На негативе не признавай юридическую вину без фактов; предложи конкретный следующий шаг."
        elif data["mode"] == "review_faq":
            prompt = f"Вопросы покупателей:\n{text}\nСоздай FAQ, предложения для карточки и недостающие слайды инфографики."
        else:
            prompt = f"Отзывы:\n{text}\nКлассифицируй тональность и темы; посчитай частоты приблизительно; выдели новые/критические жалобы; предложи изменения товара, упаковки, карточки, FAQ и ТЗ поставщику; подготовь шаблоны ответов."
        result = await AI.text("Ты руководитель клиентского сервиса маркетплейса. Не спорь с покупателем и не обещай невозможное.", prompt, max_tokens=2600)
        hid = await save_history(message.from_user.id, data["mode"], text, result)
        await answer_long(message, result, kb([[("📄 Экспорт", f"export_{hid}"), ("🏠 Меню", "home")]]))
    except Exception as exc:
        await refund(message.from_user.id, data.get("charged_feature", "review"))
        await message.answer(f"❌ {exc}")
    await state.clear()


@router.callback_query(F.data == "appeals")
async def appeals_menu(call: CallbackQuery, state: FSMContext):
    await state.clear(); await state.set_state(AppealFlow.type)
    await edit_or_answer(call, "🛡 АПЕЛЛЯЦИИ PRO\nВыберите ситуацию:", kb([[("💰 Штраф", "atype_fine"), ("🚫 Блокировка", "atype_block")], [("📦 Поставка", "atype_supply"), ("↩️ Возврат/удержание", "atype_return")], [("⭐ Отзыв", "atype_review"), ("📝 Модерация", "atype_moderation")], [("🏠 Меню", "home")]]))


@router.callback_query(F.data.startswith("atype_"), AppealFlow.type)
async def appeal_type(call: CallbackQuery, state: FSMContext):
    if not await access_or_paywall(call, "appeal"): return
    await state.update_data(appeal_type=call.data[6:], charged_feature="appeal")
    await state.set_state(AppealFlow.waiting)
    await edit_or_answer(call, "Опишите площадку, дату, артикул/поставку, сумму, причину, хронологию, доказательства и желаемое решение. Не отправляйте паспортные данные.", back_kb("appeals"))


@router.message(AppealFlow.waiting, F.text | F.voice | F.audio)
async def appeal_process(message: Message, state: FSMContext):
    data = await state.get_data()
    try:
        text = await get_message_input(message, message.bot)
        result = await AI.text("Ты специалист по обращениям в поддержку маркетплейсов, не адвокат. Не выдумывай нормы и пункты правил. Отмечай необходимость сверить актуальную оферту.", f"""Тип: {data['appeal_type']}
Ситуация: {text}

Подготовь: проверку достаточности данных; краткую и полную апелляцию; хронологию;
аргументы только из фактов; список приложений; формулировку требуемого решения;
следующие шаги при отказе; предупреждения о неподтверждённых утверждениях.""", max_tokens=2300)
        hid = await save_history(message.from_user.id, "appeal", text, result, metadata={"type": data["appeal_type"]})
        await answer_long(message, result, kb([[("📄 Экспорт", f"export_{hid}"), ("🏠 Меню", "home")]]))
    except Exception as exc:
        await refund(message.from_user.id, data.get("charged_feature", "appeal"))
        await message.answer(f"❌ {exc}")
    await state.clear()


# --- Connections and AI director --------------------------------------------
@router.callback_query(F.data == "connections")
async def connections_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    rows = await DB.fetchall("SELECT id,marketplace,name,status,last_sync_at,last_error FROM marketplace_connections WHERE user_id=?", (call.from_user.id,))
    text = "🔌 ПОДКЛЮЧЁННЫЕ КАБИНЕТЫ\n\n"
    if rows:
        for r in rows:
            text += f"• {r['name']} · {r['marketplace'].upper()} · {r['status']}\n"
    else:
        text += "Подключений нет."
    await edit_or_answer(call, text, kb([[("➕ WB", "connect_wb"), ("➕ Ozon", "connect_ozon")], [("🔄 Проверить все", "connections_check"), ("🏠 Меню", "home")]]))


@router.callback_query(F.data == "connect_wb")
async def connect_wb(call: CallbackQuery, state: FSMContext):
    plan, _ = await user_plan(call.from_user.id)
    if PLANS[plan]["shops"] <= 0:
        await call.answer("Подключение кабинета доступно на ПРО и Бизнес", show_alert=True); return
    await state.set_state(ConnectFlow.waiting_wb)
    await edit_or_answer(call, "Отправьте одной строкой: название кабинета; WB API token\n\nТокен будет сохранён в зашифрованном виде при настроенном ENCRYPTION_KEY.", back_kb("connections"))


@router.message(ConnectFlow.waiting_wb, F.text)
async def connect_wb_save(message: Message, state: FSMContext):
    try:
        name, token = [x.strip() for x in message.text.split(";", 1)]
        await MP.wb_validate(token)
        await DB.execute("""INSERT INTO marketplace_connections(user_id,marketplace,name,token_encrypted,status,created_at)
            VALUES(?,?,?,?,?,?) ON CONFLICT(user_id,marketplace,name) DO UPDATE SET token_encrypted=excluded.token_encrypted,status='active',last_error=''""",
            (message.from_user.id, "wb", name, CIPHER.encrypt(token), "active", iso_now()))
        await message.answer(f"✅ WB-кабинет «{name}» подключён. Токен: {redact_token(token)}", reply_markup=back_kb("connections"))
    except Exception as exc:
        await message.answer(f"❌ Не удалось проверить WB API: {exc}", reply_markup=back_kb("connections"))
    await state.clear()


@router.callback_query(F.data == "connect_ozon")
async def connect_ozon(call: CallbackQuery, state: FSMContext):
    plan, _ = await user_plan(call.from_user.id)
    if PLANS[plan]["shops"] <= 0:
        await call.answer("Подключение кабинета доступно на ПРО и Бизнес", show_alert=True); return
    await state.set_state(ConnectFlow.waiting_ozon)
    await edit_or_answer(call, "Отправьте: название кабинета; Client-Id; Api-Key", back_kb("connections"))


@router.message(ConnectFlow.waiting_ozon, F.text)
async def connect_ozon_save(message: Message, state: FSMContext):
    try:
        name, client_id, token = [x.strip() for x in message.text.split(";", 2)]
        await MP.ozon_validate(client_id, token)
        await DB.execute("""INSERT INTO marketplace_connections(user_id,marketplace,name,client_id,token_encrypted,status,created_at)
            VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id,marketplace,name) DO UPDATE SET client_id=excluded.client_id,token_encrypted=excluded.token_encrypted,status='active',last_error=''""",
            (message.from_user.id, "ozon", name, client_id, CIPHER.encrypt(token), "active", iso_now()))
        await message.answer(f"✅ Ozon-кабинет «{name}» подключён.", reply_markup=back_kb("connections"))
    except Exception as exc:
        await message.answer(f"❌ Не удалось проверить Ozon API: {exc}", reply_markup=back_kb("connections"))
    await state.clear()


@router.callback_query(F.data == "connections_check")
async def connections_check(call: CallbackQuery):
    rows = await DB.fetchall("SELECT * FROM marketplace_connections WHERE user_id=?", (call.from_user.id,))
    if not rows:
        await call.answer("Нет подключений", show_alert=True); return
    results = []
    for row in rows:
        try:
            token = CIPHER.decrypt(row["token_encrypted"])
            if row["marketplace"] == "wb":
                cards = await MP.wb_cards(token, 20)
            else:
                cards = await MP.ozon_products(row["client_id"], token, 20)
            await DB.execute("UPDATE marketplace_connections SET status='active',last_sync_at=?,last_error='' WHERE id=?", (iso_now(), row["id"]))
            results.append(f"✅ {row['name']}: доступ работает, получено товаров: {len(cards)}")
        except Exception as exc:
            await DB.execute("UPDATE marketplace_connections SET status='error',last_error=? WHERE id=?", (str(exc)[:500], row["id"]))
            results.append(f"❌ {row['name']}: {exc}")
    await call.answer()
    await call.message.answer("\n".join(results), reply_markup=back_kb("connections"))


@router.callback_query(F.data == "director")
async def director(call: CallbackQuery):
    if not await access_or_paywall(call, "director"): return
    connections = await DB.fetchall("SELECT * FROM marketplace_connections WHERE user_id=? AND status='active'", (call.from_user.id,))
    metrics = await DB.fetchall("SELECT source,metrics_json,created_at FROM imported_metrics WHERE user_id=? ORDER BY created_at DESC LIMIT 3", (call.from_user.id,))
    products = await DB.fetchall("SELECT id,name,price,cost,article,marketplace FROM products WHERE user_id=? AND status='active' LIMIT 50", (call.from_user.id,))
    factual: dict[str, Any] = {"products": products, "imports": []}
    for m in metrics:
        try: factual["imports"].append({"source": m["source"], "rows": json.loads(m["metrics_json"])[:40]})
        except Exception: pass
    api_notes = []
    for c in connections[:4]:
        try:
            token = CIPHER.decrypt(c["token_encrypted"])
            items = await MP.wb_cards(token, 30) if c["marketplace"] == "wb" else await MP.ozon_products(c["client_id"], token, 30)
            api_notes.append({"shop": c["name"], "marketplace": c["marketplace"], "products": items[:30]})
        except Exception as exc:
            api_notes.append({"shop": c["name"], "error": str(exc)})
    factual["api"] = api_notes
    try:
        result = await AI.text(AUDIT_SYSTEM, """Ты AI-директор магазина. На основе только этих данных сформируй:
1) что известно точно; 2) индекс здоровья 0–100 с оговоркой о полноте данных;
3) 5 задач на сегодня; 4) риски; 5) денежный эффект только там, где его можно рассчитать;
6) какие данные подключить; 7) быстрые действия. Не придумывай продажи, CTR и остатки.
Данные:
""" + json.dumps(factual, ensure_ascii=False)[:CFG.max_input_chars], max_tokens=2800)
        await save_history(call.from_user.id, "ai_director", "connected data", result)
        await call.answer()
        await answer_long(call.message, result, back_kb("home"))
    except Exception as exc:
        await refund(call.from_user.id, "director")
        await call.message.answer(f"❌ AI-директор недоступен: {exc}", reply_markup=back_kb("home"))
        await call.answer()


# --- Profile, history, export, tariffs --------------------------------------
@router.callback_query(F.data == "profile")
async def profile(call: CallbackQuery):
    plan, expiry = await user_plan(call.from_user.id)
    bal = await balance(call.from_user.id)
    user = await DB.fetchone("SELECT * FROM users WHERE user_id=?", (call.from_user.id,))
    refs = await DB.fetchone("SELECT COUNT(*) AS c FROM referral_events WHERE inviter_user_id=?", (call.from_user.id,))
    text = f"""👤 ПРОФИЛЬ

Тариф: {PLANS[plan]['name']}
Действует до: {expiry.strftime('%d.%m.%Y') if expiry else '—'}
Кредиты: {bal.get('credits',0)}
Изображения: {bal.get('image_credits',0)}
Всего операций: {bal.get('lifetime_used',0)}
Приглашено: {(refs or {}).get('c',0)}
Отчёт: {'включён' if user and user['notifications_enabled'] else 'выключен'}"""
    await edit_or_answer(call, text, kb([[("📂 История", "history"), ("👥 Реферальная ссылка", "referral")], [("🎁 Промокод", "promo"), ("💳 Тарифы", "tariffs")], [("🏠 Меню", "home")]]))


@router.callback_query(F.data == "history")
async def history_list(call: CallbackQuery):
    rows = await DB.fetchall("SELECT id,feature,created_at FROM history WHERE user_id=? ORDER BY created_at DESC LIMIT 15", (call.from_user.id,))
    buttons = [[(f"{r['feature']} · {parse_dt(r['created_at']).strftime('%d.%m %H:%M')}", f"history_{r['id']}")] for r in rows]
    buttons.append([("⬅️ Профиль", "profile"), ("🏠 Меню", "home")])
    await edit_or_answer(call, "📂 ИСТОРИЯ\n\n" + ("Выберите результат:" if rows else "История пока пуста."), kb(buttons))


@router.callback_query(F.data.startswith("history_"))
async def history_view(call: CallbackQuery):
    hid = int(call.data.split("_")[1])
    row = await DB.fetchone("SELECT * FROM history WHERE id=? AND user_id=?", (hid, call.from_user.id))
    if not row:
        await call.answer("Не найдено", show_alert=True); return
    await call.answer()
    await answer_long(call.message, row["result"], kb([[("📄 Экспорт", f"export_{hid}"), ("⬅️ История", "history")]]))


@router.callback_query(F.data.startswith("export_"))
async def export_history(call: CallbackQuery):
    hid = int(call.data.split("_")[1])
    row = await DB.fetchone("SELECT * FROM history WHERE id=? AND user_id=?", (hid, call.from_user.id))
    if not row:
        await call.answer("Не найдено", show_alert=True); return
    content = f"МАРКЕТПРО PREMIUM\nФункция: {row['feature']}\nДата: {row['created_at']}\n\n{row['result']}"
    await call.message.answer_document(BufferedInputFile(content.encode("utf-8"), filename=f"marketpro_{hid}.txt"), caption="Экспорт результата")
    await call.answer()


@router.callback_query(F.data == "referral")
async def referral(call: CallbackQuery):
    link = f"https://t.me/{CFG.bot_username}?start=ref_{call.from_user.id}"
    await edit_or_answer(call, f"👥 РЕФЕРАЛЬНАЯ ПРОГРАММА\n\nВаша ссылка:\n{link}\n\nВы получаете +{CFG.referral_inviter} кредитов, друг +{CFG.referral_invited}. Повторное начисление защищено уникальным событием регистрации.", back_kb("profile"))


@router.callback_query(F.data == "promo")
async def promo_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(PromoCodeFlow.waiting)
    await state.update_data(promo_mode=True)
    await edit_or_answer(call, "Введите промокод:", back_kb("profile"))


@router.message(PromoCodeFlow.waiting, F.text)
async def promo_use(message: Message, state: FSMContext):
    data = await state.get_data()
    if not data.get("promo_mode"):
        return
    code = message.text.strip().upper()
    row = await DB.fetchone("SELECT * FROM promo_codes WHERE code=? AND active=1", (code,))
    if not row:
        await message.answer("❌ Промокод не найден.", reply_markup=back_kb("profile")); await state.clear(); return
    expiry = parse_dt(row["expires_at"])
    if expiry and expiry < now() or row["max_uses"] and row["uses"] >= row["max_uses"]:
        await message.answer("❌ Промокод недействителен."); await state.clear(); return
    try:
        await DB.transaction([
            ("INSERT INTO promo_uses(user_id,code,used_at) VALUES(?,?,?)", (message.from_user.id, code, iso_now())),
            ("UPDATE promo_codes SET uses=uses+1 WHERE code=?", (code,)),
            ("UPDATE balances SET credits=credits+? WHERE user_id=?", (row["bonus_credits"], message.from_user.id)),
        ])
        await message.answer(f"✅ Начислено +{row['bonus_credits']} кредитов.", reply_markup=back_kb("profile"))
    except sqlite3.IntegrityError:
        await message.answer("Этот промокод уже использован.")
    await state.clear()


@router.callback_query(F.data == "tariffs")
async def tariffs(call: CallbackQuery):
    text = "💳 ТАРИФЫ\n\n"
    for key in ("start", "pro", "business"):
        p = PLANS[key]
        text += f"{p['name']} — {p['price']} ₽ / 30 дней\n{p['credits']} кредитов · {p['images']} изображений · {p['products']} товаров · {p['shops']} кабинетов\n\n"
    await edit_or_answer(call, text + "Кредиты используются вместо опасного «безлимита» и защищают экономику сервиса.", kb([[("490 ₽ Старт", "pay_start"), ("990 ₽ ПРО", "pay_pro")], [("1990 ₽ Бизнес", "pay_business")], [("🏠 Меню", "home")]]))


@router.callback_query(F.data.startswith("pay_"))
async def create_payment(call: CallbackQuery):
    plan = call.data[4:]
    if plan not in PLANS or plan == "free":
        return
    if not Payment or not CFG.yookassa_shop_id or not CFG.yookassa_secret:
        await call.answer("YooKassa не настроена", show_alert=True); return
    try:
        p = await asyncio.to_thread(Payment.create, {
            "amount": {"value": f"{PLANS[plan]['price']}.00", "currency": "RUB"},
            "confirmation": {"type": "redirect", "return_url": CFG.payment_return_url},
            "capture": True,
            "description": f"МаркетПРО {PLANS[plan]['name']} — 30 дней",
            "metadata": {"user_id": str(call.from_user.id), "plan": plan},
        }, str(uuid.uuid4()))
        await DB.execute("INSERT INTO payments(payment_id,user_id,plan,amount,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                         (p.id, call.from_user.id, plan, PLANS[plan]["price"], "pending", iso_now(), iso_now()))
        await call.message.answer(f"Тариф {PLANS[plan]['name']} — {PLANS[plan]['price']} ₽", reply_markup=kb([[("💳 Оплатить", f"url:{p.confirmation.confirmation_url}")], [("🏠 Меню", "home")]]))
        await call.answer()
    except Exception as exc:
        log.exception("payment create")
        await call.answer("Ошибка создания платежа", show_alert=True)
        await call.message.answer(f"Напишите в поддержку: {CFG.support_url}\nТехническая информация: {str(exc)[:200]}")


# --- Admin ------------------------------------------------------------------
@router.message(Command("stats"))
async def admin_stats(message: Message):
    if message.from_user.id != CFG.owner_id: return
    users = await DB.fetchone("SELECT COUNT(*) c FROM users")
    paid = await DB.fetchone("SELECT COUNT(*) c FROM subscriptions WHERE plan!='free' AND expires_at>?", (iso_now(),))
    revenue = await DB.fetchone("SELECT COALESCE(SUM(amount),0) s FROM payments WHERE status='succeeded'")
    operations = await DB.fetchone("SELECT COUNT(*) c FROM history")
    await message.answer(f"📊 МаркетПРО\nПользователи: {users['c']}\nАктивные подписки: {paid['c']}\nУспешные платежи: {money(revenue['s'])}\nРезультаты: {operations['c']}")


@router.message(Command("give"))
async def admin_give(message: Message):
    if message.from_user.id != CFG.owner_id: return
    parts = message.text.split()
    try:
        uid = int(parts[1]); kind = parts[2]
        if kind in PLANS and kind != "free":
            await ensure_user(uid); await activate_plan(uid, kind); await message.answer("✅ Тариф выдан")
        elif kind == "credits":
            amount = int(parts[3]); await DB.execute("UPDATE balances SET credits=credits+? WHERE user_id=?", (amount, uid)); await message.answer("✅ Кредиты начислены")
        else: raise ValueError
    except Exception:
        await message.answer("Формат: /give USER_ID pro | /give USER_ID credits 100")


@router.message(Command("promo_create"))
async def admin_promo_create(message: Message):
    if message.from_user.id != CFG.owner_id: return
    try:
        _, code, credits, max_uses = message.text.split()
        await DB.execute("INSERT OR REPLACE INTO promo_codes(code,bonus_credits,max_uses,active) VALUES(?,?,?,1)", (code.upper(), int(credits), int(max_uses)))
        await message.answer("✅ Промокод создан")
    except Exception:
        await message.answer("Формат: /promo_create CODE CREDITS MAX_USES")


@router.message(Command("broadcast"))
async def broadcast_start(message: Message, state: FSMContext):
    if message.from_user.id != CFG.owner_id: return
    await state.set_state(BroadcastFlow.waiting)
    await message.answer("Отправьте текст рассылки. Для отмены /cancel")


@router.message(BroadcastFlow.waiting, F.text)
async def broadcast_process(message: Message, state: FSMContext):
    if message.from_user.id != CFG.owner_id: return
    users = await DB.fetchall("SELECT user_id FROM users WHERE is_blocked=0")
    ok = fail = 0
    for item in users:
        try:
            await message.bot.send_message(item["user_id"], message.text)
            ok += 1
        except (TelegramForbiddenError, TelegramBadRequest):
            fail += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.04)
    await state.clear(); await message.answer(f"Рассылка: {ok} успешно, {fail} ошибок")


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext):
    await state.clear(); await message.answer("Действие отменено.", reply_markup=main_kb())


# Fallbacks
@router.message(F.text)
async def fallback_text(message: Message, state: FSMContext):
    if await state.get_state() is None:
        await message.answer("Выберите задачу из меню. Для отмены текущего шага используйте /cancel.", reply_markup=main_kb())


@router.message(F.photo)
async def fallback_photo(message: Message, state: FSMContext):
    if await state.get_state() is None:
        await message.answer("Фото можно использовать в «Карточка 360°» или «Аудит скриншота».", reply_markup=main_kb())


# ============================================================================
# BACKGROUND TASKS
# ============================================================================

async def payment_watcher(bot: Bot):
    if not Payment or not CFG.yookassa_shop_id or not CFG.yookassa_secret:
        return
    while True:
        await asyncio.sleep(20)
        rows = await DB.fetchall("SELECT * FROM payments WHERE status='pending' ORDER BY created_at LIMIT 100")
        for row in rows:
            try:
                p = await asyncio.to_thread(Payment.find_one, row["payment_id"])
                if p.status == "succeeded":
                    await activate_plan(row["user_id"], row["plan"])
                    await DB.execute("UPDATE payments SET status='succeeded',updated_at=? WHERE payment_id=?", (iso_now(), row["payment_id"]))
                    # One-time paid referral reward.
                    ref = await DB.fetchone("SELECT * FROM referral_events WHERE invited_user_id=? AND paid_rewarded=0", (row["user_id"],))
                    if ref:
                        await DB.transaction([
                            ("UPDATE referral_events SET paid_rewarded=1 WHERE id=?", (ref["id"],)),
                            ("UPDATE balances SET credits=credits+15 WHERE user_id=?", (ref["inviter_user_id"],)),
                        ])
                    await bot.send_message(row["user_id"], f"✅ Тариф {PLANS[row['plan']]['name']} активирован на 30 дней.", reply_markup=main_kb())
                elif p.status == "canceled":
                    await DB.execute("UPDATE payments SET status='canceled',updated_at=? WHERE payment_id=?", (iso_now(), row["payment_id"]))
            except Exception as exc:
                log.error("Payment check %s: %s", row["payment_id"], exc)


async def daily_report_worker(bot: Bot):
    sent_date: dict[int, str] = {}
    while True:
        await asyncio.sleep(60)
        current = now()
        users = await DB.fetchall("SELECT user_id,daily_report_hour,notifications_enabled FROM users WHERE notifications_enabled=1 AND is_blocked=0")
        for u in users:
            uid = u["user_id"]
            if current.hour != u["daily_report_hour"] or sent_date.get(uid) == current.date().isoformat():
                continue
            recommendations = await DB.fetchall("SELECT * FROM recommendations WHERE user_id=? AND status='new' ORDER BY CASE severity WHEN 'critical' THEN 1 WHEN 'high' THEN 2 ELSE 3 END LIMIT 5", (uid,))
            if not recommendations:
                continue
            text = "🤖 ЗАДАЧИ НА СЕГОДНЯ\n\n" + "\n\n".join(f"{r['severity'].upper()}: {r['title']}\n{r['description']}" for r in recommendations)
            try:
                await bot.send_message(uid, text, reply_markup=main_kb())
                sent_date[uid] = current.date().isoformat()
            except Exception:
                pass


async def global_error_handler(event, exception):
    log.exception("Unhandled update error: %s", exception)


async def main() -> None:
    if not CFG.bot_token:
        raise RuntimeError("BOT_TOKEN не задан")
    await DB.init()
    if CFG.redis_url and RedisStorage:
        storage = RedisStorage.from_url(CFG.redis_url)
        log.info("FSM: Redis")
    else:
        storage = MemoryStorage()
        log.warning("FSM: MemoryStorage. Для production задайте REDIS_URL.")
    bot = Bot(CFG.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=storage)
    dp.include_router(router)
    asyncio.create_task(payment_watcher(bot))
    asyncio.create_task(daily_report_worker(bot))
    log.info("МаркетПРО Premium запущен")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
