import os
import logging
import asyncio
from typing import List, Optional, Dict, Any
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
ADMIN_IDS = []  # Твой ID: 1810701319

# ============================================================
# 🛠 ЛОГИРОВАНИЕ И УТИЛИТЫ
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
        [InlineKeyboardButton(text="🧠 УМНЫЙ ПЛАН АТАКИ", callback_data="ai_plan")],
        [InlineKeyboardButton(text="⚔️ Статус Войны", callback_data="war_status"),
         InlineKeyboardButton(text="🏰 Инфо о клане", callback_data="clan_info")],
        [InlineKeyboardButton(text="👥 Список участников", callback_data="members_list"),
         InlineKeyboardButton(text="⏰ Кто не ходил", callback_data="remind_attacks")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)

def get_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 В меню", callback_data="back_menu")]])

# ============================================================
# 🧠 ЛОГИКА (AI И ТАКТИКА)
# ============================================================

async def generate_ai_attack_plan(war) -> str:
    """
    Генерирует идеальный план атаки: ТХ на ТХ.
    Возвращает красивую таблицу с рекомендациями.
    """
    our_members = sorted(war.clan.members, key=lambda m: m.town_hall, reverse=True)
    enemy_members = sorted(war.opponent.members, key=lambda m: m.town_hall, reverse=True)
    
    # Создаем копию списка врагов, чтобы отмечать занятых
    available_enemies = list(enemy_members)
    
    table = PrettyTable()
    table.field_names = ["Наш боец", "ЦЕЛЬ (Враг)", "Статус атак", "Рекомендация"]
    table.align["Наш боец"] = "l"
    table.align["ЦЕЛЬ (Враг)"] = "l"
    table.align["Статус атак"] = "c"
    table.align["Рекомендация"] = "l"

    plan_text = []

    for attacker in our_members:
        attacks_made = len(attacker.attacks)
        status_str = f"{attacks_made}/2"
        
        # Определяем статус для цвета/текста
        if attacks_made == 0: status_emoji = "❌ Не ходил"
        elif attacks_made == 1: status_emoji = "⚠️ Нужен добив"
        else: status_emoji = "✅ Готов"

        # Логика подбора цели: ищем такого же ТХ
        best_target = None
        target_idx = -1
        
        # 1. Ищем точное совпадение ТХ среди доступных
        for i, enemy in enumerate(available_enemies):
            if enemy.town_hall == attacker.town_hall:
                best_target = enemy
                target_idx = i
                break
        
        # 2. Если нет точного, ищем чуть ниже (набивка) или чуть выше (если герой)
        if not best_target:
            for i, enemy in enumerate(available_enemies):
                if enemy.town_hall <= attacker.town_hall:
                    best_target = enemy
                    target_idx = i
                    break
        
        # 3. Если совсем ничего нет (все заняты), берем первого доступного или самого жирного
        if not best_target and available_enemies:
            best_target = available_enemies[0]
            target_idx = 0

        if best_target:
            # Удаляем цель из доступных, чтобы следующий не взял её же
            available_enemies.pop(target_idx)
            
            rec_text = f"⚔️ Бей {best_target.name} (ТХ{best_target.town_hall})"
            if attacker.town_hall > best_target.town_hall:
                rec_text += " (Набивка)"
            elif attacker.town_hall < best_target.town_hall:
                rec_text += " (Сложно! Помощь?)"
            
            table.add_row([f"{attacker.name} (ТХ{attacker.town_hall})", 
                           f"{best_target.name} (ТХ{best_target.town_hall})", 
                           status_str, 
                           rec_text])
        else:
            table.add_row([f"{attacker.name} (ТХ{attacker.town_hall})", 
                           "Нет целей", 
                           status_str, 
                           "❓ Ожидание"])

    # Добавляем статистику
    total_attacks = sum(len(m.attacks) for m in our_members)
    max_attacks = len(our_members) * 2
    percent = round((total_attacks / max_attacks) * 100, 1)
    
    header = f"🧠 **ТАКТИЧЕСКИЙ ПЛАН АТАКИ**\n"
    header += f"📊 Прогресс: {total_attacks}/{max_attacks} ({percent}%)\n\n"
    header += f"🎯 *Принцип: Каждый бьет равного по ТХ. Если нет — следующего по силе.*\n\n"
    
    return header + f"<pre><code>{table}</code></pre>"

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
        logger.error(f"Error clan info: {e}")
        msg = "❌ Ошибка данных клана."
        if isinstance(update, types.Message): await update.answer(msg)
        else: await update.message.answer(msg)

async def handle_members_list(update: types.Message | types.CallbackQuery):
    if not coc_client: return
    try:
        clan = await coc_client.get_clan(CLAN_TAG)
        if not hasattr(clan, 'members') or not clan.members:
            raise ValueError("Список участников пуст")
            
        members = sorted(clan.members, key=lambda m: m.town_hall, reverse=True)
        
        table = PrettyTable()
        table.field_names = ["Имя", "Роль", "ТХ", "Трофеи"]
        table.align["Имя"] = "l"
        
        for m in members:
            role_map = {"leader": "👑", "coLeader": "🛡️", "elder": "🎖️", "member": "👤"}
            role_icon = role_map.get(getattr(m, 'role', 'member'), "👤")
            table.add_row([m.name, role_icon, getattr(m, 'town_hall', '?'), getattr(m, 'trophies', 0)])
            
        text = f"👥 **Состав клана (по ТХ):**\n<pre><code>{table}</code></pre>"
        if isinstance(update, types.Message):
            await update.answer(text, parse_mode="HTML", reply_markup=get_back_keyboard())
        else:
            await update.message.answer(text, parse_mode="HTML", reply_markup=get_back_keyboard())
    except Exception as e:
        logger.error(f"Error members list: {e}", exc_info=True)
        msg = "❌ Ошибка списка участников."
        if isinstance(update, types.Message): await update.answer(msg)
        else: await update.message.answer(msg)

async def handle_war_status(update: types.Message | types.CallbackQuery):
    if not coc_client: return
    try:
        war = await coc_client.get_current_war(CLAN_TAG)
        if war.state == "notInWar":
            msg = "🔍 Войны нет."
            if isinstance(update, types.Message): await update.answer(msg, reply_markup=get_back_keyboard())
            else: await update.message.answer(msg, reply_markup=get_back_keyboard())
            return

        our_clan = war.clan
        enemy_clan = war.opponent
        
        text = (
            f"⚔️ **ВОЙНА: {our_clan.name} vs {enemy_clan.name}**\n\n"
            f"📊 Счёт: `{our_clan.stars}` : `{enemy_clan.stars}`\n"
            f"💥 Разрушение: `{our_clan.destruction}%` : `{enemy_clan.destruction}%`\n"
            f"⏳ Статус: `{war.state}`\n"
        )
        
        # Краткий список тех, кто не доделал
        unused = [m for m in our_clan.members if len(m.attacks) < 2]
        if unused:
            text += f"\n⚠️ **Требуют внимания ({len(unused)} чел.):**\n"
            for m in unused[:5]:
                left = 2 - len(m.attacks)
                text += f"• {m.name} (ТХ{m.town_hall}) - осталось {left}\n"
            if len(unused) > 5: text += f"_... и еще {len(unused)-5}_\n"
        else:
            text += "\n✅ Все атаки использованы!"

        if isinstance(update, types.Message):
            await update.answer(text, parse_mode="Markdown", reply_markup=get_back_keyboard())
        else:
            await update.message.answer(text, parse_mode="Markdown", reply_markup=get_back_keyboard())

    except coc.PrivateWarLog:
        msg = "🔒 Лог войны закрыт."
        if isinstance(update, types.Message): await update.answer(msg)
        else: await update.message.answer(msg)
    except Exception as e:
        logger.error(f"Error war status: {e}", exc_info=True)
        msg = "❌ Ошибка данных войны."
        if isinstance(update, types.Message): await update.answer(msg)
        else: await update.message.answer(msg)

async def handle_ai_plan(update: types.Message | types.CallbackQuery):
    if not coc_client: return
    try:
        war = await coc_client.get_current_war(CLAN_TAG)
        if war.state == "notInWar":
            msg = "🔍 Войны нет, план составить нельзя."
            if isinstance(update, types.Message): await update.answer(msg, reply_markup=get_back_keyboard())
            else: await update.message.answer(msg, reply_markup=get_back_keyboard())
            return
        
        plan_text = await generate_ai_attack_plan(war)
        
        if isinstance(update, types.Message):
            await update.answer(plan_text, parse_mode="HTML", reply_markup=get_back_keyboard())
        else:
            await update.message.answer(plan_text, parse_mode="HTML", reply_markup=get_back_keyboard())
            
    except Exception as e:
        logger.error(f"Error AI plan: {e}", exc_info=True)
        msg = "❌ Ошибка генерации плана. Проверьте логи войны."
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
            
        zero_attacks = []
        one_attack = []
        
        for m in war.clan.members:
            count = len(m.attacks)
            if count == 0:
                zero_attacks.append(m)
            elif count == 1:
                one_attack.append(m)
        
        msg = "⏰ **НАПОМИНАЛКА ПО АТАКАМ**\n\n"
        
        if zero_attacks:
            msg += "🔴 **ВООБЩЕ НЕ ХОДИЛИ (0 атак):**\n"
            for m in sorted(zero_attacks, key=lambda x: x.town_hall, reverse=True):
                msg += f"• {m.name} (ТХ{m.town_hall})\n"
            msg += "\n"
        
        if one_attack:
            msg += "🟠 **НУЖНО ДОБИТЬ (1 атака):**\n"
            for m in sorted(one_attack, key=lambda x: x.town_hall, reverse=True):
                msg += f"• {m.name} (ТХ{m.town_hall})\n"
        
        if not zero_attacks and not one_attack:
            msg += "✅ Все молодцы! Все атаки сделаны!"

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
    text = f"👋 Привет, {message.from_user.first_name}!\n\n🤖 Я **Clash Assistant** с функцией AI-подбора.\nВыберите действие:"
    await message.answer(text, parse_mode="Markdown", reply_markup=get_main_keyboard())

@dp.message(Command("help"), AdminFilter())
async def cmd_help(message: types.Message):
    await cmd_start(message)

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

@dp.callback_query(F.data == "war_status")
async def cb_war(callback: types.CallbackQuery):
    await callback.answer()
    await handle_war_status(callback)

@dp.callback_query(F.data == "ai_plan")
async def cb_ai(callback: types.CallbackQuery):
    await callback.answer("Генерирую тактику...")
    await handle_ai_plan(callback)

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
