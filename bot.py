# ============ ЗАПУСК ДЛЯ RENDER (WEBHOOK) ============
from aiohttp import web

async def handle_webhook(request):
    """Обрабатывает входящие сообщения от Telegram."""
    try:
        update = await request.json()
        await dp.feed_update(bot, update)
        return web.Response(status=200)
    except Exception as e:
        logging.error(f"Webhook error: {e}")
        return web.Response(status=500)

async def health_check(request):
    """Просто отвечает 'ok', чтобы Render видел, что бот жив."""
    return web.Response(text="ok")

async def main():
    await db_init()
    logging.info(f"Бот запущен ✅ Admin: {ADMIN_ID}")

    # Получаем публичный URL от Render
    render_url = os.getenv("RENDER_EXTERNAL_URL", "")
    if not render_url:
        # Локальный запуск (для тестов) - используем polling
        logging.info("Запуск в режиме polling (локально)")
        await dp.start_polling(bot)
        return

    # Режим вебхука (для Render)
    logging.info(f"Запуск в режиме webhook: {render_url}")
    webhook_url = f"{render_url}/webhook"

    # Устанавливаем вебхук в Telegram
    await bot.set_webhook(url=webhook_url, drop_pending_updates=True)

    # Создаём веб-сервер, который будет принимать сообщения
    app = web.Application()
    app.router.add_post("/webhook", handle_webhook)
    app.router.add_get("/", health_check)

    # Render передаёт порт через переменную окружения PORT
    port = int(os.getenv("PORT", 8080))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    logging.info(f"Веб-сервер слушает порт {port}")

    # Держим приложение запущенным
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
