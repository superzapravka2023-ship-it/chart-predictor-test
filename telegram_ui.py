"""Telegram: приём скриншота, прогнозы моделей, отметка исхода, статистика."""
import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Optional

import aiohttp

from config import cfg
from openrouter import OpenRouter, Prediction
from stats import summarize, required_sample
from storage import Storage

log = logging.getLogger("telegram")

API = "https://api.telegram.org/bot{token}/{method}"
SEP = "━━━━━━━━━━━━━━━━━━"


def local_time(ms: int = None) -> str:
    ts = (ms / 1000.0 if ms else time.time()) + cfg.TZ_OFFSET * 3600
    return time.strftime("%H:%M:%S", time.gmtime(ts))


def short_model(model: str) -> str:
    return model.split("/")[-1][:28]


class TelegramUI:
    def __init__(self, store: Storage, router: OpenRouter):
        self.db = store
        self.router = router
        self.token = cfg.TG_TOKEN
        self.chat_id = cfg.TG_CHAT_ID
        self._session: Optional[aiohttp.ClientSession] = None
        self._offset = 0
        self.busy = False
        self.started_ms = int(time.time() * 1000)

    async def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=70))
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    # ------------------------------------------------------------ низкий уровень
    async def _call(self, method: str, payload: dict,
                    timeout: int = 30) -> Optional[dict]:
        url = API.format(token=self.token, method=method)
        try:
            sess = await self.session()
            async with sess.post(url, json=payload,
                                 timeout=aiohttp.ClientTimeout(
                                     total=timeout)) as r:
                data = await r.json(content_type=None)
            if not data.get("ok"):
                desc = str(data.get("description", ""))
                if "message is not modified" not in desc:
                    log.warning("Telegram %s: %s", method, desc)
                return None
            return data.get("result")
        except (aiohttp.ClientError, asyncio.TimeoutError,
                json.JSONDecodeError) as e:
            log.warning("Telegram %s: %s", method, e)
            return None

    async def send(self, text: str, keyboard: Optional[list] = None,
                   silent: bool = False) -> Optional[int]:
        payload = {"chat_id": self.chat_id, "text": text,
                   "parse_mode": "HTML", "disable_web_page_preview": True,
                   "disable_notification": silent}
        if keyboard:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        res = await self._call("sendMessage", payload)
        return res.get("message_id") if res else None

    async def edit(self, message_id: int, text: str,
                   keyboard: Optional[list] = None):
        payload = {"chat_id": self.chat_id, "message_id": message_id,
                   "text": text, "parse_mode": "HTML",
                   "disable_web_page_preview": True}
        if keyboard:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        await self._call("editMessageText", payload)

    async def answer(self, cb_id: str, text: str = "", alert: bool = False):
        await self._call("answerCallbackQuery",
                         {"callback_query_id": cb_id, "text": text[:190],
                          "show_alert": alert})

    async def download_photo(self, file_id: str) -> Optional[bytes]:
        res = await self._call("getFile", {"file_id": file_id})
        if not res or "file_path" not in res:
            return None
        url = (f"https://api.telegram.org/file/bot{self.token}/"
               f"{res['file_path']}")
        try:
            sess = await self.session()
            async with sess.get(url) as r:
                if r.status != 200:
                    return None
                return await r.read()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.warning("Скачивание фото: %s", e)
            return None

    async def set_commands(self):
        await self._call("setMyCommands", {"commands": [
            {"command": "menu", "description": "🏠 Меню"},
            {"command": "stats", "description": "📊 Статистика попаданий"},
            {"command": "pending", "description": "⏳ Незакрытые прогнозы"},
            {"command": "models", "description": "🧠 Модели"},
            {"command": "settings", "description": "⚙️ Настройки"},
        ]})

    # ------------------------------------------------------- рантайм-настройки
    def models(self) -> List[str]:
        return self.db.get_list("models", cfg.MODELS)

    def payout(self) -> float:
        return self.db.get_float("payout_pct", cfg.PAYOUT_PCT)

    def horizon(self) -> int:
        return int(self.db.get_float("horizon_min", cfg.HORIZON_MIN))

    def stake(self) -> float:
        return self.db.get_float("stake", 10.0)

    # ------------------------------------------------------------ клавиатуры
    def kb_main(self) -> list:
        return [
            [{"text": "📊 Статистика", "callback_data": "scr:stats"},
             {"text": "⏳ Незакрытые", "callback_data": "scr:pending"}],
            [{"text": "🧠 Модели", "callback_data": "scr:models"},
             {"text": "📜 История", "callback_data": "scr:history"}],
            [{"text": "⚙️ Настройки", "callback_data": "scr:settings"},
             {"text": "❓ Как это работает", "callback_data": "scr:help"}],
        ]

    def kb_back(self, refresh: str = "") -> list:
        row = [{"text": "🏠 Меню", "callback_data": "scr:menu"}]
        if refresh:
            row.insert(0, {"text": "🔄 Обновить", "callback_data": refresh})
        return [row]

    def kb_outcome(self, round_id: int) -> list:
        return [
            [{"text": "⬆️ Выше", "callback_data": f"out:{round_id}:UP"},
             {"text": "⬇️ Ниже", "callback_data": f"out:{round_id}:DOWN"}],
            [{"text": "➖ Без изменений",
              "callback_data": f"out:{round_id}:FLAT"}],
        ]

    def kb_settings(self) -> list:
        return [
            [{"text": "➖", "callback_data": "set:payout:-"},
             {"text": "💰 Выплата брокера", "callback_data": "noop"},
             {"text": "➕", "callback_data": "set:payout:+"}],
            [{"text": "➖", "callback_data": "set:horizon:-"},
             {"text": "⏱ Горизонт, мин", "callback_data": "noop"},
             {"text": "➕", "callback_data": "set:horizon:+"}],
            [{"text": "➖", "callback_data": "set:stake:-"},
             {"text": "🎲 Ставка для расчёта", "callback_data": "noop"},
             {"text": "➕", "callback_data": "set:stake:+"}],
            [{"text": "🏠 Меню", "callback_data": "scr:menu"}],
        ]

    # -------------------------------------------------------- основной поток
    async def handle_photo(self, file_id: str, caption: str):
        if self.busy:
            await self.send("⏳ Ещё обрабатываю предыдущий скриншот, подожди.")
            return
        pending = self.db.pending()
        if len(pending) >= cfg.MAX_PENDING:
            await self.send(
                f"⚠️ У тебя <b>{len(pending)}</b> неотмеченных прогнозов.\n\n"
                f"Отметь их исход, иначе статистика будет кривой: в неё "
                f"попадут только те случаи, которые ты запомнил отметить, "
                f"а это обычно удачные.",
                [[{"text": "⏳ Показать незакрытые",
                   "callback_data": "scr:pending"}]])
            return

        self.busy = True
        try:
            models = self.models()
            horizon = self.horizon()
            pair = (caption or "").strip()[:40]
            msg_id = await self.send(
                f"🔍 <b>Опрашиваю {len(models)} "
                f"{'модель' if len(models) == 1 else 'модели'}...</b>\n"
                f"<i>{', '.join(short_model(m) for m in models)}</i>")

            image = await self.download_photo(file_id)
            if not image:
                await self.send("❌ Не удалось скачать картинку, попробуй ещё раз.")
                return

            preds = await self.router.ask_all(models, image, horizon)
            rid = self.db.new_round(pair, horizon)
            for p in preds:
                self.db.add_call(rid, p)

            text = self.render_round(rid, preds, pair, horizon)
            if msg_id:
                await self.edit(msg_id, text)
            else:
                msg_id = await self.send(text)
            if msg_id:
                self.db.set_message_id(rid, msg_id)
        finally:
            self.busy = False

    def render_round(self, rid: int, preds: List[Prediction], pair: str,
                     horizon: int) -> str:
        ok = [p for p in preds if p.ok]
        lines = []
        for p in preds:
            if p.ok:
                lines.append(
                    f"<b>{short_model(p.model)}</b>\n"
                    f"   {p.arrow()} · уверенность {p.confidence:.0f}%\n"
                    f"   <i>{p.reason[:120]}</i>")
            else:
                lines.append(f"<b>{short_model(p.model)}</b>\n"
                             f"   ⚠️ <code>{p.error[:140]}</code>")

        consensus = ""
        if len(ok) >= 2:
            dirs = {p.direction for p in ok}
            if len(dirs) == 1:
                consensus = (f"\n{SEP}\n🤝 <b>Все модели сошлись: "
                             f"{ok[0].arrow()}</b>")
            else:
                consensus = (f"\n{SEP}\n🤷 <b>Модели не сошлись</b> — "
                             f"уже само по себе показательно")

        head = f"📈 <b>ПРОГНОЗ #{rid}</b>"
        if pair:
            head += f" · {pair}"
        return (
            f"{head}\n{SEP}\n"
            f"Время: {local_time()} · горизонт {horizon} мин\n{SEP}\n"
            + "\n\n".join(lines) + consensus + f"\n{SEP}\n"
            f"⏰ Через {horizon} мин я спрошу, куда ушла цена.\n"
            f"<i>Ставок бот не делает. Это проверка гипотезы.</i>")

    async def remind(self, rnd: dict):
        rid = int(rnd["id"])
        calls = self.db.calls(rid)
        made = [c for c in calls if c["direction"]]
        preview = " · ".join(
            f"{short_model(c['model'])}: "
            f"{'⬆️' if c['direction'] == 'UP' else '⬇️'}"
            for c in made) or "прогнозов не было"
        pair = rnd.get("pair") or ""
        await self.send(
            f"⏰ <b>Прошло {rnd['horizon_min']} мин · прогноз #{rid}"
            f"{' · ' + pair if pair else ''}</b>\n{SEP}\n"
            f"Прогнозы были: {preview}\n\n"
            f"<b>Цена сейчас выше или ниже, чем в "
            f"{local_time(int(rnd['created_ms']))}?</b>",
            self.kb_outcome(rid))
        self.db.mark_reminded(rid)

    async def resolve_round(self, cb_id: str, rid: int, outcome: str):
        rnd = self.db.round(rid)
        if not rnd:
            await self.answer(cb_id, "Прогноз не найден")
            return None
        if rnd.get("outcome"):
            await self.answer(cb_id, "Этот прогноз уже отмечен", True)
            return None
        self.db.resolve(rid, outcome)

        calls = [c for c in self.db.calls(rid) if c["direction"]]
        if outcome == "FLAT":
            body = ("➖ Отмечено «без изменений». Такие раунды в статистику "
                    "попаданий не идут.")
        else:
            marks = []
            for c in calls:
                hit = c["direction"] == outcome
                marks.append(
                    f"{'✅' if hit else '❌'} <b>{short_model(c['model'])}</b> "
                    f"— говорил {'вверх' if c['direction'] == 'UP' else 'вниз'}")
            body = "\n".join(marks) if marks else "прогнозов не было"

        payout = self.payout()
        total_line = ""
        models = self.db.all_models()
        if models:
            best = None
            for m in models:
                hits, n = self.db.model_tally(m)
                if n >= 1:
                    s = summarize(hits, n, payout, self.stake())
                    if best is None or s["rate"] > best[1]["rate"]:
                        best = (m, s)
            if best:
                s = best[1]
                total_line = (
                    f"\n{SEP}\n📊 Лучшая пока: <b>{short_model(best[0])}</b> — "
                    f"{s['hits']}/{s['n']} ({s['rate']:.0f}%), "
                    f"порог безубытка {s['breakeven']:.1f}%")

        await self.answer(cb_id, "Записано")
        arrow = {"UP": "⬆️ выше", "DOWN": "⬇️ ниже",
                 "FLAT": "➖ без изменений"}[outcome]
        return (f"✍️ <b>ИТОГ прогноза #{rid}: {arrow}</b>\n{SEP}\n"
                f"{body}{total_line}")

    # -------------------------------------------------------------- экраны
    async def screen(self, name: str) -> tuple:
        if name == "stats":
            return self.scr_stats(), self.kb_back("scr:stats")
        if name == "pending":
            return self.scr_pending(), self.kb_back("scr:pending")
        if name == "models":
            return await self.scr_models(), [
                [{"text": "🔄 Обновить список",
                  "callback_data": "scr:models"}],
                [{"text": "🏠 Меню", "callback_data": "scr:menu"}]]
        if name == "history":
            return self.scr_history(), self.kb_back("scr:history")
        if name == "settings":
            return self.scr_settings(), self.kb_settings()
        if name == "help":
            return self.scr_help(), self.kb_back()
        return self.scr_menu(), self.kb_main()

    def scr_menu(self) -> str:
        t = self.db.totals()
        payout = self.payout()
        be = summarize(0, 0, payout)["breakeven"]
        return (
            f"🧪 <b>ПРОВЕРКА ПРОГНОЗОВ ПО ГРАФИКУ</b>\n{SEP}\n"
            f"Пришли скриншот {self.horizon()}-минутного графика — "
            f"модели скажут, куда пойдёт цена. Бот запишет прогноз и через "
            f"{self.horizon()} мин спросит, что вышло на самом деле.\n\n"
            f"В подписи к картинке можно указать пару, например "
            f"<code>EURUSD</code>.\n{SEP}\n"
            f"📈 Прогнозов: <b>{t['rounds']}</b> · "
            f"отмечено <b>{t['resolved']}</b> · ждут <b>{t['pending']}</b>\n"
            f"💰 Порог безубытка при выплате {payout:.0f}%: "
            f"<b>{be:.1f}%</b>\n"
            f"💵 Потрачено на запросы: <b>${t['cost']:.3f}</b>\n{SEP}\n"
            f"<i>Бот не делает ставок и не советует их делать. "
            f"Он измеряет, работает ли идея вообще.</i>")

    def scr_stats(self) -> str:
        payout = self.payout()
        stake = self.stake()
        models = self.db.all_models()
        t = self.db.totals()
        if not models or t["resolved"] == 0:
            need = required_sample(max(summarize(0, 0, payout)["breakeven"]
                                       - 50.0, 2.0))
            return (f"📊 <b>СТАТИСТИКА</b>\n{SEP}\n"
                    f"Отмеченных прогнозов пока нет.\n\n"
                    f"Чтобы доказать или опровергнуть перевес при выплате "
                    f"{payout:.0f}%, понадобится порядка <b>{need}</b> "
                    f"прогнозов.")

        parts = [f"📊 <b>СТАТИСТИКА ПОПАДАНИЙ</b>\n{SEP}",
                 f"Выплата {payout:.0f}% → порог безубытка "
                 f"<b>{summarize(0, 0, payout)['breakeven']:.1f}%</b>\n"]
        for m in models:
            hits, n = self.db.model_tally(m)
            s = summarize(hits, n, payout, stake)
            if n == 0:
                parts.append(f"<b>{short_model(m)}</b>\n   нет отмеченных")
                continue
            icon = "✅" if s["beats_breakeven"] else (
                "🟡" if s["beats_chance"] else "⚪️")
            parts.append(
                f"{icon} <b>{short_model(m)}</b>\n"
                f"   <b>{hits}/{n}</b> = <b>{s['rate']:.1f}%</b>  "
                f"(95% интервал {s['lo']:.0f}–{s['hi']:.0f}%)\n"
                f"   вероятность случайности: {s['p_value']:.2f}\n"
                f"   ожидание на ставку ${stake:.0f}: "
                f"<b>${s['ev']:+.2f}</b>\n"
                f"   <i>{s['verdict']}</i>")

        ch, ct = self.db.consensus_tally()
        if ct:
            cs = summarize(ch, ct, payout, stake)
            parts.append(
                f"{SEP}\n🤝 <b>Когда модели сошлись</b>\n"
                f"   {ch}/{ct} = {cs['rate']:.1f}% "
                f"(интервал {cs['lo']:.0f}–{cs['hi']:.0f}%)\n"
                f"   <i>{cs['verdict']}</i>")

        if t["pending"]:
            parts.append(
                f"{SEP}\n⚠️ Неотмеченных прогнозов: <b>{t['pending']}</b>. "
                f"Пока они висят, статистика смещена.")
        if t["flat"]:
            parts.append(f"➖ Раундов «без изменений»: {t['flat']} "
                         f"(в расчёт не входят)")

        first = models[0]
        split = self.db.confidence_split(first)
        conf_lines = []
        for label, (h, n) in split.items():
            if n:
                conf_lines.append(f"   {label}: {h}/{n} = {h/n*100:.0f}%")
        if conf_lines:
            parts.append(f"{SEP}\n🎯 <b>{short_model(first)} по уверенности</b>\n"
                         + "\n".join(conf_lines) +
                         "\n   <i>если «уверенные» не лучше «неуверенных», "
                         "значит уверенность модели ничего не значит</i>")
        return "\n".join(parts)

    def scr_pending(self) -> str:
        pend = self.db.pending()
        if not pend:
            return (f"⏳ <b>НЕЗАКРЫТЫЕ</b>\n{SEP}\n"
                    f"Все прогнозы отмечены. Так и держи — это главное "
                    f"условие честной статистики.")
        parts = [f"⏳ <b>НЕЗАКРЫТЫЕ ПРОГНОЗЫ: {len(pend)}</b>\n{SEP}"]
        for r in pend[:10]:
            age = (time.time() * 1000 - int(r["created_ms"])) / 60000.0
            calls = [c for c in self.db.calls(int(r["id"])) if c["direction"]]
            dirs = " ".join("⬆️" if c["direction"] == "UP" else "⬇️"
                            for c in calls)
            parts.append(
                f"<b>#{r['id']}</b> {r.get('pair') or ''} · "
                f"{local_time(int(r['created_ms']))} "
                f"({age:.0f} мин назад) {dirs}")
        parts.append(f"{SEP}\n<i>Отметь исход кнопками в сообщении-напоминании. "
                     f"Если напоминание потерялось — жми ниже.</i>")
        return "\n".join(parts)

    async def scr_models(self) -> str:
        current = self.models()
        parts = [f"🧠 <b>МОДЕЛИ</b>\n{SEP}", "<b>Сейчас опрашиваются:</b>"]
        for m in current:
            hits, n = self.db.model_tally(m)
            tail = f" — {hits}/{n}" if n else ""
            parts.append(f"• <code>{m}</code>{tail}")

        available = await self.router.vision_models()
        if available:
            parts.append(f"{SEP}\n<b>Доступные vision-модели "
                         f"({len(available)}), самые дешёвые сверху:</b>")
            for m in available[:15]:
                parts.append(f"• <code>{m['id']}</code>")
            parts.append(
                f"\n<i>Чтобы сменить набор, пришли сообщение вида:</i>\n"
                f"<code>/setmodels openai/gpt-4o, google/gemini-flash-1.5"
                f"</code>")
        else:
            parts.append(f"{SEP}\n<i>Список моделей получить не удалось — "
                         f"проверь ключ OpenRouter. Актуальные ID всегда "
                         f"есть на openrouter.ai/models</i>")
        return "\n".join(parts)

    def scr_history(self) -> str:
        rows = self.db.recent_rounds(10)
        if not rows:
            return f"📜 <b>ИСТОРИЯ</b>\n{SEP}\nПока пусто."
        parts = [f"📜 <b>ПОСЛЕДНИЕ ПРОГНОЗЫ</b>\n{SEP}"]
        for r in rows:
            calls = [c for c in self.db.calls(int(r["id"])) if c["direction"]]
            out = r.get("outcome")
            if not out:
                status = "⏳ ждёт"
            elif out == "FLAT":
                status = "➖ без изменений"
            else:
                hits = sum(1 for c in calls if c["direction"] == out)
                status = (f"{'⬆️' if out == 'UP' else '⬇️'} "
                          f"{hits}/{len(calls)} угадали")
            parts.append(
                f"<b>#{r['id']}</b> {r.get('pair') or ''} "
                f"{local_time(int(r['created_ms']))} — {status}")
        return "\n".join(parts)

    def scr_settings(self) -> str:
        payout = self.payout()
        be = summarize(0, 0, payout)["breakeven"]
        need = required_sample(max(be - 50.0, 2.0))
        return (
            f"⚙️ <b>НАСТРОЙКИ</b>\n{SEP}\n"
            f"💰 Выплата брокера: <b>{payout:.0f}%</b>\n"
            f"   → порог безубытка <b>{be:.1f}%</b>\n"
            f"⏱ Горизонт прогноза: <b>{self.horizon()} мин</b>\n"
            f"🎲 Ставка для расчёта ожидания: <b>${self.stake():.0f}</b>\n"
            f"🧠 Моделей в опросе: <b>{len(self.models())}</b>\n{SEP}\n"
            f"Чтобы доказать перевес при такой выплате, нужно около "
            f"<b>{need}</b> отмеченных прогнозов.\n{SEP}\n"
            f"<i>Выплату поставь ту, которую реально даёт твой брокер на "
            f"этой паре — от неё считается порог безубытка.</i>")

    def scr_help(self) -> str:
        payout = self.payout()
        be = summarize(0, 0, payout)["breakeven"]
        need = required_sample(max(be - 50.0, 2.0))
        return (
            f"❓ <b>КАК ЭТО РАБОТАЕТ</b>\n{SEP}\n"
            f"<b>1.</b> Присылаешь скриншот {self.horizon()}-минутного "
            f"графика. В подписи можно указать пару.\n\n"
            f"<b>2.</b> Бот отправляет картинку всем выбранным моделям "
            f"и записывает их ответы.\n\n"
            f"<b>3.</b> Через {self.horizon()} мин спрашивает, куда "
            f"ушла цена. Отмечаешь кнопкой.\n\n"
            f"<b>4.</b> Копится статистика по каждой модели отдельно.\n"
            f"{SEP}\n"
            f"<b>Что именно мы проверяем</b>\n"
            f"При выплате {payout:.0f}% выигрыш даёт {payout:.0f}% от ставки, "
            f"а проигрыш забирает все 100%. Поэтому угадывать надо не "
            f"в половине случаев, а в <b>{be:.1f}%</b> — это и есть "
            f"<b>порог безубытка</b>. Ниже него счёт тает даже при "
            f"идеальной дисциплине.\n\n"
            f"Вопрос эксперимента: способна ли модель стабильно "
            f"перешагнуть порог безубытка.\n{SEP}\n"
            f"<b>Почему нужно много прогнозов</b>\n"
            f"6 попаданий из 10 — это 60%, и выглядит прекрасно. Но такое "
            f"бывает при подбрасывании монеты примерно в каждой третьей "
            f"серии. Чтобы отличить настоящий перевес от везения, нужно "
            f"около <b>{need}</b> прогнозов.\n\n"
            f"Поэтому бот показывает не только процент, но и доверительный "
            f"интервал. Вывод можно делать, только когда <b>весь</b> "
            f"интервал окажется выше {be:.1f}%.\n{SEP}\n"
            f"<b>Главное правило</b>\n"
            f"Отмечай каждый прогноз, в том числе неудачный. Если отмечать "
            f"выборочно, статистика покажет то, что ты хочешь увидеть, "
            f"а не то, что есть.")

    # ------------------------------------------------------------- обработка
    async def poll_once(self):
        url = API.format(token=self.token, method="getUpdates")
        try:
            sess = await self.session()
            async with sess.post(url, json={
                "offset": self._offset, "timeout": 25,
                "allowed_updates": ["message", "callback_query"]},
                    timeout=aiohttp.ClientTimeout(total=45)) as r:
                data = await r.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError,
                json.JSONDecodeError):
            await asyncio.sleep(2)
            return
        if not data.get("ok"):
            await asyncio.sleep(3)
            return
        for upd in data.get("result", []):
            self._offset = max(self._offset, int(upd["update_id"]) + 1)
            try:
                if "callback_query" in upd:
                    await self._on_callback(upd["callback_query"])
                elif "message" in upd:
                    await self._on_message(upd["message"])
            except Exception as e:  # noqa: BLE001
                log.exception("Апдейт: %s", e)

    def _auth(self, chat_id: Any) -> bool:
        return str(chat_id) == str(self.chat_id)

    async def _on_message(self, msg: dict):
        if not self._auth(msg.get("chat", {}).get("id")):
            return
        if "photo" in msg:
            photos = msg["photo"]
            best = max(photos, key=lambda p: p.get("file_size", 0))
            await self.handle_photo(best["file_id"], msg.get("caption", ""))
            return
        if "document" in msg:
            doc = msg["document"]
            if str(doc.get("mime_type", "")).startswith("image/"):
                await self.handle_photo(doc["file_id"],
                                        msg.get("caption", ""))
                return

        text = (msg.get("text") or "").strip()
        low = text.lower().split("@")[0]
        cmd = low.split()[0] if low else ""

        if cmd == "/setmodels":
            rest = text[len("/setmodels"):].strip()
            models = [x.strip() for x in rest.replace(";", ",").split(",")
                      if x.strip()]
            if not models:
                await self.send("Укажи модели через запятую, например:\n"
                                "<code>/setmodels openai/gpt-4o, "
                                "google/gemini-flash-1.5</code>")
                return
            self.db.set_setting("models", ",".join(models))
            await self.send(
                f"✅ Теперь опрашиваю:\n" +
                "\n".join(f"• <code>{m}</code>" for m in models),
                self.kb_main())
            return
        if cmd == "/stats":
            await self.send(self.scr_stats(), self.kb_back("scr:stats"))
        elif cmd == "/pending":
            await self.send(self.scr_pending(), self.kb_back("scr:pending"))
        elif cmd == "/models":
            await self.send(await self.scr_models(), self.kb_back("scr:models"))
        elif cmd == "/settings":
            await self.send(self.scr_settings(), self.kb_settings())
        else:
            await self.send(self.scr_menu(), self.kb_main())

    async def _on_callback(self, cb: dict):
        cb_id = cb.get("id", "")
        msg = cb.get("message", {})
        if not self._auth(msg.get("chat", {}).get("id")):
            await self.answer(cb_id, "Нет доступа")
            return
        msg_id = msg.get("message_id")
        data = cb.get("data", "") or ""
        if data == "noop":
            await self.answer(cb_id)
            return
        parts = data.split(":")
        kind = parts[0]

        if kind == "scr":
            text, kb = await self.screen(parts[1] if len(parts) > 1
                                         else "menu")
            await self.answer(cb_id)
            await self.edit(msg_id, text, kb)
            return

        if kind == "out" and len(parts) >= 3:
            rid, outcome = int(parts[1]), parts[2]
            result = await self.resolve_round(cb_id, rid, outcome)
            if result:
                await self.edit(msg_id, result, self.kb_main())
            return

        if kind == "set":
            await self._on_setting(cb_id, msg_id, parts[1:])
            return
        await self.answer(cb_id)

    async def _on_setting(self, cb_id: str, msg_id: int, parts: list):
        if not parts:
            await self.answer(cb_id)
            return
        what, op = parts[0], (parts[1] if len(parts) > 1 else "")
        if what == "payout":
            cur = self.payout()
            new = max(50.0, min(100.0, cur + (1.0 if op == "+" else -1.0)))
            self.db.set_setting("payout_pct", f"{new:.0f}")
            await self.answer(cb_id, f"Выплата {new:.0f}%")
        elif what == "horizon":
            cur = self.horizon()
            new = max(1, min(60, cur + (1 if op == "+" else -1)))
            self.db.set_setting("horizon_min", str(new))
            await self.answer(cb_id, f"Горизонт {new} мин")
        elif what == "stake":
            cur = self.stake()
            new = max(1.0, min(1000.0, cur + (5.0 if op == "+" else -5.0)))
            self.db.set_setting("stake", f"{new:.0f}")
            await self.answer(cb_id, f"Ставка ${new:.0f}")
        await self.edit(msg_id, self.scr_settings(), self.kb_settings())

    async def startup_message(self):
        payout = self.payout()
        be = summarize(0, 0, payout)["breakeven"]
        need = required_sample(max(be - 50.0, 2.0))
        await self.send(
            f"🧪 <b>БОТ ЗАПУЩЕН</b>\n{SEP}\n"
            f"Присылай скриншот {self.horizon()}-минутного графика — "
            f"опрошу {len(self.models())} "
            f"{'модель' if len(self.models()) == 1 else 'модели'} и запишу "
            f"прогноз.\n\n"
            f"Выплата {payout:.0f}% → порог безубытка <b>{be:.1f}%</b>\n"
            f"Для вывода понадобится около <b>{need}</b> прогнозов.\n{SEP}\n"
            f"<i>Ставок бот не делает — он выясняет, есть ли смысл их "
            f"делать вообще.</i>",
            self.kb_main())
