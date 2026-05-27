import os
import logging
import asyncio
from typing import List, Optional

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
        [InlineKeyboardButton(text="⚔️ Статус войны", callback_data="war_info"),
         InlineKeyboardButton(text="🏰 Клан", callback_data="clan_info")],
        [InlineKeyboardButton(text="👥 Участники", callback_data="members_list"),
         InlineKeyboardButton(text="🎯 Подбор целей (AI)", callback_data="war_targets")], # Новая кнопка
        [InlineKeyboardButton(text="⏰ Кто не атаковал", callback_data="remind_attacks")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)

def get_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="back_menu")]])

# ============================================================
# 🧠 ЛОГИКА
# ============================================================

async def handle_clan_info(update: types.Message | types.CallbackQuery):
    if not coc_client:
        msg = "❌ Клиент COC не подключен."
        await (update.answer(msg) if isinstance(update, types.Message) else update.message.answer(msg))
        return
    try:
        clan = await coc_client.get_clan(CLAN_TAG)
        text = (
            f"🏰 **{clan.name}** `{clan.tag}`\n"
            f"📊 Уровень: `{clan.level}`\n"
            f"👥 Участников: `{clan.member_count}/50`\n"
            f"🏆 Трофеи: `{clan.points}`\n"
            f"🛡️ Вход: `{clan.required_trophies}`\n"
            f"🌍 Регион: `{clan.location.name if clan.location else 'Global'}`\n"
            f"📝 Описание:\n_{clan.description if clan.description else 'Нет описания'}_"
        )
        if isinstance(update, types.Message):
            await update.answer(text, parse_mode="Markdown", reply_markup=get_back_keyboard())
        else:
            await update.message.answer(text, parse_mode="Markdown", reply_markup=get_back_keyboard())
    except Exception as e:
        logger.error(f"Error clan info: {e}", exc_info=True)
        msg = "❌ Ошибка получения данных о клане."
        if isinstance(update, types.Message): await update.answer(msg)
        else: await update.message.answer(msg)

async def handle_members_list(update: types.Message | types.CallbackQuery):
    if not coc_client: return
    try:
        clan = await coc_client.get_clan(CLAN_TAG)
        if not hasattr(clan, 'members') or not clan.members:
            raise ValueError("Список участников пуст")
            
        members = sorted(clan.members, key=lambda m: m.trophies, reverse=True)
        
        table = PrettyTable()
        table.field_names = ["Имя", "Роль", "ТХ", "Трофеи"]
        table.align["Имя"] = "l"
        
        for m in members[:25]: # Увеличил до 25
            role_map = {"leader": "👑 Глава", "coLeader": "🛡️ Совет", "elder": "🎖️ Старейшина", "member": "👤 Участник"}
            role = role_map.get(getattr(m, 'role', 'member'), "👤 Участник")
            table.add_row([m.name, role, getattr(m, 'town_hall', '?'), getattr(m, 'trophies', 0)])
            
        text = f"👥 **Топ участников клана:**\n<pre><code>{table}</code></pre>"
        if len(members) > 25:
            text += f"\n_... и еще {len(members) - 25}_"
            
        if isinstance(update, types.Message):
            await update.answer(text, parse_mode="HTML", reply_markup=get_back_keyboard())
        else:
            await update.message.answer(text, parse_mode="HTML", reply_markup=get_back_keyboard())
            
    except Exception as e:
        logger.error(f"Error members list: {e}", exc_info=True)
        msg = "❌ Ошибка списка участников. Проверьте логи."
        if isinstance(update, types.Message): await update.answer(msg)
        else: await update.message.answer(msg)

async def handle_war_info(update: types.Message | types.CallbackQuery):
    if not coc_client: return
    try:
        war = await coc_client.get_current_war(CLAN_TAG)
        
        if war.state == "notInWar":
            msg = "🔍 Сейчас нет активной войны."
            if isinstance(update, types.Message): await update.answer(msg, reply_markup=get_back_keyboard())
            else: await update.message.answer(msg, reply_markup=get_back_keyboard())
            return

        our_clan = war.clan
        enemy_clan = war.opponent
        
        our_stars = getattr(our_clan, 'stars', 0)
        enemy_stars = getattr(enemy_clan, 'stars', 0)
        our_dest = getattr(our_clan, 'destruction', 0)
        enemy_dest = getattr(enemy_clan, 'destruction', 0)

        text = (
            f"⚔️ **ВОЙНА: {our_clan.name} vs {enemy_clan.name}**\n\n"
            f"📊 Счёт: `{our_stars}` : `{enemy_stars}` (Звёзды)\n"
            f"💥 Разрушение: `{our_dest}%` : `{enemy_dest}%`\n"
            f"⏳ Статус: `{war.state}`\n\n"
        )
        
        # Таблица последних атак
        table = PrettyTable()
        table.field_names = ["Атакующий", "Цель", "Результат"]
        table.align["Атакующий"] = "l"
        table.align["Цель"] = "l"
        
        attacks_displayed = 0
        if hasattr(our_clan, 'members') and our_clan.members:
            # Собираем все атаки и сортируем по времени (свежие внизу или вверху, тут просто берем последние)
            all_attacks = []
            for member in our_clan.members:
                if hasattr(member, 'attacks'):
                    for attack in member.attacks:
                        all_attacks.append((member, attack))
            
            # Берем последние 10 атак
            recent_attacks = all_attacks[-10:] if len(all_attacks) > 10 else all_attacks
            
            for member, attack in recent_attacks:
                try:
                    defender = war.get_opponent_member(attack.defender_tag)
                    stars_str = "⭐" * attack.stars
                    dest_str = f"{attack.destruction}%"
                    table.add_row([f"{member.name} (ТХ{member.town_hall})", f"{defender.name} (ТХ{defender.town_hall})", f"{stars_str} {dest_str}"])
                    attacks_displayed += 1
                except: continue
        
        if attacks_displayed > 0:
            text += f"🕒 **Последние атаки:**\n<pre><code>{table}</code></pre>\n"
        
        # Список кто не доиграл
        unused = []
        if hasattr(our_clan, 'members'):
            unused = [m for m in our_clan.members if hasattr(m, 'attacks') and len(m.attacks) < war.attacks_per_member]
            
        if unused:
            text += "\n⚠️ **Есть неиспользованные атаки:**\n"
            for m in unused:
                left = war.attacks_per_member - len(m.attacks)
                status = "❗️ 2 атаки" if left == 2 else "⚠️ 1 атака"
                text += f"• {m.name} (ТХ{m.town_hall}): {status}\n"
        else:
            text += "\n✅ Все атаки использованы!"

        if isinstance(update, types.Message):
            await update.answer(text, parse_mode="HTML", reply_markup=get_back_keyboard())
        else:
            await update.message.answer(text, parse_mode="HTML", reply_markup=get_back_keyboard())

    except coc.PrivateWarLog:
        msg = "🔒 Лог войны закрыт настройками клана."
        if isinstance(update, types.Message): await update.answer(msg)
        else: await update.message.answer(msg)
    except Exception as e:
        logger.error(f"Error war info: {e}", exc_info=True)
        msg = "❌ Ошибка данных войны."
        if isinstance(update, types.Message): await update.answer(msg)
        else: await update.message.answer(msg)

async def handle_war_targets(update: types.Message | types.CallbackQuery):
    """Умный подбор целей"""
    if not coc_client: return
    try:
        war = await coc_client.get_current_war(CLAN_TAG)
        if war.state == "notInWar":
            msg = "🔍 Войны нет."
            if isinstance(update, types.Message): await update.answer(msg)
            else: await update.message.answer(msg)
            return

        our_members = sorted(war.clan.members, key=lambda x: x.town_hall, reverse=True)
        enemy_members = sorted(war.opponent.members, key=lambda x: x.town_hall, reverse=True)
        
        # Фильтруем только тех, у кого есть атаки (для простоты берем всех, но можно фильтровать по атакам)
        # Логика: Наш самый сильный бьет самого сильного врага.
        # Если у нас больше людей, лишние добивают нижних.
        
        text = "🎯 **РЕКОМЕНДАЦИИ ПО АТАКАМ (AI):**\n\n"
        text += "_Логика: Равный бьет равного. Если мы сильнее сверху, то наши средние бьют их топ._\n\n"
        
        targets_assigned = []
        enemy_idx = 0
        
        for our_member in our_members:
            # Пропускаем тех, кто уже сделал 2 атаки (если нужно учитывать текущий статус)
            # Но для плана на всю войну лучше расписать всем
            if enemy_idx >= len(enemy_members):
                break # Враги кончились
            
            enemy = enemy_members[enemy_idx]
            
            # Красивое описание матчапа
            matchup = ""
            if our_member.town_hall > enemy.town_hall:
                matchup = "✅ Легкая цель"
            elif our_member.town_hall < enemy.town_hall:
                matchup = "⚠️ Сложная цель"
            else:
                matchup = "⚖️ Равный бой"
            
            targets_assigned.append(f"🔹 {our_member.name} (ТХ{our_member.town_hall}) ➜ {enemy.name} (ТХ{enemy.town_hall}) [{matchup}]")
            
            # Простая логика перебора: каждый бьет своего по списку
            # Для более сложной логики (добивание) нужно усложнять алгоритм
            enemy_idx += 1
            
        # Вывод списка
        for line in targets_assigned:
            text += f"{line}\n"
            
        if isinstance(update, types.Message):
            await update.answer(text, parse_mode="Markdown", reply_markup=get_back_keyboard())
        else:
            await update.message.answer(text, parse_mode="Markdown", reply_markup=get_back_keyboard())

    except Exception as e:
        logger.error(f"Error war targets: {e}", exc_info=True)
        msg = "❌ Ошибка подбора целей."
        if isinstance(update, types.Message): await update.answer(msg)
        else: await update.message.answer(msg)

async def handle_remind_attacks(update: types.Message | types.CallbackQuery):
    if not coc_client: return
    try:
        war = await coc_client.get_current_war(CLAN_TAG)
        if war.state == "notInWar":
            msg = "🔍 Войны нет."
            if isinstance(update, types.Message): await update.answer(msg)
            else: await update.message.answer(msg)
            return
            
        unused = []
        if hasattr(war, 'clan') and hasattr(war.clan, 'members'):
            unused = [m for m in war.clan.members if hasattr(m, 'attacks') and len(m.attacks) < war.attacks_per_member]
            
        if not unused:
            msg = "✅ Все участники использовали свои атаки!"
        else:
            # Разделяем на тех у кого 1 и 2 атаки
            two_attacks = [m for m in unused if (war.attacks_per_member - len(m.attacks)) == 2]
            one_attack = [m for m in unused if (war.attacks_per_member - len(m.attacks)) == 1]
            
            msg = "⚔️ **НАПОМИНАНИЕ ОБ АТАКАХ:**\n\n"
            
            if two_attacks:
                msg += "🔴 **НЕ НАЧАЛИ ВООВЩЕ (2 атаки):**\n"
                for m in two_attacks:
                    msg += f"• {m.name} (ТХ{m.town_hall})\n"
                msg += "\n"
                
            if one_attack:
                msg += "🟠 **ДОБИТЬ (1 атака):**\n"
                for m in one_attack:
                    msg += f"• {m.name} (ТХ{m.town_hall})\n"
        
        if isinstance(update, types.Message):
            await update.answer(msg, parse_mode="Markdown", reply_markup=get_back_keyboard())
        else:
            await update.message.answer(msg, parse_mode="Markdown", reply_markup=get_back_keyboard())
            
    except Exception as e:
        logger.error(f"Error remind: {e}", exc_info=True)
        msg = "❌ Ошибка проверки атак."
        if isinstance(update, types.Message): await update.answer(msg)
        else: await update.message.answer(msg)

# ============================================================
# 🤖 ОБРАБОТЧИКИ КОМАНД
# ============================================================
@dp.message(Command("start"), AdminFilter())
async def cmd_start(message: types.Message):
    text = f"👋 Привет, {message.from_user.first_name}!\n\nЯ бот-помощник для управления кланом.\nВыберите действие:"
    await message.answer(text, reply_markup=get_main_keyboard())

@dp.message(Command("help"), AdminFilter())
async def cmd_help(message: types.Message):
    await cmd_start(message)

@dp.message(Command("clan"), AdminFilter())
async def cmd_clan(message: types.Message):
    await handle_clan_info(message)

@dp.message(Command("members"), AdminFilter())
async def cmd_members(message: types.Message):
    await handle_members_list(message)

@dp.message(Command("war"), AdminFilter())
async def cmd_war(message: types.Message):
    await handle_war_info(message)

@dp.message(Command("remind"), AdminFilter())
async def cmd_remind(message: types.Message):
    await handle_remind_attacks(message)

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

@dp.callback_query(F.data == "members_list")
async def cb_members(callback: types.CallbackQuery):
    await callback.answer()
    await handle_members_list(callback)

@dp.callback_query(F.data == "war_info")
async def cb_war(callback: types.CallbackQuery):
    await callback.answer()
    await handle_war_info(callback)

@dp.callback_query(F.data == "war_targets") # Новая функция
async def cb_targets(callback: types.CallbackQuery):
    await callback.answer()
    await handle_war_targets(callback)

@dp.callback_query(F.data == "remind_attacks")
async def cb_remind(callback: types.CallbackQuery):
    await callback.answer()
    await handle_remind_attacks(callback)

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
            coc_client = coc.Client(proxy=proxy)
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
