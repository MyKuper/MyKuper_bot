import os
import logging
import asyncio
from typing import Optional, List, Union
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
PROXY_URL = None  # Можно указать прокси, если нужен
ADMIN_IDS = [1810701319]  # Ваш ID добавлен

# Логирование
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Глобальные переменные (Инициализируем сразу, чтобы декораторы работали)
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()
coc_client: Optional[coc.Client] = None

# ============================================================
# 🔒 ФИЛЬТРЫ
# ============================================================
class AdminFilter(Filter):
    async def __call__(self, message: types.Message) -> bool:
        if not ADMIN_IDS:
            return True
        return message.from_user.id in ADMIN_IDS

# ============================================================
# ⌨️ КЛАВИАТУРЫ
# ============================================================
def get_main_keyboard() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton(text="⚔️ Война", callback_data="war_info"),
         InlineKeyboardButton(text="🏰 Клан", callback_data="clan_info")],
        [InlineKeyboardButton(text="👥 Участники", callback_data="members_list"),
         InlineKeyboardButton(text="🔍 Игрок", callback_data="player_search")],
        [InlineKeyboardButton(text="⏰ Напомнить", callback_data="remind_attacks")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)

def get_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="back_menu")]])

# ============================================================
# 🤖 КОМАНДЫ
# ============================================================
@dp.message(Command("start"), AdminFilter())
async def cmd_start(message: types.Message):
    text = f"👋 Привет, {message.from_user.first_name}!\n\nЯ бот-помощник для управления кланом Clash of Clans.\nВыберите действие в меню ниже:"
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

@dp.message(Command("player"), AdminFilter())
async def cmd_player(message: types.Message):
    args = message.text.split()
    if len(args) < 2:
        await message.answer("❌ Укажите тег игрока.\nПример: `/player #ABC123`", parse_mode="Markdown")
        return
    await handle_player_search(message, args[1].upper())

# ============================================================
# 🧠 ЛОГИКА ОБРАБОТЧИКОВ
# ============================================================

async def safe_answer(update: Union[types.Message, types.CallbackQuery], text: str, **kwargs):
    """Универсальная функция ответа, чтобы не дублировать код"""
    if isinstance(update, types.Message):
        await update.answer(text, **kwargs)
    else:
        await update.message.answer(text, **kwargs)

async def handle_clan_info(update: Union[types.Message, types.CallbackQuery]):
    if not coc_client:
        await safe_answer(update, "❌ Клиент COC еще не подключен. Попробуйте позже.")
        return

    try:
        clan = await coc_client.get_clan(CLAN_TAG)
        description = clan.description if clan.description else "Нет описания"
        location = clan.location.name if clan.location else "International"
        
        text = (
            f"🏰 **{clan.name}** `{clan.tag}`\n"
            f"📊 Уровень: `{clan.level}`\n"
            f"👥 Участников: `{clan.member_count}/50`\n"
            f"🏆 Трофеи: `{clan.points}`\n"
            f"🛡️ Требуемые трофеи: `{clan.required_trophies}`\n"
            f"🌍 Регион: `{location}`\n\n"
            f"📝 Описание:\n_{description}_"
        )
        await safe_answer(update, text, parse_mode="Markdown", reply_markup=get_back_keyboard())
    except Exception as e:
        logger.error(f"Ошибка /clan: {e}")
        await safe_answer(update, "❌ Не удалось получить информацию о клане. Проверьте логи.")

async def handle_members_list(update: Union[types.Message, types.CallbackQuery]):
    if not coc_client:
        await safe_answer(update, "❌ Клиент COC не подключен.")
        return

    try:
        clan = await coc_client.get_clan(CLAN_TAG)
        members = getattr(clan, 'members', [])
        
        if not members:
            await safe_answer(update, "⚠️ Список участников пуст или недоступен.", reply_markup=get_back_keyboard())
            return

        # Сортировка по трофеям
        members_sorted = sorted(members, key=lambda m: getattr(m, 'trophies', 0), reverse=True)
        
        table = PrettyTable()
        table.field_names = ["Имя", "Роль", "ТХ", "Трофеи"]
        table.align["Имя"] = "l"
        
        role_map = {
            "leader": "👑 Глава", "coLeader": "🛡️ Совет", 
            "elder": "🎖️ Старейшина", "member": "👤 Участник"
        }

        # Берем топ-20, чтобы не спамить
        for m in members_sorted[:20]:
            role = role_map.get(getattr(m, 'role', 'member'), "👤 Участник")
            th = getattr(m, 'town_hall', '?')
            trophies = getattr(m, 'trophies', 0)
            table.add_row([m.name, role, th, trophies])
            
        text = f"👥 **Топ участников клана:**\n<pre><code>{table}</code></pre>"
        if len(members_sorted) > 20:
            text += f"\n_... и еще {len(members_sorted) - 20} участников_"
            
        await safe_answer(update, text, parse_mode="HTML", reply_markup=get_back_keyboard())
        
    except Exception as e:
        logger.error(f"Ошибка /members: {e}")
        await safe_answer(update, "❌ Не удалось загрузить список участников.", reply_markup=get_back_keyboard())

async def handle_war_info(update: Union[types.Message, types.CallbackQuery]):
    if not coc_client:
        await safe_answer(update, "❌ Клиент COC не подключен.")
        return

    try:
        war = await coc_client.get_current_war(CLAN_TAG)
        
        if war.state == "notInWar":
            await safe_answer(update, "🔍 Сейчас нет активной войны.", reply_markup=get_back_keyboard())
            return

        our_clan = war.clan
        enemy_clan = war.opponent
        
        text = (
            f"⚔️ **ВОЙНА: {our_clan.name} vs {enemy_clan.name}**\n\n"
            f"📊 Счёт: `{our_clan.stars}` : `{enemy_clan.stars}` (Звёзды)\n"
            f"💥 Разрушение: `{our_clan.destruction}%` : `{enemy_clan.destruction}%`\n"
            f"⏳ Статус: `{war.state}`\n\n"
        )
        
        # Таблица атак
        table = PrettyTable()
        table.field_names = ["Атакующий", "Цель", "Результат"]
        table.align["Атакующий"] = "l"
        table.align["Цель"] = "l"
        
        attacks_count = 0
        # Проверяем наличие атак
        if hasattr(our_clan, 'members'):
            for member in our_clan.members:
                if not hasattr(member, 'attacks'): continue
                for attack in member.attacks:
                    if attacks_count >= 10: break
                    try:
                        defender = war.get_opponent_member(attack.defender_tag)
                        d_name = defender.name if defender else "Неизвестно"
                        d_th = defender.town_hall if defender else "?"
                    except:
                        d_name, d_th = "Неизвестно", "?"
                        
                    stars_str = "⭐" * attack.stars
                    table.add_row([f"{member.name} (ТХ{member.town_hall})", f"{d_name} (ТХ{d_th})", f"{stars_str} {attack.destruction}%"])
                    attacks_count += 1
        
        if attacks_count > 0:
            text += f"<pre><code>{table}</code></pre>\n"
        else:
            text += "_Пока нет зафиксированных атак в логе._\n"
        
        # Кто не атаковал
        unused = []
        if hasattr(our_clan, 'members'):
            unused = [m for m in our_clan.members if hasattr(m, 'attacks') and len(m.attacks) < war.attacks_per_member]
            
        if unused:
            names = ", ".join([f"{m.name} ({len(m.attacks)}/{war.attacks_per_member})" for m in unused[:5]])
            text += f"\n⚠️ **Остались атаки у:**\n{names}"
            if len(unused) > 5: text += " ..."
        else:
            text += "\n✅ Все атаки использованы!"

        await safe_answer(update, text, parse_mode="HTML", reply_markup=get_back_keyboard())

    except coc.PrivateWarLog:
        await safe_answer(update, "🔒 Лог войны закрыт настройками клана.", reply_markup=get_back_keyboard())
    except Exception as e:
        logger.error(f"Ошибка /war: {e}")
        await safe_answer(update, "❌ Ошибка получения данных о войне. Возможно, война еще не синхронизирована.", reply_markup=get_back_keyboard())

async def handle_player_search(update: Union[types.Message, types.CallbackQuery], tag: str):
    if not coc_client:
        await safe_answer(update, "❌ Клиент COC не подключен.")
        return
    
    tag = tag.replace('#', '')
    if not tag.startswith('#'): tag = '#' + tag
        
    try:
        player = await coc_client.get_player(tag)
        clan_name = player.clan.name if player.clan else "Нет клана"
        clan_tag = player.clan.tag if player.clan else "-"
        
        text = (
            f"👤 **{player.name}** `{player.tag}`\n"
            f"🏠 ТХ: `{player.town_hall}` (Ур. {player.town_hall_level})\n"
            f"🏆 Трофеи: `{player.trophies}` (Макс: `{player.best_trophies}`)\n"
            f"💪 Уровень: `{player.exp_level}`\n"
            f"🛡️ Клан: `{clan_name}` ({clan_tag})\n"
            f"🏅 Роль: `{player.role}`"
        )
        await safe_answer(update, text, parse_mode="Markdown", reply_markup=get_back_keyboard())
    except Exception as e:
        logger.error(f"Ошибка /player: {e}")
        await safe_answer(update, f"❌ Игрок `{tag}` не найден или профиль закрыт.", reply_markup=get_back_keyboard())

async def handle_remind_attacks(update: Union[types.Message, types.CallbackQuery]):
    if not coc_client:
        await safe_answer(update, "❌ Клиент COC не подключен.")
        return
    
    try:
        war = await coc_client.get_current_war(CLAN_TAG)
        if war.state == "notInWar":
            await safe_answer(update, "🔍 Войны нет.", reply_markup=get_back_keyboard())
            return
            
        unused = []
        if hasattr(war.clan, 'members'):
            unused = [m for m in war.clan.members if hasattr(m, 'attacks') and len(m.attacks) < war.attacks_per_member]
            
        if not unused:
            await safe_answer(update, "✅ Все участники использовали свои атаки!", reply_markup=get_back_keyboard())
        else:
            msg = "⚔️ **Напоминание об атаках:**\n\n"
            for m in unused:
                left = war.attacks_per_member - len(m.attacks)
                msg += f"• {m.name} (ТХ{m.town_hall}): осталось атак `{left}`\n"
            await safe_answer(update, msg, parse_mode="Markdown", reply_markup=get_back_keyboard())
            
    except Exception as e:
        logger.error(f"Ошибка /remind: {e}")
        await safe_answer(update, "❌ Не удалось проверить атаки.", reply_markup=get_back_keyboard())

# ============================================================
# 🔄 CALLBACKS (КНОПКИ)
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

@dp.callback_query(F.data == "remind_attacks")
async def cb_remind(callback: types.CallbackQuery):
    await callback.answer()
    await handle_remind_attacks(callback)

@dp.callback_query(F.data == "player_search")
async def cb_player_input(callback: types.CallbackQuery):
    await callback.answer()
    await callback.message.answer("🔍 Введите тег игрока (например `#ABC123`) следующим сообщением:", parse_mode="Markdown")

# ============================================================
# 🚀 ЗАПУСК
# ============================================================
async def init_coc_client():
    global coc_client
    logger.info("🔄 Инициализация клиента COC...")
    tries = 5
    while tries > 0:
        try:
            coc_client = coc.Client(proxy=PROXY_URL)
            await coc_client.login(COC_EMAIL, COC_PASSWORD)
            logger.info("✅ Успешный вход в COC API!")
            return
        except Exception as e:
            logger.error(f"❌ Ошибка входа COC (осталось {tries}): {e}")
            tries -= 1
            if tries == 0: 
                coc_client = None
            else: 
                await asyncio.sleep(5)

async def on_startup(app: web.Application):
    webhook_url = os.getenv('RENDER_EXTERNAL_URL')
    if webhook_url:
        await bot.set_webhook(f"{webhook_url}/webhook")
        logger.info(f"🌐 Webhook установлен: {webhook_url}/webhook")
    else:
        logger.warning("⚠️ RENDER_EXTERNAL_URL не задан.")
    
    await init_coc_client()

async def on_shutdown(app: web.Application):
    logger.info("🛑 Остановка бота...")
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
    logger.info(f"🚀 Запуск сервера на порту {port}...")
    web.run_app(app, host='0.0.0.0', port=port)

if __name__ == '__main__':
    main()