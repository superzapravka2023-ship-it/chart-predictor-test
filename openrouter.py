"""Клиент OpenRouter: опрос нескольких vision-моделей по одному скриншоту."""
import asyncio
import base64
import json
import logging
import re
from typing import Dict, List, Optional

import aiohttp

from config import cfg

log = logging.getLogger("openrouter")

PROMPT = (
    "You are looking at a {horizon}-minute candlestick chart screenshot of a "
    "financial instrument. The last candle on the right is the most recent.\n\n"
    "Task: predict whether the price {horizon} minutes from now will be "
    "HIGHER or LOWER than the current price (the close of the last candle).\n\n"
    "Answer strictly in this format, nothing else:\n"
    "DIRECTION: UP or DOWN\n"
    "CONFIDENCE: a number from 50 to 100\n"
    "REASON: one short sentence\n\n"
    "You must choose UP or DOWN. Do not refuse, do not hedge, do not say "
    "it is unpredictable — this is a calibration experiment and a forced "
    "choice is required. If you genuinely see no signal, pick either one "
    "and set CONFIDENCE to 50."
)


class Prediction:
    def __init__(self, model: str):
        self.model = model
        self.direction: Optional[str] = None   # UP | DOWN
        self.confidence: float = 50.0
        self.reason: str = ""
        self.error: str = ""
        self.cost: float = 0.0
        self.latency_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.direction in ("UP", "DOWN")

    def arrow(self) -> str:
        if self.direction == "UP":
            return "⬆️ ВВЕРХ"
        if self.direction == "DOWN":
            return "⬇️ ВНИЗ"
        return "❔ нет ответа"


def parse_answer(text: str) -> tuple:
    """Достаёт направление, уверенность и причину из ответа модели."""
    direction = None
    confidence = 50.0
    reason = ""
    if not text:
        return direction, confidence, reason

    m = re.search(r"DIRECTION\s*:\s*(UP|DOWN)", text, re.I)
    if m:
        direction = m.group(1).upper()
    else:
        # запасной разбор, если модель ответила вольно
        up = len(re.findall(r"\b(up|higher|bullish|long)\b", text, re.I))
        dn = len(re.findall(r"\b(down|lower|bearish|short)\b", text, re.I))
        if up != dn:
            direction = "UP" if up > dn else "DOWN"

    m = re.search(r"CONFIDENCE\s*:\s*(\d+(?:\.\d+)?)", text, re.I)
    if m:
        try:
            confidence = max(50.0, min(100.0, float(m.group(1))))
        except ValueError:
            pass

    m = re.search(r"REASON\s*:\s*(.+)", text, re.I)
    if m:
        reason = m.group(1).strip()[:300]
    elif text:
        reason = " ".join(text.split())[:200]
    return direction, confidence, reason


class OpenRouter:
    def __init__(self, api_key: str = None, base_url: str = None):
        self.key = api_key or cfg.OR_KEY
        self.base = (base_url or cfg.OR_URL).rstrip("/")
        self._session: Optional[aiohttp.ClientSession] = None

    async def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=cfg.OR_TIMEOUT + 20))
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/chart-predictor-test",
            "X-Title": "Chart Predictor Calibration Test",
        }

    async def vision_models(self) -> List[dict]:
        """Живой список моделей с поддержкой картинок — чтобы не гадать с ID."""
        try:
            sess = await self.session()
            async with sess.get(f"{self.base}/models",
                                headers=self._headers()) as r:
                data = await r.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError,
                json.JSONDecodeError) as e:
            log.warning("Список моделей не получен: %s", e)
            return []
        out = []
        for m in (data.get("data") or []):
            mods = (m.get("architecture") or {}).get("input_modalities") or []
            if "image" not in mods:
                # запасная проверка для старого формата ответа
                mod = (m.get("architecture") or {}).get("modality") or ""
                if "image" not in mod:
                    continue
            pricing = m.get("pricing") or {}
            try:
                prompt_price = float(pricing.get("prompt") or 0)
            except (TypeError, ValueError):
                prompt_price = 0.0
            out.append({"id": m.get("id", ""),
                        "name": m.get("name", ""),
                        "prompt_price": prompt_price})
        out.sort(key=lambda x: x["prompt_price"])
        return out

    async def ask(self, model: str, image_bytes: bytes,
                  horizon: int = 5) -> Prediction:
        """Один вопрос одной модели по одному скриншоту."""
        import time
        p = Prediction(model)
        b64 = base64.b64encode(image_bytes).decode()
        payload = {
            "model": model,
            "max_tokens": cfg.MAX_TOKENS,
            "temperature": 0,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text",
                     "text": PROMPT.format(horizon=horizon)},
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                ],
            }],
        }
        started = time.time()
        try:
            sess = await self.session()
            async with sess.post(f"{self.base}/chat/completions",
                                 headers=self._headers(),
                                 json=payload,
                                 timeout=aiohttp.ClientTimeout(
                                     total=cfg.OR_TIMEOUT)) as r:
                raw = await r.text()
                status = r.status
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                p.error = f"HTTP {status}, не JSON: {' '.join(raw.split())[:150]}"
                return p
            if status != 200 or "error" in data:
                err = data.get("error")
                msg = err.get("message") if isinstance(err, dict) else str(err)
                p.error = f"HTTP {status}: {str(msg)[:180]}"
                return p
            choices = data.get("choices") or []
            if not choices:
                p.error = "пустой ответ модели"
                return p
            text = (choices[0].get("message") or {}).get("content") or ""
            p.direction, p.confidence, p.reason = parse_answer(text)
            if not p.ok:
                p.error = f"не разобрал ответ: {' '.join(text.split())[:120]}"
            usage = data.get("usage") or {}
            try:
                p.cost = float(usage.get("cost") or 0.0)
            except (TypeError, ValueError):
                p.cost = 0.0
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            p.error = f"сеть: {e}"
        finally:
            p.latency_ms = int((time.time() - started) * 1000)
        return p

    async def ask_all(self, models: List[str], image_bytes: bytes,
                      horizon: int = 5) -> List[Prediction]:
        """Все модели параллельно — так сравнение получается на одной картинке."""
        tasks = [self.ask(m, image_bytes, horizon) for m in models]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        out = []
        for model, res in zip(models, results):
            if isinstance(res, Exception):
                p = Prediction(model)
                p.error = f"{type(res).__name__}: {res}"
                out.append(p)
            else:
                out.append(res)
        return out
