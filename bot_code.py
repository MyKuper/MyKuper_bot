import os
import logging
import asyncio
import sqlite3
import datetime
from typing import Optional, Dict, List
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, Filter
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from aiogram.webhook.aiohttp_server import SimpleRequestHandler
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.sqlite import SQLiteStorage
from aiohttp import web
import coc
from prettytable import PrettyTable

# ============================================================
# ⚙️ НАСТРОЙКИ
# ============================================================
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
COC_EMAIL = os.getenv('COC_EMAIL')
COC_PASSWORD = os.getenv('COC_PASSWORD')
PROXY_URL = os.getenv('COC_PROXY', None)
ADMIN_IDS = [1810701319]
DB_FILE = "bot_data.db"

# ============================================================
# 🛠 ЛОГИРОВАНИЕ
# ============================================================
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

class AdminFilter(Filter):
    async def __call__(self, message: types.Message) -> bool:
        if not ADMIN_IDS: return True
        return message.from_user.id in ADMIN_IDS

# ============================================================
# 🗄 БАЗА ДАННЫХ
# ============================================================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (tg_id INTEGER PRIMARY KEY, tg_username TEXT, is_admin INTEGER DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS linked_accounts
                 (tg_id INTEGER, player_tag TEXT, player_name TEXT, clan_tag TEXT,
                 PRIMARY KEY (tg_id, player_tag))''')
    c.execute('''CREATE TABLE IF NOT EXISTS user_active_clan
                 (tg_id INTEGER PRIMARY KEY, clan_tag TEXT)''')
    conn.commit()
    conn.close()

def link_account_db(tg_id, tg_username, player_tag, player_name, clan_tag):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (tg_id, tg_username) VALUES (?, ?)", (tg_id, tg_username))
    c.execute("UPDATE users SET tg_username = ? WHERE tg_id = ?", (tg_username, tg_id))
    c.execute("INSERT OR REPLACE INTO linked_accounts (tg_id, player_tag, player_name, clan_tag) VALUES (?, ?, ?, ?)",
              (tg_id, player_tag, player_name, clan_tag))
    # Автоматически устанавливаем первый привязанный клан как активный
    c.execute("INSERT OR IGNORE INTO user_active_clan (tg_id, clan_tag) VALUES (?, ?)", (tg_id, clan_tag))
    conn.commit()
    conn.close()

def get_linked_accounts_db(tg_id: int = None) -> Dict[str, str]:
    """Возвращает словарь {player_tag: @username} для указанного tg_id или всех"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    if tg_id:
        c.execute("""SELECT player_tag, tg_username FROM linked_accounts 
                     JOIN users ON linked_accounts.tg_id = users.tg_id 
                     WHERE linked_accounts.tg_id = ?""", (tg_id,))
    else:
        c.execute("""SELECT player_tag, tg_username FROM linked_accounts 
                     JOIN users ON linked_accounts.tg_id = users.tg_id""")
    mapping = {row[0]: f"@{row[1]}" for row in c.fetchall() if row[1]}
    conn.close()
    return mapping

def get_user_accounts_db(tg_id) -> List[tuple]:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT player_name, player_tag, clan_tag FROM linked_accounts WHERE tg_id = ?", (tg_id,))
    accounts = c.fetchall()
    conn.close()
    return accounts

def get_user_active_clan(tg_id) -> Optional[str]:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT clan_tag FROM user_active_clan WHERE tg_id = ?", (tg_id,))
    result = c.fetchone()
    conn.close()
    return result[0] if result else None

def set_user_active_clan(tg_id, clan_tag):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO user_active_clan (tg_id, clan_tag) VALUES (?, ?)", (tg_id, clan_tag))
    conn.commit()
    conn.close()

def get_all_user_clans(tg_id) -> List[tuple]:
    """Возвращает уникальные кланы пользователя [(clan_tag, count), ...]"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""SELECT clan_tag, COUNT(*) as cnt FROM linked_accounts 
                 WHERE tg_id = ? GROUP BY clan_tag""", (tg_id,))
    clans = c.fetchall()
    conn.close()
    return clans

# ============================================================
# 🤖 ИНИЦИАЛИЗАЦИЯ
# ============================================================
bot = Bot(token=TELEGRAM_TOKEN)
# SQLiteStorage сохраняет FSM состояния при перезапуске!
storage = SQLiteStorage(DB_FILE)
dp = Dispatcher(storage=storage)
coc_client: Optional[coc.Client] = None

# ============================================================
# 🔄 FSM СОСТОЯНИЯ
# ============================================================
class LinkAccount(StatesGroup):
    waiting_for_tag = State()
    waiting_for_confirmation = State()

# ============================================================
# 🧩 ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================
async def get_tg_id(update) -> int:
    """Получает tg_id из message или callback"""
    if isinstance(update, types.Message):
        return update.from_user.id
    elif isinstance(update, types.CallbackQuery):
        return update.from_user.id
    return 0

async def check_user_clan(update) -> Optional[str]:
    """Проверяет что у пользователя есть активный клан"""
    tg_id = await get_tg_id(update)
    clan_tag = get_user_active_clan(tg_id)
    
    if not clan_tag:
        await send_msg(update, 
            "⚠️ У вас не выбран активный клан!\n"
            "Сначала привяжите аккаунт командой /link\n"
            "или выберите клан: /switch_clan")
        return None
    return clan_tag

# ============================================================
# ⌨️ МЕНЮ
# ============================================================
def get_main_menu() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton(text="⚔️ ВОЙНА", callback_data="menu_war")],
        [InlineKeyboardButton(text="🏰 КЛАН", callback_data="menu_clan")],
        [InlineKeyboardButton(text="👤 ПРОФИЛЬ", callback_data="menu_profile")],
        [InlineKeyboardButton(text="🔗 Привязать аккаунт", callback_data="link_account")],
        [InlineKeyboardButton(text="🔄 Сменить клан", callback_data="switch_clan")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)

def get_war_menu() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton(text="🧠 AI План атак", callback_data="war_plan")],
        [InlineKeyboardButton(text="📊 Лог атак", callback_data="attack_logs")],
        [InlineKeyboardButton(text="⏰ Кто не атаковал", callback_data="remind_full")],
        [InlineKeyboardButton(text="📋 Полный отчет (.md)", callback_data="war_report")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_main")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)

def get_clan_menu() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton(text="ℹ️ Инфо о клане", callback_data="clan_info")],
        [InlineKeyboardButton(text="👥 Список участников", callback_data="clan_members")],
        [InlineKeyboardButton(text="🎁 Пожертвования", callback_data="clan_donations")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_main")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)

def get_profile_menu() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton(text="📊 Моя статистика", callback_data="my_stats")],
        [InlineKeyboardButton(text="🎮 Мои аккаунты", callback_data="my_accounts")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_main")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)

def get_back_keyboard(dest: str = "back_main") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data=dest)]])

# ============================================================
# 🧠 ЛОГИКА (с поддержкой мультикланов)
# ============================================================

async def handle_clan_info(update: types.Message | types.CallbackQuery):
    clan_tag = await check_user_clan(update)
    if not clan_tag:
        return
        
    if not coc_client:
        await send_msg(update, "❌ Клиент COC не подключен.")
        return
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
        logger.error(f"Error clan info: {e}")
        await send_msg(update, "❌ Не удалось загрузить инфо о клане.")

async def handle_clan_members(update: types.Message | types.CallbackQuery):
    clan_tag = await check_user_clan(update)
    if not clan_tag:
        return
        
    if not coc_client:
        await send_msg(update, "❌ Клиент COC не подключен.")
        return
    try:
        clan = await coc_client.get_clan(clan_tag)
        tg_id = await get_tg_id(update)
        tg_mapping = get_linked_accounts_db(tg_id)
        
        text = f"👥 <b>Участники клана {clan.name}</b> ({clan.member_count}/50):\n\n"
        
        for i, member in enumerate(clan.members, 1):
            name = member.name
            if member.tag in tg_mapping:
                name += f" {tg_mapping[member.tag]}"
            role = member.role.name if hasattr(member.role, 'name') else str(member.role)
            text += f"<code>{i:2d}.</code> {name} (ТХ{member.town_hall}) - {role}\n"
            
        await send_msg(update, text, parse_mode="HTML", reply_markup=get_back_keyboard("menu_clan"))
    except Exception as e:
        logger.error(f"Error clan members: {e}")
        await send_msg(update, "❌ Не удалось загрузить список.")

async def handle_clan_donations(update: types.Message | types.CallbackQuery):
    clan_tag = await check_user_clan(update)
    if not clan_tag:
        return
        
    if not coc_client:
        await send_msg(update, "❌ Клиент COC не подключен.")
        return
    try:
        clan = await coc_client.get_clan(clan_tag)
        tg_id = await get_tg_id(update)
        tg_mapping = get_linked_accounts_db(tg_id)
        
        members_data = []
        for member in clan.members:
            name = member.name
            if member.tag in tg_mapping:
                name += f" {tg_mapping[member.tag]}"
            members_data.append({
                'name': name,
                'donated': member.donations,
                'received': member.donations_received
            })
        
        top_donated = sorted(members_data, key=lambda x: x['donated'], reverse=True)[:10]
        top_received = sorted(members_data, key=lambda x: x['received'], reverse=True)[:10]
        
        text = f"🎁 <b>Пожертвования клана {clan.name}</b>\n\n"
        
        text += "🏆 <b>ТОП 10 ДАТЕЛЕЙ:</b>\n"
        for i, m in enumerate(top_donated, 1):
            text += f"<code>{i:2d}.</code> {m['name']} - <b>{m['donated']}</b> войск\n"
            
        text += "\n📥 <b>ТОП 10 ПОЛУЧАТЕЛЕЙ:</b>\n"
        for i, m in enumerate(top_received, 1):
            text += f"<code>{i:2d}.</code> {m['name']} - <b>{m['received']}</b> войск\n"
            
        await send_msg(update, text, parse_mode="HTML", reply_markup=get_back_keyboard("menu_clan"))
    except Exception as e:
        logger.error(f"Error donations: {e}")
        await send_msg(update, "❌ Не удалось загрузить пожертвования.")

async def handle_my_stats(update: types.Message | types.CallbackQuery):
    if not coc_client:
        await send_msg(update, "❌ Клиент COC не подключен.")
        return
        
    tg_id = await get_tg_id(update)
    accounts = get_user_accounts_db(tg_id)
    
    if not accounts:
        await send_msg(update, "🔗 У вас нет привязанных аккаунтов.\nИспользуйте /link для привязки.")
        return
    
    text = "📊 <b>Ваша статистика</b>\n\n"
    
    for name, tag, clan_tag in accounts:
        try:
            player = await coc_client.get_player(tag)
            text += f"👤 <b>{player.name}</b> (<code>{tag}</code>)\n"
            text += f"   🏰 ТХ: <code>{player.town_hall}</code>\n"
            text += f"   🏆 Трофеи: <code>{player.trophies}</code>\n"
            text += f"   ⚔️ Атак побед: <code>{player.attack_wins}</code>\n"
            text += f"   🛡️ Защит побед: <code>{player.defense_wins}</code>\n"
            text += f"   🎁 Пожертвовано: <code>{player.donations}</code>\n"
            text += f"   📥 Получено: <code>{player.donations_received}</code>\n"
            text += f"   🏰 Клан: <code>{clan_tag}</code>\n\n"
        except Exception as e:
            text += f"👤 {name} - ❌ Ошибка загрузки\n\n"
            
    await send_msg(update, text, parse_mode="HTML", reply_markup=get_back_keyboard("menu_profile"))

async def handle_war_plan(update: types.Message | types.CallbackQuery, generate_report: bool = False):
    clan_tag = await check_user_clan(update)
    if not clan_tag:
        return
        
    if not coc_client:
        await send_msg(update, "❌ Клиент COC не подключен.")
        return

    try:
        war = await coc_client.get_current_war(clan_tag)
        
        if war.state == "notInWar":
            await send_msg(update, "🔍 Сейчас нет активной войны.", reply_markup=get_back_keyboard("menu_war"))
            return

        our_clan = war.clan
        enemy_clan = war.opponent
        
        tg_id = await get_tg_id(update)
        tg_mapping = get_linked_accounts_db(tg_id)

        # --- Сбор данных ---
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

        # --- Заголовок ---
        header = (
            f"⚔️ <b>ВОЙНА: {our_clan.name} vs {enemy_clan.name}</b>\n\n"
            f"📊 Счёт: <code>{our_clan.stars}</code> : <code>{enemy_clan.stars}</code>\n"
            f"💥 Разрушение: <code>{our_clan.destruction:.1f}%</code> : <code>{enemy_clan.destruction:.1f}%</code>\n"
            f"⏳ Статус: <code>{war.state}</code>\n"
        )

        # --- Состав ---
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

        # --- AI План ---
        our_sorted_by_th = sorted(our_members_info, key=lambda x: x['th'], reverse=True)
        enemy_sorted = sorted(enemy_members_info, key=lambda x: (x['map_pos'] if isinstance(x['map_pos'], int) else 99))
        
        dobiv_role_tags = set([m['obj'].tag for m in our_sorted_by_th[:3]])
        
        plan_text = "\n🧠 <b>ТАКТИЧЕСКИЙ ПЛАН (AI)</b>\n\n"
        
        dobivators_names = []
        for tag in dobiv_role_tags:
            for m in our_members_info:
                if m['obj'].tag == tag:
                    dobivators_names.append(f"{m['name']} (ТХ{m['th']})")
        
        plan_text += f"🛡️ <b>ДОБИВАЮЩИЕ (Резерв):</b>\n"
        plan_text += ", ".join(dobivators_names) + "\n<i>Эти игроки ждут недобитые базы.</i>\n\n"

        # Основной удар
        first_strike_plan = []
        used_enemy_targets = set()
        
        for attacker in our_sorted_by_pos:
            if attacker['obj'].tag in dobiv_role_tags: continue
            if attacker['attacks_count'] >= 1: continue 
            
            target = None
            for enemy in enemy_sorted:
                if enemy['obj'].tag in used_enemy_targets: continue
                if enemy['th'] == attacker['th']:
                    target = enemy
                    break
            if not target:
                for enemy in enemy_sorted:
                    if enemy['obj'].tag in used_enemy_targets: continue
                    if enemy['th'] == attacker['th'] + 1:
                        target = enemy
                        break
            if not target:
                best_diff = 99
                for enemy in enemy_sorted:
                    if enemy['obj'].tag in used_enemy_targets: continue
                    diff = abs(enemy['th'] - attacker['th'])
                    if diff < best_diff:
                        best_diff = diff
                        target = enemy
                        
            if target:
                used_enemy_targets.add(target['obj'].tag)
                first_strike_plan.append({'attacker': attacker, 'target': target})

        table_1 = PrettyTable()
        table_1.field_names = ["Боец (Поз.)", "Цель (Поз.)", "Статус"]
        table_1.align["Боец (Поз.)"] = "l"
        table_1.align["Цель (Поз.)"] = "l"
        
        for pair in first_strike_plan:
            a = pair['attacker']
            t = pair['target']
            a_pos = a['map_pos'] if isinstance(a['map_pos'], int) else '?'
            t_pos = t['map_pos'] if isinstance(t['map_pos'], int) else '?'
            status = "Ждет 2-й ход" if a['attacks_count'] > 0 else "Первый ход"
            table_1.add_row([f"{a['name']} ({a_pos})", f"{t['name']} ({t_pos})", status])

        # Второй удар
        second_strike_plan = []
        for attacker in our_sorted_by_pos:
            if attacker['attacks_count'] >= 2: continue
            is_dobivator = attacker['obj'].tag in dobiv_role_tags
            
            target = None
            rec = ""
            
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
                    if enemy['obj'].tag in used_enemy_targets: continue
                    if not any(e['target']['obj'].tag == enemy['obj'].tag for e in second_strike_plan):
                        if enemy['th'] <= attacker['th']:
                            target = enemy
                            rec = "🧹 Набивка %"
                            break
                if not target:
                     for enemy in enemy_sorted:
                        if not any(e['target']['obj'].tag == enemy['obj'].tag for e in second_strike_plan):
                            target = enemy
                            rec = "Свободная цель"
                            break
                            
            if target:
                second_strike_plan.append({'attacker': attacker, 'target': target, 'rec': rec})

        table_2 = PrettyTable()
        table_2.field_names = ["Боец", "Цель", "Рекомендация"]
        table_2.align["Боец"] = "l"
        
        for pair in second_strike_plan:
            a = pair['attacker']
            t = pair['target']
            table_2.add_row([f"{a['name']} (ТХ{a['th']})", f"{t['name']} (ТХ{t['th']})", pair['rec']])

        # --- Отправка в чат (обычный режим) ---
        if not generate_report:
            await send_msg(update, header, parse_mode="HTML")
            
            # Разбиваем состав на части
            roster_text = "👥 <b>СОСТАВ И ПОЗИЦИИ:</b>\n"
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
                        chunk += f"\n⚠️ <b>Требуют внимания ({len(lazy_list)} чел.):</b>\n" + "\n".join(lazy_list)
                    else:
                        chunk += "\n✅ Все участники использовали все атаки!"
                await send_msg(update, chunk, parse_mode="HTML")
                
            await send_msg(update, plan_text, parse_mode="HTML")
            
            msg_1 = f"<b>1️⃣ ОСНОВНОЙ УДАР:</b>\n<pre><code>{table_1}</code></pre>"
            await send_msg(update, msg_1, parse_mode="HTML")
            
            if second_strike_plan:
                msg_2 = f"<b>2️⃣ ДОБИВАНИЕ И НАБИВКА:</b>\n<pre><code>{table_2}</code></pre>"
            else:
                msg_2 = "<i>Нет доступных вторых атак или целей.</i>"
            await send_msg(update, msg_2, parse_mode="HTML", reply_markup=get_back_keyboard("menu_war"))

        # --- Генерация полного отчета (Markdown файл) ---
        if generate_report:
            try:
                os.makedirs('reports', exist_ok=True)
                timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
                clan_name = our_clan.name.replace(' ', '_').replace('/', '_')
                enemy_name = enemy_clan.name.replace(' ', '_').replace('/', '_')
                filename = f"reports/war_{clan_name}_vs_{enemy_name}_{timestamp}.md"
                
                # Формируем полный текст
                md_content = []
                
                # Заголовок
                md_content.append("# ⚔️ ВОЙНА")
                md_content.append(f"## {our_clan.name} vs {enemy_clan.name}\n")
                md_content.append(f"📊 **Счёт:** `{our_clan.stars}` : `{enemy_clan.stars}`")
                md_content.append(f"💥 **Разрушение:** `{our_clan.destruction:.1f}%` : `{enemy_clan.destruction:.1f}%`")
                md_content.append(f"⏳ **Статус:** `{war.state}`\n")
                
                # Состав
                md_content.append("## 👥 Состав и позиции\n")
                for line in roster_lines:
                    clean = line.replace('<code>', '`').replace('</code>', '`')
                    md_content.append(clean)
                md_content.append("")
                
                if lazy_list:
                    md_content.append(f"### ⚠️ Требуют внимания ({len(lazy_list)} чел.)")
                    for l in lazy_list:
                        md_content.append(l)
                    md_content.append("")
                else:
                    md_content.append("✅ Все участники использовали все атаки!\n")
                
                # AI План
                md_content.append("## 🧠 Тактический план (AI)\n")
                md_content.append("### 🛡️ Добивающие (Резерв)")
                md_content.append(", ".join(dobivators_names))
                md_content.append("*Эти игроки ждут недобитые базы.*\n")
                
                # Таблицы
                md_content.append("## 1️⃣ Основной удар\n")
                md_content.append("```text")
                md_content.append(str(table_1))
                md_content.append("```\n")
                
                if second_strike_plan:
                    md_content.append("## 2️⃣ Добивание и набивка\n")
                    md_content.append("```text")
                    md_content.append(str(table_2))
                    md_content.append("```")
                
                # Записываем файл
                with open(filename, 'w', encoding='utf-8') as f:
                    f.write('\n'.join(md_content))
                
                # Отправляем документ
                if isinstance(update, types.Message):
                    await update.answer_document(
                        FSInputFile(filename),
                        caption=f"📁 Полный отчет войны {our_clan.name} vs {enemy_clan.name}"
                    )
                else:
                    await update.message.answer_document(
                        FSInputFile(filename),
                        caption=f"📁 Полный отчет войны {our_clan.name} vs {enemy_clan.name}"
                    )
                    
            except Exception as e:
                logger.error(f"Error saving report: {e}", exc_info=True)
                await send_msg(update, f"❌ Ошибка генерации отчета: {str(e)[:100]}")

    except coc.PrivateWarLog:
        await send_msg(update, "🔒 Лог войны закрыт настройками клана.")
    except Exception as e:
        logger.error(f"Error war plan: {e}", exc_info=True)
        await send_msg(update, f"❌ Ошибка формирования плана: {str(e)[:100]}")

async def handle_remind_full(update: types.Message | types.CallbackQuery):
    clan_tag = await check_user_clan(update)
    if not clan_tag:
        return
        
    if not coc_client: return
    try:
        war = await coc_client.get_current_war(clan_tag)
        if war.state == "notInWar":
            await send_msg(update, "🔍 Войны нет.")
            return

        tg_id = await get_tg_id(update)
        tg_mapping = get_linked_accounts_db(tg_id)
        zero_attacks = []
        one_attack = []

        if war.clan and war.clan.members:
            for m in war.clan.members:
                count = len(getattr(m, 'attacks', []) or [])
                mention = f" ({tg_mapping[m.tag]})" if m.tag in tg_mapping else ""
                
                if count == 0:
                    zero_attacks.append(f"• {m.name}{mention} (ТХ{getattr(m, 'town_hall', '?')})")
                elif count == 1:
                    one_attack.append(f"• {m.name}{mention} (ТХ{getattr(m, 'town_hall', '?')})")

        text = "⏰ <b>ПОЛНЫЙ СПИСОК НЕДОЧЕТОВ</b>\n\n"
        
        if zero_attacks:
            text += f"🔴 <b>ВООБЩЕ НЕ ХОДИЛИ ({len(zero_attacks)}):</b>\n" + "\n".join(zero_attacks) + "\n\n"
        else:
            text += "🟢 Все сделали хотя бы 1 атаку.\n\n"

        if one_attack:
            text += f"🟠 <b>НУЖНО ДОБИТЬ ({len(one_attack)}):</b>\n" + "\n".join(one_attack)
        else:
            text += "🟢 Все использовали все атаки!"

        await send_msg(update, text, parse_mode="HTML", reply_markup=get_back_keyboard("menu_war"))

    except Exception as e:
        logger.error(f"Error remind: {e}")
        await send_msg(update, "❌ Ошибка напоминания.")

async def handle_attack_logs(update: types.Message | types.CallbackQuery):
    clan_tag = await check_user_clan(update)
    if not clan_tag:
        return
        
    if not coc_client: return
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
                    
                    d_name = defender.name if defender else "Unknown"
                    d_th = getattr(defender, 'town_hall', '?') if defender else "?"
                    stars = getattr(attack, 'stars', 0) or 0
                    dest = getattr(attack, 'destruction', 0) or 0
                    
                    table.add_row([member.name, f"{d_name} (ТХ{d_th})", "⭐"*stars, f"{dest}%"])
                    count += 1
        
        if count == 0:
            await send_msg(update, "📭 Атак пока не зафиксировано.", reply_markup=get_back_keyboard("menu_war"))
        else:
            text = f"📊 <b>ИСТОРИЯ АТАК (последние {count}):</b>\n<pre><code>{table}</code></pre>"
            await send_msg(update, text, parse_mode="HTML", reply_markup=get_back_keyboard("menu_war"))

    except Exception as e:
        logger.error(f"Error logs: {e}")
        await send_msg(update, "❌ Ошибка загрузки истории.")

async def send_msg(update: types.Message | types.CallbackQuery, text: str, parse_mode=None, reply_markup=None):
    try:
        if isinstance(update, types.Message):
            await update.answer(text, parse_mode=parse_mode, reply_markup=reply_markup, disable_web_page_preview=True)
        else:
            await update.message.answer(text, parse_mode=parse_mode, reply_markup=reply_markup, disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"Error sending message: {e} | Text length: {len(text)}")

# ============================================================
# 🤖 ОБРАБОТЧИКИ КОМАНД
# ============================================================
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    tg_id = message.from_user.id
    active_clan = get_user_active_clan(tg_id)
    
    text = (f"👋 Привет, {message.from_user.first_name}!\n\n"
            f"🤖 Я Clash Assistant с AI-подбором целей.\n")
    
    if active_clan:
        text += f"🏰 Активный клан: <code>{active_clan}</code>\n\n"
        text += "Выбери раздел в меню:"
        await message.answer(text, parse_mode="HTML", reply_markup=get_main_menu())
    else:
        text += "⚠️ У вас не выбран активный клан.\n"
        text += "Привяжите аккаунт: /link"
        await message.answer(text, parse_mode="HTML")

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await cmd_start(message)

@dp.message(Command("war"))
async def cmd_war(message: types.Message):
    await message.answer("⚔️ <b>Раздел ВОЙНА:</b>", parse_mode="HTML", reply_markup=get_war_menu())

@dp.message(Command("clan"))
async def cmd_clan(message: types.Message):
    await message.answer("🏰 <b>Раздел КЛАН:</b>", parse_mode="HTML", reply_markup=get_clan_menu())

@dp.message(Command("profile"))
async def cmd_profile(message: types.Message):
    await message.answer("👤 <b>Раздел ПРОФИЛЬ:</b>", parse_mode="HTML", reply_markup=get_profile_menu())

@dp.message(Command("switch_clan"))
async def cmd_switch_clan(message: types.Message):
    tg_id = message.from_user.id
    clans = get_all_user_clans(tg_id)
    
    if not clans:
        await message.answer("🔗 У вас нет привязанных аккаунтов.\nИспользуйте /link для привязки.")
        return
    
    kb_buttons = []
    for clan_tag, count in clans:
        kb_buttons.append([InlineKeyboardButton(
            text=f"{clan_tag} ({count} акк.)",
            callback_data=f"set_clan_{clan_tag}"
        )])
    kb_buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="back_main")])
    
    await message.answer(
        "🔄 <b>Выберите активный клан:</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_buttons)
    )

# ============================================================
# 🔗 ПРИВЯЗКА АККАУНТА (FSM)
# ============================================================
@dp.message(Command("link"))
@dp.callback_query(F.data == "link_account")
async def cmd_link(update: types.Message | types.CallbackQuery, state: FSMContext):
    if isinstance(update, types.CallbackQuery):
        await update.answer()
        msg = update.message
    else:
        msg = update
        
    await msg.answer(
        "🔗 <b>Привязка аккаунта</b>\n\n"
        "Отправьте ваш тег игрока (например, <code>#2Y8QV9JQ</code>).\n"
        "Бот автоматически определит ваш клан.\n"
        "Для отмены: /cancel",
        parse_mode="HTML"
    )
    await state.set_state(LinkAccount.waiting_for_tag)

@dp.message(Command("cancel"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is None:
        return
    await state.clear()
    await message.answer("❌ Действие отменено.")

@dp.message(LinkAccount.waiting_for_tag)
async def process_tag(message: types.Message, state: FSMContext):
    tag = message.text.strip().upper()
    if not tag.startswith('#'):
        tag = '#' + tag
    
    if not coc_client:
        await message.answer("❌ Клиент COC не подключен.")
        await state.clear()
        return

    try:
        player = await coc_client.get_player(tag)
        
        # Получаем клан игрока
        clan_tag = None
        if player.clan:
            clan_tag = player.clan.tag
        
        await state.update_data(
            player_tag=tag,
            player_name=player.name,
            clan_tag=clan_tag
        )
        
        clan_info = f"🏰 Клан: <code>{clan_tag}</code>" if clan_tag else "⚠️ Игрок не в клане"
        
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, это я", callback_data="confirm_link_yes")],
            [InlineKeyboardButton(text="❌ Нет, отмена", callback_data="confirm_link_no")]
        ])
        await message.answer(
            f"🔍 Найден игрок: <b>{player.name}</b> (ТХ{player.town_hall})\n"
            f"Тег: <code>{tag}</code>\n"
            f"{clan_info}\n\n"
            f"Это ваш аккаунт? Подтвердите:",
            parse_mode="HTML", reply_markup=kb
        )
        await state.set_state(LinkAccount.waiting_for_confirmation)
    except coc.NotFound:
        await message.answer("❌ Игрок не найден. Попробуйте еще раз или /cancel.")
    except Exception as e:
        logger.error(f"Error verifying tag: {e}")
        await message.answer(f"❌ Ошибка проверки тега.")
        await state.clear()

@dp.callback_query(F.data == "confirm_link_yes", LinkAccount.waiting_for_confirmation)
async def confirm_yes(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    tg_id = callback.from_user.id
    tg_username = callback.from_user.username or ""
    
    clan_tag = data.get('clan_tag', '')
    
    if not clan_tag:
        await callback.message.edit_text(
            "⚠️ Игрок не состоит в клане. Привязка невозможна.",
            parse_mode="HTML"
        )
        await state.clear()
        return
    
    link_account_db(tg_id, tg_username, data['player_tag'], data['player_name'], clan_tag)
    
    await callback.message.edit_text(
        f"✅ Аккаунт <b>{data['player_name']}</b> привязан!\n"
        f"🏰 Клан: <code>{clan_tag}</code>\n"
        f"Теперь ваш @username будет в отчетах.\n"
        f"Этот клан установлен как активный.",
        parse_mode="HTML"
    )
    await state.clear()

@dp.callback_query(F.data == "confirm_link_no", LinkAccount.waiting_for_confirmation)
async def confirm_no(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("❌ Привязка отменена.")
    await state.clear()

# ============================================================
# 🔄 CALLBACK QUERY (Навигация по меню)
# ============================================================
@dp.callback_query(F.data == "back_main")
async def cb_back_main(callback: types.CallbackQuery):
    await callback.answer()
    await callback.message.delete()
    await cmd_start(callback.message)

@dp.callback_query(F.data.startswith("set_clan_"))
async def cb_set_clan(callback: types.CallbackQuery):
    clan_tag = callback.data.replace("set_clan_", "")
    tg_id = callback.from_user.id
    
    set_user_active_clan(tg_id, clan_tag)
    
    await callback.answer(f"✅ Активный клан: {clan_tag}", show_alert=True)
    await callback.message.delete()
    await cmd_start(callback.message)

@dp.callback_query(F.data == "menu_war")
async def cb_menu_war(callback: types.CallbackQuery):
    await callback.answer()
    await callback.message.edit_text("⚔️ <b>Раздел ВОЙНА:</b>", parse_mode="HTML", reply_markup=get_war_menu())

@dp.callback_query(F.data == "menu_clan")
async def cb_menu_clan(callback: types.CallbackQuery):
    await callback.answer()
    await callback.message.edit_text("🏰 <b>Раздел КЛАН:</b>", parse_mode="HTML", reply_markup=get_clan_menu())

@dp.callback_query(F.data == "menu_profile")
async def cb_menu_profile(callback: types.CallbackQuery):
    await callback.answer()
    await callback.message.edit_text("👤 <b>Раздел ПРОФИЛЬ:</b>", parse_mode="HTML", reply_markup=get_profile_menu())

@dp.callback_query(F.data == "switch_clan")
async def cb_switch_clan(callback: types.CallbackQuery):
    await callback.answer()
    await callback.message.delete()
    await cmd_switch_clan(callback.message)

@dp.callback_query(F.data == "clan_info")
async def cb_clan(callback: types.CallbackQuery):
    await callback.answer()
    await handle_clan_info(callback)

@dp.callback_query(F.data == "clan_members")
async def cb_clan_members(callback: types.CallbackQuery):
    await callback.answer()
    await handle_clan_members(callback)

@dp.callback_query(F.data == "clan_donations")
async def cb_clan_donations(callback: types.CallbackQuery):
    await callback.answer()
    await handle_clan_donations(callback)

@dp.callback_query(F.data == "my_stats")
async def cb_my_stats(callback: types.CallbackQuery):
    await callback.answer()
    await handle_my_stats(callback)

@dp.callback_query(F.data == "my_accounts")
async def cb_my_accounts(callback: types.CallbackQuery):
    await callback.answer()
    tg_id = callback.from_user.id
    accounts = get_user_accounts_db(tg_id)
    
    if not accounts:
        await callback.message.edit_text(
            "🔗 У вас нет привязанных аккаунтов.\nИспользуйте /link для привязки.",
            reply_markup=get_back_keyboard("menu_profile")
        )
        return
        
    text = "👤 <b>Ваши привязанные аккаунты:</b>\n\n"
    for name, tag, clan_tag in accounts:
        text += f"• {name} (<code>{tag}</code>)\n"
        text += f"  🏰 Клан: <code>{clan_tag}</code>\n\n"
        
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=get_back_keyboard("menu_profile"))

@dp.callback_query(F.data == "war_plan")
async def cb_war(callback: types.CallbackQuery):
    await callback.answer("⏳ Формирую план...", show_alert=False)
    await handle_war_plan(callback)

@dp.callback_query(F.data == "war_report")
async def cb_war_report(callback: types.CallbackQuery):
    await callback.answer("⏳ Генерирую отчет...", show_alert=False)
    await handle_war_plan(callback, generate_report=True)

@dp.callback_query(F.data == "remind_full")
async def cb_remind(callback: types.CallbackQuery):
    await callback.answer()
    await handle_remind_full(callback)

@dp.callback_query(F.data == "attack_logs")
async def cb_logs(callback: types.CallbackQuery):
    await callback.answer()
    await handle_attack_logs(callback)

# ============================================================
# 🚀 ЗАПУСК
# ============================================================
async def init_coc_client():
    global coc_client
    logger.info("🔄 Инициализация клиента COC...")
    tries = 5
    while tries > 0:
        try:
            proxy = PROXY_URL if PROXY_URL else None
            coc_client = coc.Client(proxy=proxy, throttle_limit=10)
            await coc_client.login(COC_EMAIL, COC_PASSWORD)
            logger.info("✅ Успешный вход в COC API!")
            return
        except Exception as e:
            logger.error(f"❌ Ошибка входа COC (осталось {tries}): {e}")
            tries -= 1
            if tries == 0: coc_client = None
            else: await asyncio.sleep(5)

async def on_startup(app: web.Application):
    webhook_url = os.getenv('RENDER_EXTERNAL_URL')
    if webhook_url:
        await bot.set_webhook(f"{webhook_url}/webhook")
        logger.info(f"🌐 Webhook: {webhook_url}/webhook")
    init_db()
    await init_coc_client()

async def on_shutdown(app: web.Application):
    if coc_client: await coc_client.close()
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
