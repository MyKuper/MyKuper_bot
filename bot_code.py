import os
import logging
import asyncio
import sqlite3
import datetime
from typing import Optional
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, Filter
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from aiogram.webhook.aiohttp_server import SimpleRequestHandler
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiohttp import web
import coc
from prettytable import PrettyTable

# ============================================================
# ⚙️ НАСТРОЙКИ
# ============================================================
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
COC_EMAIL = os.getenv('COC_EMAIL')
COC_PASSWORD = os.getenv('COC_PASSWORD')
CLAN_TAG = "#2CY00G2VU"
PROXY_URL = os.getenv('COC_PROXY', None)
ADMIN_IDS = [1810701319]  # Ваш ID (оставлен для возможных будущих админ-команд)
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
# 🗄 БАЗА ДАННЫХ (SQLite для аккаунтов)
# ============================================================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (tg_id INTEGER PRIMARY KEY, tg_username TEXT, is_admin INTEGER DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS linked_accounts
                 (tg_id INTEGER, player_tag TEXT, player_name TEXT,
                 PRIMARY KEY (tg_id, player_tag))''')
    conn.commit()
    conn.close()

def link_account_db(tg_id, tg_username, player_tag, player_name):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (tg_id, tg_username) VALUES (?, ?)", (tg_id, tg_username))
    c.execute("UPDATE users SET tg_username = ? WHERE tg_id = ?", (tg_username, tg_id))
    c.execute("INSERT OR REPLACE INTO linked_accounts (tg_id, player_tag, player_name) VALUES (?, ?, ?)",
              (tg_id, player_tag, player_name))
    conn.commit()
    conn.close()

def get_linked_accounts_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT player_tag, tg_username FROM linked_accounts JOIN users ON linked_accounts.tg_id = users.tg_id")
    # Возвращаем словарь: { "#TAG": "@username" }
    mapping = {row[0]: f"@{row[1]}" for row in c.fetchall() if row[1]}
    conn.close()
    return mapping

# ============================================================
# 🤖 ИНИЦИАЛИЗАЦИЯ
# ============================================================
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher(storage=MemoryStorage()) # MemoryStorage нужен для FSM (сценариев)
coc_client: Optional[coc.Client] = None

# ============================================================
# 🔄 FSM СОСТОЯНИЯ (Для привязки аккаунта)
# ============================================================
class LinkAccount(StatesGroup):
    waiting_for_tag = State()
    waiting_for_confirmation = State()

# ============================================================
# ⌨️ КЛАВИАТУРЫ
# ============================================================
def get_main_keyboard() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton(text="⚔️ Война и План", callback_data="war_plan"),
         InlineKeyboardButton(text="🏰 Инфо о клане", callback_data="clan_info")],
        [InlineKeyboardButton(text="⏰ Кто не ходил", callback_data="remind_full"),
         InlineKeyboardButton(text="📊 История атак", callback_data="attack_logs")],
        [InlineKeyboardButton(text="🔗 Привязать аккаунт", callback_data="link_account")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)

def get_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 В меню", callback_data="back_menu")]])

# ============================================================
# 🧠 ЛОГИКА (AI И ОТЧЕТЫ)
# ============================================================

async def handle_clan_info(update: types.Message | types.CallbackQuery):
    if not coc_client:
        await send_msg(update, "❌ Клиент COC не подключен.")
        return
    try:
        clan = await coc_client.get_clan(CLAN_TAG)
        text = (
            f"🏰 <b>{clan.name}</b> <code>{clan.tag}</code>\n"
            f"📊 Уровень: <code>{clan.level}</code>\n"
            f"👥 Участников: <code>{clan.member_count}/50</code>\n"
            f"🏆 Трофеи: <code>{clan.points}</code>\n"
            f"🛡️ Вход: <code>{clan.required_trophies}</code>\n"
            f"🌍 Регион: <code>{clan.location.name if clan.location else 'Global'}</code>\n\n"
            f"📝 <i>{clan.description or 'Нет описания'}</i>"
        )
        await send_msg(update, text, parse_mode="HTML", reply_markup=get_back_keyboard())
    except Exception as e:
        logger.error(f"Error clan info: {e}")
        await send_msg(update, "❌ Не удалось загрузить инфо о клане.")

async def handle_war_plan(update: types.Message | types.CallbackQuery):
    if not coc_client:
        await send_msg(update, "❌ Клиент COC не подключен.")
        return

    try:
        war = await coc_client.get_current_war(CLAN_TAG)
        
        if war.state == "notInWar":
            await send_msg(update, "🔍 Сейчас нет активной войны.", reply_markup=get_back_keyboard())
            return

        our_clan = war.clan
        enemy_clan = war.opponent
        
        # Подтягиваем привязанные Telegram-юзернеймы
        tg_mapping = get_linked_accounts_db()

        # --- Заголовок войны (HTML) ---
        header = (
            f"⚔️ <b>ВОЙНА: {our_clan.name} vs {enemy_clan.name}</b>\n\n"
            f"📊 Счёт: <code>{our_clan.stars}</code> : <code>{enemy_clan.stars}</code>\n"
            f"💥 Разрушение: <code>{our_clan.destruction:.1f}%</code> : <code>{enemy_clan.destruction:.1f}%</code>\n"
            f"⏳ Статус: <code>{war.state}</code>\n"
        )

        our_members_info = []
        if our_clan and our_clan.members:
            for m in our_clan.members:
                attacks = getattr(m, 'attacks', []) or []
                
                # Добавляем Telegram юзернейм, если он привязан
                display_name = m.name
                if m.tag in tg_mapping:
                    display_name += f" ({tg_mapping[m.tag]})"

                our_members_info.append({
                    'obj': m,
                    'name': display_name,
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

        await send_msg(update, header, parse_mode="HTML")

        our_sorted_by_pos = sorted(our_members_info, key=lambda x: (x['map_pos'] if isinstance(x['map_pos'], int) else 99))
        
        roster_chunks = []
        current_chunk = "👥 <b>СОСТАВ И ПОЗИЦИИ:</b>\n"
        lazy_list = []
        
        for m in our_sorted_by_pos:
            pos_str = f"Поз. {m['map_pos']}" if isinstance(m['map_pos'], int) else "Поз. ?"
            att_str = f"{m['attacks_count']}/{war.attacks_per_member}"
            line = f"• <code>{pos_str}</code> | {m['name']} (ТХ{m['th']}) - Атаки: {att_str}\n"
            
            if len(current_chunk) + len(line) > 3800:
                roster_chunks.append(current_chunk)
                current_chunk = "👥 <b>СОСТАВ (продолжение):</b>\n" + line
            else:
                current_chunk += line
                
            if m['attacks_count'] < war.attacks_per_member:
                lazy_list.append(f"• {m['name']} (ТХ{m['th']}) - осталось {war.attacks_per_member - m['attacks_count']}")
        
        roster_chunks.append(current_chunk)
        
        for i, chunk in enumerate(roster_chunks):
            if i == len(roster_chunks) - 1:
                if lazy_list:
                    chunk += f"\n⚠️ <b>Требуют внимания ({len(lazy_list)} чел.):</b>\n" + "\n".join(lazy_list)
                else:
                    chunk += "\n✅ Все участники использовали все атаки!"
            await send_msg(update, chunk, parse_mode="HTML")

        our_sorted_by_th = sorted(our_members_info, key=lambda x: x['th'], reverse=True)
        enemy_sorted = sorted(enemy_members_info, key=lambda x: (x['map_pos'] if isinstance(x['map_pos'], int) else 99))
        
        dobiv_role_tags = set([m['obj'].tag for m in our_sorted_by_th[:3]])
        
        plan_header = "🧠 <b>ТАКТИЧЕСКИЙ ПЛАН (AI)</b>\n\n"
        
        dobivators_names = []
        for tag in dobiv_role_tags:
            for m in our_members_info:
                if m['obj'].tag == tag:
                    dobivators_names.append(f"{m['name']} (ТХ{m['th']})")
        
        plan_header += f"🛡️ <b>ДОБИВАЮЩИЕ (Резерв):</b>\n"
        plan_header += ", ".join(dobivators_names) + "\n<i>Эти игроки ждут недобитые базы.</i>\n\n"
        
        await send_msg(update, plan_header, parse_mode="HTML")

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
            
        msg_1 = f"<b>1️⃣ ОСНОВНОЙ УДАР:</b>\n<pre><code>{table_1}</code></pre>"
        await send_msg(update, msg_1, parse_mode="HTML")
        
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
            
        if second_strike_plan:
            msg_2 = f"<b>2️⃣ ДОБИВАНИЕ И НАБИВКА:</b>\n<pre><code>{table_2}</code></pre>"
        else:
            msg_2 = "<i>Нет доступных вторых атак или целей.</i>"
            
        await send_msg(update, msg_2, parse_mode="HTML", reply_markup=get_back_keyboard())

        # ============================================================
        # 💾 СОХРАНЕНИЕ ОТЧЕТА В MARKDOWN (Для Git/GitHub)
        # ============================================================
        try:
            os.makedirs('reports', exist_ok=True)
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
            filename = f"reports/war_report_{timestamp}.md"
            
            with open(filename, 'w', encoding='utf-8') as f:
                # Заголовок
                clean_header = header.replace('<b>', '**').replace('</b>', '**').replace('<code>', '`').replace('</code>', '`').replace('<i>', '*').replace('</i>', '*')
                f.write(clean_header + "\n\n")
                
                # Состав
                for chunk in roster_chunks:
                    clean_chunk = chunk.replace('<b>', '**').replace('</b>', '**').replace('<code>', '`').replace('</code>', '`')
                    f.write(clean_chunk + "\n")
                    
                # План
                clean_plan = plan_header.replace('<b>', '**').replace('</b>', '**').replace('<i>', '*').replace('</i>', '*')
                f.write("\n" + clean_plan + "\n")
                
                # Таблицы
                f.write(f"**1️⃣ ОСНОВНОЙ УДАР:**\n```text\n{table_1}\n```\n\n")
                if second_strike_plan:
                    f.write(f"**2️⃣ ДОБИВАНИЕ И НАБИВКА:**\n```text\n{table_2}\n```\n")
                    
            # Отправка файла в чат
            if isinstance(update, types.Message):
                await update.answer_document(FSInputFile(filename), caption="📁 Отчет сохранен в Markdown (готов для коммита в Git/GitHub).")
            else:
                await update.message.answer_document(FSInputFile(filename), caption="📁 Отчет сохранен в Markdown (готов для коммита в Git/GitHub).")
                
        except Exception as e:
            logger.error(f"Error saving report: {e}")

    except coc.PrivateWarLog:
        await send_msg(update, "🔒 Лог войны закрыт настройками клана.")
    except Exception as e:
        logger.error(f"Error war plan: {e}", exc_info=True)
        await send_msg(update, f"❌ Ошибка формирования плана войны: {str(e)[:100]}")

async def handle_remind_full(update: types.Message | types.CallbackQuery):
    if not coc_client: return
    try:
        war = await coc_client.get_current_war(CLAN_TAG)
        if war.state == "notInWar":
            await send_msg(update, "🔍 Войны нет.")
            return

        zero_attacks = []
        one_attack = []

        if war.clan and war.clan.members:
            for m in war.clan.members:
                count = len(getattr(m, 'attacks', []) or [])
                if count == 0:
                    zero_attacks.append(f"• {m.name} (ТХ{getattr(m, 'town_hall', '?')})")
                elif count == 1:
                    one_attack.append(f"• {m.name} (ТХ{getattr(m, 'town_hall', '?')})")

        text = "⏰ <b>ПОЛНЫЙ СПИСОК НЕДОЧЕТОВ</b>\n\n"
        
        if zero_attacks:
            text += f"🔴 <b>ВООБЩЕ НЕ ХОДИЛИ ({len(zero_attacks)}):</b>\n" + "\n".join(zero_attacks) + "\n\n"
        else:
            text += "🟢 Все сделали хотя бы 1 атаку.\n\n"

        if one_attack:
            text += f"🟠 <b>НУЖНО ДОБИТЬ ({len(one_attack)}):</b>\n" + "\n".join(one_attack)
        else:
            text += "🟢 Все использовали все атаки!"

        await send_msg(update, text, parse_mode="HTML", reply_markup=get_back_keyboard())

    except Exception as e:
        logger.error(f"Error remind: {e}")
        await send_msg(update, "❌ Ошибка напоминания.")

async def handle_attack_logs(update: types.Message | types.CallbackQuery):
    if not coc_client: return
    try:
        war = await coc_client.get_current_war(CLAN_TAG)
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
            await send_msg(update, "📭 Атак пока не зафиксировано.", reply_markup=get_back_keyboard())
        else:
            text = f"📊 <b>ИСТОРИЯ АТАК (последние {count}):</b>\n<pre><code>{table}</code></pre>"
            await send_msg(update, text, parse_mode="HTML", reply_markup=get_back_keyboard())

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
# 🤖 ОБРАБОТЧИКИ КОМАНД (Публичные)
# ============================================================
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    text = f"👋 Привет, {message.from_user.first_name}!\n\n🤖 Я Clash Assistant с функцией AI-подбора.\nВыберите действие:"
    await message.answer(text, reply_markup=get_main_keyboard())

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await cmd_start(message)

@dp.message(Command("clan"))
async def cmd_clan(message: types.Message):
    await handle_clan_info(message)

@dp.message(Command("war"))
async def cmd_war(message: types.Message):
    await handle_war_plan(message)

@dp.message(Command("remind"))
async def cmd_remind(message: types.Message):
    await handle_remind_full(message)

@dp.message(Command("myaccounts"))
async def cmd_myaccounts(message: types.Message):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT player_name, player_tag FROM linked_accounts WHERE tg_id = ?", (message.from_user.id,))
    accounts = c.fetchall()
    conn.close()
    
    if not accounts:
        await message.answer("🔗 У вас нет привязанных аккаунтов.\nИспользуйте /link для привязки.")
        return
        
    text = "👤 <b>Ваши привязанные аккаунты:</b>\n\n"
    for name, tag in accounts:
        text += f"• {name} (<code>{tag}</code>)\n"
        
    await message.answer(text, parse_mode="HTML")

# ============================================================
# 🔗 ПРИВЯЗКА АККАУНТА (FSM Сценарий)
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
        "Отправьте ваш тег игрока в Clash of Clans (например, <code>#2Y8QV9JQ</code>).\n"
        "Чтобы отменить, отправьте /cancel.",
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
        await state.update_data(player_tag=tag, player_name=player.name)
        
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, это я", callback_data="confirm_link_yes")],
            [InlineKeyboardButton(text="❌ Нет, отмена", callback_data="confirm_link_no")]
        ])
        await message.answer(
            f"🔍 Найден игрок: <b>{player.name}</b> (ТХ{player.town_hall})\n"
            f"Тег: <code>{tag}</code>\n\n"
            f"Это ваш аккаунт? Подтвердите привязку:",
            parse_mode="HTML", reply_markup=kb
        )
        await state.set_state(LinkAccount.waiting_for_confirmation)
    except coc.NotFound:
        await message.answer("❌ Игрок с таким тегом не найден. Попробуйте еще раз или /cancel.")
    except Exception as e:
        logger.error(f"Error verifying tag: {e}")
        await message.answer(f"❌ Ошибка проверки тега. Попробуйте позже.")
        await state.clear()

@dp.callback_query(F.data == "confirm_link_yes", LinkAccount.waiting_for_confirmation)
async def confirm_yes(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    tg_id = callback.from_user.id
    tg_username = callback.from_user.username or ""
    
    link_account_db(tg_id, tg_username, data['player_tag'], data['player_name'])
    
    await callback.message.edit_text(
        f"✅ Аккаунт <b>{data['player_name']}</b> успешно привязан к вашему Telegram!\n"
        f"Теперь ваш ник будет отображаться в отчетах.",
        parse_mode="HTML"
    )
    await state.clear()

@dp.callback_query(F.data == "confirm_link_no", LinkAccount.waiting_for_confirmation)
async def confirm_no(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("❌ Привязка отменена.")
    await state.clear()

# ============================================================
# 🔄 CALLBACK QUERY (Основное меню)
# ============================================================
@dp.callback_query(F.data == "back_menu")
async def cb_back(callback: types.CallbackQuery):
    await callback.message.delete_reply_markup()
    await cmd_start(callback.message)

@dp.callback_query(F.data == "clan_info")
async def cb_clan(callback: types.CallbackQuery):
    await callback.answer()
    await handle_clan_info(callback)

@dp.callback_query(F.data == "war_plan")
async def cb_war(callback: types.CallbackQuery):
    await callback.answer("⏳ Формирую план войны...", show_alert=False)
    await handle_war_plan(callback)

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
    init_db() # Инициализация БД при старте
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
