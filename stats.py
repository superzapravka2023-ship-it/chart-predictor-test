"""Честная статистика попаданий: интервал Вилсона и проверка против монетки.

Главное, что здесь считается, — отличим ли результат от случайного угадывания.
Процент попаданий сам по себе ничего не значит: 6 из 10 это 60%, но такое
бывает при подбрасывании монеты в трети случаев.
"""
import math
from typing import Dict, List, Optional

Z95 = 1.959963984540054


def breakeven_winrate(payout_pct: float) -> float:
    """Какой процент попаданий нужен, чтобы выйти в ноль при такой выплате.

    Выигрыш даёт payout от ставки, проигрыш забирает ставку целиком:
    w * payout = (1 - w)  →  w = 1 / (1 + payout)
    """
    p = max(payout_pct, 0.0) / 100.0
    if p <= 0:
        return 100.0
    return 1.0 / (1.0 + p) * 100.0


def wilson_interval(hits: int, n: int, z: float = Z95) -> tuple:
    """Доверительный интервал Вилсона — корректен и на малых выборках."""
    if n <= 0:
        return (0.0, 100.0)
    p = hits / n
    denom = 1.0 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, (center - half)) * 100.0,
            min(1.0, (center + half)) * 100.0)


def binomial_p_value(hits: int, n: int, p0: float = 0.5) -> float:
    """Двусторонняя вероятность получить такой или более крайний результат
    при честной монетке. Чем меньше, тем менее правдоподобна случайность."""
    if n <= 0:
        return 1.0
    if n > 2000:   # для больших n считаем нормальным приближением
        se = math.sqrt(p0 * (1 - p0) / n)
        if se == 0:
            return 1.0
        zz = abs(hits / n - p0) / se
        return max(0.0, min(1.0, math.erfc(zz / math.sqrt(2))))

    def pmf(k: int) -> float:
        return math.comb(n, k) * (p0 ** k) * ((1 - p0) ** (n - k))

    observed = pmf(hits)
    total = 0.0
    for k in range(n + 1):
        val = pmf(k)
        if val <= observed * (1 + 1e-9):
            total += val
    return max(0.0, min(1.0, total))


def required_sample(edge_pct: float, p0: float = 0.5,
                    z: float = Z95) -> int:
    """Сколько прогнозов нужно, чтобы доказать перевес такого размера."""
    edge = abs(edge_pct) / 100.0
    if edge <= 0:
        return 0
    return int(math.ceil((z * z * p0 * (1 - p0)) / (edge * edge)))


def expected_value(hit_rate_pct: float, payout_pct: float,
                   stake: float) -> float:
    """Ожидание на одну ставку при таком проценте попаданий."""
    w = hit_rate_pct / 100.0
    p = payout_pct / 100.0
    return (w * p - (1 - w)) * stake


def summarize(hits: int, n: int, payout_pct: float,
              stake: float = 10.0) -> Dict[str, object]:
    """Полная сводка по серии прогнозов одной модели."""
    be = breakeven_winrate(payout_pct)
    if n == 0:
        return {"n": 0, "hits": 0, "rate": 0.0, "lo": 0.0, "hi": 100.0,
                "breakeven": be, "p_value": 1.0, "beats_chance": False,
                "beats_breakeven": False, "verdict": "нет данных",
                "ev": 0.0, "need": required_sample(be - 50.0)}
    rate = hits / n * 100.0
    lo, hi = wilson_interval(hits, n)
    pval = binomial_p_value(hits, n, 0.5)
    beats_chance = pval < 0.05 and rate > 50.0
    # уверенно лучше безубытка, только если ВЕСЬ интервал выше порога
    beats_be = lo > be

    if n < 30:
        verdict = "данных слишком мало, любой вывод преждевременен"
    elif beats_be:
        verdict = "результат уверенно выше порога безубытка"
    elif hi < be:
        verdict = "результат уверенно НИЖЕ порога безубытка"
    elif beats_chance:
        verdict = ("лучше монетки, но порог безубытка не перекрыт "
                   "уверенно")
    else:
        verdict = "от подбрасывания монеты не отличается"

    return {
        "n": n, "hits": hits, "rate": rate, "lo": lo, "hi": hi,
        "breakeven": be, "p_value": pval,
        "beats_chance": beats_chance, "beats_breakeven": beats_be,
        "verdict": verdict,
        "ev": expected_value(rate, payout_pct, stake),
        "need": required_sample(max(be - 50.0, 2.0)),
    }
