import asyncio
import os
import threading
from aiohttp import web
import bot_code

# --- Веб-сервер для health checks ---
async def health_check(request):
    """Обработчик для проверки здоровья приложения"""
    return web.Response(text='OK')

async def run_web_server():
    """Запускает веб-сервер на указанном порту"""
    app = web.Application()
    app.router.add_get('/health', health_check)
    port = int(os.environ.get('PORT', 8080))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"Веб-сервер для health checks запущен на порту {port}")
    # Держим сервер запущенным бесконечно
    await asyncio.Event().wait()

# --- Запуск ---
if __name__ == '__main__':
    # Запускаем бота в отдельном потоке
    bot_thread = threading.Thread(target=bot_code.run, daemon=True)
    bot_thread.start()
    print("Бот запущен в фоновом потоке")
    
    # Запускаем веб-сервер в основном потоке
    try:
        asyncio.run(run_web_server())
    except KeyboardInterrupt:
        print("Принудительная остановка...")
