import os
import logging
import asyncio
from typing import List, Optional, Dict, Tuple
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
        [InlineKeyboardButton(text="⚔️ Статус Войны", callback_data="war_status"),
         InlineKeyboardButton(text="🏰 Инфо Клана", callback_data="clan_info")],
        [InlineKeyboardButton(text="🧠 УМНЫЙ ПОДБОР (AI)", callback_data="ai_attack_plan")],
        [InlineKeyboardButton(text="📊 Кто кого атаковал", callback_data="war_attacks_log")],
        [InlineKeyboardButton(text="⏰ Напоминание (Лентяи)", callback_data="remind_attacks")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)

def get_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 В меню", callback_data="back_menu")]])

# ============================================================
# 🧠 УМНЫЙ АЛГОРИТМ ПОДБОРА (AI SIMULATION)
# ============================================================

def calculate_attack_plan(war) -> str:
    """
    Алгоритм 'Умный Штурм':
    1. Сортируем наших и врагов по ТХ.
    2. Пытаемся найти равного противника (ТХ == ТХ).
    3. Если нет равного, ищем следующего доступного.
    4. Учитываем уже сделанные атаки.
    """
    our_members = sorted(war.clan.members, key=lambda m: m.town_hall, reverse=True)
    enemy_members = sorted(war.opponent.members, key=lambda m: m.town_hall, reverse=True)
    
    # Создаем копию списка врагов, чтобы отмечать занятых
    available_enemies = list(enemy_members)
    
    plan_text = "🧠 **ПЛАН АТАКИ (УМНЫЙ ПОДБОР)**\n\n"
    plan_text += "🎯 *Приоритет: Равный ТХ -> Выше ТХ (добив) -> Ниже ТХ (набив)*\n\n"
    
    table = PrettyTable()
    table.field_names = ["Наш боец", "Цель (Враг)", "Статус", "Рекомендация"]
    table.align["Наш боец"] = "l"
    table.align["Цель (Враг)"] = "l"
    
    attacks_per_member = war.attacks_per_member
    
    for member in our_members:
        # Сколько атак уже сделал наш боец
        attacks_done = len(member.attacks)
        if attacks_done >= attacks_per_member:
            continue # Этот уже все сделал
            
        # Ищем цель
        target = None
        target_idx = -1
        recommendation = ""
        
        # 1. Ищем равного по ТХ
        for i, enemy in enumerate(available_enemies):
            if enemy.town_hall == member.town_hall:
                # Проверка: не атаковал ли он уже этого врага?
                already_attacked = any(a.defender_tag == enemy.tag for a in member.attacks)
                if not already_attacked:
                    target = enemy
                    target_idx = i
                    recommendation = "⚖️ Равный бой"
                    break
        
        # 2. Если равного нет, ищем чуть выше (для добивания сильных) или ниже (для набивки)
        if not target:
            for i, enemy in enumerate(available_enemies):
                already_attacked = any(a.defender_tag == enemy.tag for a in member.attacks)
                if not already_attacked:
                    # Приоритет: бить того, кто еще не получил 2 звезды от других (упрощенно)
                    target = enemy
                    target_idx = i
                    if enemy.town_hall > member.town_hall:
                        recommendation = "⚠️ Сложная цель (Герои?)"
                    else:
                        recommendation = "✅ Легкая цель (Набивка)"
                    break
        
        if target:
            status = f"{attacks_done}/{attacks_per_member}"
            table.add_row([f"{member.name} (ТХ{member.town_hall})", f"{target.name} (ТХ{target.town_hall})", status, recommendation])
            # Если атака планируется, временно помечаем врага как занятого для следующих (логика упрощена)
            # В реальном бою список динамический, но для плана это работает
            # available_enemies.pop(target_idx) 

    plan_text += f"<pre><code>{table}</code></pre>"
    
    # Добавляем статистику
    total_attacks = sum(len(m.attacks) for m in our_members)
    max_attacks = len(our_members) * attacks_per_member
    progress = round((total_attacks / max_attacks) * 100, 1)
    
    plan_text += f"\n📊 **Прогресс войны:** {total_attacks}/{max_attacks} атак ({progress}%)"
    
    return plan_text

def analyze_existing_attacks(war) -> str:
    """Анализирует, кто кого уже атаковал"""
    our_members = war.clan.members
    text = "⚔️ **ХРОНИКА БОЯ (Кто кого атаковал)**\n\n"
    
    table = PrettyTable()
    table.field_names = ["Атакующий", "Защитник", "Звезды", "%", "Статус"]
    table.align["Атакующий"] = "l"
    table.align["Защитник"] = "l"
    
    count = 0
    for member in our_members:
        for attack in member.attacks:
            try:
                defender = war.get_opponent_member(attack.defender_tag)
                stars = "⭐" * attack.stars
                status = "💀 Снос" if attack.stars == 3 else ("🔥 Частично" if attack.stars > 0 else "❌ Пусто")
                table.add_row([
                    f"{member.name} (ТХ{member.town_hall})",
                    f"{defender.name} (ТХ{defender.town_hall})",
                    stars,
                    f"{attack.destruction}%",
                    status
                ])
                count += 1
            except:
                continue
    
    if count == 0:
        return "🔍 Атак пока не зафиксировано."
        
    text += f"<pre><code>{table}</code></pre>"
    text += f"\n_Всего атак: {count}_"
    return text

# ============================================================
# 🧠 ОСНОВНАЯ ЛОГИКА
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
        logger.error(f"Error clan info: {e}")
        msg = "❌ Ошибка данных клана."
        if isinstance(update, types.Message): await update.answer(msg)
        else: await update.message.answer(msg)

async def handle_war_status(update: types.Message | types.CallbackQuery):
    if not coc_client: return
    try:
        war = await coc_client.get_current_war(CLAN_TAG)
        if war.state == "notInWar":
            msg = "🔍 Войны сейчас нет."
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
        
        # Краткий список лентяев
        unused = [m for m in our_clan.members if len(m.attacks) < war.attacks_per_member]
        if unused:
            text += f"\n⚠️ **Ждут хода:** {len(unused)} чел.\n"
            # Топ 3 кто не ходил
            for m in unused[:3]:
                text += f"• {m.name} (ТХ{m.town_hall})\n"
        
        if isinstance(update, types.Message):
            await update.answer(text, parse_mode="Markdown", reply_markup=get_back_keyboard())
        else:
            await update.message.answer(text, parse_mode="Markdown", reply_markup=get_back_keyboard())
            
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
            if isinstance(update, types.Message): await update.answer(msg)
            else: await update.message.answer(msg)
            return
            
        plan = calculate_attack_plan(war)
        if isinstance(update, types.Message):
            await update.answer(plan, parse_mode="HTML", reply_markup=get_back_keyboard())
        else:
            await update.message.answer(plan, parse_mode="HTML", reply_markup=get_back_keyboard())
            
    except Exception as e:
        logger.error(f"Error AI plan: {e}", exc_info=True)
        msg = "❌ Ошибка при расчете плана."
        if isinstance(update, types.Message): await update.answer(msg)
        else: await update.message.answer(msg)

async def handle_attacks_log(update: types.Message | types.CallbackQuery):
    if not coc_client: return
    try:
        war = await coc_client.get_current_war(CLAN_TAG)
        if war.state == "notInWar":
            msg = "🔍 Войны нет."
            if isinstance(update, types.Message): await update.answer(msg)
            else: await update.message.answer(msg)
            return
            
        log = analyze_existing_attacks(war)
        if isinstance(update, types.Message):
            await update.answer(log, parse_mode="HTML", reply_markup=get_back_keyboard())
        else:
            await update.message.answer(log, parse_mode="HTML", reply_markup=get_back_keyboard())
            
    except Exception as e:
        logger.error(f"Error attacks log: {e}", exc_info=True)
        msg = "❌ Ошибка получения логов атак."
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
            
        unused_full = [] # 0 атак
        unused_partial = [] # 1 атака
        
        for m in war.clan.members:
            count = len(m.attacks)
            if count == 0:
                unused_full.append(m)
            elif count < war.attacks_per_member:
                unused_partial.append(m)
        
        msg = "⏰ **НАПОМИНАЛКА**\n\n"
        
        if unused_full:
            msg += "🔴 **ВООБЩЕ НЕ ХОДИЛИ (0 атак):**\n"
            for m in sorted(unused_full, key=lambda x: x.town_hall, reverse=True):
                msg += f"• {m.name} (ТХ{m.town_hall})\n"
            msg += "\n"
            
        if unused_partial:
            msg += "🟠 **НУЖНО ДОБИТЬ (1 атака):**\n"
            for m in sorted(unused_partial, key=lambda x: x.town_hall, reverse=True):
                left = war.attacks_per_member - len(m.attacks)
                msg += f"• {m.name} (ТХ{m.town_hall}) - осталось {left}\n"
        elif not unused_full:
            msg += "✅ Все отходили!"
            
        if isinstance(update, types.Message):
            await update.answer(msg, parse_mode="Markdown", reply_markup=get_back_keyboard())
        else:
            await update.message.answer(msg, parse_mode="Markdown", reply_markup=get_back_keyboard())
            
    except Exception as e:
        logger.error(f"Error remind: {e}", exc_info=True)
        msg = "❌ Ошибка напоминания."
        if isinstance(update, types.Message): await update.answer(msg)
        else: await update.message.answer(msg)

# ============================================================
# 🤖 ОБРАБОТЧИКИ КОМАНД
# ============================================================
@dp.message(Command("start"), AdminFilter())
async def cmd_start(message: types.Message):
    text = f"👋 Привет, {message.from_user.first_name}!\n\n🤖 Я **Clash Assistant** с функцией **AI-подбора**.\nВыберите действие:"
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

@dp.callback_query(F.data == "war_status")
async def cb_war(callback: types.CallbackQuery):
    await callback.answer()
    await handle_war_status(callback)

@dp.callback_query(F.data == "ai_attack_plan")
async def cb_ai(callback: types.CallbackQuery):
    await callback.answer("🧠 Расчет плана...")
    await handle_ai_plan(callback)

@dp.callback_query(F.data == "war_attacks_log")
async def cb_log(callback: types.CallbackQuery):
    await callback.answer("📊 Загрузка логов...")
    await handle_attacks_log(callback)

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
