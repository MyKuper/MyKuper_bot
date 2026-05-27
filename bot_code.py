import sys
import os

# Пишем в stderr, чтобы точно попасть в логи Render
print("=== БОТ ЗАПУЩЕН ===", file=sys.stderr)
print(f"Python version: {sys.version}", file=sys.stderr)
print(f"TELEGRAM_TOKEN set: {bool(os.getenv('TELEGRAM_TOKEN'))}", file=sys.stderr)

import logging
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.webhook.aiohttp_server import SimpleRequestHandler
from aiohttp import web

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN not set")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

@dp.message(Command("start"))
async def start(message: types.Message):
    await message.answer("Бот работает!")

async def on_startup(app):
    webhook_url = os.getenv('RENDER_EXTERNAL_URL')
    logger.info(f"RENDER_EXTERNAL_URL: {webhook_url}")
    if webhook_url:
        await bot.set_webhook(f"{webhook_url}/webhook")
        logger.info(f"Webhook set to {webhook_url}/webhook")
    else:
        logger.error("RENDER_EXTERNAL_URL not set!")

def main():
    logger.info("Starting web application...")
    app = web.Application()
    handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
    handler.register(app, path="/webhook")
    app.router.add_get("/health", lambda req: web.Response(text="OK"))
    app.on_startup.append(on_startup)
    port = int(os.environ.get('PORT', 8080))
    logger.info(f"Listening on port {port}")
    web.run_app(app, host='0.0.0.0', port=port)

if __name__ == '__main__':
    main()
