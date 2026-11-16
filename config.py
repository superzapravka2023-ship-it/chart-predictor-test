"""Конфигурация. Всё из переменных окружения Render."""
import os


def _f(name: str, default: float) -> float:
    raw = os.getenv(name, "")
    if raw is None or str(raw).strip() == "":
        return float(default)
    try:
        return float(str(raw).strip())
    except ValueError:
        return float(default)


def _i(name: str, default: int) -> int:
    return int(_f(name, default))


def _b(name: str, default: bool) -> bool:
    raw = os.getenv(name, "")
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "y", "on", "да")


def _list(name: str, default: str) -> list:
    raw = os.getenv(name, default) or ""
    return [x.strip() for x in raw.replace(";", ",").split(",") if x.strip()]


class Config:
    # ---------- Telegram ----------
    TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    # ---------- OpenRouter ----------
    OR_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
    OR_URL = os.getenv("OPENROUTER_URL",
                       "https://openrouter.ai/api/v1").rstrip("/")
    # ID моделей могут устареть — актуальные смотри на openrouter.ai/models
    # или командой /models прямо в боте
    MODELS = _list("MODELS", "openai/gpt-4o,google/gemini-flash-1.5")
    OR_TIMEOUT = _i("OR_TIMEOUT", 90)
    MAX_TOKENS = _i("MAX_TOKENS", 300)

    # ---------- Логика проверки ----------
    HORIZON_MIN = _i("HORIZON_MIN", 5)      # горизонт прогноза, минут
    PAYOUT_PCT = _f("PAYOUT_PCT", 80.0)     # выплата брокера при выигрыше, %
    REMIND_GRACE_SEC = _i("REMIND_GRACE_SEC", 10)
    MAX_PENDING = _i("MAX_PENDING", 3)      # сколько незакрытых прогнозов терпим

    # ---------- Прочее ----------
    TZ_OFFSET = _f("TZ_OFFSET", 3.0)
    DATA_DIR = os.getenv("DATA_DIR", "").strip() or "."
    PORT = _i("PORT", 10000)
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").strip().upper()

    @classmethod
    def validate(cls) -> list:
        errs = []
        if not cls.TG_TOKEN:
            errs.append("TELEGRAM_BOT_TOKEN не задан")
        if not cls.TG_CHAT_ID:
            errs.append("TELEGRAM_CHAT_ID не задан")
        if not cls.OR_KEY:
            errs.append("OPENROUTER_API_KEY не задан")
        if not cls.MODELS:
            errs.append("MODELS пуст — нечего опрашивать")
        return errs


cfg = Config
