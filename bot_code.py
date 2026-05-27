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
# ⚙️ НАСТРОЙКИ (Конфигурация)
# ============================================================

# Telegram
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')

# Clash of Clans Developer Portal
COC_EMAIL = os.getenv('COC_EMAIL')
COC_PASSWORD = os.getenv('COC_PASSWORD')

# Теги кланов (без пробелов)
CLAN_TAG = "#2CY00G2VU" 

# Прокси (если нужен, иначе оставьте None или пустую строку)
# Пример: "http://user:pass@ip:port"
PROXY_URL = os.getenv('COC_PROXY', None) 

# 🔒 БЕЗОПАСНОСТЬ: Список ID пользователей, которым разрешено управлять ботом
# Узнайте свой ID через бота @userinfobot
# Пример: ADMIN_IDS = [123456789, 987654321]
ADMIN_IDS = [] 

# ============================================================
# 🛠 ЛОГИРОВАНИЕ И УТИЛИТЫ
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

class AdminFilter(Filter):
    """Фильтр: пропускает только администраторов из списка ADMIN_IDS"""
    async def __call__(self, message: types.Message) -> bool:
        if not ADMIN_IDS:
            return True # Если список пуст, разрешаем всем (для тестов)
        return message.from_user.id in ADMIN_IDS

# Глобальные переменные
bot: Optional[Bot] = None
dp: Optional[Dispatcher] = None
coc_client: Optional[coc.Client] = None

# ============================================================
# ⌨️ КЛАВИАТУРЫ
# ============================================================

def get_main_keyboard() -> InlineKeyboardMarkup:
    kb = [
        [
            InlineKeyboardButton(text="⚔️ Война", callback_data="war_info"),
            InlineKeyboardButton(text="🏰 Клан", callback_data="clan_info"),
        ],
        [
            InlineKeyboardButton(text="👥 Участники", callback_data="members_list"),
            InlineKeyboardButton(text="🔍 Игрок", callback_data="player_search"),
        ],
        [
            InlineKeyboardButton(text="⏰ Напомнить об атаках", callback_data="remind_attacks"),
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)

def get_back_keyboard() -> InlineKeyboardMarkup:
    kb = [[InlineKeyboardButton(text="🔙 Назад", callback_data="back_menu")]]
    return InlineKeyboardMarkup(inline_keyboard=kb)

# ============================================================
# 🤖 ОБРАБОТЧИКИ КОМАНД
# ============================================================

@dp.message(Command("start"), AdminFilter())
async def cmd_start(message: types.Message):
    text = (
        f"👋 Привет, {message.from_user.first_name}!\n\n"
        "Я бот-помощник для управления кланом в Clash of Clans.\n"
        "Выберите действие в меню ниже:"
    )
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
# 🧠 ЛОГИКА (HANDLERS)
# ============================================================

async def get_coc_client():
    if coc_client is None:
        logger.warning("Попытка доступа к COC до инициализации")
    return coc_client

async def handle_clan_info(update: types.Message | types.CallbackQuery):
    client = await get_coc_client()
    if not client:
        txt = "❌ Клиент COC не подключен."
        if isinstance(update, types.Message):
            await update.answer(txt)
        else:
            await update.message.answer(txt)
        return

    try:
        clan = await client.get_clan(CLAN_TAG)
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
        msg = "❌ Ошибка получения данных о клане."
        if isinstance(update, types.Message): await update.answer(msg)
        else: await update.message.answer(msg)

async def handle_members_list(update: types.Message | types.CallbackQuery):
    client = await get_coc_client()
    if not client: return
    
    try:
        clan = await client.get_clan(CLAN_TAG)
        members = sorted(clan.members, key=lambda m: m.trophies, reverse=True)
        
        table = PrettyTable()
        table.field_names = ["Имя", "Роль", "ТХ", "Трофеи"]
        table.align["Имя"] = "l"
        
        for m in members[:20]: # Показываем топ-20
            role_map = {"leader": "👑 Глава", "coLeader": "🛡️ Совет", "elder": "🎖️ Старейшина", "member": "👤 Участник"}
            role = role_map.get(m.role, m.role)
            table.add_row([m.name, role, m.town_hall, m.trophies])
            
        text = f"👥 **Топ участников клана:**\n<pre><code>{table}</code></pre>"
        if len(members) > 20:
            text += f"\n_... и еще {len(members) - 20} участников_"
            
        if isinstance(update, types.Message):
            await update.answer(text, parse_mode="HTML", reply_markup=get_back_keyboard())
        else:
            await update.message.answer(text, parse_mode="HTML", reply_markup=get_back_keyboard())
            
    except Exception as e:
        logger.error(f"Error members list: {e}")
        msg = "❌ Ошибка списка участников."
        if isinstance(update, types.Message): await update.answer(msg)
        else: await update.message.answer(msg)

async def handle_war_info(update: types.Message | types.CallbackQuery):
    client = await get_coc_client()
    if not client: return

    try:
        war = await client.get_current_war(CLAN_TAG)
        
        if war.state == "notInWar":
            msg = "🔍 Сейчас нет активной войны."
            if isinstance(update, types.Message): await update.answer(msg, reply_markup=get_back_keyboard())
            else: await update.message.answer(msg, reply_markup=get_back_keyboard())
            return

        our_clan = war.clan
        enemy_clan = war.opponent
        
        # Статистика
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
        
        attacks_displayed = 0
        for member in war.clan.members:
            for attack in member.attacks:
                if attacks_displayed >= 10: break # Лимит вывода
                defender = war.get_opponent_member(attack.defender_tag)
                stars_str = "⭐" * attack.stars
                dest_str = f"{attack.destruction}%"
                table.add_row([f"{member.name} (ТХ{member.town_hall})", f"{defender.name} (ТХ{defender.town_hall})", f"{stars_str} {dest_str}"])
                attacks_displayed += 1
        
        if attacks_displayed > 0:
            text += f"<pre><code>{table}</code></pre>\n"
        
        # Кто не атаковал
        unused = [m for m in war.clan.members if len(m.attacks) < war.attacks_per_member]
        if unused:
            names = ", ".join([f"{m.name} ({len(m.attacks)}/{war.attacks_per_member})" for m in unused[:5]])
            text += f"\n⚠️ **Не использовали атаки:**\n{names}"
            if len(unused) > 5: text += " ..."
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
        logger.error(f"Error war info: {e}")
        msg = "❌ Ошибка данных войны."
        if isinstance(update, types.Message): await update.answer(msg)
        else: await update.message.answer(msg)

async def handle_player_search(update: types.Message | types.CallbackQuery, tag: str):
    client = await get_coc_client()
    if not client: return
    
    # Очистка тега
    tag = tag.replace('#', '')
    if not tag.startswith('#'):
        tag = '#' + tag
        
    try:
        player = await client.get_player(tag)
        text = (
            f"👤 **{player.name}** `{player.tag}`\n"
            f"🏠 ТХ: `{player.town_hall}` (Ур. {player.town_hall_level})\n"
            f"🏆 Трофеи: `{player.trophies}` (Макс: `{player.best_trophies}`)\n"
            f"💪 Уровень: `{player.exp_level}`\n"
            f"🛡️ Клан: `{player.clan.name}` ({player.clan.tag})\n"
            f"🏅 Роль в клане: `{player.role}`"
        )
        
        if isinstance(update, types.Message):
            await update.answer(text, parse_mode="Markdown", reply_markup=get_back_keyboard())
        else:
            await update.message.answer(text, parse_mode="Markdown", reply_markup=get_back_keyboard())
            
    except Exception as e:
        logger.error(f"Error player search: {e}")
        msg = f"❌ Игрок `{tag}` не найден или профиль закрыт."
        if isinstance(update, types.Message): await update.answer(msg)
        else: await update.message.answer(msg)

async def handle_remind_attacks(update: types.Message | types.CallbackQuery):
    client = await get_coc_client()
    if not client: return
    
    try:
        war = await client.get_current_war(CLAN_TAG)
        if war.state == "notInWar":
            msg = "🔍 Войны нет."
            if isinstance(update, types.Message): await update.answer(msg)
            else: await update.message.answer(msg)
            return
            
        unused = [m for m in war.clan.members if len(m.attacks) < war.attacks_per_member]
        if not unused:
            msg = "✅ Все участники использовали свои атаки!"
        else:
            msg = "⚔️ **Напоминание об атаках:**\n\n"
            for m in unused:
                left = war.attacks_per_member - len(m.attacks)
                msg += f"• {m.name} (ТХ{m.town_hall}): осталось атак `{left}`\n"
        
        if isinstance(update, types.Message):
            await update.answer(msg, parse_mode="Markdown", reply_markup=get_back_keyboard())
        else:
            await update.message.answer(msg, parse_mode="Markdown", reply_markup=get_back_keyboard())
            
    except Exception as e:
        logger.error(f"Error remind: {e}")
        msg = "❌ Ошибка проверки атак."
        if isinstance(update, types.Message): await update.answer(msg)
        else: await update.message.answer(msg)

# ============================================================
# 🔄 CALLBACK QUERY (КНОПКИ)
# ============================================================

@dp.callback_query(F.data == "back_menu")
async def cb_back(callback: types.CallbackQuery):
    await callback.message.delete_reply_markup() # Убираем кнопки "Назад"
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
    await callback.message.answer("🔍 Введите тег игрока (например `#ABC123`) следующим сообщением:", parse_mode="Markdown")
    # Здесь можно добавить машину состояний (FSM) для ожидания ввода, 
    # но для простоты пока используем команду /player

# ============================================================
# 🚀 ЗАПУСК СЕРВЕРА
# ============================================================

async def init_coc_client():
    global coc_client
    logger.info("🔄 Инициализация клиента COC...")
    
    tries = 5
    while tries > 0:
        try:
            # Настройка прокси если есть
            proxy = PROXY_URL if PROXY_URL else None
            
            coc_client = coc.Client(proxy=proxy)
            await coc_client.login(COC_EMAIL, COC_PASSWORD)
            logger.info("✅ Успешный вход в COC API!")
            return
        except Exception as e:
            logger.error(f"❌ Ошибка входа COC (попыток осталось {tries}): {e}")
            tries -= 1
            if tries == 0:
                logger.critical("Не удалось подключиться к COC API после нескольких попыток.")
                coc_client = None
            else:
                await asyncio.sleep(5)

async def on_startup(app: web.Application):
    global bot
    bot = Bot(token=TELEGRAM_TOKEN)
    
    # Инициализация COC
    await init_coc_client()
    
    # Настройка вебхука
    webhook_url = os.getenv('RENDER_EXTERNAL_URL')
    if webhook_url:
        await bot.set_webhook(f"{webhook_url}/webhook")
        logger.info(f"🌐 Webhook установлен: {webhook_url}/webhook")
    else:
        logger.warning("⚠️ RENDER_EXTERNAL_URL не задан. Бот не получит сообщения (если не polling).")

async def on_shutdown(app: web.Application):
    global coc_client, bot
    logger.info("🛑 Остановка бота...")
    if coc_client:
        await coc_client.close()
    if bot:
        await bot.session.close()

def main():
    global dp, bot
    dp = Dispatcher()
    
    app = web.Application()
    
    # Регистрация хендлеров
    handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
    handler.register(app, path="/webhook")
    
    # Health check
    async def health_check(request):
        return web.Response(text="OK")
    app.router.add_get("/health", health_check)
    
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    
    port = int(os.environ.get('PORT', 8080))
    logger.info(f"🚀 Запуск сервера на порту {port}...")
    web.run_app(app, host='0.0.0.0', port=port)

if __name__ == '__main__':
    main()