import os
import logging
import asyncio
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
CLAN_TAG = "#2CY00G2VU"  # 👈 Укажите тег вашего клана

# Прокси для COC API (рекомендуется для Render)
PROXY = "http://45.79.218.79:80"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

# Глобальная переменная для клиента COC
coc_client = None

# ------------------------------------------------------------
# Обработчики команд (ваша логика остаётся без изменений)
# ------------------------------------------------------------
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    text = (
        "👋 Привет! Я бот-помощник для твоего клана в Clash of Clans.\n\n"
        "📋 Доступные команды:\n"
        "/war – анализ текущей войны (таблица атак)\n"
        "/clan – информация о клане\n"
        "/members – список участников клана\n"
        "/player [тег] – данные об игроке\n"
        "/remind – напомнить о неиспользованных атаках\n"
        "/help – повтор этого сообщения"
    )
    await message.answer(text)

# ... (остальные ваши обработчики /help, /clan, /members, /war, /player, /remind)
# !!! ВАЖНО: Не копируйте их отсюда, вставьте свои готовые функции из предыдущей версии кода.
# В этих функциях не нужно ничего менять, они будут использовать coc_client.

# ------------------------------------------------------------
# Инициализация клиента COC (ИСПРАВЛЕНА)
# ------------------------------------------------------------
async def on_startup(app: web.Application):
    global coc_client
    logger.info("Инициализация клиента COC...")
    try:
        # ✅ ПРАВИЛЬНЫЙ способ создать клиент (v3+)
        coc_client = coc.Client(client=coc.EventsClient, proxy=PROXY)
        # ✅ ПРАВИЛЬНЫЙ способ выполнить вход
        await coc_client.login(COC_EMAIL, COC_PASSWORD)
        logger.info("Клиент COC успешно создан и авторизован")
    except Exception as e:
        logger.error(f"Ошибка при создании клиента COC: {e}", exc_info=True)
        coc_client = None

    webhook_url = os.getenv('RENDER_EXTERNAL_URL')
    if webhook_url:
        await bot.set_webhook(f"{webhook_url}/webhook")
        logger.info(f"Webhook установлен на {webhook_url}/webhook")
    else:
        logger.error("RENDER_EXTERNAL_URL не задана!")

async def on_shutdown(app: web.Application):
    """Корректное завершение работы приложения."""
    global coc_client
    logger.info("Завершение работы приложения...")
    if coc_client:
        await coc_client.close()
    await bot.session.close()

# ------------------------------------------------------------
# Запуск приложения
# ------------------------------------------------------------
def main():
    app = web.Application()
    webhook_handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
    webhook_handler.register(app, path="/webhook")
    app.router.add_get("/health", lambda request: web.Response(text="OK"))
    
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    
    port = int(os.environ.get('PORT', 8080))
    web.run_app(app, host='0.0.0.0', port=port)

if __name__ == '__main__':
    main()
