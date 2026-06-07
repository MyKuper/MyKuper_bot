#!/usr/bin/env python3
"""
MyKuper Bot - Улучшенная версия (Redis опционален)
"""

import os
import re
import csv
import io
import json
import time
import logging
import asyncio
import datetime
from typing import Optional, Dict, List, Any, Tuple
from dataclasses import dataclass, field
from collections import defaultdict

import asyncpg
from aiogram import Bot, Dispatcher, Router, F, BaseMiddleware
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, 
    InlineKeyboardButton, BufferedInputFile, FSInputFile
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiohttp import web
import coc
from prettytable import PrettyTable
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ============================================================
# 🔴 ВАЖНО: Redis теперь опционален!
# ============================================================
REDIS_AVAILABLE = False
aioredis = None
RedisStorage = None

try:
    from redis import asyncio as _aioredis
    from aiogram.fsm.storage.redis import RedisStorage as _RedisStorage
    aioredis = _aioredis
    RedisStorage = _RedisStorage
    REDIS_AVAILABLE = True
    print("✅ Redis модуль найден")
except ImportError:
    print("⚠️ Redis не установлен - используем in-memory хранилище")

# ============================================================
# ⚙️ КОНФИГУРАЦИЯ
# ============================================================
@dataclass
class Config:
    TELEGRAM_TOKEN: str = os.getenv('TELEGRAM_TOKEN', '')
    ADMIN_IDS: List[int] = field(default_factory=lambda: [
        int(x) for x in os.getenv('ADMIN_IDS', '0').split(',') if x.strip()
    ])
    COC_EMAIL: str = os.getenv('COC_EMAIL', '')
    COC_PASSWORD: str = os.getenv('COC_PASSWORD', '')
    COC_PROXY: Optional[str] = os.getenv('COC_PROXY', None)
    DATABASE_URL: str = os.getenv('DATABASE_URL', '')
    REDIS_URL: str = os.getenv('REDIS_URL', 'redis://localhost:6379')
    PORT: int = int(os.getenv('PORT', 8080))
    WEBHOOK_URL: Optional[str] = os.getenv('RENDER_EXTERNAL_URL', None)
    
    RATE_LIMITS: Dict[str, Tuple[int, int]] = field(default_factory=lambda: {
        "war_plan": (1, 10),
        "war_report": (1, 30),
        "default": (20, 60),
    })
    
    CACHE_TTL: Dict[str, int] = field(default_factory=lambda: {
        "clan_info": 300,
        "war_info": 120,
        "player_info": 600,
        "cwl_info": 900,
    })

CONFIG = Config()

# ============================================================
# 🛠 ЛОГИРОВАНИЕ
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("MyKuperBot")

# ============================================================
# 🔧 УТИЛИТЫ
# ============================================================
class Validators:
    @staticmethod
    def validate_coc_tag(tag: str) -> bool:
        if not tag:
            return False
        tag = tag.upper().lstrip('#')
        if not re.match(r'^[0-9A-Z]+$', tag):
            return False
        return 3 <= len(tag) <= 15
    
    @staticmethod
    def normalize_tag(tag: str) -> str:
        tag = tag.upper().strip()
        if not tag.startswith('#'):
            tag = '#' + tag
        return tag


# ============================================================
# 💾 КЭШИРОВАНИЕ (работает БЕЗ Redis!)
# ============================================================
class CacheService:
    """In-memory кэш (работает всегда, даже без Redis)"""
    
    def __init__(self, redis_client=None):
        self.redis = redis_client
        self.memory_cache: Dict[str, Tuple[Any, float]] = {}
        self.ttl = CONFIG.CACHE_TTL
    
    async def get(self, key: str) -> Optional[Any]:
        try:
            if self.redis:
                data = await self.redis.get(key)
                if data:
                    return json.loads(data)
            else:
                if key in self.memory_cache:
                    value, expire_at = self.memory_cache[key]
                    if time.time() < expire_at:
                        return value
                    else:
                        del self.memory_cache[key]
        except Exception as e:
            logger.warning(f"Cache get error: {e}")
        return None
    
    async def set(self, key: str, value: Any, ttl_key: str = "default"):
        try:
            ttl = self.ttl.get(ttl_key, 300)
            
            if self.redis:
                await self.redis.setex(key, ttl, json.dumps(value, default=str))
            else:
                self.memory_cache[key] = (value, time.time() + ttl)
        except Exception as e:
            logger.warning(f"Cache set error: {e}")
    
    async def close(self):
        if self.redis:
            try:
                await self.redis.close()
            except:
                pass


# ============================================================
# 🗄️ БАЗА ДАННЫХ
# ============================================================
class Database:
    def __init__(self):
        self.pool: Optional[asyncpg.Pool] = None
    
    async def init(self):
        if not CONFIG.DATABASE_URL:
            logger.error("❌ DATABASE_URL не задан!")
            return False
        
        try:
            self.pool = await asyncpg.create_pool(
                CONFIG.DATABASE_URL, min_size=2, max_size=10
            )
            logger.info("✅ Подключение к PostgreSQL установлено")
            await self._create_tables()
            return True
        except Exception as e:
            logger.error(f"❌ Ошибка инициализации БД: {e}")
            return False
    
    async def _create_tables(self):
        async with self.pool.acquire() as conn:
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    tg_id BIGINT PRIMARY KEY,
                    tg_username TEXT,
                    is_admin BOOLEAN DEFAULT FALSE,
                    is_banned BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS linked_accounts (
                    id SERIAL PRIMARY KEY,
                    tg_id BIGINT REFERENCES users(tg_id),
                    player_tag TEXT NOT NULL,
                    player_name TEXT NOT NULL,
                    clan_tag TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(tg_id, player_tag)
                )
            ''')
            
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS user_active_clan (
                    tg_id BIGINT PRIMARY KEY REFERENCES users(tg_id),
                    clan_tag TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS user_settings (
                    tg_id BIGINT PRIMARY KEY REFERENCES users(tg_id),
                    war_reminders BOOLEAN DEFAULT TRUE,
                    reminder_4h BOOLEAN DEFAULT TRUE,
                    reminder_1h BOOLEAN DEFAULT TRUE,
                    language TEXT DEFAULT 'ru',
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_linked_accounts_tg_id ON linked_accounts(tg_id)')
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_linked_accounts_clan_tag ON linked_accounts(clan_tag)')
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_user_active_clan_clan_tag ON user_active_clan(clan_tag)')
            
            logger.info("✅ Таблицы БД созданы/проверены")
    
    async def close(self):
        if self.pool:
            await self.pool.close()


# ============================================================
# 📦 РЕПОЗИТОРИИ
# ============================================================
class UserRepository:
    def __init__(self, db: Database):
        self.db = db
    
    async def upsert_user(self, tg_id: int, username: Optional[str] = None):
        if not self.db.pool: return
        async with self.db.pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO users (tg_id, tg_username, last_active) 
                VALUES ($1, $2, CURRENT_TIMESTAMP)
                ON CONFLICT (tg_id) DO UPDATE SET 
                    tg_username = EXCLUDED.tg_username,
                    last_active = CURRENT_TIMESTAMP
            ''', tg_id, username)
    
    async def is_banned(self, tg_id: int) -> bool:
        if not self.db.pool: return False
        async with self.db.pool.acquire() as conn:
            row = await conn.fetchrow('SELECT is_banned FROM users WHERE tg_id = $1', tg_id)
            return row['is_banned'] if row else False
    
    async def ban_user(self, tg_id: int):
        if not self.db.pool: return
        async with self.db.pool.acquire() as conn:
            await conn.execute('UPDATE users SET is_banned = TRUE WHERE tg_id = $1', tg_id)
    
    async def unban_user(self, tg_id: int):
        if not self.db.pool: return
        async with self.db.pool.acquire() as conn:
            await conn.execute('UPDATE users SET is_banned = FALSE WHERE tg_id = $1', tg_id)
    
    async def get_all_active_users(self) -> List[int]:
        if not self.db.pool: return []
        async with self.db.pool.acquire() as conn:
            rows = await conn.fetch('SELECT tg_id FROM users WHERE is_banned = FALSE')
            return [row['tg_id'] for row in rows]
    
    async def get_statistics(self) -> Dict[str, int]:
        if not self.db.pool: return {}
        async with self.db.pool.acquire() as conn:
            return {
                'total_users': await conn.fetchval('SELECT COUNT(*) FROM users WHERE is_banned = FALSE') or 0,
                'total_accounts': await conn.fetchval('SELECT COUNT(*) FROM linked_accounts') or 0,
                'active_clans': await conn.fetchval('SELECT COUNT(DISTINCT clan_tag) FROM user_active_clan') or 0,
            }


class AccountRepository:
    def __init__(self, db: Database):
        self.db = db
    
    async def link_account(self, tg_id: int, username: str, player_tag: str, player_name: str, clan_tag: str) -> bool:
        if not self.db.pool: return False
        try:
            async with self.db.pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute('''
                        INSERT INTO users (tg_id, tg_username) VALUES ($1, $2)
                        ON CONFLICT (tg_id) DO UPDATE SET tg_username = EXCLUDED.tg_username
                    ''', tg_id, username)
                    await conn.execute('''
                        INSERT INTO linked_accounts (tg_id, player_tag, player_name, clan_tag)
                        VALUES ($1, $2, $3, $4)
                        ON CONFLICT (tg_id, player_tag) DO UPDATE SET 
                            player_name = EXCLUDED.player_name, clan_tag = EXCLUDED.clan_tag
                    ''', tg_id, player_tag, player_name, clan_tag)
                    await conn.execute('''
                        INSERT INTO user_active_clan (tg_id, clan_tag) VALUES ($1, $2)
                        ON CONFLICT (tg_id) DO UPDATE SET clan_tag = EXCLUDED.clan_tag, updated_at = CURRENT_TIMESTAMP
                    ''', tg_id, clan_tag)
                    return True
        except Exception as e:
            logger.error(f"❌ Ошибка привязки: {e}")
            return False
    
    async def unlink_account(self, tg_id: int, player_tag: str) -> bool:
        if not self.db.pool: return False
        try:
            async with self.db.pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute('DELETE FROM linked_accounts WHERE tg_id = $1 AND player_tag = $2', tg_id, player_tag)
                    row = await conn.fetchrow('SELECT clan_tag FROM linked_accounts WHERE tg_id = $1 LIMIT 1', tg_id)
                    if row:
                        await conn.execute('''
                            INSERT INTO user_active_clan (tg_id, clan_tag) VALUES ($1, $2)
                            ON CONFLICT (tg_id) DO UPDATE SET clan_tag = EXCLUDED.clan_tag, updated_at = CURRENT_TIMESTAMP
                        ''', tg_id, row['clan_tag'])
                    else:
                        await conn.execute('DELETE FROM user_active_clan WHERE tg_id = $1', tg_id)
                    return True
        except Exception as e:
            logger.error(f"❌ Ошибка отвязки: {e}")
            return False
    
    async def get_user_accounts(self, tg_id: int) -> List[dict]:
        if not self.db.pool: return []
        try:
            async with self.db.pool.acquire() as conn:
                rows = await conn.fetch('SELECT player_tag, player_name, clan_tag FROM linked_accounts WHERE tg_id = $1', tg_id)
                return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"❌ Ошибка: {e}")
            return []
    
    async def get_linked_accounts_mapping(self, clan_tag: Optional[str] = None) -> Dict[str, str]:
        if not self.db.pool: return {}
        try:
            query = """
                SELECT la.player_tag, u.tg_username
                FROM linked_accounts la
                JOIN users u ON la.tg_id = u.tg_id
                WHERE u.tg_username IS NOT NULL AND u.tg_username != ''
            """
            params = []
            if clan_tag:
                query += " AND la.clan_tag = $1"
                params.append(clan_tag)
            async with self.db.pool.acquire() as conn:
                rows = await conn.fetch(query, *params)
                return {row['player_tag']: f"@{row['tg_username']}" for row in rows}
        except Exception as e:
            logger.error(f"❌ Ошибка: {e}")
            return {}


class ClanRepository:
    def __init__(self, db: Database):
        self.db = db
    
    async def set_active_clan(self, tg_id: int, clan_tag: str) -> bool:
        if not self.db.pool: return False
        try:
            async with self.db.pool.acquire() as conn:
                await conn.execute('''
                    INSERT INTO user_active_clan (tg_id, clan_tag) VALUES ($1, $2)
                    ON CONFLICT (tg_id) DO UPDATE SET clan_tag = EXCLUDED.clan_tag, updated_at = CURRENT_TIMESTAMP
                ''', tg_id, clan_tag)
                return True
        except Exception as e:
            logger.error(f"❌ Ошибка: {e}")
            return False
    
    async def clear_active_clan(self, tg_id: int) -> bool:
        if not self.db.pool: return False
        try:
            async with self.db.pool.acquire() as conn:
                await conn.execute('DELETE FROM user_active_clan WHERE tg_id = $1', tg_id)
                return True
        except Exception as e:
            logger.error(f"❌ Ошибка: {e}")
            return False
    
    async def get_active_clan(self, tg_id: int) -> Optional[str]:
        if not self.db.pool: return None
        try:
            async with self.db.pool.acquire() as conn:
                row = await conn.fetchrow('SELECT clan_tag FROM user_active_clan WHERE tg_id = $1', tg_id)
                return row['clan_tag'] if row else None
        except Exception as e:
            logger.error(f"❌ Ошибка: {e}")
            return None
    
    async def get_clans_with_active_wars(self) -> List[str]:
        if not self.db.pool: return []
        try:
            async with self.db.pool.acquire() as conn:
                rows = await conn.fetch('SELECT DISTINCT clan_tag FROM user_active_clan')
                return [row['clan_tag'] for row in rows]
        except Exception as e:
            logger.error(f"❌ Ошибка: {e}")
            return []


class SettingsRepository:
    def __init__(self, db: Database):
        self.db = db
    
    async def get_user_settings(self, tg_id: int) -> dict:
        default = {'tg_id': 0, 'war_reminders': True, 'reminder_4h': True, 'reminder_1h': True, 'language': 'ru'}
        if not self.db.pool: return default
        try:
            async with self.db.pool.acquire() as conn:
                row = await conn.fetchrow('SELECT * FROM user_settings WHERE tg_id = $1', tg_id)
                if row:
                    return dict(row)
                else:
                    await conn.execute('INSERT INTO user_settings (tg_id) VALUES ($1) ON CONFLICT (tg_id) DO NOTHING', tg_id)
                    return default
        except Exception as e:
            logger.error(f"❌ Ошибка: {e}")
            return default
    
    async def update_setting(self, tg_id: int, key: str, value: Any) -> bool:
        if not self.db.pool: return False
        try:
            async with self.db.pool.acquire() as conn:
                await conn.execute(f'''
                    INSERT INTO user_settings (tg_id, {key}) VALUES ($1, $2)
                    ON CONFLICT (tg_id) DO UPDATE SET {key} = EXCLUDED.{key}, updated_at = CURRENT_TIMESTAMP
                ''', tg_id, value)
                return True
        except Exception as e:
            logger.error(f"❌ Ошибка: {e}")
            return False


# ============================================================
# 🎮 COC СЕРВИС
# ============================================================
class COCService:
    def __init__(self, client: coc.Client, cache: CacheService):
        self.client = client
        self.cache = cache
    
    async def get_clan(self, clan_tag: str) -> Optional[coc.Clan]:
        cache_key = f"clan:{clan_tag}"
        cached = await self.cache.get(cache_key)
        if cached:
            return coc.Clan(data=cached, client=self.client)
        try:
            clan = await self.client.get_clan(clan_tag)
            await self.cache.set(cache_key, clan._raw_data, "clan_info")
            return clan
        except coc.NotFound:
            return None
    
    async def get_player(self, player_tag: str) -> Optional[coc.Player]:
        cache_key = f"player:{player_tag}"
        cached = await self.cache.get(cache_key)
        if cached:
            return coc.Player(data=cached, client=self.client)
        try:
            player = await self.client.get_player(player_tag)
            await self.cache.set(cache_key, player._raw_data, "player_info")
            return player
        except coc.NotFound:
            return None
    
    async def get_current_war(self, clan_tag: str) -> Optional[coc.ClanWar]:
        cache_key = f"war:{clan_tag}"
        cached = await self.cache.get(cache_key)
        if cached:
            return coc.ClanWar(data=cached, client=self.client)
        try:
            war = await self.client.get_current_war(clan_tag)
            if war and war.state != "notInWar":
                await self.cache.set(cache_key, war._raw_data, "war_info")
            return war
        except coc.PrivateWarLog:
            raise
    
    async def get_league_group(self, clan_tag: str) -> Optional[coc.LeagueGroup]:
        cache_key = f"cwl:{clan_tag}"
        cached = await self.cache.get(cache_key)
        if cached:
            return coc.LeagueGroup(data=cached, client=self.client)
        try:
            league = await self.client.get_league_group(clan_tag)
            await self.cache.set(cache_key, league._raw_data, "cwl_info")
            return league
        except Exception as e:
            logger.error(f"CWL error: {e}")
            return None
    
    async def get_league_war(self, war_tag: str) -> Optional[coc.ClanWar]:
        try:
            return await self.client.get_league_war(war_tag)
        except Exception as e:
            logger.error(f"League war error: {e}")
            return None
    
    async def get_war_log(self, clan_tag: str, limit: int = 10):
        try:
            return await self.client.get_war_log(clan_tag, limit=limit)
        except coc.PrivateWarLog:
            raise


# ============================================================
# 🔐 MIDDLEWARE
# ============================================================
class LoggingMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user_id = event.from_user.id if hasattr(event, 'from_user') else None
        if hasattr(event, 'text') and event.text:
            logger.info(f"📥 {user_id}: {event.text[:50]}")
        elif hasattr(event, 'data') and event.data:
            logger.info(f"🔘 {user_id}: {event.data}")
        return await handler(event, data)


class RateLimitMiddleware(BaseMiddleware):
    def __init__(self):
        self.limits = CONFIG.RATE_LIMITS
        self.user_requests: Dict[str, List[float]] = defaultdict(list)
    
    async def __call__(self, handler, event, data):
        user_id = event.from_user.id if hasattr(event, 'from_user') else None
        if not user_id:
            return await handler(event, data)
        
        action = "default"
        if hasattr(event, 'data') and event.data:
            action = event.data
        elif hasattr(event, 'text') and event.text and event.text.startswith('/'):
            action = event.text.split('@')[0][1:]
        
        limit, window = self.limits.get(action, self.limits["default"])
        now = time.time()
        key = f"{user_id}:{action}"
        self.user_requests[key] = [t for t in self.user_requests[key] if now - t < window]
        
        if len(self.user_requests[key]) >= limit:
            if hasattr(event, 'answer'):
                await event.answer("⏳ Подождите...")
            return
        
        self.user_requests[key].append(now)
        return await handler(event, data)


class ThrottleMiddleware(BaseMiddleware):
    def __init__(self):
        self.last_clicks: Dict[str, float] = {}
    
    async def __call__(self, handler, event, data):
        if isinstance(event, CallbackQuery):
            key = f"{event.from_user.id}:{event.data}"
            now = time.time()
            if now - self.last_clicks.get(key, 0) < 1.5:
                await event.answer()
                return
            self.last_clicks[key] = now
        return await handler(event, data)


class BanCheckMiddleware(BaseMiddleware):
    def __init__(self, user_repo: UserRepository):
        self.user_repo = user_repo
    
    async def __call__(self, handler, event, data):
        user_id = event.from_user.id if hasattr(event, 'from_user') else None
        if user_id and await self.user_repo.is_banned(user_id):
            if hasattr(event, 'answer'):
                await event.answer("🚫 Вы заблокированы")
            return
        return await handler(event, data)


# ============================================================
# ⌨️ КЛАВИАТУРЫ
# ============================================================
class Keyboards:
    @staticmethod
    def main_menu():
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⚔️ ВОЙНА", callback_data="menu_war")],
            [InlineKeyboardButton(text="🏆 CWL", callback_data="menu_cwl")],
            [InlineKeyboardButton(text="🏰 КЛАН", callback_data="menu_clan")],
            [InlineKeyboardButton(text="👤 ПРОФИЛЬ", callback_data="menu_profile")],
            [InlineKeyboardButton(text="🔗 Привязать | 🎯 Клан", callback_data="link_account")],
        ])
    
    @staticmethod
    def war_menu():
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🧠 AI План", callback_data="war_plan")],
            [InlineKeyboardButton(text="📋 Отчет (.md)", callback_data="war_report")],
            [InlineKeyboardButton(text="📜 История войн", callback_data="war_history")],
            [InlineKeyboardButton(text="⏰ Кто не атаковал", callback_data="remind_full")],
            [InlineKeyboardButton(text="📊 Лог атак", callback_data="attack_logs")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_main")],
        ])
    
    @staticmethod
    def cwl_menu():
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🏆 Текущая лига", callback_data="cwl_current")],
            [InlineKeyboardButton(text="👥 Состав группы", callback_data="cwl_group")],
            [InlineKeyboardButton(text="⭐ Звезды участников", callback_data="cwl_stars")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_main")],
        ])
    
    @staticmethod
    def clan_menu():
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="ℹ️ Инфо", callback_data="clan_info")],
            [InlineKeyboardButton(text="👥 Участники", callback_data="clan_members")],
            [InlineKeyboardButton(text="🎁 Пожертвования", callback_data="clan_donations")],
            [InlineKeyboardButton(text="🏰 Столица", callback_data="clan_capital")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_main")],
        ])
    
    @staticmethod
    def profile_menu():
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📊 Моя статистика", callback_data="my_stats")],
            [InlineKeyboardButton(text="🎮 Мои аккаунты", callback_data="my_accounts")],
            [InlineKeyboardButton(text="⚙️ Настройки", callback_data="settings_menu")],
            [InlineKeyboardButton(text="📤 Экспорт статистики", callback_data="export_stats")],
            [InlineKeyboardButton(text="🗑 Удалить активный клан", callback_data="clear_active_clan")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_main")],
        ])
    
    @staticmethod
    def settings_menu():
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔔 Напоминания о войне", callback_data="toggle_war_reminders")],
            [InlineKeyboardButton(text="⏰ За 4 часа", callback_data="toggle_reminder_4h")],
            [InlineKeyboardButton(text="⏰ За 1 час", callback_data="toggle_reminder_1h")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="menu_profile")],
        ])
    
    @staticmethod
    def back(dest: str = "back_main"):
        return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data=dest)]])
    
    @staticmethod
    def confirm(action: str):
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да", callback_data=f"{action}_yes")],
            [InlineKeyboardButton(text="❌ Нет", callback_data=f"{action}_no")]
        ])


# ============================================================
# 🔄 FSM
# ============================================================
class LinkAccount(StatesGroup):
    waiting_for_tag = State()
    waiting_for_confirmation = State()

class SetClan(StatesGroup):
    waiting_for_clan_tag = State()
    waiting_for_clan_confirmation = State()


# ============================================================
# 🤖 РОУТЕРЫ
# ============================================================
common_router = Router()
war_router = Router()
cwl_router = Router()
clan_router = Router()
profile_router = Router()
admin_router = Router()
link_router = Router()


# --- COMMON ---
@common_router.message(Command("start"))
async def cmd_start(message: Message):
    if message.from_user.is_bot: return
    tg_id = message.from_user.id
    await message.bot.user_repo.upsert_user(tg_id, message.from_user.username)
    
    active_clan = await message.bot.clan_repo.get_active_clan(tg_id)
    text = f"👋 Привет, {message.from_user.first_name}!\n\n"
    
    if active_clan:
        text += f"🏰 Клан: <code>{active_clan}</code>\n\nВыбери раздел:"
        await message.answer(text, parse_mode="HTML", reply_markup=Keyboards.main_menu())
    else:
        text += "⚠️ Клан не выбран.\n\n🔗 /link - привязать\n🎯 /set_clan - указать клан"
        await message.answer(text, parse_mode="HTML")

@common_router.message(Command("help"))
async def cmd_help(message: Message):
    await cmd_start(message)

@common_router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Отменено.")


# --- WAR ---
@war_router.callback_query(F.data == "menu_war")
async def cb_menu_war(callback: CallbackQuery):
    await callback.answer()
    await callback.message.edit_text("⚔️ <b>ВОЙНА:</b>", parse_mode="HTML", reply_markup=Keyboards.war_menu())

@war_router.callback_query(F.data == "war_plan")
async def cb_war_plan(callback: CallbackQuery):
    await callback.answer("⏳ Формирую план...")
    await handle_war_plan(callback, False)

@war_router.callback_query(F.data == "war_report")
async def cb_war_report(callback: CallbackQuery):
    await callback.answer("⏳ Генерирую отчет...")
    await handle_war_plan(callback, True)

@war_router.callback_query(F.data == "war_history")
async def cb_war_history(callback: CallbackQuery):
    await callback.answer("⏳ Загружаю...")
    await handle_war_history(callback)

@war_router.callback_query(F.data == "remind_full")
async def cb_remind(callback: CallbackQuery):
    await callback.answer()
    await handle_remind_full(callback)

@war_router.callback_query(F.data == "attack_logs")
async def cb_logs(callback: CallbackQuery):
    await callback.answer()
    await handle_attack_logs(callback)


# --- CWL ---
@cwl_router.callback_query(F.data == "menu_cwl")
async def cb_menu_cwl(callback: CallbackQuery):
    await callback.answer()
    await callback.message.edit_text("🏆 <b>CWL:</b>", parse_mode="HTML", reply_markup=Keyboards.cwl_menu())

@cwl_router.callback_query(F.data == "cwl_current")
async def cb_cwl_current(callback: CallbackQuery):
    await callback.answer("⏳ Загружаю...")
    await handle_cwl_current(callback)

@cwl_router.callback_query(F.data == "cwl_group")
async def cb_cwl_group(callback: CallbackQuery):
    await callback.answer("⏳ Загружаю...")
    await handle_cwl_group(callback)

@cwl_router.callback_query(F.data == "cwl_stars")
async def cb_cwl_stars(callback: CallbackQuery):
    await callback.answer("⏳ Загружаю...")
    await handle_cwl_stars(callback)


# --- CLAN ---
@clan_router.callback_query(F.data == "menu_clan")
async def cb_menu_clan(callback: CallbackQuery):
    await callback.answer()
    await callback.message.edit_text("🏰 <b>КЛАН:</b>", parse_mode="HTML", reply_markup=Keyboards.clan_menu())

@clan_router.callback_query(F.data == "clan_info")
async def cb_clan_info(callback: CallbackQuery):
    await callback.answer()
    await handle_clan_info(callback)

@clan_router.callback_query(F.data == "clan_members")
async def cb_clan_members(callback: CallbackQuery):
    await callback.answer()
    await handle_clan_members(callback)

@clan_router.callback_query(F.data == "clan_donations")
async def cb_clan_donations(callback: CallbackQuery):
    await callback.answer()
    await handle_clan_donations(callback)

@clan_router.callback_query(F.data == "clan_capital")
async def cb_clan_capital(callback: CallbackQuery):
    await callback.answer("⏳ Загружаю...")
    await handle_clan_capital(callback)


# --- PROFILE ---
@profile_router.callback_query(F.data == "menu_profile")
async def cb_menu_profile(callback: CallbackQuery):
    await callback.answer()
    await callback.message.edit_text("👤 <b>ПРОФИЛЬ:</b>", parse_mode="HTML", reply_markup=Keyboards.profile_menu())

@profile_router.callback_query(F.data == "my_stats")
async def cb_my_stats(callback: CallbackQuery):
    await callback.answer()
    await handle_my_stats(callback)

@profile_router.callback_query(F.data == "my_accounts")
async def cb_my_accounts(callback: CallbackQuery):
    await callback.answer()
    await handle_my_accounts(callback)

@profile_router.callback_query(F.data == "settings_menu")
async def cb_settings_menu(callback: CallbackQuery):
    await callback.answer()
    settings = await callback.bot.settings_repo.get_user_settings(callback.from_user.id)
    text = "⚙️ <b>НАСТРОЙКИ УВЕДОМЛЕНИЙ</b>\n\n"
    text += f"🔔 Напоминания: {'✅' if settings['war_reminders'] else '❌'}\n"
    text += f"⏰ За 4 часа: {'✅' if settings['reminder_4h'] else '❌'}\n"
    text += f"⏰ За 1 час: {'✅' if settings['reminder_1h'] else '❌'}\n"
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=Keyboards.settings_menu())

@profile_router.callback_query(F.data.startswith("toggle_"))
async def cb_toggle_setting(callback: CallbackQuery):
    setting_key = callback.data.replace("toggle_", "")
    settings = await callback.bot.settings_repo.get_user_settings(callback.from_user.id)
    new_value = not settings.get(setting_key, False)
    await callback.bot.settings_repo.update_setting(callback.from_user.id, setting_key, new_value)
    await callback.answer(f"{'✅ Вкл' if new_value else '❌ Выкл'}")
    await cb_settings_menu(callback)

@profile_router.callback_query(F.data == "export_stats")
async def cb_export_stats(callback: CallbackQuery):
    await callback.answer("⏳ Генерирую...")
    accounts = await callback.bot.account_repo.get_user_accounts(callback.from_user.id)
    
    if not accounts:
        await callback.message.edit_text("🔗 Нет аккаунтов", reply_markup=Keyboards.back("menu_profile"))
        return
    
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Имя', 'Тег', 'ТХ', 'Трофеи', 'Атак побед', 'Донат'])
    
    for acc in accounts:
        try:
            player = await callback.bot.coc_service.get_player(acc['player_tag'])
            writer.writerow([player.name, player.tag, player.town_hall, player.trophies, player.attack_wins, player.donations])
        except:
            writer.writerow([acc['player_name'], acc['player_tag'], 'Ошибка', '', '', ''])
    
    file = BufferedInputFile(output.getvalue().encode('utf-8'), filename="stats.csv")
    await callback.message.answer_document(file, caption="📊 Статистика", reply_markup=Keyboards.back("menu_profile"))

@profile_router.callback_query(F.data == "clear_active_clan")
async def cb_clear_active_clan(callback: CallbackQuery):
    await callback.answer()
    await callback.message.edit_text(
        "⚠️ <b>Сброс активного клана</b>\n\nВы уверены?",
        parse_mode="HTML", reply_markup=Keyboards.confirm("confirm_clear_clan")
    )

@profile_router.callback_query(F.data == "confirm_clear_clan_yes")
async def cb_confirm_clear_yes(callback: CallbackQuery):
    await callback.bot.clan_repo.clear_active_clan(callback.from_user.id)
    await callback.message.edit_text("✅ Сброшен", parse_mode="HTML", reply_markup=Keyboards.back("menu_profile"))

@profile_router.callback_query(F.data == "confirm_clear_clan_no")
async def cb_confirm_clear_no(callback: CallbackQuery):
    await callback.message.edit_text("❌ Отменено", reply_markup=Keyboards.back("menu_profile"))


# --- ADMIN ---
@admin_router.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id not in CONFIG.ADMIN_IDS:
        await message.answer("🚫 Доступ запрещен")
        return
    stats = await message.bot.user_repo.get_statistics()
    text = (
        "🔧 <b>АДМИН-ПАНЕЛЬ</b>\n\n"
        f"👥 Пользователей: <code>{stats['total_users']}</code>\n"
        f"🔗 Аккаунтов: <code>{stats['total_accounts']}</code>\n"
        f"🏰 Кланов: <code>{stats['active_clans']}</code>\n\n"
        "/ban <code>id</code> - бан\n/unban <code>id</code> - разбан\n/broadcast <code>текст</code> - рассылка"
    )
    await message.answer(text, parse_mode="HTML")

@admin_router.message(Command("ban"))
async def cmd_ban(message: Message, command: CommandObject):
    if message.from_user.id not in CONFIG.ADMIN_IDS:
        return await message.answer("🚫")
    if not command.args:
        return await message.answer("❌ /ban user_id")
    try:
        await message.bot.user_repo.ban_user(int(command.args))
        await message.answer(f"✅ Забанен {command.args}")
    except:
        await message.answer("❌ Ошибка")

@admin_router.message(Command("unban"))
async def cmd_unban(message: Message, command: CommandObject):
    if message.from_user.id not in CONFIG.ADMIN_IDS:
        return await message.answer("🚫")
    if not command.args:
        return await message.answer("❌ /unban user_id")
    try:
        await message.bot.user_repo.unban_user(int(command.args))
        await message.answer(f"✅ Разбанен {command.args}")
    except:
        await message.answer("❌ Ошибка")

@admin_router.message(Command("broadcast"))
async def cmd_broadcast(message: Message, command: CommandObject):
    if message.from_user.id not in CONFIG.ADMIN_IDS:
        return await message.answer("🚫")
    if not command.args:
        return await message.answer("❌ /broadcast текст")
    
    users = await message.bot.user_repo.get_all_active_users()
    success = failed = 0
    for user_id in users:
        try:
            await message.bot.send_message(user_id, command.args)
            success += 1
            await asyncio.sleep(0.05)
        except:
            failed += 1
    await message.answer(f"📤 Успешно: {success}, Ошибок: {failed}")


# --- LINK ---
@link_router.callback_query(F.data == "back_main")
async def cb_back_main(callback: CallbackQuery):
    await callback.answer()
    active = await callback.bot.clan_repo.get_active_clan(callback.from_user.id)
    text = "👋 Главное меню\n\n"
    text += f"🏰 Клан: <code>{active}</code>" if active else "⚠️ Клан не выбран"
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=Keyboards.main_menu())

@link_router.message(Command("set_clan"))
@link_router.callback_query(F.data == "set_clan_menu")
async def cmd_set_clan(update, state: FSMContext):
    if isinstance(update, CallbackQuery):
        await update.answer()
        msg = update.message
    else:
        if update.from_user.is_bot: return
        msg = update
    await msg.answer("🎯 Отправь тег клана (например, <code>#2CY00G2VU</code>)", parse_mode="HTML")
    await state.set_state(SetClan.waiting_for_clan_tag)

@link_router.message(SetClan.waiting_for_clan_tag)
async def process_clan_tag(message: Message, state: FSMContext):
    tag = message.text.strip()
    if not Validators.validate_coc_tag(tag):
        return await message.answer("❌ Неверный тег")
    tag = Validators.normalize_tag(tag)
    
    if not message.bot.coc_service:
        await message.answer("❌ COC не подключен")
        await state.clear()
        return
    
    try:
        clan = await message.bot.coc_service.get_clan(tag)
        if not clan:
            await message.answer("❌ Не найден")
            await state.clear()
            return
        await state.update_data(clan_tag=tag, clan_name=clan.name)
        await message.answer(
            f"🏰 <b>{clan.name}</b>\nТег: <code>{tag}</code>\nУр: <code>{clan.level}</code>\n\nУстановить?",
            parse_mode="HTML", reply_markup=Keyboards.confirm("confirm_clan")
        )
        await state.set_state(SetClan.waiting_for_clan_confirmation)
    except Exception as e:
        await message.answer(f"❌ {str(e)[:100]}")
        await state.clear()

@link_router.callback_query(F.data == "confirm_clan_yes", SetClan.waiting_for_clan_confirmation)
async def confirm_clan_yes(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await callback.bot.clan_repo.set_active_clan(callback.from_user.id, data['clan_tag'])
    await callback.message.edit_text(f"✅ Клан: <b>{data['clan_name']}</b>", parse_mode="HTML")
    await state.clear()

@link_router.callback_query(F.data == "confirm_clan_no", SetClan.waiting_for_clan_confirmation)
async def confirm_clan_no(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("❌ Отменено")
    await state.clear()

@link_router.message(Command("link"))
@link_router.callback_query(F.data == "link_account")
async def cmd_link(update, state: FSMContext):
    if isinstance(update, CallbackQuery):
        await update.answer()
        msg = update.message
    else:
        if update.from_user.is_bot: return
        msg = update
    await msg.answer("🔗 Отправь тег игрока (например, <code>#QV2Q9V8L2</code>)", parse_mode="HTML")
    await state.set_state(LinkAccount.waiting_for_tag)

@link_router.message(LinkAccount.waiting_for_tag)
async def process_tag(message: Message, state: FSMContext):
    tag = message.text.strip()
    if not Validators.validate_coc_tag(tag):
        return await message.answer("❌ Неверный тег")
    tag = Validators.normalize_tag(tag)
    
    if not message.bot.coc_service:
        await message.answer("❌ COC не подключен")
        await state.clear()
        return
    
    try:
        player = await message.bot.coc_service.get_player(tag)
        if not player:
            await message.answer("❌ Не найден")
            await state.clear()
            return
        clan_tag = player.clan.tag if player.clan else None
        await state.update_data(player_tag=tag, player_name=player.name, clan_tag=clan_tag)
        clan_info = f"🏰 Клан: <code>{clan_tag}</code>" if clan_tag else "⚠️ Не в клане"
        await message.answer(
            f"🔍 <b>{player.name}</b> (ТХ{player.town_hall})\nТег: <code>{tag}</code>\n{clan_info}",
            parse_mode="HTML", reply_markup=Keyboards.confirm("confirm_link")
        )
        await state.set_state(LinkAccount.waiting_for_confirmation)
    except Exception as e:
        await message.answer(f"❌ {str(e)[:100]}")
        await state.clear()

@link_router.callback_query(F.data == "confirm_link_yes", LinkAccount.waiting_for_confirmation)
async def confirm_link_yes(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get('clan_tag'):
        await callback.message.edit_text("⚠️ Не в клане")
        await state.clear()
        return
    success = await callback.bot.account_repo.link_account(
        callback.from_user.id, callback.from_user.username or "",
        data['player_tag'], data['player_name'], data['clan_tag']
    )
    if success:
        await callback.message.edit_text(f"✅ <b>{data['player_name']}</b> привязан!\n🏰 <code>{data['clan_tag']}</code>", parse_mode="HTML")
    else:
        await callback.message.edit_text("❌ Ошибка")
    await state.clear()

@link_router.callback_query(F.data == "confirm_link_no", LinkAccount.waiting_for_confirmation)
async def confirm_link_no(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("❌ Отменено")
    await state.clear()

@link_router.message(Command("unlink"))
async def cmd_unlink(message: Message):
    if message.from_user.is_bot: return
    accounts = await message.bot.account_repo.get_user_accounts(message.from_user.id)
    if not accounts:
        return await message.answer("🔗 Нет аккаунтов")
    
    kb = []
    text = "🗑 <b>Выберите аккаунт:</b>\n\n"
    for a in accounts:
        text += f"• {a['player_name']} (<code>{a['player_tag']}</code>)\n"
        kb.append([InlineKeyboardButton(text=f"🗑 {a['player_name']}", callback_data=f"unlink_{a['player_tag']}")])
    kb.append([InlineKeyboardButton(text="❌ Отмена", callback_data="back_main")])
    await message.answer(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@link_router.callback_query(F.data.startswith("unlink_"))
async def cb_unlink_request(callback: CallbackQuery):
    player_tag = callback.data.replace("unlink_", "")
    accounts = await callback.bot.account_repo.get_user_accounts(callback.from_user.id)
    player_name = next((a['player_name'] for a in accounts if a['player_tag'] == player_tag), "Неизвестно")
    
    await callback.message.edit_text(
        f"⚠️ Удалить <b>{player_name}</b> (<code>{player_tag}</code>)?",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да", callback_data=f"confirm_unlink_{player_tag}")],
            [InlineKeyboardButton(text="❌ Нет", callback_data="my_accounts")],
        ])
    )
    await callback.answer()

@link_router.callback_query(F.data.startswith("confirm_unlink_"))
async def cb_unlink_confirm(callback: CallbackQuery):
    player_tag = callback.data.replace("confirm_unlink_", "")
    success = await callback.bot.account_repo.unlink_account(callback.from_user.id, player_tag)
    await callback.answer("✅ Удален" if success else "❌ Ошибка", show_alert=True)
    await handle_my_accounts(callback)

@link_router.callback_query(F.data.startswith("set_active_"))
async def cb_set_active_clan(callback: CallbackQuery):
    player_tag = callback.data.replace("set_active_", "")
    accounts = await callback.bot.account_repo.get_user_accounts(callback.from_user.id)
    clan_tag = next((a['clan_tag'] for a in accounts if a['player_tag'] == player_tag), None)
    if clan_tag:
        await callback.bot.clan_repo.set_active_clan(callback.from_user.id, clan_tag)
        await callback.answer(f"✅ {clan_tag}", show_alert=True)
    else:
        await callback.answer("❌", show_alert=True)
    await handle_my_accounts(callback)


# ============================================================
# 🎯 HANDLERS
# ============================================================
async def check_user_clan(callback_or_message) -> Optional[str]:
    tg_id = callback_or_message.from_user.id
    clan_tag = await callback_or_message.bot.clan_repo.get_active_clan(tg_id)
    if not clan_tag:
        text = "⚠️ <b>Клан не выбран!</b>\n\n🔗 /link\n🎯 /set_clan"
        if isinstance(callback_or_message, CallbackQuery):
            await callback_or_message.message.answer(text, parse_mode="HTML")
        else:
            await callback_or_message.answer(text, parse_mode="HTML")
        return None
    return clan_tag


async def handle_cwl_current(callback: CallbackQuery):
    clan_tag = await check_user_clan(callback)
    if not clan_tag or not callback.bot.coc_service: return
    try:
        league = await callback.bot.coc_service.get_league_group(clan_tag)
        if not league or league.state == 'notInWar':
            return await callback.message.answer("🔍 Нет CWL", reply_markup=Keyboards.back("menu_cwl"))
        text = f"🏆 <b>CWL — {league.state.upper()}</b>\n📅 Сезон: <code>{league.season}</code>\n👥 Кланы: <code>{len(league.clans)}</code>\n\n"
        for i, clan in enumerate(league.clans, 1):
            text += f"<code>{i}.</code> {clan.name} (Ур.{clan.level})\n"
        await callback.message.answer(text, parse_mode="HTML", reply_markup=Keyboards.back("menu_cwl"))
    except Exception as e:
        await callback.message.answer(f"❌ {str(e)[:100]}")


async def handle_cwl_group(callback: CallbackQuery):
    clan_tag = await check_user_clan(callback)
    if not clan_tag or not callback.bot.coc_service: return
    try:
        league = await callback.bot.coc_service.get_league_group(clan_tag)
        if not league:
            return await callback.message.answer("❌ CWL не найдена")
        text = "👥 <b>Кланы:</b>\n\n"
        for clan in league.clans:
            text += f"🏰 <b>{clan.name}</b>\n   <code>{clan.tag}</code> (Ур.{clan.level})\n\n"
        await callback.message.answer(text, parse_mode="HTML", reply_markup=Keyboards.back("menu_cwl"))
    except Exception as e:
        await callback.message.answer(f"❌ {str(e)[:100]}")


async def handle_cwl_stars(callback: CallbackQuery):
    clan_tag = await check_user_clan(callback)
    if not clan_tag or not callback.bot.coc_service: return
    try:
        league = await callback.bot.coc_service.get_league_group(clan_tag)
        if not league:
            return await callback.message.answer("❌ CWL не найдена")
        
        player_stars = {}
        war_tasks = []
        for round_tag in league.rounds:
            if round_tag:
                for war_tag in round_tag:
                    if war_tag:
                        war_tasks.append(callback.bot.coc_service.get_league_war(war_tag))
        
        wars = await asyncio.gather(*war_tasks, return_exceptions=True)
        for war in wars:
            if isinstance(war, Exception) or not war: continue
            if war.clan and war.clan.tag == clan_tag:
                for member in war.clan.members:
                    for attack in (member.attacks or []):
                        if member.tag not in player_stars:
                            player_stars[member.tag] = {'name': member.name, 'stars': 0, 'attacks': 0}
                        player_stars[member.tag]['stars'] += attack.stars
                        player_stars[member.tag]['attacks'] += 1
        
        if not player_stars:
            return await callback.message.answer("📭 Нет данных", reply_markup=Keyboards.back("menu_cwl"))
        
        top = sorted(player_stars.values(), key=lambda x: x['stars'], reverse=True)[:15]
        text = "⭐ <b>ТОП CWL:</b>\n\n"
        for i, p in enumerate(top, 1):
            avg = p['stars'] / p['attacks'] if p['attacks'] > 0 else 0
            text += f"<code>{i:2d}.</code> {p['name']} — <b>{p['stars']}</b>⭐ ({p['attacks']} ат., avg {avg:.1f})\n"
        await callback.message.answer(text, parse_mode="HTML", reply_markup=Keyboards.back("menu_cwl"))
    except Exception as e:
        await callback.message.answer(f"❌ {str(e)[:100]}")


async def handle_war_history(callback: CallbackQuery):
    clan_tag = await check_user_clan(callback)
    if not clan_tag or not callback.bot.coc_service: return
    try:
        wars = await callback.bot.coc_service.get_war_log(clan_tag, limit=10)
        text = "📜 <b>Последние 10 войн:</b>\n\n"
        wins = losses = 0
        for i, war in enumerate(wars, 1):
            our, enemy = war.clan, war.opponent
            if our.stars > enemy.stars or (our.stars == enemy.stars and our.destruction > enemy.destruction):
                status = "🟢 ПОБЕДА"; wins += 1
            elif our.stars < enemy.stars or (our.stars == enemy.stars and our.destruction < enemy.destruction):
                status = "🔴 ПОРАЖЕНИЕ"; losses += 1
            else: status = "🟡 НИЧЬЯ"
            date_str = f"{war.end_time.day:02d}.{war.end_time.month:02d}" if war.end_time else "???"
            text += f"<code>{i:2d}.</code> {date_str} {status}\n    vs <b>{enemy.name}</b>\n    ⭐ {our.stars}:{enemy.stars} | 💥 {our.destruction:.0f}%:{enemy.destruction:.0f}%\n\n"
        text += f"📊 <b>Итог:</b> 🟢 {wins} / 🔴 {losses}"
        await callback.message.answer(text, parse_mode="HTML", reply_markup=Keyboards.back("menu_war"))
    except coc.PrivateWarLog:
        await callback.message.answer("🔒 Лог закрыт", reply_markup=Keyboards.back("menu_war"))
    except Exception as e:
        await callback.message.answer(f"❌ {str(e)[:100]}")


async def handle_clan_info(callback: CallbackQuery):
    clan_tag = await check_user_clan(callback)
    if not clan_tag or not callback.bot.coc_service: return
    try:
        clan = await callback.bot.coc_service.get_clan(clan_tag)
        if not clan:
            return await callback.message.answer("❌ Не найден")
        text = (
            f"🏰 <b>{clan.name}</b> <code>{clan.tag}</code>\n\n"
            f"📊 Уровень: <code>{clan.level}</code>\n"
            f"👥 Участников: <code>{clan.member_count}/50</code>\n"
            f"🏆 Трофеи: <code>{clan.points}</code>\n"
            f"🛡️ Вход: <code>{clan.required_trophies}</code>\n"
            f"🌍 Регион: <code>{clan.location.name if clan.location else 'Global'}</code>\n\n"
            f"📝 <i>{clan.description or 'Нет описания'}</i>"
        )
        await callback.message.answer(text, parse_mode="HTML", reply_markup=Keyboards.back("menu_clan"))
    except Exception as e:
        await callback.message.answer(f"❌ {str(e)[:100]}")


async def handle_clan_members(callback: CallbackQuery):
    clan_tag = await check_user_clan(callback)
    if not clan_tag or not callback.bot.coc_service: return
    try:
        clan = await callback.bot.coc_service.get_clan(clan_tag)
        if not clan:
            return await callback.message.answer("❌ Не найден")
        tg_mapping = await callback.bot.account_repo.get_linked_accounts_mapping(clan_tag)
        text = f"👥 <b>Участники {clan.name}</b> ({clan.member_count}/50):\n\n"
        for i, member in enumerate(clan.members, 1):
            name = member.name
            if member.tag in tg_mapping:
                name += f" {tg_mapping[member.tag]}"
            role = member.role.name if hasattr(member.role, 'name') else str(member.role)
            text += f"<code>{i:2d}.</code> {name} (ТХ{member.town_hall}) - {role}\n"
        await callback.message.answer(text, parse_mode="HTML", reply_markup=Keyboards.back("menu_clan"))
    except Exception as e:
        await callback.message.answer(f"❌ {str(e)[:100]}")


async def handle_clan_donations(callback: CallbackQuery):
    clan_tag = await check_user_clan(callback)
    if not clan_tag or not callback.bot.coc_service: return
    try:
        clan = await callback.bot.coc_service.get_clan(clan_tag)
        if not clan:
            return await callback.message.answer("❌ Не найден")
        tg_mapping = await callback.bot.account_repo.get_linked_accounts_mapping(clan_tag)
        members_data = []
        for member in clan.members:
            name = member.name
            if member.tag in tg_mapping:
                name += f" {tg_mapping[member.tag]}"
            members_data.append({
                'name': name,
                'donated': getattr(member, 'donations', 0) or 0,
                'received': getattr(member, 'donations_received', 0) or 0
            })
        top_donated = sorted(members_data, key=lambda x: x['donated'], reverse=True)[:10]
        top_received = sorted(members_data, key=lambda x: x['received'], reverse=True)[:10]
        text = f"🎁 <b>Пожертвования {clan.name}</b>\n\n🏆 <b>ТОП 10 ДАТЕЛЕЙ:</b>\n"
        for i, m in enumerate(top_donated, 1):
            text += f"<code>{i:2d}.</code> {m['name']} - <b>{m['donated']}</b>\n"
        text += "\n📥 <b>ТОП 10 ПОЛУЧАТЕЛЕЙ:</b>\n"
        for i, m in enumerate(top_received, 1):
            text += f"<code>{i:2d}.</code> {m['name']} - <b>{m['received']}</b>\n"
        await callback.message.answer(text, parse_mode="HTML", reply_markup=Keyboards.back("menu_clan"))
    except Exception as e:
        await callback.message.answer(f"❌ {str(e)[:100]}")


async def handle_clan_capital(callback: CallbackQuery):
    clan_tag = await check_user_clan(callback)
    if not clan_tag or not callback.bot.coc_service: return
    try:
        clan = await callback.bot.coc_service.get_clan(clan_tag)
        if not clan:
            return await callback.message.answer("❌ Не найден")
        text = f"🏰 <b>Столица {clan.name}</b>\n\n"
        capital = getattr(clan, 'clan_capital', None)
        if capital:
            text += f"📊 Уровень: <code>{getattr(capital, 'capital_hall_level', '?')}</code>\n"
            districts = getattr(capital, 'districts', []) or []
            if districts:
                text += f"\n🗺️ <b>Районы ({len(districts)}):</b>\n"
                for d in districts[:10]:
                    text += f"  • {getattr(d, 'name', '?')} (Ур.{getattr(d, 'district_hall_level', '?')})\n"
        else:
            text += "⚠️ Данные недоступны\n"
        await callback.message.answer(text, parse_mode="HTML", reply_markup=Keyboards.back("menu_clan"))
    except Exception as e:
        await callback.message.answer(f"❌ {str(e)[:100]}")


async def handle_my_stats(callback: CallbackQuery):
    if not callback.bot.coc_service: return
    accounts = await callback.bot.account_repo.get_user_accounts(callback.from_user.id)
    if not accounts:
        return await callback.message.answer("🔗 Нет аккаунтов\n/link")
    
    player_tasks = [callback.bot.coc_service.get_player(acc['player_tag']) for acc in accounts]
    players = await asyncio.gather(*player_tasks, return_exceptions=True)
    
    text = "📊 <b>Ваша статистика</b>\n\n"
    for acc, player in zip(accounts, players):
        if isinstance(player, Exception) or not player:
            text += f"👤 {acc['player_name']} - ❌ Ошибка\n\n"
            continue
        text += (
            f"👤 <b>{player.name}</b> (<code>{acc['player_tag']}</code>)\n"
            f"   🏰 ТХ: <code>{player.town_hall}</code>\n"
            f"   🏆 Трофеи: <code>{player.trophies}</code>\n"
            f"   ⚔️ Побед: <code>{player.attack_wins}</code>\n"
            f"   🛡️ Защит: <code>{player.defense_wins}</code>\n"
            f"   🎁 Донат: <code>{player.donations}</code>\n\n"
        )
    await callback.message.answer(text, parse_mode="HTML", reply_markup=Keyboards.back("menu_profile"))


async def handle_my_accounts(callback: CallbackQuery):
    accounts = await callback.bot.account_repo.get_user_accounts(callback.from_user.id)
    active_clan = await callback.bot.clan_repo.get_active_clan(callback.from_user.id)
    
    if not accounts:
        return await callback.message.edit_text("🔗 Нет аккаунтов\n/link", parse_mode="HTML", reply_markup=Keyboards.back("menu_profile"))
    
    text = f"👤 <b>Аккаунты ({len(accounts)}):</b>\n\n🏰 <b>Активный:</b> <code>{active_clan or 'НЕТ'}</code>\n\n"
    kb = []
    for a in accounts:
        text += f"• <b>{a['player_name']}</b>\n  Тег: <code>{a['player_tag']}</code>\n  Клан: <code>{a['clan_tag']}</code>\n\n"
        if a['clan_tag'] != active_clan:
            kb.append([InlineKeyboardButton(text=f"🎯 Активный: {a['player_name']}", callback_data=f"set_active_{a['player_tag']}")])
        kb.append([InlineKeyboardButton(text=f"🗑 Удалить: {a['player_name']}", callback_data=f"unlink_{a['player_tag']}")])
    kb.append([InlineKeyboardButton(text="➕ Привязать ещё", callback_data="link_account")])
    kb.append([InlineKeyboardButton(text="◀️ Назад", callback_data="menu_profile")])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))


async def handle_war_plan(callback: CallbackQuery, generate_report: bool = False):
    clan_tag = await check_user_clan(callback)
    if not clan_tag or not callback.bot.coc_service: return
    try:
        war = await callback.bot.coc_service.get_current_war(clan_tag)
        if not war or war.state == "notInWar":
            return await callback.message.answer("🔍 Нет войны", reply_markup=Keyboards.back("menu_war"))
        
        our_clan, enemy_clan = war.clan, war.opponent
        tg_mapping = await callback.bot.account_repo.get_linked_accounts_mapping(clan_tag)
        
        our_members_info = []
        if our_clan and our_clan.members:
            for m in our_clan.members:
                attacks = getattr(m, 'attacks', []) or []
                display_name = m.name
                if m.tag in tg_mapping:
                    display_name += f" ({tg_mapping[m.tag]})"
                our_members_info.append({
                    'obj': m, 'name': display_name, 'raw_name': m.name,
                    'th': getattr(m, 'town_hall', 0) or 0,
                    'map_pos': getattr(m, 'map_position', '?'),
                    'attacks_count': len(attacks), 'attacks': attacks
                })
        
        enemy_members_info = []
        if enemy_clan and enemy_clan.members:
            for m in enemy_clan.members:
                enemy_members_info.append({
                    'obj': m, 'name': m.name,
                    'th': getattr(m, 'town_hall', 0) or 0,
                    'map_pos': getattr(m, 'map_position', '?'),
                    'destruction': getattr(m, 'destruction', 0) or 0,
                })
        
        header = (
            f"⚔️ <b>ВОЙНА: {our_clan.name} vs {enemy_clan.name}</b>\n\n"
            f"📊 Счёт: <code>{our_clan.stars}</code> : <code>{enemy_clan.stars}</code>\n"
            f"💥 Разрушение: <code>{our_clan.destruction:.1f}%</code> : <code>{enemy_clan.destruction:.1f}%</code>\n"
            f"⏳ Статус: <code>{war.state}</code>\n"
        )
        
        our_sorted_by_pos = sorted(our_members_info, key=lambda x: (x['map_pos'] if isinstance(x['map_pos'], int) else 99))
        roster_lines = []
        lazy_list = []
        for m in our_sorted_by_pos:
            pos_str = f"Поз. {m['map_pos']}" if isinstance(m['map_pos'], int) else "Поз. ?"
            att_str = f"{m['attacks_count']}/{war.attacks_per_member}"
            roster_lines.append(f"• <code>{pos_str}</code> | {m['name']} (ТХ{m['th']}) - Атаки: {att_str}")
            if m['attacks_count'] < war.attacks_per_member:
                mention = f" ({tg_mapping[m['obj'].tag]})" if m['obj'].tag in tg_mapping else ""
                lazy_list.append(f"• {m['raw_name']}{mention} (ТХ{m['th']}) - осталось {war.attacks_per_member - m['attacks_count']}")
        
        our_sorted_by_th = sorted(our_members_info, key=lambda x: x['th'], reverse=True)
        enemy_sorted = sorted(enemy_members_info, key=lambda x: (x['map_pos'] if isinstance(x['map_pos'], int) else 99))
        
        dobiv_role_tags = set([m['obj'].tag for m in our_sorted_by_th[:3]])
        plan_text = "\n🧠 <b>ТАКТИЧЕСКИЙ ПЛАН (AI)</b>\n\n"
        dobivators_names = []
        for tag in dobiv_role_tags:
            for m in our_members_info:
                if m['obj'].tag == tag:
                    dobivators_names.append(f"{m['name']} (ТХ{m['th']})")
        plan_text += f"🛡️ <b>ДОБИВАЮЩИЕ:</b>\n" + ", ".join(dobivators_names) + "\n\n"
        
        first_strike_plan = []
        used_enemy_targets = set()
        for attacker in our_sorted_by_pos:
            if attacker['obj'].tag in dobiv_role_tags or attacker['attacks_count'] >= 1: continue
            target = None
            for enemy in enemy_sorted:
                if enemy['obj'].tag in used_enemy_targets: continue
                if enemy['th'] == attacker['th']:
                    target = enemy; break
            if not target:
                for enemy in enemy_sorted:
                    if enemy['obj'].tag in used_enemy_targets: continue
                    if enemy['th'] == attacker['th'] + 1:
                        target = enemy; break
            if not target:
                best_diff = 99
                for enemy in enemy_sorted:
                    if enemy['obj'].tag in used_enemy_targets: continue
                    diff = abs(enemy['th'] - attacker['th'])
                    if diff < best_diff:
                        best_diff = diff; target = enemy
            if target:
                used_enemy_targets.add(target['obj'].tag)
                first_strike_plan.append({'attacker': attacker, 'target': target})
        
        table_1 = PrettyTable()
        table_1.field_names = ["Боец (Поз.)", "Цель (Поз.)", "Статус"]
        table_1.align["Боец (Поз.)"] = "l"
        table_1.align["Цель (Поз.)"] = "l"
        for pair in first_strike_plan:
            a, t = pair['attacker'], pair['target']
            a_pos = a['map_pos'] if isinstance(a['map_pos'], int) else '?'
            t_pos = t['map_pos'] if isinstance(t['map_pos'], int) else '?'
            status = "Ждет 2-й" if a['attacks_count'] > 0 else "Первый"
            table_1.add_row([f"{a['name']} ({a_pos})", f"{t['name']} ({t_pos})", status])
        
        second_strike_plan = []
        for attacker in our_sorted_by_pos:
            if attacker['attacks_count'] >= 2: continue
            is_dobivator = attacker['obj'].tag in dobiv_role_tags
            target = None; rec = ""
            for enemy in enemy_sorted:
                if enemy['destruction'] > 0 and enemy['destruction'] < 100:
                    if not any(e['target']['obj'].tag == enemy['obj'].tag for e in second_strike_plan):
                        target = enemy; rec = f"🚑 ДОБИТЬ ({enemy['destruction']}%)"; break
            if is_dobivator and not target: continue
            if not target and not is_dobivator:
                for enemy in enemy_sorted:
                    if enemy['obj'].tag in used_enemy_targets: continue
                    if not any(e['target']['obj'].tag == enemy['obj'].tag for e in second_strike_plan):
                        if enemy['th'] <= attacker['th']:
                            target = enemy; rec = "🧹 Набивка %"; break
                if not target:
                    for enemy in enemy_sorted:
                        if not any(e['target']['obj'].tag == enemy['obj'].tag for e in second_strike_plan):
                            target = enemy; rec = "Свободная"; break
            if target:
                second_strike_plan.append({'attacker': attacker, 'target': target, 'rec': rec})
        
        table_2 = PrettyTable()
        table_2.field_names = ["Боец", "Цель", "Рекомендация"]
        table_2.align["Боец"] = "l"
        for pair in second_strike_plan:
            a, t = pair['attacker'], pair['target']
            table_2.add_row([f"{a['name']} (ТХ{a['th']})", f"{t['name']} (ТХ{t['th']})", pair['rec']])
        
        if not generate_report:
            await callback.message.answer(header, parse_mode="HTML")
            roster_text = "👥 <b>СОСТАВ:</b>\n"
            roster_chunks = []
            current_chunk = roster_text
            for line in roster_lines:
                if len(current_chunk) + len(line) + 1 > 3800:
                    roster_chunks.append(current_chunk); current_chunk = line + '\n'
                else:
                    current_chunk += line + '\n'
            if current_chunk.strip() != roster_text.strip():
                roster_chunks.append(current_chunk)
            for i, chunk in enumerate(roster_chunks):
                if i == len(roster_chunks) - 1:
                    if lazy_list:
                        chunk += f"\n⚠️ <b>Требуют внимания ({len(lazy_list)}):</b>\n" + "\n".join(lazy_list)
                    else:
                        chunk += "\n✅ Все атаковали!"
                await callback.message.answer(chunk, parse_mode="HTML")
            await callback.message.answer(plan_text, parse_mode="HTML")
            await callback.message.answer(f"<b>1️⃣ ОСНОВНОЙ УДАР:</b>\n<pre><code>{table_1}</code></pre>", parse_mode="HTML")
            if second_strike_plan:
                await callback.message.answer(f"<b>2️⃣ ДОБИВАНИЕ:</b>\n<pre><code>{table_2}</code></pre>", parse_mode="HTML", reply_markup=Keyboards.back("menu_war"))
            else:
                await callback.message.answer("<i>Нет вторых атак.</i>", reply_markup=Keyboards.back("menu_war"))
        
        if generate_report:
            try:
                os.makedirs('reports', exist_ok=True)
                timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
                c_name = our_clan.name.replace(' ', '_').replace('/', '_')[:30]
                e_name = enemy_clan.name.replace(' ', '_').replace('/', '_')[:30]
                filename = f"reports/war_{c_name}_vs_{e_name}_{timestamp}.md"
                md = []
                md.append(f"# ⚔️ {our_clan.name} vs {enemy_clan.name}")
                md.append(f"**Счёт:** `{our_clan.stars}` : `{enemy_clan.stars}`")
                md.append(f"**Разрушение:** `{our_clan.destruction:.1f}%` : `{enemy_clan.destruction:.1f}%`")
                md.append(f"**Статус:** `{war.state}`\n")
                md.append("## 👥 Состав\n")
                for line in roster_lines:
                    md.append(line.replace('<code>', '`').replace('</code>', '`'))
                if lazy_list:
                    md.append(f"\n### ⚠️ Требуют внимания ({len(lazy_list)})")
                    md.extend(lazy_list)
                md.append("\n## 🧠 AI План\n")
                md.append(f"**Добивающие:** {', '.join(dobivators_names)}\n")
                md.append("## 1️⃣ Основной удар\n```text")
                md.append(str(table_1))
                md.append("```")
                if second_strike_plan:
                    md.append("\n## 2️⃣ Добивание\n```text")
                    md.append(str(table_2))
                    md.append("```")
                with open(filename, 'w', encoding='utf-8') as f:
                    f.write('\n'.join(md))
                await callback.message.answer_document(FSInputFile(filename), caption="📁 Отчет войны")
            except Exception as e:
                logger.error(f"Report error: {e}", exc_info=True)
                await callback.message.answer(f"❌ Ошибка отчета: {str(e)[:100]}")
    except coc.PrivateWarLog:
        await callback.message.answer("🔒 Лог закрыт", reply_markup=Keyboards.back("menu_war"))
    except Exception as e:
        logger.error(f"War plan error: {e}", exc_info=True)
        await callback.message.answer(f"❌ {str(e)[:100]}")


async def handle_remind_full(callback: CallbackQuery):
    clan_tag = await check_user_clan(callback)
    if not clan_tag or not callback.bot.coc_service: return
    try:
        war = await callback.bot.coc_service.get_current_war(clan_tag)
        if not war or war.state == "notInWar":
            return await callback.message.answer("🔍 Нет войны")
        tg_mapping = await callback.bot.account_repo.get_linked_accounts_mapping(clan_tag)
        zero_attacks = []; one_attack = []
        if war.clan and war.clan.members:
            for m in war.clan.members:
                count = len(getattr(m, 'attacks', []) or [])
                mention = f" ({tg_mapping[m.tag]})" if m.tag in tg_mapping else ""
                if count == 0:
                    zero_attacks.append(f"• {m.name}{mention} (ТХ{getattr(m, 'town_hall', '?')})")
                elif count == 1:
                    one_attack.append(f"• {m.name}{mention} (ТХ{getattr(m, 'town_hall', '?')})")
        text = "⏰ <b>СПИСОК НЕДОЧЕТОВ</b>\n\n"
        if zero_attacks:
            text += f"🔴 <b>НЕ ХОДИЛИ ({len(zero_attacks)}):</b>\n" + "\n".join(zero_attacks) + "\n\n"
        else:
            text += "🟢 Все сделали 1 атаку\n\n"
        if one_attack:
            text += f"🟠 <b>ДОБИТЬ ({len(one_attack)}):</b>\n" + "\n".join(one_attack)
        else:
            text += "🟢 Все атаковали!"
        await callback.message.answer(text, parse_mode="HTML", reply_markup=Keyboards.back("menu_war"))
    except Exception as e:
        await callback.message.answer("❌ Ошибка")


async def handle_attack_logs(callback: CallbackQuery):
    clan_tag = await check_user_clan(callback)
    if not clan_tag or not callback.bot.coc_service: return
    try:
        war = await callback.bot.coc_service.get_current_war(clan_tag)
        if not war or war.state == "notInWar":
            return await callback.message.answer("🔍 Нет войны")
        table = PrettyTable()
        table.field_names = ["Атакующий", "Цель", "Звезды", "%"]
        table.align["Атакующий"] = "l"
        table.align["Цель"] = "l"
        count = 0
        if war.clan and war.clan.members:
            for member in war.clan.members:
                for attack in (getattr(member, 'attacks', []) or []):
                    if count >= 20: break
                    defender_tag = getattr(attack, 'defender_tag', None)
                    defender = war.opponent.get_member(defender_tag) if defender_tag and war.opponent else None
                    d_name = defender.name if defender else "?"
                    d_th = getattr(defender, 'town_hall', '?') if defender else "?"
                    stars = getattr(attack, 'stars', 0) or 0
                    dest = getattr(attack, 'destruction', 0) or 0
                    table.add_row([member.name, f"{d_name} (ТХ{d_th})", "⭐"*stars, f"{dest}%"])
                    count += 1
        if count == 0:
            await callback.message.answer("📭 Нет атак", reply_markup=Keyboards.back("menu_war"))
        else:
            await callback.message.answer(f"📊 <b>АТАКИ ({count}):</b>\n<pre><code>{table}</code></pre>", parse_mode="HTML", reply_markup=Keyboards.back("menu_war"))
    except Exception as e:
        await callback.message.answer("❌ Ошибка")


# ============================================================
# 🔔 АВТО-УВЕДОМЛЕНИЯ
# ============================================================
class NotificationService:
    def __init__(self, bot, coc_service, clan_repo, account_repo, settings_repo):
        self.bot = bot
        self.coc_service = coc_service
        self.clan_repo = clan_repo
        self.account_repo = account_repo
        self.settings_repo = settings_repo
    
    async def auto_war_reminders(self):
        if not self.coc_service: return
        logger.info("🔔 Авто-проверка войн...")
        try:
            clans = await self.clan_repo.get_clans_with_active_wars()
            for clan_tag in clans:
                try:
                    war = await self.coc_service.get_current_war(clan_tag)
                    if not war or war.state != "inWar": continue
                    end_time = war.end_time
                    if not end_time: continue
                    now = datetime.datetime.now(datetime.timezone.utc)
                    try:
                        end_dt = end_time.raw_time
                        if isinstance(end_dt, str):
                            end_dt = datetime.datetime.fromisoformat(end_dt.replace('Z', '+00:00'))
                        hours_left = (end_dt - now).total_seconds() / 3600
                    except: continue
                    if not (3.5 < hours_left < 4.5 or 0.5 < hours_left < 1.5): continue
                    
                    tg_mapping = await self.account_repo.get_linked_accounts_mapping()
                    lazy = []
                    if war.clan and war.clan.members:
                        for m in war.clan.members:
                            count = len(getattr(m, 'attacks', []) or [])
                            if count < war.attacks_per_member:
                                mention = tg_mapping.get(m.tag, '')
                                lazy.append(f"• {m.name} {mention} ({war.attacks_per_member - count} ат.)")
                    if not lazy: continue
                    
                    text = (
                        f"🔔 <b>НАПОМИНАНИЕ!</b>\n\n"
                        f"⏰ До конца: <b>{hours_left:.1f} ч.</b>\n"
                        f"🏰 Клан: <code>{clan_tag}</code>\n\n"
                        f"⚠️ <b>Не атаковали:</b>\n" + "\n".join(lazy)
                    )
                    
                    if self.clan_repo.db.pool:
                        async with self.clan_repo.db.pool.acquire() as conn:
                            users = await conn.fetch('SELECT tg_id FROM user_active_clan WHERE clan_tag = $1', clan_tag)
                            for user_row in users:
                                try:
                                    await self.bot.send_message(user_row['tg_id'], text, parse_mode="HTML")
                                except Exception as e:
                                    logger.error(f"Remind error: {e}")
                except Exception as e:
                    logger.error(f"Auto-check error: {e}")
                await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"Auto reminders error: {e}")


# ============================================================
# 🚀 ЗАПУСК
# ============================================================
async def init_coc_client() -> Optional[coc.Client]:
    for i in range(5):
        try:
            client = coc.Client(proxy=CONFIG.COC_PROXY, throttle_limit=10)
            await client.login(CONFIG.COC_EMAIL, CONFIG.COC_PASSWORD)
            logger.info("✅ COC клиент готов!")
            return client
        except Exception as e:
            logger.error(f"❌ COC ошибка ({5-i}): {e}")
            if i < 4: await asyncio.sleep(5)
    return None


async def init_redis():
    """Инициализация Redis (опционально)"""
    if not REDIS_AVAILABLE:
        logger.info("ℹ️ Redis не установлен - пропускаем")
        return None
    
    try:
        redis = aioredis.from_url(CONFIG.REDIS_URL, encoding='utf-8')
        await redis.ping()
        logger.info("✅ Redis подключен")
        return redis
    except Exception as e:
        logger.warning(f"⚠️ Redis недоступен: {e}")
        return None


async def on_startup(app: web.Application):
    if CONFIG.WEBHOOK_URL:
        await app['bot'].set_webhook(f"{CONFIG.WEBHOOK_URL}/webhook")
        logger.info(f"🌐 Webhook: {CONFIG.WEBHOOK_URL}/webhook")
    
    redis = await init_redis()
    app['redis'] = redis
    
    db = Database()
    await db.init()
    app['db'] = db
    
    user_repo = UserRepository(db)
    account_repo = AccountRepository(db)
    clan_repo = ClanRepository(db)
    settings_repo = SettingsRepository(db)
    
    coc_client = await init_coc_client()
    cache = CacheService(redis)
    coc_service = COCService(coc_client, cache) if coc_client else None
    
    app['bot'].user_repo = user_repo
    app['bot'].account_repo = account_repo
    app['bot'].clan_repo = clan_repo
    app['bot'].settings_repo = settings_repo
    app['bot'].coc_service = coc_service
    
    notification_service = NotificationService(app['bot'], coc_service, clan_repo, account_repo, settings_repo)
    scheduler = AsyncIOScheduler()
    scheduler.add_job(notification_service.auto_war_reminders, 'interval', minutes=30, id='war_reminders')
    scheduler.start()
    app['scheduler'] = scheduler
    
    logger.info("🔔 Авто-уведомления запущены")


async def on_shutdown(app: web.Application):
    if 'scheduler' in app: app['scheduler'].shutdown()
    if 'coc_client' in app and app.get('coc_client'): await app['coc_client'].close()
    if 'db' in app: await app['db'].close()
    if 'redis' in app and app['redis']:
        try: await app['redis'].close()
        except: pass
    await app['bot'].session.close()


def setup_routers(dp: Dispatcher):
    dp.include_router(common_router)
    dp.include_router(war_router)
    dp.include_router(cwl_router)
    dp.include_router(clan_router)
    dp.include_router(profile_router)
    dp.include_router(admin_router)
    dp.include_router(link_router)


def main():
    bot = Bot(token=CONFIG.TELEGRAM_TOKEN)
    
    # 🔴 ВАЖНО: Redis опционален!
    if REDIS_AVAILABLE and RedisStorage:
        try:
            redis = aioredis.from_url(CONFIG.REDIS_URL)
            storage = RedisStorage(redis)
            logger.info("✅ Используем Redis storage")
        except Exception as e:
            logger.warning(f"⚠️ Redis storage недоступен: {e}")
            storage = MemoryStorage()
            logger.info("ℹ️ Используем Memory storage")
    else:
        storage = MemoryStorage()
        logger.info("ℹ️ Используем Memory storage (Redis не установлен)")
    
    dp = Dispatcher(storage=storage)
    setup_routers(dp)
    
    # Временно для middleware
    db = Database()
    user_repo = UserRepository(db)
    dp.update.outer_middleware(LoggingMiddleware())
    dp.update.outer_middleware(RateLimitMiddleware())
    dp.update.middleware(ThrottleMiddleware())
    dp.update.middleware(BanCheckMiddleware(user_repo))
    
    app = web.Application()
    handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
    handler.register(app, path="/webhook")
    app.router.add_get("/health", lambda r: web.Response(text="OK"))
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    
    logger.info(f"🚀 Запуск на порту {CONFIG.PORT}...")
    web.run_app(app, host='0.0.0.0', port=CONFIG.PORT)


if __name__ == '__main__':
    main()
