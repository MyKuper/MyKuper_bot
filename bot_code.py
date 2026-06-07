#!/usr/bin/env python3
"""
MyKuper Bot - Улучшенная версия
Clash of Clans Telegram Bot с полным функционалом

Улучшения:
- Модульная архитектура через классы
- Кэширование (Redis с fallback на in-memory)
- Оптимизированные SQL запросы
- Middleware (rate limiting, logging, throttling)
- Роутеры aiogram 3.x
- Админ-панель
- Настройки уведомлений
- Экспорт данных
- Параллельные запросы
- Валидация входных данных
"""

import os
import re
import csv
import io
import json
import time
import logging
import asyncio
import hashlib
import datetime
from typing import Optional, Dict, List, Any, Tuple, Callable, Awaitable
from dataclasses import dataclass, field
from functools import wraps
from collections import defaultdict

import asyncpg
from redis import asyncio as aioredis
from aiogram import Bot, Dispatcher, Router, F, BaseMiddleware
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, 
    InlineKeyboardButton, BufferedInputFile, FSInputFile
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.fsm.storage.memory import MemoryStorage
from aiohttp import web
import coc
from prettytable import PrettyTable
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ============================================================
# ⚙️ КОНФИГУРАЦИЯ
# ============================================================
@dataclass
class Config:
    """Централизованная конфигурация"""
    # Telegram
    TELEGRAM_TOKEN: str = os.getenv('TELEGRAM_TOKEN', '')
    ADMIN_IDS: List[int] = field(default_factory=lambda: [
        int(x) for x in os.getenv('ADMIN_IDS', '0').split(',') if x.strip()
    ])
    
    # Clash of Clans API
    COC_EMAIL: str = os.getenv('COC_EMAIL', '')
    COC_PASSWORD: str = os.getenv('COC_PASSWORD', '')
    COC_PROXY: Optional[str] = os.getenv('COC_PROXY', None)
    
    # Database
    DATABASE_URL: str = os.getenv('DATABASE_URL', '')
    
    # Redis
    REDIS_URL: str = os.getenv('REDIS_URL', 'redis://localhost:6379')
    
    # Server
    PORT: int = int(os.getenv('PORT', 8080))
    WEBHOOK_URL: Optional[str] = os.getenv('RENDER_EXTERNAL_URL', None)
    
    # Rate Limits
    RATE_LIMITS: Dict[str, Tuple[int, int]] = field(default_factory=lambda: {
        "war_plan": (1, 10),
        "war_report": (1, 30),
        "default": (20, 60),
    })
    
    # Cache TTL (в секундах)
    CACHE_TTL: Dict[str, int] = field(default_factory=lambda: {
        "clan_info": 300,
        "war_info": 120,
        "player_info": 600,
        "cwl_info": 900,
        "user_settings": 3600,
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
    """Валидаторы входных данных"""
    
    @staticmethod
    def validate_coc_tag(tag: str) -> bool:
        """Валидация тега COC"""
        if not tag:
            return False
        tag = tag.upper().lstrip('#')
        if not re.match(r'^[0-9A-Z]+$', tag):
            return False
        if not (3 <= len(tag) <= 15):
            return False
        return True
    
    @staticmethod
    def normalize_tag(tag: str) -> str:
        """Нормализация тега (добавление #)"""
        tag = tag.upper().strip()
        if not tag.startswith('#'):
            tag = '#' + tag
        return tag


class Formatters:
    """Форматировщики данных"""
    
    @staticmethod
    def format_number(num: int) -> str:
        """Форматирование больших чисел"""
        if num >= 1_000_000:
            return f"{num / 1_000_000:.1f}M"
        elif num >= 1_000:
            return f"{num / 1_000:.1f}K"
        return str(num)
    
    @staticmethod
    def format_time_ago(dt: datetime.datetime) -> str:
        """Форматирование времени 'назад'"""
        now = datetime.datetime.now(datetime.timezone.utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        diff = now - dt
        
        if diff.days > 0:
            return f"{diff.days}д назад"
        elif diff.seconds > 3600:
            return f"{diff.seconds // 3600}ч назад"
        elif diff.seconds > 60:
            return f"{diff.seconds // 60}мин назад"
        return "только что"
    
    @staticmethod
    def format_time_left(seconds: float) -> str:
        """Форматирование оставшегося времени"""
        if seconds < 0:
            return "завершена"
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        if hours > 0:
            return f"{hours}ч {minutes}мин"
        return f"{minutes}мин"


# ============================================================
# 💾 КЭШИРОВАНИЕ
# ============================================================
class CacheService:
    """Сервис кэширования с Redis и fallback на in-memory"""
    
    def __init__(self, redis_client: Optional[aioredis.Redis] = None):
        self.redis = redis_client
        self.memory_cache: Dict[str, Tuple[Any, float]] = {}
        self.ttl = CONFIG.CACHE_TTL
    
    async def get(self, key: str) -> Optional[Any]:
        """Получить данные из кэша"""
        try:
            if self.redis:
                data = await self.redis.get(key)
                if data:
                    return json.loads(data)
            else:
                # In-memory fallback
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
        """Сохранить данные в кэш"""
        try:
            ttl = self.ttl.get(ttl_key, 300)
            
            if self.redis:
                await self.redis.setex(
                    key,
                    ttl,
                    json.dumps(value, default=str)
                )
            else:
                # In-memory fallback
                self.memory_cache[key] = (value, time.time() + ttl)
        except Exception as e:
            logger.warning(f"Cache set error: {e}")
    
    async def invalidate_pattern(self, pattern: str):
        """Удалить все ключи по паттерну"""
        try:
            if self.redis:
                async for key in self.redis.scan_iter(match=pattern):
                    await self.redis.delete(key)
            else:
                keys_to_delete = [
                    k for k in self.memory_cache.keys()
                    if re.match(pattern.replace('*', '.*'), k)
                ]
                for key in keys_to_delete:
                    del self.memory_cache[key]
        except Exception as e:
            logger.warning(f"Cache invalidate error: {e}")
    
    async def close(self):
        """Закрыть соединение"""
        if self.redis:
            await self.redis.close()


# ============================================================
# 🗄️ БАЗА ДАННЫХ
# ============================================================
class Database:
    """Управление подключением к БД"""
    
    def __init__(self):
        self.pool: Optional[asyncpg.Pool] = None
    
    async def init(self):
        """Инициализация пула соединений"""
        if not CONFIG.DATABASE_URL:
            logger.error("❌ DATABASE_URL не задан!")
            return False
        
        try:
            self.pool = await asyncpg.create_pool(
                CONFIG.DATABASE_URL,
                min_size=2,
                max_size=10
            )
            logger.info("✅ Подключение к PostgreSQL установлено")
            await self._create_tables()
            return True
        except Exception as e:
            logger.error(f"❌ Ошибка инициализации БД: {e}")
            return False
    
    async def _create_tables(self):
        """Создание таблиц"""
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
            
            # Индексы для оптимизации
            await conn.execute('''
                CREATE INDEX IF NOT EXISTS idx_linked_accounts_tg_id 
                ON linked_accounts(tg_id)
            ''')
            await conn.execute('''
                CREATE INDEX IF NOT EXISTS idx_linked_accounts_clan_tag 
                ON linked_accounts(clan_tag)
            ''')
            await conn.execute('''
                CREATE INDEX IF NOT EXISTS idx_user_active_clan_clan_tag 
                ON user_active_clan(clan_tag)
            ''')
            
            logger.info("✅ Таблицы БД созданы/проверены")
    
    async def close(self):
        """Закрытие пула"""
        if self.pool:
            await self.pool.close()


# ============================================================
# 📦 РЕПОЗИТОРИИ (Работа с БД)
# ============================================================
class UserRepository:
    """Репозиторий пользователей"""
    
    def __init__(self, db: Database):
        self.db = db
    
    async def upsert_user(self, tg_id: int, username: Optional[str] = None):
        """Создать или обновить пользователя"""
        if not self.db.pool:
            return
        
        async with self.db.pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO users (tg_id, tg_username, last_active) 
                VALUES ($1, $2, CURRENT_TIMESTAMP)
                ON CONFLICT (tg_id) 
                DO UPDATE SET 
                    tg_username = EXCLUDED.tg_username,
                    last_active = CURRENT_TIMESTAMP
            ''', tg_id, username)
    
    async def is_banned(self, tg_id: int) -> bool:
        """Проверить бан"""
        if not self.db.pool:
            return False
        
        async with self.db.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT is_banned FROM users WHERE tg_id = $1',
                tg_id
            )
            return row['is_banned'] if row else False
    
    async def ban_user(self, tg_id: int):
        """Забанить пользователя"""
        if not self.db.pool:
            return
        
        async with self.db.pool.acquire() as conn:
            await conn.execute(
                'UPDATE users SET is_banned = TRUE WHERE tg_id = $1',
                tg_id
            )
    
    async def unban_user(self, tg_id: int):
        """Разбанить пользователя"""
        if not self.db.pool:
            return
        
        async with self.db.pool.acquire() as conn:
            await conn.execute(
                'UPDATE users SET is_banned = FALSE WHERE tg_id = $1',
                tg_id
            )
    
    async def get_all_active_users(self) -> List[int]:
        """Получить всех активных пользователей"""
        if not self.db.pool:
            return []
        
        async with self.db.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT tg_id FROM users WHERE is_banned = FALSE'
            )
            return [row['tg_id'] for row in rows]
    
    async def get_statistics(self) -> Dict[str, int]:
        """Получить статистику"""
        if not self.db.pool:
            return {}
        
        async with self.db.pool.acquire() as conn:
            total_users = await conn.fetchval('SELECT COUNT(*) FROM users WHERE is_banned = FALSE')
            total_accounts = await conn.fetchval('SELECT COUNT(*) FROM linked_accounts')
            active_clans = await conn.fetchval('SELECT COUNT(DISTINCT clan_tag) FROM user_active_clan')
            
            return {
                'total_users': total_users or 0,
                'total_accounts': total_accounts or 0,
                'active_clans': active_clans or 0,
            }


class AccountRepository:
    """Репозиторий аккаунтов"""
    
    def __init__(self, db: Database):
        self.db = db
    
    async def link_account(
        self, 
        tg_id: int, 
        username: str, 
        player_tag: str, 
        player_name: str, 
        clan_tag: str
    ) -> bool:
        """Привязать аккаунт"""
        if not self.db.pool:
            return False
        
        try:
            async with self.db.pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute('''
                        INSERT INTO users (tg_id, tg_username) 
                        VALUES ($1, $2)
                        ON CONFLICT (tg_id) DO UPDATE SET tg_username = EXCLUDED.tg_username
                    ''', tg_id, username)
                    
                    await conn.execute('''
                        INSERT INTO linked_accounts (tg_id, player_tag, player_name, clan_tag)
                        VALUES ($1, $2, $3, $4)
                        ON CONFLICT (tg_id, player_tag) 
                        DO UPDATE SET 
                            player_name = EXCLUDED.player_name, 
                            clan_tag = EXCLUDED.clan_tag
                    ''', tg_id, player_tag, player_name, clan_tag)
                    
                    await conn.execute('''
                        INSERT INTO user_active_clan (tg_id, clan_tag) 
                        VALUES ($1, $2)
                        ON CONFLICT (tg_id) 
                        DO UPDATE SET 
                            clan_tag = EXCLUDED.clan_tag, 
                            updated_at = CURRENT_TIMESTAMP
                    ''', tg_id, clan_tag)
                    
                    logger.info(f"✅ Привязан: {player_name} ({player_tag}) для {tg_id}")
                    return True
        except Exception as e:
            logger.error(f"❌ Ошибка привязки: {e}")
            return False
    
    async def unlink_account(self, tg_id: int, player_tag: str) -> bool:
        """Отвязать аккаунт"""
        if not self.db.pool:
            return False
        
        try:
            async with self.db.pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(
                        'DELETE FROM linked_accounts WHERE tg_id = $1 AND player_tag = $2',
                        tg_id, player_tag
                    )
                    
                    row = await conn.fetchrow(
                        'SELECT clan_tag FROM linked_accounts WHERE tg_id = $1 LIMIT 1',
                        tg_id
                    )
                    
                    if row:
                        await conn.execute('''
                            INSERT INTO user_active_clan (tg_id, clan_tag) 
                            VALUES ($1, $2)
                            ON CONFLICT (tg_id) 
                            DO UPDATE SET 
                                clan_tag = EXCLUDED.clan_tag, 
                                updated_at = CURRENT_TIMESTAMP
                        ''', tg_id, row['clan_tag'])
                    else:
                        await conn.execute(
                            'DELETE FROM user_active_clan WHERE tg_id = $1', 
                            tg_id
                        )
                    
                    logger.info(f"🗑 Отвязан: {player_tag} для {tg_id}")
                    return True
        except Exception as e:
            logger.error(f"❌ Ошибка отвязки: {e}")
            return False
    
    async def get_user_accounts(self, tg_id: int) -> List[dict]:
        """Получить все аккаунты пользователя"""
        if not self.db.pool:
            return []
        
        try:
            async with self.db.pool.acquire() as conn:
                rows = await conn.fetch('''
                    SELECT player_tag, player_name, clan_tag 
                    FROM linked_accounts 
                    WHERE tg_id = $1
                ''', tg_id)
                return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"❌ Ошибка чтения аккаунтов: {e}")
            return []
    
    async def get_linked_accounts_mapping(
        self, 
        clan_tag: Optional[str] = None
    ) -> Dict[str, str]:
        """
        Получить маппинг player_tag -> @username
        ОПТИМИЗИРОВАНО: один запрос вместо N запросов
        """
        if not self.db.pool:
            return {}
        
        try:
            query = """
                SELECT la.player_tag, u.tg_username
                FROM linked_accounts la
                JOIN users u ON la.tg_id = u.tg_id
                WHERE u.tg_username IS NOT NULL 
                  AND u.tg_username != ''
            """
            
            params = []
            if clan_tag:
                query += " AND la.clan_tag = $1"
                params.append(clan_tag)
            
            async with self.db.pool.acquire() as conn:
                rows = await conn.fetch(query, *params)
                return {
                    row['player_tag']: f"@{row['tg_username']}"
                    for row in rows
                }
        except Exception as e:
            logger.error(f"❌ Ошибка чтения привязок: {e}")
            return {}


class ClanRepository:
    """Репозиторий кланов"""
    
    def __init__(self, db: Database):
        self.db = db
    
    async def set_active_clan(self, tg_id: int, clan_tag: str) -> bool:
        """Установить активный клан"""
        if not self.db.pool:
            return False
        
        try:
            async with self.db.pool.acquire() as conn:
                await conn.execute('''
                    INSERT INTO user_active_clan (tg_id, clan_tag) 
                    VALUES ($1, $2)
                    ON CONFLICT (tg_id) 
                    DO UPDATE SET 
                        clan_tag = EXCLUDED.clan_tag, 
                        updated_at = CURRENT_TIMESTAMP
                ''', tg_id, clan_tag)
                return True
        except Exception as e:
            logger.error(f"❌ Ошибка смены клана: {e}")
            return False
    
    async def clear_active_clan(self, tg_id: int) -> bool:
        """Сбросить активный клан"""
        if not self.db.pool:
            return False
        
        try:
            async with self.db.pool.acquire() as conn:
                await conn.execute(
                    'DELETE FROM user_active_clan WHERE tg_id = $1', 
                    tg_id
                )
                return True
        except Exception as e:
            logger.error(f"❌ Ошибка сброса клана: {e}")
            return False
    
    async def get_active_clan(self, tg_id: int) -> Optional[str]:
        """Получить активный клан"""
        if not self.db.pool:
            return None
        
        try:
            async with self.db.pool.acquire() as conn:
                row = await conn.fetchrow(
                    'SELECT clan_tag FROM user_active_clan WHERE tg_id = $1',
                    tg_id
                )
                return row['clan_tag'] if row else None
        except Exception as e:
            logger.error(f"❌ Ошибка чтения клана: {e}")
            return None
    
    async def get_clans_with_active_wars(self) -> List[str]:
        """Получить все уникальные активные кланы"""
        if not self.db.pool:
            return []
        
        try:
            async with self.db.pool.acquire() as conn:
                rows = await conn.fetch(
                    'SELECT DISTINCT clan_tag FROM user_active_clan'
                )
                return [row['clan_tag'] for row in rows]
        except Exception as e:
            logger.error(f"❌ Ошибка чтения кланов: {e}")
            return []


class SettingsRepository:
    """Репозиторий настроек"""
    
    def __init__(self, db: Database):
        self.db = db
    
    async def get_user_settings(self, tg_id: int) -> dict:
        """Получить настройки пользователя"""
        if not self.db.pool:
            return self._default_settings()
        
        try:
            async with self.db.pool.acquire() as conn:
                row = await conn.fetchrow(
                    'SELECT * FROM user_settings WHERE tg_id = $1',
                    tg_id
                )
                
                if row:
                    return dict(row)
                else:
                    # Создаем настройки по умолчанию
                    await conn.execute('''
                        INSERT INTO user_settings (tg_id) 
                        VALUES ($1)
                        ON CONFLICT (tg_id) DO NOTHING
                    ''', tg_id)
                    return self._default_settings()
        except Exception as e:
            logger.error(f"❌ Ошибка чтения настроек: {e}")
            return self._default_settings()
    
    async def update_setting(self, tg_id: int, key: str, value: Any) -> bool:
        """Обновить настройку"""
        if not self.db.pool:
            return False
        
        try:
            async with self.db.pool.acquire() as conn:
                await conn.execute(f'''
                    INSERT INTO user_settings (tg_id, {key}) 
                    VALUES ($1, $2)
                    ON CONFLICT (tg_id) 
                    DO UPDATE SET {key} = EXCLUDED.{key}, updated_at = CURRENT_TIMESTAMP
                ''', tg_id, value)
                return True
        except Exception as e:
            logger.error(f"❌ Ошибка обновления настройки: {e}")
            return False
    
    def _default_settings(self) -> dict:
        """Настройки по умолчанию"""
        return {
            'tg_id': 0,
            'war_reminders': True,
            'reminder_4h': True,
            'reminder_1h': True,
            'language': 'ru',
        }


# ============================================================
# 🎮 COC СЕРВИС (с кэшированием)
# ============================================================
class COCService:
    """Сервис для работы с COC API с кэшированием"""
    
    def __init__(self, client: coc.Client, cache: CacheService):
        self.client = client
        self.cache = cache
    
    async def get_clan(self, clan_tag: str) -> Optional[coc.Clan]:
        """Получить информацию о клане с кэшированием"""
        cache_key = f"clan:{clan_tag}"
        
        cached = await self.cache.get(cache_key)
        if cached:
            logger.debug(f"Cache hit: {cache_key}")
            return coc.Clan(data=cached, client=self.client)
        
        try:
            clan = await self.client.get_clan(clan_tag)
            await self.cache.set(cache_key, clan._raw_data, "clan_info")
            return clan
        except coc.NotFound:
            return None
        except Exception as e:
            logger.error(f"Error fetching clan {clan_tag}: {e}")
            raise
    
    async def get_player(self, player_tag: str) -> Optional[coc.Player]:
        """Получить информацию об игроке с кэшированием"""
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
        except Exception as e:
            logger.error(f"Error fetching player {player_tag}: {e}")
            raise
    
    async def get_current_war(self, clan_tag: str) -> Optional[coc.ClanWar]:
        """Получить текущую войну с кэшированием"""
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
        except Exception as e:
            logger.error(f"Error fetching war for {clan_tag}: {e}")
            raise
    
    async def get_league_group(self, clan_tag: str) -> Optional[coc.LeagueGroup]:
        """Получить CWL с кэшированием"""
        cache_key = f"cwl:{clan_tag}"
        
        cached = await self.cache.get(cache_key)
        if cached:
            return coc.LeagueGroup(data=cached, client=self.client)
        
        try:
            league = await self.client.get_league_group(clan_tag)
            await self.cache.set(cache_key, league._raw_data, "cwl_info")
            return league
        except Exception as e:
            logger.error(f"Error fetching CWL for {clan_tag}: {e}")
            raise
    
    async def get_league_war(self, war_tag: str) -> Optional[coc.ClanWar]:
        """Получить CWL войну"""
        try:
            return await self.client.get_league_war(war_tag)
        except Exception as e:
            logger.error(f"Error fetching league war {war_tag}: {e}")
            return None
    
    async def get_war_log(self, clan_tag: str, limit: int = 10) -> List:
        """Получить лог войн"""
        try:
            return await self.client.get_war_log(clan_tag, limit=limit)
        except coc.PrivateWarLog:
            raise
        except Exception as e:
            logger.error(f"Error fetching war log for {clan_tag}: {e}")
            raise


# ============================================================
# 🔐 MIDDLEWARE
# ============================================================
class LoggingMiddleware(BaseMiddleware):
    """Логирование всех запросов"""
    
    async def __call__(self, handler, event, data):
        user_id = event.from_user.id if hasattr(event, 'from_user') else None
        username = event.from_user.username if hasattr(event, 'from_user') else None
        
        if hasattr(event, 'text') and event.text:
            logger.info(f"📥 Message from {user_id} (@{username}): {event.text[:50]}")
        elif hasattr(event, 'data') and event.data:
            logger.info(f"🔘 Callback from {user_id} (@{username}): {event.data}")
        
        start_time = time.time()
        
        try:
            result = await handler(event, data)
            duration = time.time() - start_time
            logger.debug(f"✅ Handled in {duration:.2f}s")
            return result
        except Exception as e:
            logger.error(f"❌ Error: {e}", exc_info=True)
            raise


class RateLimitMiddleware(BaseMiddleware):
    """Rate limiting через in-memory storage"""
    
    def __init__(self):
        self.limits = CONFIG.RATE_LIMITS
        self.user_requests: Dict[str, List[float]] = defaultdict(list)
    
    async def __call__(self, handler, event, data):
        user_id = event.from_user.id if hasattr(event, 'from_user') else None
        if not user_id:
            return await handler(event, data)
        
        # Определяем тип действия
        action = "default"
        if hasattr(event, 'data') and event.data:
            action = event.data
        elif hasattr(event, 'text') and event.text and event.text.startswith('/'):
            action = event.text.split('@')[0][1:]
        
        limit, window = self.limits.get(action, self.limits["default"])
        
        # Очищаем старые запросы
        now = time.time()
        key = f"{user_id}:{action}"
        self.user_requests[key] = [
            t for t in self.user_requests[key] 
            if now - t < window
        ]
        
        # Проверяем лимит
        if len(self.user_requests[key]) >= limit:
            if hasattr(event, 'answer'):
                await event.answer("⏳ Слишком много запросов. Подождите...")
            return
        
        self.user_requests[key].append(now)
        return await handler(event, data)


class ThrottleMiddleware(BaseMiddleware):
    """Защита от двойных кликов"""
    
    def __init__(self):
        self.last_clicks: Dict[str, float] = {}
        self.throttle_time = 1.5
    
    async def __call__(self, handler, event, data):
        if not isinstance(event, CallbackQuery):
            return await handler(event, data)
        
        user_id = event.from_user.id
        action = event.data or "unknown"
        key = f"{user_id}:{action}"
        
        now = time.time()
        last = self.last_clicks.get(key, 0)
        
        if now - last < self.throttle_time:
            await event.answer()
            return
        
        self.last_clicks[key] = now
        return await handler(event, data)


class BanCheckMiddleware(BaseMiddleware):
    """Проверка бана пользователя"""
    
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
    """Генераторы клавиатур"""
    
    @staticmethod
    def main_menu() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⚔️ ВОЙНА", callback_data="menu_war")],
            [InlineKeyboardButton(text="🏆 CWL", callback_data="menu_cwl")],
            [InlineKeyboardButton(text="🏰 КЛАН", callback_data="menu_clan")],
            [InlineKeyboardButton(text="👤 ПРОФИЛЬ", callback_data="menu_profile")],
            [InlineKeyboardButton(text="🔗 Привязать | 🎯 Клан", callback_data="link_account")],
        ])
    
    @staticmethod
    def war_menu() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🧠 AI План", callback_data="war_plan")],
            [InlineKeyboardButton(text="📋 Отчет (.md)", callback_data="war_report")],
            [InlineKeyboardButton(text="📜 История войн", callback_data="war_history")],
            [InlineKeyboardButton(text="⏰ Кто не атаковал", callback_data="remind_full")],
            [InlineKeyboardButton(text="📊 Лог атак", callback_data="attack_logs")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_main")],
        ])
    
    @staticmethod
    def cwl_menu() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🏆 Текущая лига", callback_data="cwl_current")],
            [InlineKeyboardButton(text="👥 Состав группы", callback_data="cwl_group")],
            [InlineKeyboardButton(text="⭐ Звезды участников", callback_data="cwl_stars")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_main")],
        ])
    
    @staticmethod
    def clan_menu() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="ℹ️ Инфо", callback_data="clan_info")],
            [InlineKeyboardButton(text="👥 Участники", callback_data="clan_members")],
            [InlineKeyboardButton(text="🎁 Пожертвования", callback_data="clan_donations")],
            [InlineKeyboardButton(text="🏰 Столица", callback_data="clan_capital")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_main")],
        ])
    
    @staticmethod
    def profile_menu() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📊 Моя статистика", callback_data="my_stats")],
            [InlineKeyboardButton(text="🎮 Мои аккаунты", callback_data="my_accounts")],
            [InlineKeyboardButton(text="⚙️ Настройки", callback_data="settings_menu")],
            [InlineKeyboardButton(text="📤 Экспорт статистики", callback_data="export_stats")],
            [InlineKeyboardButton(text="🗑 Удалить активный клан", callback_data="clear_active_clan")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_main")],
        ])
    
    @staticmethod
    def settings_menu() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔔 Напоминания о войне", callback_data="toggle_war_reminders")],
            [InlineKeyboardButton(text="⏰ За 4 часа", callback_data="toggle_reminder_4h")],
            [InlineKeyboardButton(text="⏰ За 1 час", callback_data="toggle_reminder_1h")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="menu_profile")],
        ])
    
    @staticmethod
    def back(dest: str = "back_main") -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data=dest)]
        ])
    
    @staticmethod
    def confirm(action: str, text_yes: str = "✅ Да", text_no: str = "❌ Нет") -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=text_yes, callback_data=f"{action}_yes")],
            [InlineKeyboardButton(text=text_no, callback_data=f"{action}_no")]
        ])


# ============================================================
# 🔄 FSM СОСТОЯНИЯ
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

# --- Common Router ---
common_router = Router()

@common_router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    """Команда /start"""
    if message.from_user.is_bot:
        return
    
    tg_id = message.from_user.id
    
    # Сохраняем пользователя
    user_repo = message.bot.user_repo
    await user_repo.upsert_user(tg_id, message.from_user.username)
    
    clan_repo = message.bot.clan_repo
    active_clan = await clan_repo.get_active_clan(tg_id)
    
    text = f"👋 Привет, {message.from_user.first_name}!\n\n"
    
    if active_clan:
        text += f"🏰 Клан: <code>{active_clan}</code>\n\nВыбери раздел:"
        await message.answer(
            text, 
            parse_mode="HTML", 
            reply_markup=Keyboards.main_menu()
        )
    else:
        text += (
            "⚠️ Клан не выбран.\n\n"
            "🔗 /link - привязать аккаунт\n"
            "🎯 /set_clan - указать клан\n"
            "🗑 /unlink - управление аккаунтами"
        )
        await message.answer(text, parse_mode="HTML")

@common_router.message(Command("help"))
async def cmd_help(message: Message):
    """Команда /help"""
    if message.from_user.is_bot:
        return
    await cmd_start(message)

@common_router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    """Команда /cancel"""
    if message.from_user.is_bot:
        return
    await state.clear()
    await message.answer("❌ Отменено.")


# --- War Router ---
war_router = Router()

@war_router.callback_query(F.data == "menu_war")
async def cb_menu_war(callback: CallbackQuery):
    """Меню войны"""
    await callback.answer()
    await callback.message.edit_text(
        "⚔️ <b>ВОЙНА:</b>", 
        parse_mode="HTML", 
        reply_markup=Keyboards.war_menu()
    )

@war_router.callback_query(F.data == "war_plan")
async def cb_war_plan(callback: CallbackQuery):
    """AI план войны"""
    await callback.answer("⏳ Формирую план...")
    await handle_war_plan(callback, generate_report=False)

@war_router.callback_query(F.data == "war_report")
async def cb_war_report(callback: CallbackQuery):
    """Отчет войны"""
    await callback.answer("⏳ Генерирую отчет...")
    await handle_war_plan(callback, generate_report=True)

@war_router.callback_query(F.data == "war_history")
async def cb_war_history(callback: CallbackQuery):
    """История войн"""
    await callback.answer("⏳ Загружаю историю...")
    await handle_war_history(callback)

@war_router.callback_query(F.data == "remind_full")
async def cb_remind(callback: CallbackQuery):
    """Кто не атаковал"""
    await callback.answer()
    await handle_remind_full(callback)

@war_router.callback_query(F.data == "attack_logs")
async def cb_logs(callback: CallbackQuery):
    """Лог атак"""
    await callback.answer()
    await handle_attack_logs(callback)


# --- CWL Router ---
cwl_router = Router()

@cwl_router.callback_query(F.data == "menu_cwl")
async def cb_menu_cwl(callback: CallbackQuery):
    """Меню CWL"""
    await callback.answer()
    await callback.message.edit_text(
        "🏆 <b>CWL:</b>", 
        parse_mode="HTML", 
        reply_markup=Keyboards.cwl_menu()
    )

@cwl_router.callback_query(F.data == "cwl_current")
async def cb_cwl_current(callback: CallbackQuery):
    """Текущая CWL"""
    await callback.answer("⏳ Загружаю...")
    await handle_cwl_current(callback)

@cwl_router.callback_query(F.data == "cwl_group")
async def cb_cwl_group(callback: CallbackQuery):
    """Группа CWL"""
    await callback.answer("⏳ Загружаю...")
    await handle_cwl_group(callback)

@cwl_router.callback_query(F.data == "cwl_stars")
async def cb_cwl_stars(callback: CallbackQuery):
    """Звезды CWL"""
    await callback.answer("⏳ Загружаю...")
    await handle_cwl_stars(callback)


# --- Clan Router ---
clan_router = Router()

@clan_router.callback_query(F.data == "menu_clan")
async def cb_menu_clan(callback: CallbackQuery):
    """Меню клана"""
    await callback.answer()
    await callback.message.edit_text(
        "🏰 <b>КЛАН:</b>", 
        parse_mode="HTML", 
        reply_markup=Keyboards.clan_menu()
    )

@clan_router.callback_query(F.data == "clan_info")
async def cb_clan_info(callback: CallbackQuery):
    """Инфо о клане"""
    await callback.answer()
    await handle_clan_info(callback)

@clan_router.callback_query(F.data == "clan_members")
async def cb_clan_members(callback: CallbackQuery):
    """Участники клана"""
    await callback.answer()
    await handle_clan_members(callback)

@clan_router.callback_query(F.data == "clan_donations")
async def cb_clan_donations(callback: CallbackQuery):
    """Пожертвования"""
    await callback.answer()
    await handle_clan_donations(callback)

@clan_router.callback_query(F.data == "clan_capital")
async def cb_clan_capital(callback: CallbackQuery):
    """Столица клана"""
    await callback.answer("⏳ Загружаю...")
    await handle_clan_capital(callback)


# --- Profile Router ---
profile_router = Router()

@profile_router.callback_query(F.data == "menu_profile")
async def cb_menu_profile(callback: CallbackQuery):
    """Меню профиля"""
    await callback.answer()
    await callback.message.edit_text(
        "👤 <b>ПРОФИЛЬ:</b>", 
        parse_mode="HTML", 
        reply_markup=Keyboards.profile_menu()
    )

@profile_router.callback_query(F.data == "my_stats")
async def cb_my_stats(callback: CallbackQuery):
    """Моя статистика"""
    await callback.answer()
    await handle_my_stats(callback)

@profile_router.callback_query(F.data == "my_accounts")
async def cb_my_accounts(callback: CallbackQuery):
    """Мои аккаунты"""
    await callback.answer()
    await handle_my_accounts(callback)

@profile_router.callback_query(F.data == "settings_menu")
async def cb_settings_menu(callback: CallbackQuery):
    """Меню настроек"""
    await callback.answer()
    settings_repo = callback.bot.settings_repo
    settings = await settings_repo.get_user_settings(callback.from_user.id)
    
    text = "⚙️ <b>НАСТРОЙКИ УВЕДОМЛЕНИЙ</b>\n\n"
    text += f"🔔 Напоминания: {'✅' if settings['war_reminders'] else '❌'}\n"
    text += f"⏰ За 4 часа: {'✅' if settings['reminder_4h'] else '❌'}\n"
    text += f"⏰ За 1 час: {'✅' if settings['reminder_1h'] else '❌'}\n"
    
    await callback.message.edit_text(
        text, 
        parse_mode="HTML", 
        reply_markup=Keyboards.settings_menu()
    )

@profile_router.callback_query(F.data.startswith("toggle_"))
async def cb_toggle_setting(callback: CallbackQuery):
    """Переключение настройки"""
    setting_key = callback.data.replace("toggle_", "")
    settings_repo = callback.bot.settings_repo
    settings = await settings_repo.get_user_settings(callback.from_user.id)
    
    new_value = not settings.get(setting_key, False)
    await settings_repo.update_setting(callback.from_user.id, setting_key, new_value)
    
    await callback.answer(f"✅ {'Включено' if new_value else '❌ Выключено'}")
    await cb_settings_menu(callback)

@profile_router.callback_query(F.data == "export_stats")
async def cb_export_stats(callback: CallbackQuery):
    """Экспорт статистики в CSV"""
    await callback.answer("⏳ Генерирую файл...")
    
    account_repo = callback.bot.account_repo
    coc_service = callback.bot.coc_service
    
    accounts = await account_repo.get_user_accounts(callback.from_user.id)
    
    if not accounts:
        await callback.message.edit_text(
            "🔗 Нет привязанных аккаунтов",
            reply_markup=Keyboards.back("menu_profile")
        )
        return
    
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Имя', 'Тег', 'ТХ', 'Трофеи', 'Атак побед', 'Донат', 'Получено'])
    
    for acc in accounts:
        try:
            player = await coc_service.get_player(acc['player_tag'])
            writer.writerow([
                player.name,
                player.tag,
                player.town_hall,
                player.trophies,
                player.attack_wins,
                player.donations,
                player.donations_received
            ])
        except Exception as e:
            logger.error(f"Error exporting {acc['player_tag']}: {e}")
            writer.writerow([acc['player_name'], acc['player_tag'], 'Ошибка', '', '', '', ''])
    
    file = BufferedInputFile(
        output.getvalue().encode('utf-8'),
        filename="my_stats.csv"
    )
    
    await callback.message.answer_document(
        file,
        caption="📊 Ваша статистика",
        reply_markup=Keyboards.back("menu_profile")
    )

@profile_router.callback_query(F.data == "clear_active_clan")
async def cb_clear_active_clan(callback: CallbackQuery):
    """Сброс активного клана"""
    await callback.answer()
    await callback.message.edit_text(
        "⚠️ <b>Сброс активного клана</b>\n\n"
        "Вы уверены? Вам придётся заново выбрать клан.",
        parse_mode="HTML",
        reply_markup=Keyboards.confirm("confirm_clear_clan")
    )

@profile_router.callback_query(F.data == "confirm_clear_clan_yes")
async def cb_confirm_clear_clan_yes(callback: CallbackQuery):
    """Подтверждение сброса"""
    clan_repo = callback.bot.clan_repo
    await clan_repo.clear_active_clan(callback.from_user.id)
    
    await callback.message.edit_text(
        "✅ Активный клан сброшен.\n\n"
        "Используйте /link или /set_clan.",
        parse_mode="HTML",
        reply_markup=Keyboards.back("menu_profile")
    )

@profile_router.callback_query(F.data == "confirm_clear_clan_no")
async def cb_confirm_clear_clan_no(callback: CallbackQuery):
    """Отмена сброса"""
    await callback.message.edit_text(
        "❌ Отменено",
        reply_markup=Keyboards.back("menu_profile")
    )


# --- Admin Router ---
admin_router = Router()

@admin_router.message(Command("admin"))
async def cmd_admin(message: Message):
    """Админ-панель"""
    if message.from_user.id not in CONFIG.ADMIN_IDS:
        await message.answer("🚫 Доступ запрещен")
        return
    
    user_repo = message.bot.user_repo
    stats = await user_repo.get_statistics()
    
    text = (
        "🔧 <b>АДМИН-ПАНЕЛЬ</b>\n\n"
        f"👥 Всего пользователей: <code>{stats['total_users']}</code>\n"
        f"🔗 Всего аккаунтов: <code>{stats['total_accounts']}</code>\n"
        f"🏰 Активных кланов: <code>{stats['active_clans']}</code>\n\n"
        "<b>Команды:</b>\n"
        "/ban <code>user_id</code> - заблокировать\n"
        "/unban <code>user_id</code> - разблокировать\n"
        "/broadcast <code>текст</code> - рассылка\n"
        "/stats - детальная статистика"
    )
    
    await message.answer(text, parse_mode="HTML")

@admin_router.message(Command("ban"))
async def cmd_ban(message: Message, command: CommandObject):
    """Бан пользователя"""
    if message.from_user.id not in CONFIG.ADMIN_IDS:
        await message.answer("🚫 Доступ запрещен")
        return
    
    if not command.args:
        await message.answer("❌ Укажите user_id: /ban 123456789")
        return
    
    try:
        user_id = int(command.args)
        user_repo = message.bot.user_repo
        await user_repo.ban_user(user_id)
        await message.answer(f"✅ Пользователь {user_id} заблокирован")
    except ValueError:
        await message.answer("❌ Неверный user_id")

@admin_router.message(Command("unban"))
async def cmd_unban(message: Message, command: CommandObject):
    """Разбан пользователя"""
    if message.from_user.id not in CONFIG.ADMIN_IDS:
        await message.answer("🚫 Доступ запрещен")
        return
    
    if not command.args:
        await message.answer("❌ Укажите user_id: /unban 123456789")
        return
    
    try:
        user_id = int(command.args)
        user_repo = message.bot.user_repo
        await user_repo.unban_user(user_id)
        await message.answer(f"✅ Пользователь {user_id} разблокирован")
    except ValueError:
        await message.answer("❌ Неверный user_id")

@admin_router.message(Command("broadcast"))
async def cmd_broadcast(message: Message, command: CommandObject):
    """Рассылка сообщений"""
    if message.from_user.id not in CONFIG.ADMIN_IDS:
        await message.answer("🚫 Доступ запрещен")
        return
    
    if not command.args:
        await message.answer("❌ Укажите текст: /broadcast Текст")
        return
    
    text = command.args
    user_repo = message.bot.user_repo
    users = await user_repo.get_all_active_users()
    
    success = 0
    failed = 0
    
    for user_id in users:
        try:
            await message.bot.send_message(user_id, text)
            success += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            failed += 1
            logger.error(f"Broadcast failed to {user_id}: {e}")
    
    await message.answer(
        f"📤 Рассылка завершена\n✅ Успешно: {success}\n❌ Ошибок: {failed}"
    )

@admin_router.message(Command("stats"))
async def cmd_stats(message: Message):
    """Детальная статистика"""
    if message.from_user.id not in CONFIG.ADMIN_IDS:
        await message.answer("🚫 Доступ запрещен")
        return
    
    user_repo = message.bot.user_repo
    stats = await user_repo.get_statistics()
    
    text = (
        "📊 <b>ДЕТАЛЬНАЯ СТАТИСТИКА</b>\n\n"
        f"👥 Пользователей: <code>{stats['total_users']}</code>\n"
        f"🔗 Аккаунтов: <code>{stats['total_accounts']}</code>\n"
        f"🏰 Кланов: <code>{stats['active_clans']}</code>\n"
    )
    
    await message.answer(text, parse_mode="HTML")

@admin_router.message(Command("debug"))
async def cmd_debug(message: Message):
    """Debug информация"""
    if message.from_user.id not in CONFIG.ADMIN_IDS:
        await message.answer("🚫 Доступ запрещен")
        return
    
    tg_id = message.from_user.id
    clan_repo = message.bot.clan_repo
    account_repo = message.bot.account_repo
    
    active = await clan_repo.get_active_clan(tg_id)
    accs = await account_repo.get_user_accounts(tg_id)
    
    text = (
        f"🔧 <b>DEBUG INFO</b>\n\n"
        f"👤 <b>Ваши данные:</b>\n"
        f"   Активный клан: <code>{active or 'НЕТ'}</code>\n"
        f"   Аккаунтов: <code>{len(accs)}</code>\n"
    )
    for a in accs:
        text += f"   • {a['player_name']} (<code>{a['player_tag']}</code>) → <code>{a['clan_tag']}</code>\n"
    
    await message.answer(text, parse_mode="HTML")


# --- Link/Unlink Router ---
link_router = Router()

@link_router.callback_query(F.data == "back_main")
async def cb_back_main(callback: CallbackQuery):
    """Назад в главное меню"""
    await callback.answer()
    
    clan_repo = callback.bot.clan_repo
    active = await clan_repo.get_active_clan(callback.from_user.id)
    
    text = "👋 Главное меню\n\n"
    text += f"🏰 Клан: <code>{active}</code>" if active else "⚠️ Клан не выбран"
    
    await callback.message.edit_text(
        text, 
        parse_mode="HTML", 
        reply_markup=Keyboards.main_menu()
    )

@link_router.message(Command("set_clan"))
@link_router.callback_query(F.data == "set_clan_menu")
async def cmd_set_clan(update, state: FSMContext):
    """Установить клан"""
    if isinstance(update, CallbackQuery):
        await update.answer()
        msg = update.message
    else:
        if update.from_user.is_bot:
            return
        msg = update
    
    await msg.answer(
        "🎯 Отправь тег клана (например, <code>#2CY00G2VU</code>)", 
        parse_mode="HTML"
    )
    await state.set_state(SetClan.waiting_for_clan_tag)

@link_router.message(SetClan.waiting_for_clan_tag)
async def process_clan_tag(message: Message, state: FSMContext):
    """Обработка тега клана"""
    tag = message.text.strip()
    
    if not Validators.validate_coc_tag(tag):
        await message.answer("❌ Неверный формат тега")
        return
    
    tag = Validators.normalize_tag(tag)
    
    coc_service = message.bot.coc_service
    if not coc_service:
        await message.answer("❌ COC не подключен")
        await state.clear()
        return
    
    try:
        clan = await coc_service.get_clan(tag)
        if not clan:
            await message.answer("❌ Клан не найден")
            await state.clear()
            return
        
        await state.update_data(clan_tag=tag, clan_name=clan.name)
        
        await message.answer(
            f"🏰 <b>{clan.name}</b>\n"
            f"Тег: <code>{tag}</code>\n"
            f"Ур: <code>{clan.level}</code>\n\n"
            f"Установить?",
            parse_mode="HTML",
            reply_markup=Keyboards.confirm("confirm_clan")
        )
        await state.set_state(SetClan.waiting_for_clan_confirmation)
    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)[:100]}")
        await state.clear()

@link_router.callback_query(F.data == "confirm_clan_yes", SetClan.waiting_for_clan_confirmation)
async def confirm_clan_yes(callback: CallbackQuery, state: FSMContext):
    """Подтверждение клана"""
    data = await state.get_data()
    clan_repo = callback.bot.clan_repo
    await clan_repo.set_active_clan(callback.from_user.id, data['clan_tag'])
    
    await callback.message.edit_text(
        f"✅ Клан установлен: <b>{data['clan_name']}</b>",
        parse_mode="HTML"
    )
    await state.clear()

@link_router.callback_query(F.data == "confirm_clan_no", SetClan.waiting_for_clan_confirmation)
async def confirm_clan_no(callback: CallbackQuery, state: FSMContext):
    """Отмена клана"""
    await callback.message.edit_text("❌ Отменено")
    await state.clear()

@link_router.message(Command("link"))
@link_router.callback_query(F.data == "link_account")
async def cmd_link(update, state: FSMContext):
    """Привязать аккаунт"""
    if isinstance(update, CallbackQuery):
        await update.answer()
        msg = update.message
    else:
        if update.from_user.is_bot:
            return
        msg = update
    
    await msg.answer(
        "🔗 Отправь тег игрока (например, <code>#QV2Q9V8L2</code>)", 
        parse_mode="HTML"
    )
    await state.set_state(LinkAccount.waiting_for_tag)

@link_router.message(LinkAccount.waiting_for_tag)
async def process_tag(message: Message, state: FSMContext):
    """Обработка тега игрока"""
    tag = message.text.strip()
    
    if not Validators.validate_coc_tag(tag):
        await message.answer("❌ Неверный формат тега")
        return
    
    tag = Validators.normalize_tag(tag)
    
    coc_service = message.bot.coc_service
    if not coc_service:
        await message.answer("❌ COC не подключен")
        await state.clear()
        return
    
    try:
        player = await coc_service.get_player(tag)
        if not player:
            await message.answer("❌ Игрок не найден")
            await state.clear()
            return
        
        clan_tag = player.clan.tag if player.clan else None
        await state.update_data(
            player_tag=tag,
            player_name=player.name,
            clan_tag=clan_tag
        )
        
        clan_info = f"🏰 Клан: <code>{clan_tag}</code>" if clan_tag else "⚠️ Не в клане"
        
        await message.answer(
            f"🔍 <b>{player.name}</b> (ТХ{player.town_hall})\n"
            f"Тег: <code>{tag}</code>\n"
            f"{clan_info}",
            parse_mode="HTML",
            reply_markup=Keyboards.confirm("confirm_link")
        )
        await state.set_state(LinkAccount.waiting_for_confirmation)
    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)[:100]}")
        await state.clear()

@link_router.callback_query(F.data == "confirm_link_yes", LinkAccount.waiting_for_confirmation)
async def confirm_link_yes(callback: CallbackQuery, state: FSMContext):
    """Подтверждение привязки"""
    data = await state.get_data()
    
    if not data.get('clan_tag'):
        await callback.message.edit_text("⚠️ Игрок не в клане")
        await state.clear()
        return
    
    account_repo = callback.bot.account_repo
    success = await account_repo.link_account(
        callback.from_user.id,
        callback.from_user.username or "",
        data['player_tag'],
        data['player_name'],
        data['clan_tag']
    )
    
    if success:
        await callback.message.edit_text(
            f"✅ <b>{data['player_name']}</b> привязан!\n"
            f"🏰 <code>{data['clan_tag']}</code>",
            parse_mode="HTML"
        )
    else:
        await callback.message.edit_text("❌ Ошибка сохранения")
    
    await state.clear()

@link_router.callback_query(F.data == "confirm_link_no", LinkAccount.waiting_for_confirmation)
async def confirm_link_no(callback: CallbackQuery, state: FSMContext):
    """Отмена привязки"""
    await callback.message.edit_text("❌ Отменено")
    await state.clear()

@link_router.message(Command("unlink"))
async def cmd_unlink(message: Message):
    """Управление аккаунтами"""
    if message.from_user.is_bot:
        return
    
    account_repo = message.bot.account_repo
    accounts = await account_repo.get_user_accounts(message.from_user.id)
    
    if not accounts:
        await message.answer(
            "🔗 У вас нет привязанных аккаунтов.\n/link - чтобы привязать."
        )
        return
    
    kb_buttons = []
    text = "🗑 <b>Выберите аккаунт для удаления:</b>\n\n"
    
    for a in accounts:
        text += f"• {a['player_name']} (<code>{a['player_tag']}</code>)\n"
        kb_buttons.append([InlineKeyboardButton(
            text=f"🗑 {a['player_name']}",
            callback_data=f"unlink_{a['player_tag']}"
        )])
    
    kb_buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="back_main")])
    
    await message.answer(
        text, 
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_buttons)
    )

@link_router.callback_query(F.data.startswith("unlink_"))
async def cb_unlink_request(callback: CallbackQuery):
    """Запрос удаления"""
    player_tag = callback.data.replace("unlink_", "")
    
    account_repo = callback.bot.account_repo
    accounts = await account_repo.get_user_accounts(callback.from_user.id)
    
    player_name = "Неизвестно"
    for a in accounts:
        if a['player_tag'] == player_tag:
            player_name = a['player_name']
            break
    
    await callback.message.edit_text(
        f"⚠️ <b>Подтверждение удаления</b>\n\n"
        f"Удалить аккаунт <b>{player_name}</b> (<code>{player_tag}</code>)?\n\n"
        f"Это действие нельзя отменить!",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"confirm_unlink_{player_tag}")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="my_accounts")],
        ])
    )
    await callback.answer()

@link_router.callback_query(F.data.startswith("confirm_unlink_"))
async def cb_unlink_confirm(callback: CallbackQuery):
    """Подтверждение удаления"""
    player_tag = callback.data.replace("confirm_unlink_", "")
    
    account_repo = callback.bot.account_repo
    success = await account_repo.unlink_account(callback.from_user.id, player_tag)
    
    if success:
        await callback.answer("✅ Аккаунт удален!", show_alert=True)
    else:
        await callback.answer("❌ Ошибка удаления", show_alert=True)
    
    await handle_my_accounts(callback)

@link_router.callback_query(F.data.startswith("set_active_"))
async def cb_set_active_clan(callback: CallbackQuery):
    """Установить активный клан"""
    player_tag = callback.data.replace("set_active_", "")
    
    account_repo = callback.bot.account_repo
    accounts = await account_repo.get_user_accounts(callback.from_user.id)
    
    clan_tag = None
    for a in accounts:
        if a['player_tag'] == player_tag:
            clan_tag = a['clan_tag']
            break
    
    if clan_tag:
        clan_repo = callback.bot.clan_repo
        await clan_repo.set_active_clan(callback.from_user.id, clan_tag)
        await callback.answer(f"✅ Активный клан: {clan_tag}", show_alert=True)
    else:
        await callback.answer("❌ Клан не найден", show_alert=True)
    
    await handle_my_accounts(callback)


# ============================================================
# 🎯 ОБРАБОТЧИКИ (Handlers)
# ============================================================

async def check_user_clan(callback_or_message) -> Optional[str]:
    """Проверить наличие активного клана"""
    tg_id = callback_or_message.from_user.id
    clan_repo = callback_or_message.bot.clan_repo
    clan_tag = await clan_repo.get_active_clan(tg_id)
    
    if not clan_tag:
        text = (
            "⚠️ <b>Клан не выбран!</b>\n\n"
            "🔗 /link - привязать аккаунт\n"
            "🎯 /set_clan - указать клан"
        )
        
        if isinstance(callback_or_message, CallbackQuery):
            await callback_or_message.message.answer(
                text, 
                parse_mode="HTML"
            )
        else:
            await callback_or_message.answer(
                text, 
                parse_mode="HTML"
            )
        return None
    
    return clan_tag


async def handle_cwl_current(callback: CallbackQuery):
    """Текущая CWL"""
    clan_tag = await check_user_clan(callback)
    if not clan_tag:
        return
    
    coc_service = callback.bot.coc_service
    if not coc_service:
        return
    
    try:
        league = await coc_service.get_league_group(clan_tag)
        if not league or league.state == 'notInWar':
            await callback.message.answer(
                "🔍 Нет активной CWL.",
                reply_markup=Keyboards.back("menu_cwl")
            )
            return
        
        text = f"🏆 <b>CWL — {league.state.upper()}</b>\n\n"
        text += f"📅 Сезон: <code>{league.season}</code>\n"
        text += f"👥 Кланы: <code>{len(league.clans)}</code>\n\n"
        text += "<b>📊 Таблица:</b>\n"
        
        for i, clan in enumerate(league.clans, 1):
            text += f"<code>{i}.</code> {clan.name} (Ур.{clan.level})\n"
        
        await callback.message.answer(
            text, 
            parse_mode="HTML",
            reply_markup=Keyboards.back("menu_cwl")
        )
    except Exception as e:
        logger.error(f"CWL error: {e}")
        await callback.message.answer(f"❌ Ошибка: {str(e)[:100]}")


async def handle_cwl_group(callback: CallbackQuery):
    """Группа CWL"""
    clan_tag = await check_user_clan(callback)
    if not clan_tag:
        return
    
    coc_service = callback.bot.coc_service
    if not coc_service:
        return
    
    try:
        league = await coc_service.get_league_group(clan_tag)
        if not league:
            await callback.message.answer("❌ CWL не найдена")
            return
        
        text = "👥 <b>Кланы в группе:</b>\n\n"
        for clan in league.clans:
            text += f"🏰 <b>{clan.name}</b>\n   <code>{clan.tag}</code> (Ур.{clan.level})\n\n"
        
        await callback.message.answer(
            text, 
            parse_mode="HTML",
            reply_markup=Keyboards.back("menu_cwl")
        )
    except Exception as e:
        await callback.message.answer(f"❌ Ошибка: {str(e)[:100]}")


async def handle_cwl_stars(callback: CallbackQuery):
    """Звезды CWL"""
    clan_tag = await check_user_clan(callback)
    if not clan_tag:
        return
    
    coc_service = callback.bot.coc_service
    if not coc_service:
        return
    
    try:
        league = await coc_service.get_league_group(clan_tag)
        if not league:
            await callback.message.answer("❌ CWL не найдена")
            return
        
        player_stars = {}
        
        # Параллельная загрузка войн
        war_tasks = []
        for round_tag in league.rounds:
            if not round_tag:
                continue
            for war_tag in round_tag:
                if war_tag:
                    war_tasks.append(coc_service.get_league_war(war_tag))
        
        wars = await asyncio.gather(*war_tasks, return_exceptions=True)
        
        for war in wars:
            if isinstance(war, Exception) or not war:
                continue
            
            if war.clan and war.clan.tag == clan_tag:
                for member in war.clan.members:
                    for attack in (member.attacks or []):
                        if member.tag not in player_stars:
                            player_stars[member.tag] = {
                                'name': member.name,
                                'stars': 0,
                                'attacks': 0
                            }
                        player_stars[member.tag]['stars'] += attack.stars
                        player_stars[member.tag]['attacks'] += 1
        
        if not player_stars:
            await callback.message.answer(
                "📭 Нет данных CWL",
                reply_markup=Keyboards.back("menu_cwl")
            )
            return
        
        top = sorted(
            player_stars.values(),
            key=lambda x: x['stars'],
            reverse=True
        )[:15]
        
        text = "⭐ <b>ТОП CWL атакующих:</b>\n\n"
        for i, p in enumerate(top, 1):
            avg = p['stars'] / p['attacks'] if p['attacks'] > 0 else 0
            text += (
                f"<code>{i:2d}.</code> {p['name']} — "
                f"<b>{p['stars']}</b>⭐ ({p['attacks']} ат., avg {avg:.1f})\n"
            )
        
        await callback.message.answer(
            text, 
            parse_mode="HTML",
            reply_markup=Keyboards.back("menu_cwl")
        )
    except Exception as e:
        await callback.message.answer(f"❌ Ошибка: {str(e)[:100]}")


async def handle_war_history(callback: CallbackQuery):
    """История войн"""
    clan_tag = await check_user_clan(callback)
    if not clan_tag:
        return
    
    coc_service = callback.bot.coc_service
    if not coc_service:
        return
    
    try:
        wars = await coc_service.get_war_log(clan_tag, limit=10)
        text = "📜 <b>Последние 10 войн:</b>\n\n"
        wins = losses = 0
        
        for i, war in enumerate(wars, 1):
            our, enemy = war.clan, war.opponent
            
            if our.stars > enemy.stars or (
                our.stars == enemy.stars and 
                our.destruction > enemy.destruction
            ):
                status = "🟢 ПОБЕДА"
                wins += 1
            elif our.stars < enemy.stars or (
                our.stars == enemy.stars and 
                our.destruction < enemy.destruction
            ):
                status = "🔴 ПОРАЖЕНИЕ"
                losses += 1
            else:
                status = "🟡 НИЧЬЯ"
            
            end_time = war.end_time
            date_str = f"{end_time.day:02d}.{end_time.month:02d}" if end_time else "???"
            
            text += f"<code>{i:2d}.</code> {date_str} {status}\n"
            text += f"    vs <b>{enemy.name}</b>\n"
            text += (
                f"    ⭐ {our.stars}:{enemy.stars} | "
                f"💥 {our.destruction:.0f}%:{enemy.destruction:.0f}%\n\n"
            )
        
        text += f"📊 <b>Итог:</b> 🟢 {wins} / 🔴 {losses}"
        
        await callback.message.answer(
            text, 
            parse_mode="HTML",
            reply_markup=Keyboards.back("menu_war")
        )
    except coc.PrivateWarLog:
        await callback.message.answer(
            "🔒 Лог войны закрыт",
            reply_markup=Keyboards.back("menu_war")
        )
    except Exception as e:
        await callback.message.answer(f"❌ Ошибка: {str(e)[:100]}")


async def handle_clan_info(callback: CallbackQuery):
    """Инфо о клане"""
    clan_tag = await check_user_clan(callback)
    if not clan_tag:
        return
    
    coc_service = callback.bot.coc_service
    if not coc_service:
        return
    
    try:
        clan = await coc_service.get_clan(clan_tag)
        if not clan:
            await callback.message.answer("❌ Клан не найден")
            return
        
        text = (
            f"🏰 <b>{clan.name}</b> <code>{clan.tag}</code>\n\n"
            f"📊 Уровень: <code>{clan.level}</code>\n"
            f"👥 Участников: <code>{clan.member_count}/50</code>\n"
            f"🏆 Трофеи: <code>{clan.points}</code>\n"
            f"🛡️ Вход: <code>{clan.required_trophies}</code>\n"
            f"🌍 Регион: <code>{clan.location.name if clan.location else 'Global'}</code>\n\n"
            f"📝 <i>{clan.description or 'Нет описания'}</i>"
        )
        
        await callback.message.answer(
            text, 
            parse_mode="HTML",
            reply_markup=Keyboards.back("menu_clan")
        )
    except Exception as e:
        await callback.message.answer(f"❌ Ошибка: {str(e)[:100]}")


async def handle_clan_members(callback: CallbackQuery):
    """Участники клана"""
    clan_tag = await check_user_clan(callback)
    if not clan_tag:
        return
    
    coc_service = callback.bot.coc_service
    if not coc_service:
        return
    
    try:
        clan = await coc_service.get_clan(clan_tag)
        if not clan:
            await callback.message.answer("❌ Клан не найден")
            return
        
        account_repo = callback.bot.account_repo
        tg_mapping = await account_repo.get_linked_accounts_mapping(clan_tag)
        
        text = f"👥 <b>Участники {clan.name}</b> ({clan.member_count}/50):\n\n"
        
        for i, member in enumerate(clan.members, 1):
            name = member.name
            if member.tag in tg_mapping:
                name += f" {tg_mapping[member.tag]}"
            
            role = member.role.name if hasattr(member.role, 'name') else str(member.role)
            text += f"<code>{i:2d}.</code> {name} (ТХ{member.town_hall}) - {role}\n"
        
        await callback.message.answer(
            text, 
            parse_mode="HTML",
            reply_markup=Keyboards.back("menu_clan")
        )
    except Exception as e:
        await callback.message.answer(f"❌ Ошибка: {str(e)[:100]}")


async def handle_clan_donations(callback: CallbackQuery):
    """Пожертвования"""
    clan_tag = await check_user_clan(callback)
    if not clan_tag:
        return
    
    coc_service = callback.bot.coc_service
    if not coc_service:
        return
    
    try:
        clan = await coc_service.get_clan(clan_tag)
        if not clan:
            await callback.message.answer("❌ Клан не найден")
            return
        
        account_repo = callback.bot.account_repo
        tg_mapping = await account_repo.get_linked_accounts_mapping(clan_tag)
        
        members_data = []
        for member in clan.members:
            name = member.name
            if member.tag in tg_mapping:
                name += f" {tg_mapping[member.tag]}"
            
            donated = getattr(member, 'donations', 0) or 0
            received = getattr(member, 'donations_received', 0) or 0
            members_data.append({
                'name': name,
                'donated': donated,
                'received': received
            })
        
        top_donated = sorted(
            members_data,
            key=lambda x: x['donated'],
            reverse=True
        )[:10]
        
        top_received = sorted(
            members_data,
            key=lambda x: x['received'],
            reverse=True
        )[:10]
        
        text = f"🎁 <b>Пожертвования {clan.name}</b>\n\n"
        text += "🏆 <b>ТОП 10 ДАТЕЛЕЙ:</b>\n"
        
        for i, m in enumerate(top_donated, 1):
            text += f"<code>{i:2d}.</code> {m['name']} - <b>{m['donated']}</b>\n"
        
        text += "\n📥 <b>ТОП 10 ПОЛУЧАТЕЛЕЙ:</b>\n"
        
        for i, m in enumerate(top_received, 1):
            text += f"<code>{i:2d}.</code> {m['name']} - <b>{m['received']}</b>\n"
        
        await callback.message.answer(
            text, 
            parse_mode="HTML",
            reply_markup=Keyboards.back("menu_clan")
        )
    except Exception as e:
        logger.error(f"Donations error: {e}", exc_info=True)
        await callback.message.answer(f"❌ Ошибка: {str(e)[:100]}")


async def handle_clan_capital(callback: CallbackQuery):
    """Столица клана"""
    clan_tag = await check_user_clan(callback)
    if not clan_tag:
        return
    
    coc_service = callback.bot.coc_service
    if not coc_service:
        return
    
    try:
        clan = await coc_service.get_clan(clan_tag)
        if not clan:
            await callback.message.answer("❌ Клан не найден")
            return
        
        text = f"🏰 <b>Столица {clan.name}</b>\n\n"
        
        try:
            capital = getattr(clan, 'clan_capital', None)
            if capital:
                text += f"📊 Уровень: <code>{getattr(capital, 'capital_hall_level', '?')}</code>\n"
                districts = getattr(capital, 'districts', []) or []
                
                if districts:
                    text += f"\n🗺️ <b>Районы ({len(districts)}):</b>\n"
                    for d in districts[:10]:
                        text += (
                            f"  • {getattr(d, 'name', '?')} "
                            f"(Ур.{getattr(d, 'district_hall_level', '?')})\n"
                        )
            else:
                text += "⚠️ Данные недоступны\n"
        except Exception as cap_e:
            text += f"⚠️ Ошибка: {cap_e}\n"
        
        await callback.message.answer(
            text, 
            parse_mode="HTML",
            reply_markup=Keyboards.back("menu_clan")
        )
    except Exception as e:
        await callback.message.answer(f"❌ Ошибка: {str(e)[:100]}")


async def handle_my_stats(callback: CallbackQuery):
    """Моя статистика"""
    coc_service = callback.bot.coc_service
    if not coc_service:
        return
    
    account_repo = callback.bot.account_repo
    accounts = await account_repo.get_user_accounts(callback.from_user.id)
    
    if not accounts:
        await callback.message.answer("🔗 Нет привязанных аккаунтов\n/link")
        return
    
    # Параллельная загрузка игроков
    player_tasks = [
        coc_service.get_player(acc['player_tag'])
        for acc in accounts
    ]
    players = await asyncio.gather(*player_tasks, return_exceptions=True)
    
    text = "📊 <b>Ваша статистика</b>\n\n"
    
    for acc, player in zip(accounts, players):
        if isinstance(player, Exception) or not player:
            text += f"👤 {acc['player_name']} - ❌ Ошибка\n\n"
            continue
        
        text += f"👤 <b>{player.name}</b> (<code>{acc['player_tag']}</code>)\n"
        text += f"   🏰 ТХ: <code>{player.town_hall}</code>\n"
        text += f"   🏆 Трофеи: <code>{player.trophies}</code>\n"
        text += f"   ⚔️ Атак побед: <code>{player.attack_wins}</code>\n"
        text += f"   🛡️ Защит побед: <code>{player.defense_wins}</code>\n"
        text += f"   🎁 Донат: <code>{player.donations}</code>\n"
        text += f"   📥 Получено: <code>{player.donations_received}</code>\n\n"
    
    await callback.message.answer(
        text, 
        parse_mode="HTML",
        reply_markup=Keyboards.back("menu_profile")
    )


async def handle_my_accounts(callback: CallbackQuery):
    """Мои аккаунты"""
    account_repo = callback.bot.account_repo
    clan_repo = callback.bot.clan_repo
    
    accounts = await account_repo.get_user_accounts(callback.from_user.id)
    active_clan = await clan_repo.get_active_clan(callback.from_user.id)
    
    if not accounts:
        await callback.message.edit_text(
            "🔗 <b>Нет привязанных аккаунтов.</b>\n\nИспользуйте /link",
            parse_mode="HTML",
            reply_markup=Keyboards.back("menu_profile")
        )
        return
    
    text = f"👤 <b>Ваши аккаунты ({len(accounts)}):</b>\n\n"
    text += f"🏰 <b>Активный клан:</b> <code>{active_clan or 'НЕТ'}</code>\n\n"
    
    kb_buttons = []
    for a in accounts:
        text += f"• <b>{a['player_name']}</b>\n"
        text += f"  Тег: <code>{a['player_tag']}</code>\n"
        text += f"  Клан: <code>{a['clan_tag']}</code>\n\n"
        
        if a['clan_tag'] != active_clan:
            kb_buttons.append([InlineKeyboardButton(
                text=f"🎯 Активный: {a['player_name']}",
                callback_data=f"set_active_{a['player_tag']}"
            )])
        
        kb_buttons.append([InlineKeyboardButton(
            text=f"🗑 Удалить: {a['player_name']}",
            callback_data=f"unlink_{a['player_tag']}"
        )])
    
    kb_buttons.append([InlineKeyboardButton(text="➕ Привязать ещё", callback_data="link_account")])
    kb_buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="menu_profile")])
    
    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_buttons)
    )


async def handle_war_plan(callback: CallbackQuery, generate_report: bool = False):
    """AI план войны"""
    clan_tag = await check_user_clan(callback)
    if not clan_tag:
        return
    
    coc_service = callback.bot.coc_service
    if not coc_service:
        return
    
    try:
        war = await coc_service.get_current_war(clan_tag)
        if not war or war.state == "notInWar":
            await callback.message.answer(
                "🔍 Нет активной войны",
                reply_markup=Keyboards.back("menu_war")
            )
            return
        
        our_clan, enemy_clan = war.clan, war.opponent
        
        # Параллельная загрузка маппинга
        account_repo = callback.bot.account_repo
        tg_mapping = await account_repo.get_linked_accounts_mapping(clan_tag)
        
        # Обработка наших участников
        our_members_info = []
        if our_clan and our_clan.members:
            for m in our_clan.members:
                attacks = getattr(m, 'attacks', []) or []
                display_name = m.name
                if m.tag in tg_mapping:
                    display_name += f" ({tg_mapping[m.tag]})"
                
                our_members_info.append({
                    'obj': m,
                    'name': display_name,
                    'raw_name': m.name,
                    'th': getattr(m, 'town_hall', 0) or 0,
                    'map_pos': getattr(m, 'map_position', '?'),
                    'attacks_count': len(attacks),
                    'attacks': attacks
                })
        
        # Обработка вражеских участников
        enemy_members_info = []
        if enemy_clan and enemy_clan.members:
            for m in enemy_clan.members:
                enemy_members_info.append({
                    'obj': m,
                    'name': m.name,
                    'th': getattr(m, 'town_hall', 0) or 0,
                    'map_pos': getattr(m, 'map_position', '?'),
                    'destruction': getattr(m, 'destruction', 0) or 0,
                })
        
        # Заголовок
        header = (
            f"⚔️ <b>ВОЙНА: {our_clan.name} vs {enemy_clan.name}</b>\n\n"
            f"📊 Счёт: <code>{our_clan.stars}</code> : <code>{enemy_clan.stars}</code>\n"
            f"💥 Разрушение: <code>{our_clan.destruction:.1f}%</code> : <code>{enemy_clan.destruction:.1f}%</code>\n"
            f"⏳ Статус: <code>{war.state}</code>\n"
        )
        
        # Сортировка
        our_sorted_by_pos = sorted(
            our_members_info,
            key=lambda x: (x['map_pos'] if isinstance(x['map_pos'], int) else 99)
        )
        
        roster_lines = []
        lazy_list = []
        
        for m in our_sorted_by_pos:
            pos_str = f"Поз. {m['map_pos']}" if isinstance(m['map_pos'], int) else "Поз. ?"
            att_str = f"{m['attacks_count']}/{war.attacks_per_member}"
            roster_lines.append(
                f"• <code>{pos_str}</code> | {m['name']} (ТХ{m['th']}) - Атаки: {att_str}"
            )
            
            if m['attacks_count'] < war.attacks_per_member:
                mention = f" ({tg_mapping[m['obj'].tag]})" if m['obj'].tag in tg_mapping else ""
                lazy_list.append(
                    f"• {m['raw_name']}{mention} (ТХ{m['th']}) - "
                    f"осталось {war.attacks_per_member - m['attacks_count']}"
                )
        
        our_sorted_by_th = sorted(our_members_info, key=lambda x: x['th'], reverse=True)
        enemy_sorted = sorted(
            enemy_members_info,
            key=lambda x: (x['map_pos'] if isinstance(x['map_pos'], int) else 99)
        )
        
        # Добивающие
        dobiv_role_tags = set([m['obj'].tag for m in our_sorted_by_th[:3]])
        plan_text = "\n🧠 <b>ТАКТИЧЕСКИЙ ПЛАН (AI)</b>\n\n"
        
        dobivators_names = []
        for tag in dobiv_role_tags:
            for m in our_members_info:
                if m['obj'].tag == tag:
                    dobivators_names.append(f"{m['name']} (ТХ{m['th']})")
        
        plan_text += f"🛡️ <b>ДОБИВАЮЩИЕ:</b>\n" + ", ".join(dobivators_names) + "\n\n"
        
        # Первый удар
        first_strike_plan = []
        used_enemy_targets = set()
        
        for attacker in our_sorted_by_pos:
            if attacker['obj'].tag in dobiv_role_tags:
                continue
            if attacker['attacks_count'] >= 1:
                continue
            
            target = None
            
            # Ищем равный ТХ
            for enemy in enemy_sorted:
                if enemy['obj'].tag in used_enemy_targets:
                    continue
                if enemy['th'] == attacker['th']:
                    target = enemy
                    break
            
            # Ищем ТХ +1
            if not target:
                for enemy in enemy_sorted:
                    if enemy['obj'].tag in used_enemy_targets:
                        continue
                    if enemy['th'] == attacker['th'] + 1:
                        target = enemy
                        break
            
            # Ищем ближайший ТХ
            if not target:
                best_diff = 99
                for enemy in enemy_sorted:
                    if enemy['obj'].tag in used_enemy_targets:
                        continue
                    diff = abs(enemy['th'] - attacker['th'])
                    if diff < best_diff:
                        best_diff = diff
                        target = enemy
            
            if target:
                used_enemy_targets.add(target['obj'].tag)
                first_strike_plan.append({'attacker': attacker, 'target': target})
        
        # Таблица 1
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
        
        # Второй удар
        second_strike_plan = []
        
        for attacker in our_sorted_by_pos:
            if attacker['attacks_count'] >= 2:
                continue
            
            is_dobivator = attacker['obj'].tag in dobiv_role_tags
            target = None
            rec = ""
            
            # Ищем добить
            for enemy in enemy_sorted:
                if enemy['destruction'] > 0 and enemy['destruction'] < 100:
                    if not any(e['target']['obj'].tag == enemy['obj'].tag for e in second_strike_plan):
                        target = enemy
                        rec = f"🚑 ДОБИТЬ ({enemy['destruction']}%)"
                        break
            
            if is_dobivator and not target:
                continue
            
            if not target and not is_dobivator:
                for enemy in enemy_sorted:
                    if enemy['obj'].tag in used_enemy_targets:
                        continue
                    if not any(e['target']['obj'].tag == enemy['obj'].tag for e in second_strike_plan):
                        if enemy['th'] <= attacker['th']:
                            target = enemy
                            rec = "🧹 Набивка %"
                            break
                
                if not target:
                    for enemy in enemy_sorted:
                        if not any(e['target']['obj'].tag == enemy['obj'].tag for e in second_strike_plan):
                            target = enemy
                            rec = "Свободная"
                            break
            
            if target:
                second_strike_plan.append({
                    'attacker': attacker,
                    'target': target,
                    'rec': rec
                })
        
        # Таблица 2
        table_2 = PrettyTable()
        table_2.field_names = ["Боец", "Цель", "Рекомендация"]
        table_2.align["Боец"] = "l"
        
        for pair in second_strike_plan:
            a, t = pair['attacker'], pair['target']
            table_2.add_row([
                f"{a['name']} (ТХ{a['th']})",
                f"{t['name']} (ТХ{t['th']})",
                pair['rec']
            ])
        
        if not generate_report:
            await callback.message.answer(header, parse_mode="HTML")
            
            roster_text = "👥 <b>СОСТАВ:</b>\n"
            roster_chunks = []
            current_chunk = roster_text
            
            for line in roster_lines:
                if len(current_chunk) + len(line) + 1 > 3800:
                    roster_chunks.append(current_chunk)
                    current_chunk = line + '\n'
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
            await callback.message.answer(
                f"<b>1️⃣ ОСНОВНОЙ УДАР:</b>\n<pre><code>{table_1}</code></pre>",
                parse_mode="HTML"
            )
            
            if second_strike_plan:
                await callback.message.answer(
                    f"<b>2️⃣ ДОБИВАНИЕ:</b>\n<pre><code>{table_2}</code></pre>",
                    parse_mode="HTML",
                    reply_markup=Keyboards.back("menu_war")
                )
            else:
                await callback.message.answer(
                    "<i>Нет вторых атак.</i>",
                    reply_markup=Keyboards.back("menu_war")
                )
        
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
                
                await callback.message.answer_document(
                    FSInputFile(filename),
                    caption="📁 Отчет войны"
                )
            except Exception as e:
                logger.error(f"Report error: {e}", exc_info=True)
                await callback.message.answer(f"❌ Ошибка отчета: {str(e)[:100]}")
    
    except coc.PrivateWarLog:
        await callback.message.answer(
            "🔒 Лог войны закрыт",
            reply_markup=Keyboards.back("menu_war")
        )
    except Exception as e:
        logger.error(f"War plan error: {e}", exc_info=True)
        await callback.message.answer(f"❌ Ошибка: {str(e)[:100]}")


async def handle_remind_full(callback: CallbackQuery):
    """Кто не атаковал"""
    clan_tag = await check_user_clan(callback)
    if not clan_tag:
        return
    
    coc_service = callback.bot.coc_service
    if not coc_service:
        return
    
    try:
        war = await coc_service.get_current_war(clan_tag)
        if not war or war.state == "notInWar":
            await callback.message.answer("🔍 Войны нет")
            return
        
        account_repo = callback.bot.account_repo
        tg_mapping = await account_repo.get_linked_accounts_mapping(clan_tag)
        
        zero_attacks = []
        one_attack = []
        
        if war.clan and war.clan.members:
            for m in war.clan.members:
                count = len(getattr(m, 'attacks', []) or [])
                mention = f" ({tg_mapping[m.tag]})" if m.tag in tg_mapping else ""
                
                if count == 0:
                    zero_attacks.append(
                        f"• {m.name}{mention} (ТХ{getattr(m, 'town_hall', '?')})"
                    )
                elif count == 1:
                    one_attack.append(
                        f"• {m.name}{mention} (ТХ{getattr(m, 'town_hall', '?')})"
                    )
        
        text = "⏰ <b>СПИСОК НЕДОЧЕТОВ</b>\n\n"
        
        if zero_attacks:
            text += f"🔴 <b>НЕ ХОДИЛИ ({len(zero_attacks)}):</b>\n" + "\n".join(zero_attacks) + "\n\n"
        else:
            text += "🟢 Все сделали 1 атаку\n\n"
        
        if one_attack:
            text += f"🟠 <b>ДОБИТЬ ({len(one_attack)}):</b>\n" + "\n".join(one_attack)
        else:
            text += "🟢 Все атаковали!"
        
        await callback.message.answer(
            text, 
            parse_mode="HTML",
            reply_markup=Keyboards.back("menu_war")
        )
    except Exception as e:
        await callback.message.answer("❌ Ошибка")


async def handle_attack_logs(callback: CallbackQuery):
    """Лог атак"""
    clan_tag = await check_user_clan(callback)
    if not clan_tag:
        return
    
    coc_service = callback.bot.coc_service
    if not coc_service:
        return
    
    try:
        war = await coc_service.get_current_war(clan_tag)
        if not war or war.state == "notInWar":
            await callback.message.answer("🔍 Войны нет")
            return
        
        table = PrettyTable()
        table.field_names = ["Атакующий", "Цель", "Звезды", "%"]
        table.align["Атакующий"] = "l"
        table.align["Цель"] = "l"
        
        count = 0
        if war.clan and war.clan.members:
            for member in war.clan.members:
                attacks = getattr(member, 'attacks', []) or []
                for attack in attacks:
                    if count >= 20:
                        break
                    
                    defender_tag = getattr(attack, 'defender_tag', None)
                    defender = war.opponent.get_member(defender_tag) if defender_tag and war.opponent else None
                    d_name = defender.name if defender else "?"
                    d_th = getattr(defender, 'town_hall', '?') if defender else "?"
                    stars = getattr(attack, 'stars', 0) or 0
                    dest = getattr(attack, 'destruction', 0) or 0
                    
                    table.add_row([
                        member.name,
                        f"{d_name} (ТХ{d_th})",
                        "⭐" * stars,
                        f"{dest}%"
                    ])
                    count += 1
        
        if count == 0:
            await callback.message.answer(
                "📭 Нет атак",
                reply_markup=Keyboards.back("menu_war")
            )
        else:
            text = f"📊 <b>АТАКИ ({count}):</b>\n<pre><code>{table}</code></pre>"
            await callback.message.answer(
                text, 
                parse_mode="HTML",
                reply_markup=Keyboards.back("menu_war")
            )
    except Exception as e:
        await callback.message.answer("❌ Ошибка")


# ============================================================
# 🔔 АВТО-УВЕДОМЛЕНИЯ
# ============================================================
class NotificationService:
    """Сервис уведомлений"""
    
    def __init__(
        self,
        bot: Bot,
        coc_service: COCService,
        clan_repo: ClanRepository,
        account_repo: AccountRepository,
        settings_repo: SettingsRepository
    ):
        self.bot = bot
        self.coc_service = coc_service
        self.clan_repo = clan_repo
        self.account_repo = account_repo
        self.settings_repo = settings_repo
    
    async def auto_war_reminders(self):
        """Авто-проверка войн"""
        if not self.coc_service:
            return
        
        logger.info("🔔 Авто-проверка войн...")
        
        try:
            clans = await self.clan_repo.get_clans_with_active_wars()
            
            for clan_tag in clans:
                try:
                    war = await self.coc_service.get_current_war(clan_tag)
                    if not war or war.state != "inWar":
                        continue
                    
                    end_time = war.end_time
                    if not end_time:
                        continue
                    
                    now = datetime.datetime.now(datetime.timezone.utc)
                    
                    try:
                        end_dt = end_time.raw_time
                        if isinstance(end_dt, str):
                            end_dt = datetime.datetime.fromisoformat(
                                end_dt.replace('Z', '+00:00')
                            )
                        time_left = end_dt - now
                        hours_left = time_left.total_seconds() / 3600
                    except:
                        continue
                    
                    # Проверяем время для напоминания
                    if not (3.5 < hours_left < 4.5 or 0.5 < hours_left < 1.5):
                        continue
                    
                    # Получаем маппинг
                    tg_mapping = await self.account_repo.get_linked_accounts_mapping()
                    
                    lazy = []
                    if war.clan and war.clan.members:
                        for m in war.clan.members:
                            count = len(getattr(m, 'attacks', []) or [])
                            if count < war.attacks_per_member:
                                mention = tg_mapping.get(m.tag, '')
                                lazy.append(
                                    f"• {m.name} {mention} ({war.attacks_per_member - count} ат.)"
                                )
                    
                    if not lazy:
                        continue
                    
                    text = (
                        f"🔔 <b>НАПОМИНАНИЕ!</b>\n\n"
                        f"⏰ До конца войны: <b>{hours_left:.1f} ч.</b>\n"
                        f"🏰 Клан: <code>{clan_tag}</code>\n\n"
                        f"⚠️ <b>Не атаковали:</b>\n" + "\n".join(lazy)
                    )
                    
                    # Отправляем уведомления
                    if self.clan_repo.db.pool:
                        async with self.clan_repo.db.pool.acquire() as conn:
                            users = await conn.fetch(
                                'SELECT tg_id FROM user_active_clan WHERE clan_tag = $1',
                                clan_tag
                            )
                            
                            for user_row in users:
                                try:
                                    await self.bot.send_message(
                                        user_row['tg_id'],
                                        text,
                                        parse_mode="HTML"
                                    )
                                except Exception as send_e:
                                    logger.error(
                                        f"Auto-remind error to {user_row['tg_id']}: {send_e}"
                                    )
                
                except Exception as e:
                    logger.error(f"Auto-check error for {clan_tag}: {e}")
                
                await asyncio.sleep(1)
        
        except Exception as e:
            logger.error(f"Auto reminders error: {e}")


# ============================================================
# 🚀 ЗАПУСК
# ============================================================
async def init_coc_client() -> Optional[coc.Client]:
    """Инициализация COC клиента"""
    for i in range(5):
        try:
            client = coc.Client(proxy=CONFIG.COC_PROXY, throttle_limit=10)
            await client.login(CONFIG.COC_EMAIL, CONFIG.COC_PASSWORD)
            logger.info("✅ COC клиент готов!")
            return client
        except Exception as e:
            logger.error(f"❌ COC ошибка ({5-i}): {e}")
            if i < 4:
                await asyncio.sleep(5)
    
    return None


async def init_redis() -> Optional[aioredis.Redis]:
    """Инициализация Redis"""
    try:
        redis = aioredis.from_url(CONFIG.REDIS_URL, encoding='utf-8')
        await redis.ping()
        logger.info("✅ Redis подключен")
        return redis
    except Exception as e:
        logger.warning(f"⚠️ Redis недоступен, используем in-memory: {e}")
        return None


async def on_startup(app: web.Application):
    """Действия при запуске"""
    # Webhook
    if CONFIG.WEBHOOK_URL:
        await app['bot'].set_webhook(f"{CONFIG.WEBHOOK_URL}/webhook")
        logger.info(f"🌐 Webhook: {CONFIG.WEBHOOK_URL}/webhook")
    
    # Redis
    redis = await init_redis()
    app['redis'] = redis
    
    # Database
    db = Database()
    await db.init()
    app['db'] = db
    
    # Repositories
    user_repo = UserRepository(db)
    account_repo = AccountRepository(db)
    clan_repo = ClanRepository(db)
    settings_repo = SettingsRepository(db)
    
    app['user_repo'] = user_repo
    app['account_repo'] = account_repo
    app['clan_repo'] = clan_repo
    app['settings_repo'] = settings_repo
    
    # COC Client
    coc_client = await init_coc_client()
    app['coc_client'] = coc_client
    
    # Cache
    cache = CacheService(redis)
    app['cache'] = cache
    
    # COC Service
    coc_service = COCService(coc_client, cache) if coc_client else None
    app['coc_service'] = coc_service
    
    # Inject repos to bot
    app['bot'].user_repo = user_repo
    app['bot'].account_repo = account_repo
    app['bot'].clan_repo = clan_repo
    app['bot'].settings_repo = settings_repo
    app['bot'].coc_service = coc_service
    
    # Notification Service
    notification_service = NotificationService(
        app['bot'],
        coc_service,
        clan_repo,
        account_repo,
        settings_repo
    )
    app['notification_service'] = notification_service
    
    # Scheduler
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        notification_service.auto_war_reminders,
        'interval',
        minutes=30,
        id='war_reminders'
    )
    scheduler.start()
    app['scheduler'] = scheduler
    
    logger.info("🔔 Авто-уведомления запущены (каждые 30 мин)")


async def on_shutdown(app: web.Application):
    """Действия при остановке"""
    if 'scheduler' in app:
        app['scheduler'].shutdown()
    
    if 'coc_client' in app and app['coc_client']:
        await app['coc_client'].close()
    
    if 'db' in app:
        await app['db'].close()
    
    if 'cache' in app:
        await app['cache'].close()
    
    if 'redis' in app and app['redis']:
        await app['redis'].close()
    
    await app['bot'].session.close()


def setup_routers(dp: Dispatcher):
    """Регистрация роутеров"""
    dp.include_router(common_router)
    dp.include_router(war_router)
    dp.include_router(cwl_router)
    dp.include_router(clan_router)
    dp.include_router(profile_router)
    dp.include_router(admin_router)
    dp.include_router(link_router)


def setup_middlewares(dp: Dispatcher, user_repo: UserRepository):
    """Регистрация middleware"""
    # Outer middleware (выполняются первыми)
    dp.update.outer_middleware(LoggingMiddleware())
    dp.update.outer_middleware(RateLimitMiddleware())
    
    # Inner middleware
    dp.update.middleware(ThrottleMiddleware())
    dp.update.middleware(BanCheckMiddleware(user_repo))


def main():
    """Главная функция"""
    # Bot
    bot = Bot(token=CONFIG.TELEGRAM_TOKEN)
    
    # Storage (Redis или Memory)
    try:
        redis = aioredis.from_url(CONFIG.REDIS_URL)
        storage = RedisStorage(redis)
        logger.info("✅ Используем Redis storage")
    except:
        storage = MemoryStorage()
        logger.info("⚠️ Используем Memory storage")
    
    # Dispatcher
    dp = Dispatcher(storage=storage)
    
    # Routers
    setup_routers(dp)
    
    # Temp middleware setup (будет обновлен в on_startup)
    db = Database()
    user_repo = UserRepository(db)
    setup_middlewares(dp, user_repo)
    
    # App
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
