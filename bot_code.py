import os
import logging
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.webhook.aiohttp_server import SimpleRequestHandler
from aiohttp import web
import coc
from prettytable import PrettyTable

# --- Настройки ---
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
COC_EMAIL = os.getenv('COC_EMAIL')
COC_PASSWORD = os.getenv('COC_PASSWORD')
CLAN_TAG = "#2CY00G2VU" # 👈 НЕ ЗАБУДЬТЕ ЗАМЕНИТЬ НА ТЕГ ВАШЕГО КЛАНА!

# Прокси для COC API (обязательно для работы на Render)
PROXY = "http://45.79.218.79:80"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

# Создаём клиент COC
coc_client = coc.login(
    COC_EMAIL,
    COC_PASSWORD,
    client=coc.EventsClient,
    proxy=PROXY
)

# ------------------------------------------------------------
# Обработчики команд (они остаются без изменений)
# ------------------------------------------------------------
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    text = (
        "👋 Привет! Я бот-помощник для твоего клана в Clash of Clans.\n\n"
        "📋 Доступные команды:\n"
        "/war – анализ текущей войны (таблица атак)\n"
        "/clan – информация о клане\n"
        "/members – список участников клана\n"
        "/player [тег] – данные об игроке (например /player #ABC123)\n"
        "/remind – напомнить о неиспользованных атаках\n"
        "/help – повтор этого сообщения"
    )
    await message.answer(text)

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await cmd_start(message)

@dp.message(Command("clan"))
async def cmd_clan(message: types.Message):
    try:
        clan = await coc_client.get_clan(CLAN_TAG)
        text = (
            f"🏰 **{clan.name}** ({clan.tag})\n"
            f"📊 Уровень: {clan.level}\n"
            f"👥 Участников: {clan.member_count}/50\n"
            f"🏆 Трофеи: {clan.points}\n"
            f"🛡️ Требуемые трофеи: {clan.required_trophies}\n"
            f"🌍 Регион: {clan.location.name if clan.location else 'Не указан'}"
        )
        await message.answer(text, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Ошибка /clan: {e}")
        await message.answer("❌ Не удалось получить информацию о клане.")

@dp.message(Command("members"))
async def cmd_members(message: types.Message):
    try:
        clan = await coc_client.get_clan(CLAN_TAG)
        members = clan.members
        members_sorted = sorted(members, key=lambda m: m.town_hall, reverse=True)
        table = PrettyTable()
        table.field_names = ["Игрок", "Роль", "ТХ", "Трофеи"]
        for m in members_sorted:
            role = "Глава" if m.role == "leader" else "Совет" if m.role == "coLeader" else "Старейшина" if m.role == "elder" else "Участник"
            table.add_row([m.name, role, m.town_hall, m.trophies])
        await message.answer(f"<pre><code>{table}</code></pre>", parse_mode="HTML")
    except Exception as e:
        logger.error(f"Ошибка /members: {e}")
        await message.answer("❌ Не удалось получить список участников.")

@dp.message(Command("war"))
async def cmd_war(message: types.Message):
    try:
        war = await coc_client.get_current_war(CLAN_TAG)
        if war.state == "notInWar":
            await message.answer("🔍 Клан не участвует в войне.")
            return
        our_clan = war.clan
        enemy_clan = war.opponent
        text = (
            f"⚔️ **Война: {our_clan.name} vs {enemy_clan.name}**\n"
            f"📊 Статус: {war.state}\n"
            f"⭐ Наши звёзды: {our_clan.stars} / {war.team_size*3}\n"
            f"🏆 Процент разрушения: {our_clan.destruction}%\n\n"
        )
        our_members = sorted(war.members, key=lambda m: m.town_hall, reverse=True)
        enemy_members = sorted(war.opponent.members, key=lambda m: m.town_hall, reverse=True)
        table = PrettyTable()
        table.field_names = ["Атакующий (ТХ)", "Противник (ТХ)", "Рекомендация"]
        for our, enemy in zip(our_members, enemy_members):
            recommendation = "⚖️ Равный"
            if our.town_hall > enemy.town_hall:
                recommendation = "✅ Легкая цель"
            elif our.town_hall < enemy.town_hall:
                recommendation = "⚠️ Тяжелая цель"
            table.add_row([f"{our.name} ({our.town_hall})", f"{enemy.name} ({enemy.town_hall})", recommendation])
        unused_attacks = [m for m in our_members if m.attacks_used < m.attacks_per_member]
        unused_text = "\n⚠️ **Остались атаки:**\n" + "\n".join([f"• {m.name} ({m.attacks_used}/{m.attacks_per_member})" for m in unused_attacks]) if unused_attacks else "\n✅ Все атаки использованы!"
        await message.answer(text + f"<pre><code>{table}</code></pre>" + unused_text, parse_mode="Markdown")
    except coc.PrivateWarLog:
        await message.answer("🔒 Лог войны клана закрыт. Невозможно получить данные.")
    except Exception as e:
        logger.error(f"Ошибка /war: {e}")
        await message.answer("❌ Не удалось получить данные о войне.")

@dp.message(Command("player"))
async def cmd_player(message: types.Message):
    args = message.text.split()
    if len(args) < 2:
        await message.answer("❌ Укажите тег игрока, например: `/player #ABC123`", parse_mode="Markdown")
        return
    player_tag = args[1].upper()
    try:
        player = await coc_client.get_player(player_tag)
        text = (
            f"👤 **{player.name}** ({player.tag})\n"
            f"🏠 Ратуша: {player.town_hall}\n"
            f"🏆 Трофеи: {player.trophies}\n"
            f"🏅 Наивысшие трофеи: {player.best_trophies}\n"
            f"💪 Опыт: {player.exp_level}\n"
            f"📅 В клане: {player.clan.name if player.clan else 'Не в клане'}"
        )
        await message.answer(text, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Ошибка /player: {e}")
        await message.answer("❌ Игрок не найден. Проверьте тег (начинается с #).")

@dp.message(Command("remind"))
async def cmd_remind(message: types.Message):
    try:
        war = await coc_client.get_current_war(CLAN_TAG)
        if war.state == "notInWar":
            await message.answer("🔍 Клан не в войне, напоминать не о чем.")
            return
        unused = [m for m in war.members if m.attacks_used < m.attacks_per_member]
        if not unused:
            await message.answer("✅ Все атаки уже использованы!")
        else:
            remind_text = "⚔️ **У кого остались атаки:**\n"
            for m in unused:
                remind_text += f"• {m.name}: {m.attacks_used}/{m.attacks_per_member}\n"
            await message.answer(remind_text, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Ошибка /remind: {e}")
        await message.answer("❌ Не удалось проверить атаки.")

# ------------------------------------------------------------
# Основная функция для запуска бота через вебхук
# ------------------------------------------------------------
async def on_startup():
    """Устанавливает вебхук при запуске приложения."""
    webhook_url = os.getenv('RENDER_EXTERNAL_URL')
    if webhook_url:
        await bot.set_webhook(f"{webhook_url}/webhook")
        logger.info(f"Webhook set to {webhook_url}/webhook")
    else:
        logger.error("RENDER_EXTERNAL_URL not set!")

def main():
    """Запускает aiohttp сервер, который слушает вебхуки от Telegram."""
    app = web.Application()
    
    # Подключаем обработчик вебхуков от aiogram
    webhook_handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
    webhook_handler.register(app, path="/webhook")
    
    # Регистрируем обработчик проверки здоровья для Render
    app.router.add_get("/health", lambda request: web.Response(text="OK"))
    
    # Устанавливаем функцию, которая выполнится при старте приложения
    app.on_startup.append(on_startup)
    
    # Запускаем веб-сервер
    port = int(os.environ.get('PORT', 8080))
    web.run_app(app, host='0.0.0.0', port=port)

if __name__ == '__main__':
    main()
