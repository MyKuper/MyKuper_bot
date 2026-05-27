import os
import logging
import asyncio
from typing import Optional, List
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, Filter
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.webhook.aiohttp_server import SimpleRequestHandler
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
ADMIN_IDS = []  # Ваш ID: 1810701319

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
# 🤖 ИНИЦИАЛИЗАЦИЯ
# ============================================================
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()
coc_client: Optional[coc.Client] = None

# ============================================================
# ⌨️ КЛАВИАТУРЫ
# ============================================================
def get_main_keyboard() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton(text="⚔️ Война и План", callback_data="war_plan"),
         InlineKeyboardButton(text="🏰 Инфо о клане", callback_data="clan_info")],
        [InlineKeyboardButton(text="⏰ Кто не ходил", callback_data="remind_full"),
         InlineKeyboardButton(text="📊 История атак", callback_data="attack_logs")],
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
        
        # --- Заголовок войны (HTML) ---
        header = (
            f"⚔️ <b>ВОЙНА: {our_clan.name} vs {enemy_clan.name}</b>\n\n"
            f"📊 Счёт: <code>{our_clan.stars}</code> : <code>{enemy_clan.stars}</code>\n"
            f"💥 Разрушение: <code>{our_clan.destruction:.1f}%</code> : <code>{enemy_clan.destruction:.1f}%</code>\n"
            f"⏳ Статус: <code>{war.state}</code>\n"
        )

        # Сбор информации об участниках
        our_members_info = []
        if our_clan and our_clan.members:
            for m in our_clan.members:
                attacks = getattr(m, 'attacks', []) or []
                our_members_info.append({
                    'obj': m,
                    'name': m.name,
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

        # 1. Отправляем Заголовок
        await send_msg(update, header, parse_mode="HTML")

        # --- Полный список состава и позиций ---
        our_sorted_by_pos = sorted(our_members_info, key=lambda x: (x['map_pos'] if isinstance(x['map_pos'], int) else 99))
        
        roster_chunks = []
        current_chunk = "👥 <b>СОСТАВ И ПОЗИЦИИ:</b>\n"
        lazy_list = []
        
        for m in our_sorted_by_pos:
            pos_str = f"Поз. {m['map_pos']}" if isinstance(m['map_pos'], int) else "Поз. ?"
            att_str = f"{m['attacks_count']}/{war.attacks_per_member}"
            line = f"• <code>{pos_str}</code> | {m['name']} (ТХ{m['th']}) - Атаки: {att_str}\n"
            
            # Проверка на лимит Telegram (4096), оставляем запас
            if len(current_chunk) + len(line) > 3800:
                roster_chunks.append(current_chunk)
                current_chunk = "👥 <b>СОСТАВ (продолжение):</b>\n" + line
            else:
                current_chunk += line
                
            if m['attacks_count'] < war.attacks_per_member:
                lazy_list.append(f"• {m['name']} (ТХ{m['th']}) - осталось {war.attacks_per_member - m['attacks_count']}")
        
        roster_chunks.append(current_chunk)
        
        # Отправляем состав частями
        for i, chunk in enumerate(roster_chunks):
            # Если это последний кусок, добавляем список "Требуют внимания" в конце
            if i == len(roster_chunks) - 1:
                if lazy_list:
                    chunk += f"\n⚠️ <b>Требуют внимания ({len(lazy_list)} чел.):</b>\n" + "\n".join(lazy_list)
                else:
                    chunk += "\n✅ Все участники использовали все атаки!"
            await send_msg(update, chunk, parse_mode="HTML")

        # --- УМНЫЙ ПЛАН АТАКИ (AI) ---
        our_sorted_by_th = sorted(our_members_info, key=lambda x: x['th'], reverse=True)
        enemy_sorted = sorted(enemy_members_info, key=lambda x: (x['map_pos'] if isinstance(x['map_pos'], int) else 99))
        
        dobiv_role_tags = set([m['obj'].tag for m in our_sorted_by_th[:3]])
        
        plan_header = "🧠 <b>ТАКТИЧЕСКИЙ ПЛАН (AI)</b>\n\n"
        
        # Добивающие
        dobivators_names = []
        for tag in dobiv_role_tags:
            for m in our_members_info:
                if m['obj'].tag == tag:
                    dobivators_names.append(f"{m['name']} (ТХ{m['th']})")
        
        plan_header += f"🛡️ <b>ДОБИВАЮЩИЕ (Резерв):</b>\n"
        plan_header += ", ".join(dobivators_names) + "\n<i>Эти игроки ждут недобитые базы.</i>\n\n"
        
        await send_msg(update, plan_header, parse_mode="HTML")

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
            
        msg_1 = f"<b>1️⃣ ОСНОВНОЙ УДАР:</b>\n<pre><code>{table_1}</code></pre>"
        await send_msg(update, msg_1, parse_mode="HTML")
        
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
            
        if second_strike_plan:
            msg_2 = f"<b>2️⃣ ДОБИВАНИЕ И НАБИВКА:</b>\n<pre><code>{table_2}</code></pre>"
        else:
            msg_2 = "<i>Нет доступных вторых атак или целей.</i>"
            
        await send_msg(update, msg_2, parse_mode="HTML", reply_markup=get_back_keyboard())

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

# Вспомогательная функция
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
@dp.message(Command("start"), AdminFilter())
async def cmd_start(message: types.Message):
    text = f"👋 Привет, {message.from_user.first_name}!\n\n🤖 Я Clash Assistant с функцией AI-подбора.\nВыберите действие:"
    await message.answer(text, reply_markup=get_main_keyboard())

@dp.message(Command("help"), AdminFilter())
async def cmd_help(message: types.Message):
    await cmd_start(message)

@dp.message(Command("clan"), AdminFilter())
async def cmd_clan(message: types.Message):
    await handle_clan_info(message)

@dp.message(Command("war"), AdminFilter())
async def cmd_war(message: types.Message):
    await handle_war_plan(message)

@dp.message(Command("remind"), AdminFilter())
async def cmd_remind(message: types.Message):
    await handle_remind_full(message)

# ============================================================
# 🔄 CALLBACK QUERY
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
