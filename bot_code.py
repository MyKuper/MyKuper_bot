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
        msg = "❌ Клиент COC не подключен."
        await send_msg(update, msg)
        return
    try:
        clan = await coc_client.get_clan(CLAN_TAG)
        text = (
            f"🏰 **{clan.name}** `{clan.tag}`\n"
            f"📊 Уровень: `{clan.level}`\n"
            f"👥 Участников: `{clan.member_count}/50`\n"
            f"🏆 Трофеи: `{clan.points}`\n"
            f"🛡️ Вход: `{clan.required_trophies}`\n"
            f"🌍 Регион: `{clan.location.name if clan.location else 'Global'}`\n\n"
            f"📝 _{clan.description or 'Нет описания'}_"
        )
        await send_msg(update, text, parse_mode="Markdown", reply_markup=get_back_keyboard())
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
        
        # --- Заголовок войны ---
        header = (
            f"⚔️ **ВОЙНА: {our_clan.name} vs {enemy_clan.name}**\n\n"
            f"📊 Счёт: `{our_clan.stars}` : `{enemy_clan.stars}`\n"
            f"💥 Разрушение: `{our_clan.destruction:.1f}%` : `{enemy_clan.destruction:.1f}%`\n"
            f"⏳ Статус: `{war.state}`\n\n"
        )

        # --- Список тех, кто требует внимания (ПОЛНЫЙ) ---
        lazy_list = []
        for m in our_clan.members:
            attacks_count = len(m.attacks) if hasattr(m, 'attacks') else 0
            if attacks_count < war.attacks_per_member:
                lazy_list.append(f"• {m.name} (ТХ{m.town_hall}) - осталось {war.attacks_per_member - attacks_count}")
        
        if lazy_list:
            header += f"⚠️ **Требуют внимания ({len(lazy_list)} чел.):**\n" + "\n".join(lazy_list) + "\n\n"
        else:
            header += "✅ Все участники использовали все атаки!\n\n"

        # --- УМНЫЙ ПЛАН АТАКИ (AI) ---
        # Сортируем наших и врагов по ТХ
        our_members = sorted([m for m in our_clan.members if len(m.attacks) < war.attacks_per_member], key=lambda x: x.town_hall, reverse=True)
        enemy_members = sorted(enemy_clan.members, key=lambda x: x.town_hall, reverse=True)
        
        # Фильтруем только тех врагов, кого еще не забрали на 3 звезды (упрощенно: всех, кроме тех, у кого 3 звезды)
        # Для простоты берем всех врагов, но в реальном бою лучше фильтровать по destruction < 100
        available_targets = [e for e in enemy_members if e.destruction < 100] 
        
        plan_text = "🧠 **ТАКТИЧЕСКИЙ ПЛАН (AI)**\n"
        plan_text += "_Распределение целей по приоритету:_\n\n"

        if not our_members:
            plan_text += "🎉 Все наши бойцы уже отатаковали!"
        else:
            # Таблица 1: Первый удар (Основные цели)
            table_1 = PrettyTable()
            table_1.field_names = ["Боец", "Цель (Приоритет)", "Статус"]
            table_1.align["Боец"] = "l"
            table_1.align["Цель (Приоритет)"] = "l"
            
            used_targets = set()
            first_strike_pairs = []

            # Попытка подобрать равных
            for attacker in our_members:
                if len(attacker.attacks) >= 1: continue # Если уже ходил первый раз, пропускаем в этом этапе
                
                target = None
                # Ищем равного или чуть выше
                for enemy in available_targets:
                    if enemy.tag in used_targets: continue
                    if enemy.town_hall >= attacker.town_hall:
                        target = enemy
                        break
                
                # Если не нашли равного, берем самого сильного из оставшихся
                if not target:
                    for enemy in available_targets:
                        if enemy.tag in used_targets: continue
                        target = enemy
                        break
                
                if target:
                    used_targets.add(target.tag)
                    status = "Ждет хода" if len(attacker.attacks) == 0 else "Нужен 2-й удар"
                    table_1.add_row([f"{attacker.name} (ТХ{attacker.town_hall})", f"{target.name} (ТХ{target.town_hall})", status])
                    first_strike_pairs.append((attacker, target))

            plan_text += f"**1️⃣ ОСНОВНОЙ УДАР (Первые атаки):**\n<pre><code>{table_1}</code></pre>\n\n"

            # Таблица 2: Добивание и Набивка (Вторые атаки)
            table_2 = PrettyTable()
            table_2.field_names = ["Боец (Добивающий)", "Цель (Добить/Набить)", "Рекомендация"]
            table_2.align["Боец (Добивающий)"] = "l"
            
            plan_text += f"**2️⃣ ДОБИВАНИЕ И НАБИВКА (Вторые атаки):**\n"
            
            has_second_strikes = False
            for attacker in our_members:
                if len(attacker.attacks) < 2: # Есть вторая атака
                    # Ищем цель с высоким %, но не 100, или легкую для набивки
                    # Логика: если есть раненый враг высокого уровня - добиваем. Если нет - бьем низкого.
                    target = None
                    rec = "Поиск цели..."
                    
                    # Приоритет: недобитые высокие
                    for enemy in available_targets:
                        if enemy.tag in used_targets: continue
                        if enemy.town_hall >= attacker.town_hall and enemy.destruction > 0 and enemy.destruction < 100:
                            target = enemy
                            rec = f"🚑 ДОБИТЬ ({enemy.destruction}%)"
                            break
                    
                    # Если нет добивания, ищем легкую цель
                    if not target:
                        for enemy in available_targets:
                            if enemy.tag in used_targets: continue
                            if enemy.town_hall <= attacker.town_hall:
                                target = enemy
                                rec = "⚔️ Набивка звезд"
                                break
                    
                    # Если совсем ничего нет, берем любого свободного
                    if not target:
                         for enemy in available_targets:
                            if enemy.tag in used_targets: continue
                            target = enemy
                            rec = "Свободная цель"
                            break
                    
                    if target:
                        has_second_strikes = True
                        used_targets.add(target.tag)
                        table_2.add_row([f"{attacker.name} (ТХ{attacker.town_hall})", f"{target.name} (ТХ{target.town_hall})", rec])
            
            if has_second_strikes:
                plan_text += f"<pre><code>{table_2}</code></pre>"
            else:
                plan_text += "_Нет доступных вторых атак или целей._"

        full_text = header + plan_text
        await send_msg(update, full_text, parse_mode="HTML", reply_markup=get_back_keyboard())

    except coc.PrivateWarLog:
        await send_msg(update, "🔒 Лог войны закрыт настройками клана.")
    except Exception as e:
        logger.error(f"Error war plan: {e}", exc_info=True)
        await send_msg(update, "❌ Ошибка формирования плана войны.")

async def handle_remind_full(update: types.Message | types.CallbackQuery):
    if not coc_client: return
    try:
        war = await coc_client.get_current_war(CLAN_TAG)
        if war.state == "notInWar":
            await send_msg(update, "🔍 Войны нет.")
            return

        zero_attacks = []
        one_attack = []

        for m in war.clan.members:
            count = len(m.attacks) if hasattr(m, 'attacks') else 0
            if count == 0:
                zero_attacks.append(f"• {m.name} (ТХ{m.town_hall})")
            elif count == 1:
                one_attack.append(f"• {m.name} (ТХ{m.town_hall})")

        text = "⏰ **ПОЛНЫЙ СПИСОК НЕДОЧЕТОВ**\n\n"
        
        if zero_attacks:
            text += f"🔴 **ВООБЩЕ НЕ ХОДИЛИ ({len(zero_attacks)}):**\n" + "\n".join(zero_attacks) + "\n\n"
        else:
            text += "🟢 Все сделали хотя бы 1 атаку.\n\n"

        if one_attack:
            text += f"🟠 **НУЖНО ДОБИТЬ ({len(one_attack)}):**\n" + "\n".join(one_attack)
        else:
            text += "🟢 Все использовали все атаки!"

        await send_msg(update, text, parse_mode="Markdown", reply_markup=get_back_keyboard())

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
        for member in war.clan.members:
            for attack in member.attacks:
                if count >= 20: break # Лимит вывода
                defender = war.get_opponent_member(attack.defender_tag)
                d_name = defender.name if defender else "Unknown"
                d_th = defender.town_hall if defender else "?"
                table.add_row([f"{member.name}", f"{d_name} (ТХ{d_th})", "⭐"*attack.stars, f"{attack.destruction}%"])
                count += 1
        
        if count == 0:
            await send_msg(update, "📭 Атак пока не зафиксировано.", reply_markup=get_back_keyboard())
        else:
            text = f"📊 **ИСТОРИЯ АТАК (последние {count}):**\n<pre><code>{table}</code></pre>"
            await send_msg(update, text, parse_mode="HTML", reply_markup=get_back_keyboard())

    except Exception as e:
        logger.error(f"Error logs: {e}")
        await send_msg(update, "❌ Ошибка загрузки истории.")

# Вспомогательная функция
async def send_msg(update: types.Message | types.CallbackQuery, text: str, parse_mode=None, reply_markup=None):
    if isinstance(update, types.Message):
        await update.answer(text, parse_mode=parse_mode, reply_markup=reply_markup)
    else:
        await update.message.answer(text, parse_mode=parse_mode, reply_markup=reply_markup)

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
    await callback.answer()
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
