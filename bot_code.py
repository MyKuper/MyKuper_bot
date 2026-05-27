import os
import logging
import asyncio
import datetime
from typing import Optional, Dict, List
import asyncpg
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from aiogram.webhook.aiohttp_server import SimpleRequestHandler
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiohttp import web
import coc
from prettytable import PrettyTable
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ============================================================
# ⚙️ НАСТРОЙКИ
# ============================================================
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
COC_EMAIL = os.getenv('COC_EMAIL')
COC_PASSWORD = os.getenv('COC_PASSWORD')
PROXY_URL = os.getenv('COC_PROXY', None)
DATABASE_URL = os.getenv('DATABASE_URL')

ADMIN_IDS = [1810701319]

# ============================================================
# 🛠 ЛОГИРОВАНИЕ
# ============================================================
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ============================================================
# 🗄 POSTGRESQL БАЗА ДАННЫХ
# ============================================================
db_pool: Optional[asyncpg.Pool] = None

async def init_db():
    """Инициализирует соединение с PostgreSQL и создаёт таблицы"""
    global db_pool
    
    if not DATABASE_URL:
        logger.error("❌ DATABASE_URL не задан!")
        return
    
    try:
        db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
        logger.info("✅ Подключение к PostgreSQL установлено")
        
        async with db_pool.acquire() as conn:
            # Таблица пользователей
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    tg_id BIGINT PRIMARY KEY,
                    tg_username TEXT,
                    is_admin BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Таблица привязанных аккаунтов
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
            
            # Таблица активных кланов
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS user_active_clan (
                    tg_id BIGINT PRIMARY KEY REFERENCES users(tg_id),
                    clan_tag TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            logger.info("✅ Таблицы БД созданы/проверены")
    except Exception as e:
        logger.error(f"❌ Ошибка инициализации БД: {e}")
        db_pool = None

async def link_account_db(tg_id: int, tg_username: str, player_tag: str, player_name: str, clan_tag: str):
    """Привязывает аккаунт и устанавливает активный клан"""
    if not db_pool:
        logger.error("❌ БД не подключена")
        return False
    
    try:
        async with db_pool.acquire() as conn:
            # Сохраняем/обновляем пользователя
            await conn.execute('''
                INSERT INTO users (tg_id, tg_username) VALUES ($1, $2)
                ON CONFLICT (tg_id) DO UPDATE SET tg_username = EXCLUDED.tg_username
            ''', tg_id, tg_username)
            
            # Сохраняем аккаунт
            await conn.execute('''
                INSERT INTO linked_accounts (tg_id, player_tag, player_name, clan_tag)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (tg_id, player_tag) 
                DO UPDATE SET player_name = EXCLUDED.player_name, clan_tag = EXCLUDED.clan_tag
            ''', tg_id, player_tag, player_name, clan_tag)
            
            # Устанавливаем активный клан
            await conn.execute('''
                INSERT INTO user_active_clan (tg_id, clan_tag) VALUES ($1, $2)
                ON CONFLICT (tg_id) DO UPDATE SET clan_tag = EXCLUDED.clan_tag, updated_at = CURRENT_TIMESTAMP
            ''', tg_id, clan_tag)
            
            logger.info(f"✅ Привязан: {player_name} ({player_tag}) для {tg_id}")
            return True
    except Exception as e:
        logger.error(f"❌ Ошибка привязки: {e}")
        return False

async def set_user_active_clan(tg_id: int, clan_tag: str):
    """Устанавливает активный клан"""
    if not db_pool:
        return False
    try:
        async with db_pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO user_active_clan (tg_id, clan_tag) VALUES ($1, $2)
                ON CONFLICT (tg_id) DO UPDATE SET clan_tag = EXCLUDED.clan_tag, updated_at = CURRENT_TIMESTAMP
            ''', tg_id, clan_tag)
            return True
    except Exception as e:
        logger.error(f"❌ Ошибка смены клана: {e}")
        return False

async def get_user_active_clan(tg_id: int) -> Optional[str]:
    """Получает активный клан пользователя"""
    if not db_pool:
        return None
    try:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow('SELECT clan_tag FROM user_active_clan WHERE tg_id = $1', tg_id)
            return row['clan_tag'] if row else None
    except Exception as e:
        logger.error(f"❌ Ошибка чтения клана: {e}")
        return None

async def get_linked_accounts_db(tg_id: int = None) -> Dict[str, str]:
    """Возвращает словарь {player_tag: @username}"""
    if not db_pool:
        return {}
    try:
        async with db_pool.acquire() as conn:
            if tg_id:
                rows = await conn.fetch('''
                    SELECT la.player_tag, u.tg_username
                    FROM linked_accounts la
                    JOIN users u ON la.tg_id = u.tg_id
                    WHERE la.tg_id = $1 AND u.tg_username IS NOT NULL AND u.tg_username != ''
                ''', tg_id)
            else:
                rows = await conn.fetch('''
                    SELECT la.player_tag, u.tg_username
                    FROM linked_accounts la
                    JOIN users u ON la.tg_id = u.tg_id
                    WHERE u.tg_username IS NOT NULL AND u.tg_username != ''
                ''')
            return {row['player_tag']: f"@{row['tg_username']}" for row in rows}
    except Exception as e:
        logger.error(f"❌ Ошибка чтения привязок: {e}")
        return {}

async def get_user_accounts_db(tg_id: int) -> List[dict]:
    """Получает все аккаунты пользователя"""
    if not db_pool:
        return []
    try:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch('''
                SELECT player_tag, player_name, clan_tag 
                FROM linked_accounts 
                WHERE tg_id = $1
            ''', tg_id)
            return [dict(row) for row in rows]
    except Exception as e:
        logger.error(f"❌ Ошибка чтения аккаунтов: {e}")
        return []

# ============================================================
# 🤖 ИНИЦИАЛИЗАЦИЯ
# ============================================================
bot = Bot(token=TELEGRAM_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
coc_client: Optional[coc.Client] = None
scheduler = AsyncIOScheduler()

# Анти-спам
_last_clicks = {}

def check_click_throttle(user_id: int, action: str) -> bool:
    key = f"{user_id}:{action}"
    now = datetime.datetime.now().timestamp()
    last = _last_clicks.get(key, 0)
    if now - last < 1.5:
        return False
    _last_clicks[key] = now
    return True

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
# 🧩 HELPERS
# ============================================================
async def get_tg_id(update) -> int:
    if isinstance(update, types.Message):
        return update.from_user.id
    return update.from_user.id

async def check_user_clan(update) -> Optional[str]:
    tg_id = await get_tg_id(update)
    clan_tag = await get_user_active_clan(tg_id)
    if not clan_tag:
        await send_msg(update, 
            "⚠️ <b>Клан не выбран!</b>\n\n"
            "🔗 /link - привязать аккаунт\n"
            "🎯 /set_clan - указать клан",
            parse_mode="HTML")
        return None
    return clan_tag

# ============================================================
# ⌨️ МЕНЮ
# ============================================================
def get_main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚔️ ВОЙНА", callback_data="menu_war")],
        [InlineKeyboardButton(text="🏆 CWL", callback_data="menu_cwl")],
        [InlineKeyboardButton(text="🏰 КЛАН", callback_data="menu_clan")],
        [InlineKeyboardButton(text="👤 ПРОФИЛЬ", callback_data="menu_profile")],
        [InlineKeyboardButton(text="🔗 Привязать | 🎯 Клан", callback_data="link_account")],
    ])

def get_war_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧠 AI План", callback_data="war_plan")],
        [InlineKeyboardButton(text="📋 Отчет (.md)", callback_data="war_report")],
        [InlineKeyboardButton(text="📜 История войн", callback_data="war_history")],
        [InlineKeyboardButton(text="⏰ Кто не атаковал", callback_data="remind_full")],
        [InlineKeyboardButton(text="📊 Лог атак", callback_data="attack_logs")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_main")],
    ])

def get_cwl_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏆 Текущая лига", callback_data="cwl_current")],
        [InlineKeyboardButton(text="👥 Состав группы", callback_data="cwl_group")],
        [InlineKeyboardButton(text="⭐ Звезды участников", callback_data="cwl_stars")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_main")],
    ])

def get_clan_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="ℹ️ Инфо", callback_data="clan_info")],
        [InlineKeyboardButton(text="👥 Участники", callback_data="clan_members")],
        [InlineKeyboardButton(text="🎁 Пожертвования", callback_data="clan_donations")],
        [InlineKeyboardButton(text="🏰 Столица", callback_data="clan_capital")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_main")],
    ])

def get_profile_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Моя статистика", callback_data="my_stats")],
        [InlineKeyboardButton(text="🎮 Мои аккаунты", callback_data="my_accounts")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_main")],
    ])

def get_back_keyboard(dest: str = "back_main") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data=dest)]])

# ============================================================
# 🏆 CWL
# ============================================================
async def handle_cwl_current(update):
    clan_tag = await check_user_clan(update)
    if not clan_tag: return
    if not coc_client: return
    try:
        league = await coc_client.get_league_group(clan_tag)
        if not league or league.state == 'notInWar':
            await send_msg(update, "🔍 Нет активной CWL.", reply_markup=get_back_keyboard("menu_cwl"))
            return
        text = f"🏆 <b>CWL — {league.state.upper()}</b>\n\n"
        text += f"📅 Сезон: <code>{league.season}</code>\n"
        text += f"👥 Кланы: <code>{len(league.clans)}</code>\n\n"
        text += "<b>📊 Таблица:</b>\n"
        for i, clan in enumerate(league.clans, 1):
            text += f"<code>{i}.</code> {clan.name} (Ур.{clan.level})\n"
        await send_msg(update, text, parse_mode="HTML", reply_markup=get_back_keyboard("menu_cwl"))
    except Exception as e:
        logger.error(f"CWL error: {e}")
        await send_msg(update, f"❌ Ошибка: {str(e)[:100]}")

async def handle_cwl_group(update):
    clan_tag = await check_user_clan(update)
    if not clan_tag or not coc_client: return
    try:
        league = await coc_client.get_league_group(clan_tag)
        if not league:
            await send_msg(update, "❌ CWL не найдена.")
            return
        text = f"👥 <b>Кланы в группе:</b>\n\n"
        for clan in league.clans:
            text += f"🏰 <b>{clan.name}</b>\n   <code>{clan.tag}</code> (Ур.{clan.level})\n\n"
        await send_msg(update, text, parse_mode="HTML", reply_markup=get_back_keyboard("menu_cwl"))
    except Exception as e:
        await send_msg(update, f"❌ Ошибка: {str(e)[:100]}")

async def handle_cwl_stars(update):
    clan_tag = await check_user_clan(update)
    if not clan_tag or not coc_client: return
    try:
        league = await coc_client.get_league_group(clan_tag)
        if not league:
            await send_msg(update, "❌ CWL не найдена.")
            return
        player_stars = {}
        for round_tag in league.rounds:
            if not round_tag: continue
            for war_tag in round_tag:
                if not war_tag: continue
                try:
                    war = await coc_client.get_league_war(war_tag)
                    if war.clan and war.clan.tag == clan_tag:
                        for member in war.clan.members:
                            for attack in (member.attacks or []):
                                if member.tag not in player_stars:
                                    player_stars[member.tag] = {'name': member.name, 'stars': 0, 'attacks': 0}
                                player_stars[member.tag]['stars'] += attack.stars
                                player_stars[member.tag]['attacks'] += 1
                except: continue
        if not player_stars:
            await send_msg(update, "📭 Нет данных CWL.", reply_markup=get_back_keyboard("menu_cwl"))
            return
        top = sorted(player_stars.values(), key=lambda x: x['stars'], reverse=True)[:15]
        text = f"⭐ <b>ТОП CWL атакующих:</b>\n\n"
        for i, p in enumerate(top, 1):
            avg = p['stars'] / p['attacks'] if p['attacks'] > 0 else 0
            text += f"<code>{i:2d}.</code> {p['name']} — <b>{p['stars']}</b>⭐ ({p['attacks']} ат., avg {avg:.1f})\n"
        await send_msg(update, text, parse_mode="HTML", reply_markup=get_back_keyboard("menu_cwl"))
    except Exception as e:
        await send_msg(update, f"❌ Ошибка: {str(e)[:100]}")

# ============================================================
# 📜 ИСТОРИЯ ВОЙН
# ============================================================
async def handle_war_history(update):
    clan_tag = await check_user_clan(update)
    if not clan_tag or not coc_client: return
    try:
        wars = await coc_client.get_war_log(clan_tag, limit=10)
        text = f"📜 <b>Последние 10 войн:</b>\n\n"
        wins = losses = 0
        for i, war in enumerate(wars, 1):
            our, enemy = war.clan, war.opponent
            if our.stars > enemy.stars or (our.stars == enemy.stars and our.destruction > enemy.destruction):
                status = "🟢 ПОБЕДА"; wins += 1
            elif our.stars < enemy.stars or (our.stars == enemy.stars and our.destruction < enemy.destruction):
                status = "🔴 ПОРАЖЕНИЕ"; losses += 1
            else: status = "🟡 НИЧЬЯ"
            end_time = war.end_time
            date_str = f"{end_time.day:02d}.{end_time.month:02d}" if end_time else "???"
            text += f"<code>{i:2d}.</code> {date_str} {status}\n"
            text += f"    vs <b>{enemy.name}</b>\n"
            text += f"    ⭐ {our.stars}:{enemy.stars} | 💥 {our.destruction:.0f}%:{enemy.destruction:.0f}%\n\n"
        text += f"📊 <b>Итог:</b> 🟢 {wins} / 🔴 {losses}"
        await send_msg(update, text, parse_mode="HTML", reply_markup=get_back_keyboard("menu_war"))
    except coc.PrivateWarLog:
        await send_msg(update, "🔒 Лог войны закрыт.", reply_markup=get_back_keyboard("menu_war"))
    except Exception as e:
        await send_msg(update, f"❌ Ошибка: {str(e)[:100]}")

# ============================================================
# 🏰 КЛАН
# ============================================================
async def handle_clan_info(update):
    clan_tag = await check_user_clan(update)
    if not clan_tag or not coc_client: return
    try:
        clan = await coc_client.get_clan(clan_tag)
        text = (
            f"🏰 <b>{clan.name}</b> <code>{clan.tag}</code>\n\n"
            f"📊 Уровень: <code>{clan.level}</code>\n"
            f"👥 Участников: <code>{clan.member_count}/50</code>\n"
            f"🏆 Трофеи: <code>{clan.points}</code>\n"
            f"🛡️ Вход: <code>{clan.required_trophies}</code>\n"
            f"🌍 Регион: <code>{clan.location.name if clan.location else 'Global'}</code>\n\n"
            f"📝 <i>{clan.description or 'Нет описания'}</i>"
        )
        await send_msg(update, text, parse_mode="HTML", reply_markup=get_back_keyboard("menu_clan"))
    except Exception as e:
        await send_msg(update, f"❌ Ошибка: {str(e)[:100]}")

async def handle_clan_members(update):
    clan_tag = await check_user_clan(update)
    if not clan_tag or not coc_client: return
    try:
        clan = await coc_client.get_clan(clan_tag)
        tg_id = await get_tg_id(update)
        tg_mapping = await get_linked_accounts_db(tg_id)
        text = f"👥 <b>Участники {clan.name}</b> ({clan.member_count}/50):\n\n"
        for i, member in enumerate(clan.members, 1):
            name = member.name
            if member.tag in tg_mapping:
                name += f" {tg_mapping[member.tag]}"
            role = member.role.name if hasattr(member.role, 'name') else str(member.role)
            text += f"<code>{i:2d}.</code> {name} (ТХ{member.town_hall}) - {role}\n"
        await send_msg(update, text, parse_mode="HTML", reply_markup=get_back_keyboard("menu_clan"))
    except Exception as e:
        await send_msg(update, f"❌ Ошибка: {str(e)[:100]}")

async def handle_clan_donations(update):
    clan_tag = await check_user_clan(update)
    if not clan_tag or not coc_client: return
    try:
        clan = await coc_client.get_clan(clan_tag)
        tg_id = await get_tg_id(update)
        tg_mapping = await get_linked_accounts_db(tg_id)
        members_data = []
        for member in clan.members:
            name = member.name
            if member.tag in tg_mapping:
                name += f" {tg_mapping[member.tag]}"
            donated = getattr(member, 'donations', 0) or 0
            received = getattr(member, 'donations_received', 0) or 0
            members_data.append({'name': name, 'donated': donated, 'received': received})
        top_donated = sorted(members_data, key=lambda x: x['donated'], reverse=True)[:10]
        top_received = sorted(members_data, key=lambda x: x['received'], reverse=True)[:10]
        text = f"🎁 <b>Пожертвования {clan.name}</b>\n\n"
        text += "🏆 <b>ТОП 10 ДАТЕЛЕЙ:</b>\n"
        for i, m in enumerate(top_donated, 1):
            text += f"<code>{i:2d}.</code> {m['name']} - <b>{m['donated']}</b>\n"
        text += "\n📥 <b>ТОП 10 ПОЛУЧАТЕЛЕЙ:</b>\n"
        for i, m in enumerate(top_received, 1):
            text += f"<code>{i:2d}.</code> {m['name']} - <b>{m['received']}</b>\n"
        await send_msg(update, text, parse_mode="HTML", reply_markup=get_back_keyboard("menu_clan"))
    except Exception as e:
        logger.error(f"Donations error: {e}", exc_info=True)
        await send_msg(update, f"❌ Ошибка: {str(e)[:100]}")

async def handle_clan_capital(update):
    clan_tag = await check_user_clan(update)
    if not clan_tag or not coc_client: return
    try:
        clan = await coc_client.get_clan(clan_tag)
        text = f"🏰 <b>Столица {clan.name}</b>\n\n"
        try:
            capital = getattr(clan, 'clan_capital', None)
            if capital:
                text += f"📊 Уровень: <code>{getattr(capital, 'capital_hall_level', '?')}</code>\n"
                districts = getattr(capital, 'districts', []) or []
                if districts:
                    text += f"\n🗺️ <b>Районы ({len(districts)}):</b>\n"
                    for d in districts[:10]:
                        text += f"  • {getattr(d, 'name', '?')} (Ур.{getattr(d, 'district_hall_level', '?')})\n"
            else:
                text += "⚠️ Данные недоступны.\n"
        except Exception as cap_e:
            text += f"⚠️ Ошибка: {cap_e}\n"
        await send_msg(update, text, parse_mode="HTML", reply_markup=get_back_keyboard("menu_clan"))
    except Exception as e:
        await send_msg(update, f"❌ Ошибка: {str(e)[:100]}")

# ============================================================
# 👤 ПРОФИЛЬ
# ============================================================
async def handle_my_stats(update):
    if not coc_client: return
    tg_id = await get_tg_id(update)
    accounts = await get_user_accounts_db(tg_id)
    if not accounts:
        await send_msg(update, "🔗 Нет привязанных аккаунтов.\n/link")
        return
    text = "📊 <b>Ваша статистика</b>\n\n"
    for acc in accounts:
        try:
            player = await coc_client.get_player(acc['player_tag'])
            text += f"👤 <b>{player.name}</b> (<code>{acc['player_tag']}</code>)\n"
            text += f"   🏰 ТХ: <code>{player.town_hall}</code>\n"
            text += f"   🏆 Трофеи: <code>{player.trophies}</code>\n"
            text += f"   ⚔️ Атак побед: <code>{player.attack_wins}</code>\n"
            text += f"   🛡️ Защит побед: <code>{player.defense_wins}</code>\n"
            text += f"   🎁 Донат: <code>{player.donations}</code>\n"
            text += f"   📥 Получено: <code>{player.donations_received}</code>\n\n"
        except Exception as e:
            text += f"👤 {acc['player_name']} - ❌ Ошибка\n\n"
    await send_msg(update, text, parse_mode="HTML", reply_markup=get_back_keyboard("menu_profile"))

# ============================================================
# ⚔️ ВОЙНА (AI ПЛАН + ОТЧЕТ)
# ============================================================
async def handle_war_plan(update, generate_report: bool = False):
    clan_tag = await check_user_clan(update)
    if not clan_tag or not coc_client: return
    try:
        war = await coc_client.get_current_war(clan_tag)
        if war.state == "notInWar":
            await send_msg(update, "🔍 Нет активной войны.", reply_markup=get_back_keyboard("menu_war"))
            return
        our_clan, enemy_clan = war.clan, war.opponent
        tg_id = await get_tg_id(update)
        tg_mapping = await get_linked_accounts_db(tg_id)
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
            if attacker['obj'].tag in dobiv_role_tags: continue
            if attacker['attacks_count'] >= 1: continue
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
            await send_msg(update, header, parse_mode="HTML")
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
                await send_msg(update, chunk, parse_mode="HTML")
            await send_msg(update, plan_text, parse_mode="HTML")
            await send_msg(update, f"<b>1️⃣ ОСНОВНОЙ УДАР:</b>\n<pre><code>{table_1}</code></pre>", parse_mode="HTML")
            if second_strike_plan:
                await send_msg(update, f"<b>2️⃣ ДОБИВАНИЕ:</b>\n<pre><code>{table_2}</code></pre>", parse_mode="HTML", reply_markup=get_back_keyboard("menu_war"))
            else:
                await send_msg(update, "<i>Нет вторых атак.</i>", reply_markup=get_back_keyboard("menu_war"))
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
                if isinstance(update, types.Message):
                    await update.answer_document(FSInputFile(filename), caption="📁 Отчет войны")
                else:
                    await update.message.answer_document(FSInputFile(filename), caption="📁 Отчет войны")
            except Exception as e:
                logger.error(f"Report error: {e}", exc_info=True)
                await send_msg(update, f"❌ Ошибка отчета: {str(e)[:100]}")
    except coc.PrivateWarLog:
        await send_msg(update, "🔒 Лог войны закрыт.", reply_markup=get_back_keyboard("menu_war"))
    except Exception as e:
        logger.error(f"War plan error: {e}", exc_info=True)
        await send_msg(update, f"❌ Ошибка: {str(e)[:100]}")

async def handle_remind_full(update):
    clan_tag = await check_user_clan(update)
    if not clan_tag or not coc_client: return
    try:
        war = await coc_client.get_current_war(clan_tag)
        if war.state == "notInWar":
            await send_msg(update, "🔍 Войны нет.")
            return
        tg_id = await get_tg_id(update)
        tg_mapping = await get_linked_accounts_db(tg_id)
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
            text += "🟢 Все сделали 1 атаку.\n\n"
        if one_attack:
            text += f"🟠 <b>ДОБИТЬ ({len(one_attack)}):</b>\n" + "\n".join(one_attack)
        else:
            text += "🟢 Все атаковали!"
        await send_msg(update, text, parse_mode="HTML", reply_markup=get_back_keyboard("menu_war"))
    except Exception as e:
        await send_msg(update, "❌ Ошибка.")

async def handle_attack_logs(update):
    clan_tag = await check_user_clan(update)
    if not clan_tag or not coc_client: return
    try:
        war = await coc_client.get_current_war(clan_tag)
        if war.state == "notInWar":
            await send_msg(update, "🔍 Войны нет.")
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
            await send_msg(update, "📭 Нет атак.", reply_markup=get_back_keyboard("menu_war"))
        else:
            text = f"📊 <b>АТАКИ ({count}):</b>\n<pre><code>{table}</code></pre>"
            await send_msg(update, text, parse_mode="HTML", reply_markup=get_back_keyboard("menu_war"))
    except Exception as e:
        await send_msg(update, "❌ Ошибка.")

async def send_msg(update, text: str, parse_mode=None, reply_markup=None):
    try:
        if isinstance(update, types.Message):
            await update.answer(text, parse_mode=parse_mode, reply_markup=reply_markup, disable_web_page_preview=True)
        else:
            await update.message.answer(text, parse_mode=parse_mode, reply_markup=reply_markup, disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"Send error: {e}")

# ============================================================
# 🔔 АВТО-УВЕДОМЛЕНИЯ
# ============================================================
async def auto_war_reminders():
    if not coc_client or not db_pool:
        return
    logger.info("🔔 Авто-проверка войн...")
    try:
        async with db_pool.acquire() as conn:
            clans_to_check = await conn.fetch('SELECT DISTINCT clan_tag FROM user_active_clan')
        
        for clan_row in clans_to_check:
            clan_tag = clan_row['clan_tag']
            try:
                war = await coc_client.get_current_war(clan_tag)
                if war.state != "inWar": continue
                end_time = war.end_time
                if not end_time: continue
                now = datetime.datetime.now(datetime.timezone.utc)
                try:
                    end_dt = end_time.raw_time
                    if isinstance(end_dt, str):
                        end_dt = datetime.datetime.fromisoformat(end_dt.replace('Z', '+00:00'))
                    time_left = end_dt - now
                    hours_left = time_left.total_seconds() / 3600
                except: continue
                if not (3.5 < hours_left < 4.5 or 0.5 < hours_left < 1.5): continue
                tg_mapping = await get_linked_accounts_db()
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
                    f"⏰ До конца войны: <b>{hours_left:.1f} ч.</b>\n"
                    f"🏰 Клан: <code>{clan_tag}</code>\n\n"
                    f"⚠️ <b>Не атаковали:</b>\n" + "\n".join(lazy)
                )
                async with db_pool.acquire() as conn:
                    users = await conn.fetch('SELECT tg_id FROM user_active_clan WHERE clan_tag = $1', clan_tag)
                    for user_row in users:
                        try:
                            await bot.send_message(user_row['tg_id'], text, parse_mode="HTML")
                        except Exception as send_e:
                            logger.error(f"Auto-remind error to {user_row['tg_id']}: {send_e}")
            except Exception as e:
                logger.error(f"Auto-check error for {clan_tag}: {e}")
            await asyncio.sleep(1)
    except Exception as e:
        logger.error(f"Auto reminders error: {e}")

# ============================================================
# 🤖 КОМАНДЫ
# ============================================================
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if message.from_user.is_bot: return
    tg_id = message.from_user.id
    active_clan = await get_user_active_clan(tg_id)
    text = f"👋 Привет, {message.from_user.first_name}!\n\n"
    if active_clan:
        text += f"🏰 Клан: <code>{active_clan}</code>\n\nВыбери раздел:"
        await message.answer(text, parse_mode="HTML", reply_markup=get_main_menu())
    else:
        text += "⚠️ Клан не выбран.\n\n🔗 /link - привязать аккаунт\n🎯 /set_clan - указать клан"
        await message.answer(text, parse_mode="HTML")

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    if message.from_user.is_bot: return
    await cmd_start(message)

@dp.message(Command("set_clan"))
@dp.callback_query(F.data == "set_clan_menu")
async def cmd_set_clan(update, state: FSMContext):
    if isinstance(update, types.CallbackQuery):
        await update.answer()
        msg = update.message
    else:
        if update.from_user.is_bot: return
        msg = update
    await msg.answer("🎯 Отправь тег клана (например, <code>#2CY00G2VU</code>)", parse_mode="HTML")
    await state.set_state(SetClan.waiting_for_clan_tag)

@dp.message(SetClan.waiting_for_clan_tag)
async def process_clan_tag(message: types.Message, state: FSMContext):
    tag = message.text.strip().upper()
    if not tag.startswith('#'): tag = '#' + tag
    if not coc_client:
        await message.answer("❌ COC не подключен."); await state.clear(); return
    try:
        clan = await coc_client.get_clan(tag)
        await state.update_data(clan_tag=tag, clan_name=clan.name)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да", callback_data="confirm_clan_yes")],
            [InlineKeyboardButton(text="❌ Нет", callback_data="confirm_clan_no")]
        ])
        await message.answer(
            f"🏰 <b>{clan.name}</b>\nТег: <code>{tag}</code>\nУр: <code>{clan.level}</code>\n\nУстановить?",
            parse_mode="HTML", reply_markup=kb
        )
        await state.set_state(SetClan.waiting_for_clan_confirmation)
    except coc.NotFound:
        await message.answer("❌ Клан не найден.")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)[:100]}")
        await state.clear()

@dp.callback_query(F.data == "confirm_clan_yes", SetClan.waiting_for_clan_confirmation)
async def confirm_clan_yes(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await set_user_active_clan(callback.from_user.id, data['clan_tag'])
    await callback.message.edit_text(f"✅ Клан установлен: <b>{data['clan_name']}</b>", parse_mode="HTML")
    await state.clear()

@dp.callback_query(F.data == "confirm_clan_no", SetClan.waiting_for_clan_confirmation)
async def confirm_clan_no(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("❌ Отменено.")
    await state.clear()

@dp.message(Command("link"))
@dp.callback_query(F.data == "link_account")
async def cmd_link(update, state: FSMContext):
    if isinstance(update, types.CallbackQuery):
        await update.answer()
        msg = update.message
    else:
        if update.from_user.is_bot: return
        msg = update
    await msg.answer("🔗 Отправь тег игрока (например, <code>#QV2Q9V8L2</code>)", parse_mode="HTML")
    await state.set_state(LinkAccount.waiting_for_tag)

@dp.message(Command("cancel"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    if message.from_user.is_bot: return
    await state.clear()
    await message.answer("❌ Отменено.")

@dp.message(LinkAccount.waiting_for_tag)
async def process_tag(message: types.Message, state: FSMContext):
    tag = message.text.strip().upper()
    if not tag.startswith('#'): tag = '#' + tag
    if not coc_client:
        await message.answer("❌ COC не подключен."); await state.clear(); return
    try:
        player = await coc_client.get_player(tag)
        clan_tag = player.clan.tag if player.clan else None
        await state.update_data(player_tag=tag, player_name=player.name, clan_tag=clan_tag)
        clan_info = f"🏰 Клан: <code>{clan_tag}</code>" if clan_tag else "⚠️ Не в клане"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да", callback_data="confirm_link_yes")],
            [InlineKeyboardButton(text="❌ Нет", callback_data="confirm_link_no")]
        ])
        await message.answer(
            f"🔍 <b>{player.name}</b> (ТХ{player.town_hall})\nТег: <code>{tag}</code>\n{clan_info}",
            parse_mode="HTML", reply_markup=kb
        )
        await state.set_state(LinkAccount.waiting_for_confirmation)
    except coc.NotFound:
        await message.answer("❌ Игрок не найден.")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)[:100]}")
        await state.clear()

@dp.callback_query(F.data == "confirm_link_yes", LinkAccount.waiting_for_confirmation)
async def confirm_yes(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get('clan_tag'):
        await callback.message.edit_text("⚠️ Игрок не в клане.")
        await state.clear(); return
    success = await link_account_db(
        callback.from_user.id,
        callback.from_user.username or "",
        data['player_tag'], data['player_name'], data['clan_tag']
    )
    if success:
        await callback.message.edit_text(
            f"✅ <b>{data['player_name']}</b> привязан!\n🏰 <code>{data['clan_tag']}</code>",
            parse_mode="HTML"
        )
    else:
        await callback.message.edit_text("❌ Ошибка сохранения в БД.")
    await state.clear()

@dp.callback_query(F.data == "confirm_link_no", LinkAccount.waiting_for_confirmation)
async def confirm_no(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("❌ Отменено.")
    await state.clear()

@dp.message(Command("debug"))
async def cmd_debug(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("🚫 Доступ запрещен."); return
    tg_id = message.from_user.id
    active = await get_user_active_clan(tg_id)
    accs = await get_user_accounts_db(tg_id)
    db_status = "✅" if db_pool else "❌"
    text = (
        f"🔧 <b>DEBUG</b>\n\n"
        f"💾 PostgreSQL: {db_status}\n\n"
        f"👤 <b>Вы:</b>\n"
        f"   Клан: <code>{active or 'НЕТ'}</code>\n"
        f"   Аккаунтов: <code>{len(accs)}</code>\n"
    )
    for a in accs:
        text += f"   • {a['player_name']} ({a['player_tag']})\n"
    await message.answer(text, parse_mode="HTML")

# ============================================================
# 🔄 CALLBACKS
# ============================================================
@dp.callback_query(F.data == "back_main")
async def cb_back_main(callback: types.CallbackQuery):
    if not check_click_throttle(callback.from_user.id, "back"): 
        await callback.answer(); return
    await callback.answer()
    try: await callback.message.delete()
    except: pass
    active = await get_user_active_clan(callback.from_user.id)
    text = f"👋 Главное меню\n\n"
    text += f"🏰 Клан: <code>{active}</code>" if active else "⚠️ Клан не выбран"
    await callback.message.answer(text, parse_mode="HTML", reply_markup=get_main_menu())

@dp.callback_query(F.data == "menu_war")
async def cb_menu_war(callback: types.CallbackQuery):
    if not check_click_throttle(callback.from_user.id, "war"):
        await callback.answer(); return
    await callback.answer()
    await callback.message.edit_text("⚔️ <b>ВОЙНА:</b>", parse_mode="HTML", reply_markup=get_war_menu())

@dp.callback_query(F.data == "menu_cwl")
async def cb_menu_cwl(callback: types.CallbackQuery):
    if not check_click_throttle(callback.from_user.id, "cwl"):
        await callback.answer(); return
    await callback.answer()
    await callback.message.edit_text("🏆 <b>CWL:</b>", parse_mode="HTML", reply_markup=get_cwl_menu())

@dp.callback_query(F.data == "menu_clan")
async def cb_menu_clan(callback: types.CallbackQuery):
    if not check_click_throttle(callback.from_user.id, "clan"):
        await callback.answer(); return
    await callback.answer()
    await callback.message.edit_text("🏰 <b>КЛАН:</b>", parse_mode="HTML", reply_markup=get_clan_menu())

@dp.callback_query(F.data == "menu_profile")
async def cb_menu_profile(callback: types.CallbackQuery):
    if not check_click_throttle(callback.from_user.id, "profile"):
        await callback.answer(); return
    await callback.answer()
    await callback.message.edit_text("👤 <b>ПРОФИЛЬ:</b>", parse_mode="HTML", reply_markup=get_profile_menu())

@dp.callback_query(F.data == "cwl_current")
async def cb_cwl_current(callback: types.CallbackQuery):
    await callback.answer("⏳ Загружаю..."); await handle_cwl_current(callback)

@dp.callback_query(F.data == "cwl_group")
async def cb_cwl_group(callback: types.CallbackQuery):
    await callback.answer("⏳ Загружаю..."); await handle_cwl_group(callback)

@dp.callback_query(F.data == "cwl_stars")
async def cb_cwl_stars(callback: types.CallbackQuery):
    await callback.answer("⏳ Загружаю..."); await handle_cwl_stars(callback)

@dp.callback_query(F.data == "war_history")
async def cb_war_history(callback: types.CallbackQuery):
    await callback.answer("⏳ Загружаю историю..."); await handle_war_history(callback)

@dp.callback_query(F.data == "clan_capital")
async def cb_capital(callback: types.CallbackQuery):
    await callback.answer("⏳ Загружаю..."); await handle_clan_capital(callback)

@dp.callback_query(F.data == "clan_info")
async def cb_clan(callback: types.CallbackQuery):
    await callback.answer(); await handle_clan_info(callback)

@dp.callback_query(F.data == "clan_members")
async def cb_members(callback: types.CallbackQuery):
    await callback.answer(); await handle_clan_members(callback)

@dp.callback_query(F.data == "clan_donations")
async def cb_donations(callback: types.CallbackQuery):
    await callback.answer(); await handle_clan_donations(callback)

@dp.callback_query(F.data == "my_stats")
async def cb_stats(callback: types.CallbackQuery):
    await callback.answer(); await handle_my_stats(callback)

@dp.callback_query(F.data == "my_accounts")
async def cb_my_accs(callback: types.CallbackQuery):
    await callback.answer()
    accounts = await get_user_accounts_db(callback.from_user.id)
    if not accounts:
        await callback.message.edit_text("🔗 Нет аккаунтов. /link", reply_markup=get_back_keyboard("menu_profile"))
        return
    text = "👤 <b>Ваши аккаунты:</b>\n\n"
    for a in accounts:
        text += f"• {a['player_name']} (<code>{a['player_tag']}</code>)\n  🏰 <code>{a['clan_tag']}</code>\n\n"
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=get_back_keyboard("menu_profile"))

@dp.callback_query(F.data == "war_plan")
async def cb_war(callback: types.CallbackQuery):
    if not check_click_throttle(callback.from_user.id, "war_plan"):
        await callback.answer("⏳ Подождите..."); return
    await callback.answer("⏳ Формирую план..."); await handle_war_plan(callback)

@dp.callback_query(F.data == "war_report")
async def cb_report(callback: types.CallbackQuery):
    if not check_click_throttle(callback.from_user.id, "report"):
        await callback.answer("⏳ Подождите..."); return
    await callback.answer("⏳ Генерирую..."); await handle_war_plan(callback, generate_report=True)

@dp.callback_query(F.data == "remind_full")
async def cb_remind(callback: types.CallbackQuery):
    await callback.answer(); await handle_remind_full(callback)

@dp.callback_query(F.data == "attack_logs")
async def cb_logs(callback: types.CallbackQuery):
    await callback.answer(); await handle_attack_logs(callback)

# ============================================================
# 🚀 ЗАПУСК
# ============================================================
async def init_coc_client():
    global coc_client
    for i in range(5):
        try:
            coc_client = coc.Client(proxy=PROXY_URL, throttle_limit=10)
            await coc_client.login(COC_EMAIL, COC_PASSWORD)
            logger.info("✅ COC клиент готов!"); return
        except Exception as e:
            logger.error(f"❌ COC ошибка ({5-i}): {e}")
            if i < 4: await asyncio.sleep(5)
    coc_client = None

async def on_startup(app: web.Application):
    webhook_url = os.getenv('RENDER_EXTERNAL_URL')
    if webhook_url:
        await bot.set_webhook(f"{webhook_url}/webhook")
        logger.info(f"🌐 Webhook: {webhook_url}/webhook")
    
    # Инициализация PostgreSQL
    await init_db()
    
    await init_coc_client()
    
    # Авто-уведомления
    scheduler.add_job(auto_war_reminders, 'interval', minutes=30, id='war_reminders')
    scheduler.start()
    logger.info("🔔 Авто-уведомления запущены (каждые 30 мин)")

async def on_shutdown(app: web.Application):
    global db_pool
    scheduler.shutdown()
    if coc_client: await coc_client.close()
    if db_pool: await db_pool.close()
    await bot.session.close()

def main():
    app = web.Application()
    handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
    handler.register(app, path="/webhook")
    app.router.add_get("/health", lambda r: web.Response(text="OK"))
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    port = int(os.environ.get('PORT', 8080))
    logger.info(f"🚀 Запуск на порту {port}...")
    web.run_app(app, host='0.0.0.0', port=port)

if __name__ == '__main__':
    main()
