import asyncio
import os
import logging
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
import coc

# --- Настройка логирования (чтобы видеть, что происходит) ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Получение секретов из переменных окружения ---
# Это те самые данные, которые вы добавили в Render
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
COC_EMAIL = os.getenv('COC_EMAIL')
COC_PASSWORD = os.getenv('COC_PASSWORD')

# --- Инициализация клиентов ---
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()
# Инициализируем клиент COC
coc_client = coc.login(
    COC_EMAIL,
    COC_PASSWORD,
    client=coc.EventsClient
)

# --- Обработчики команд ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    """Приветственное сообщение для проверки, что бот работает"""
    await message.answer("Привет! Я бот для Clash of Clans. Я жив и готов к работе!")

# Этот обработчик нужен, чтобы бот не падал, если его пинговать без команды
@dp.message()
async def echo(message: types.Message):
    await message.answer("Я вас не понимаю. Напишите /start")

# --- Основная функция запуска бота ---
async def start_bot():
    """Запускает polling бота"""
    logger.info("Telegram-бот запускается...")
    # Удаляем старые обновления, чтобы бот не отвечал на старые сообщения
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

def run():
    """Точка входа для запуска из app.py"""
    asyncio.run(start_bot())
