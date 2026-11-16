"""Точка входа: Telegram, напоминания об отметке исхода, health для Render."""
import asyncio
import logging
import signal as signal_module
import sys
import time

from aiohttp import web

from config import cfg
from openrouter import OpenRouter
from stats import summarize
from storage import Storage
from telegram_ui import TelegramUI

logging.basicConfig(
    level=getattr(logging, cfg.LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)-7s %(name)-10s %(message)s",
    stream=sys.stdout)
log = logging.getLogger("bot")

_stop = asyncio.Event()


async def telegram_loop(ui: TelegramUI):
    while not _stop.is_set():
        try:
            await ui.poll_once()
        except Exception as e:  # noqa: BLE001
            log.exception("telegram_loop: %s", e)
            await asyncio.sleep(5)


async def reminder_loop(ui: TelegramUI, store: Storage):
    """Каждые несколько секунд проверяет, не пора ли спросить про исход."""
    await asyncio.sleep(5)
    while not _stop.is_set():
        try:
            for rnd in store.due_unreminded():
                await ui.remind(rnd)
                await asyncio.sleep(0.5)
        except Exception as e:  # noqa: BLE001
            log.exception("reminder_loop: %s", e)
        try:
            await asyncio.wait_for(_stop.wait(), timeout=15)
        except asyncio.TimeoutError:
            pass


async def health_server(store: Storage, ui: TelegramUI):
    async def health(_req):
        t = store.totals()
        payout = ui.payout()
        models = {}
        for m in store.all_models():
            hits, n = store.model_tally(m)
            s = summarize(hits, n, payout, ui.stake())
            models[m] = {"hits": hits, "n": n,
                         "rate": round(s["rate"], 1),
                         "ci": [round(s["lo"], 1), round(s["hi"], 1)],
                         "verdict": s["verdict"]}
        return web.json_response({
            "status": "ok",
            "rounds": t["rounds"], "resolved": t["resolved"],
            "pending": t["pending"],
            "breakeven_winrate": round(summarize(0, 0, payout)["breakeven"], 2),
            "spent_usd": round(t["cost"], 4),
            "models": models,
        })

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", cfg.PORT).start()
    log.info("Health-сервер на порту %d", cfg.PORT)
    return runner


async def main():
    errs = cfg.validate()
    for e in errs:
        log.error("КОНФИГ: %s", e)
    if not cfg.TG_TOKEN or not cfg.TG_CHAT_ID:
        app = web.Application()
        app.router.add_get("/", lambda r: web.json_response(
            {"status": "config_error", "errors": errs}, status=503))
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", cfg.PORT).start()
        await _stop.wait()
        await runner.cleanup()
        return

    store = Storage()
    router = OpenRouter()
    ui = TelegramUI(store, router)
    runner = await health_server(store, ui)
    tasks = []
    try:
        await ui.set_commands()
        if cfg.OR_KEY:
            await ui.startup_message()
        else:
            await ui.send(
                "⚠️ <b>Не задан OPENROUTER_API_KEY</b>\n"
                "Добавь его в переменные окружения на Render, иначе модели "
                "опрашивать нечем.")
        tasks = [
            asyncio.create_task(telegram_loop(ui)),
            asyncio.create_task(reminder_loop(ui, store)),
        ]
        log.info("Бот запущен. Моделей: %s", ui.models())
        await _stop.wait()
    finally:
        log.info("Останавливаюсь...")
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await router.close()
        await ui.close()
        await runner.cleanup()


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for sig in (signal_module.SIGINT, signal_module.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop.set)
        except NotImplementedError:
            pass
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()
