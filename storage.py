"""SQLite: прогнозы, исходы, настройки."""
import logging
import os
import sqlite3
import time
from typing import Any, Dict, List, Optional

from config import cfg

log = logging.getLogger("storage")


class Storage:
    def __init__(self, data_dir: str = None):
        data_dir = data_dir or cfg.DATA_DIR
        try:
            os.makedirs(data_dir, exist_ok=True)
        except OSError:
            data_dir = "."
        self.path = os.path.join(data_dir, "predictions.db")
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS rounds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_ms INTEGER NOT NULL,
            due_ms INTEGER NOT NULL,
            pair TEXT,
            horizon_min INTEGER,
            outcome TEXT,              -- UP | DOWN | FLAT | NULL пока не отмечен
            resolved_ms INTEGER,
            reminded INTEGER DEFAULT 0,
            tg_message_id INTEGER
        );
        CREATE TABLE IF NOT EXISTS calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            round_id INTEGER NOT NULL,
            model TEXT NOT NULL,
            direction TEXT,
            confidence REAL,
            reason TEXT,
            error TEXT,
            cost REAL DEFAULT 0,
            latency_ms INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
        CREATE INDEX IF NOT EXISTS idx_rounds_outcome ON rounds(outcome);
        CREATE INDEX IF NOT EXISTS idx_calls_round ON calls(round_id);
        CREATE INDEX IF NOT EXISTS idx_calls_model ON calls(model);
        """)
        self.conn.commit()
        log.info("База: %s", self.path)

    # ------------------------------------------------------------- настройки
    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT value FROM settings WHERE key=?",
                                (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: Any):
        self.conn.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)))
        self.conn.commit()

    def get_float(self, key: str, default: float) -> float:
        v = self.get_setting(key)
        try:
            return float(v) if v is not None else default
        except (TypeError, ValueError):
            return default

    def get_list(self, key: str, default: List[str]) -> List[str]:
        v = self.get_setting(key)
        if not v:
            return default
        out = [x.strip() for x in v.split(",") if x.strip()]
        return out or default

    # --------------------------------------------------------------- раунды
    def new_round(self, pair: str, horizon_min: int) -> int:
        now = int(time.time() * 1000)
        cur = self.conn.execute(
            "INSERT INTO rounds(created_ms,due_ms,pair,horizon_min) "
            "VALUES(?,?,?,?)",
            (now, now + horizon_min * 60_000, pair or "", horizon_min))
        self.conn.commit()
        return cur.lastrowid

    def add_call(self, round_id: int, p) -> int:
        cur = self.conn.execute(
            "INSERT INTO calls(round_id,model,direction,confidence,reason,"
            "error,cost,latency_ms) VALUES(?,?,?,?,?,?,?,?)",
            (round_id, p.model, p.direction, p.confidence, p.reason,
             p.error, p.cost, p.latency_ms))
        self.conn.commit()
        return cur.lastrowid

    def set_message_id(self, round_id: int, msg_id: int):
        self.conn.execute("UPDATE rounds SET tg_message_id=? WHERE id=?",
                          (msg_id, round_id))
        self.conn.commit()

    def round(self, round_id: int) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM rounds WHERE id=?",
                                (round_id,)).fetchone()
        return dict(row) if row else None

    def calls(self, round_id: int) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM calls WHERE round_id=? ORDER BY id",
            (round_id,)).fetchall()
        return [dict(r) for r in rows]

    def resolve(self, round_id: int, outcome: str):
        self.conn.execute(
            "UPDATE rounds SET outcome=?, resolved_ms=? WHERE id=?",
            (outcome, int(time.time() * 1000), round_id))
        self.conn.commit()

    def pending(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM rounds WHERE outcome IS NULL ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]

    def due_unreminded(self) -> List[Dict[str, Any]]:
        now = int(time.time() * 1000)
        rows = self.conn.execute(
            "SELECT * FROM rounds WHERE outcome IS NULL AND reminded=0 "
            "AND due_ms <= ? ORDER BY id", (now,)).fetchall()
        return [dict(r) for r in rows]

    def mark_reminded(self, round_id: int):
        self.conn.execute("UPDATE rounds SET reminded=1 WHERE id=?",
                          (round_id,))
        self.conn.commit()

    def recent_rounds(self, limit: int = 10) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM rounds ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ статистика
    def model_tally(self, model: str, include_flat: bool = False) -> tuple:
        """(попаданий, всего) по модели среди отмеченных раундов."""
        outcomes = "('UP','DOWN','FLAT')" if include_flat else "('UP','DOWN')"
        row = self.conn.execute(f"""
            SELECT
              SUM(CASE WHEN c.direction = r.outcome THEN 1 ELSE 0 END) hits,
              COUNT(*) n
            FROM calls c JOIN rounds r ON r.id = c.round_id
            WHERE c.model = ? AND c.direction IS NOT NULL
              AND r.outcome IN {outcomes}
        """, (model,)).fetchone()
        return (int(row["hits"] or 0), int(row["n"] or 0))

    def all_models(self) -> List[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT model FROM calls ORDER BY model").fetchall()
        return [r["model"] for r in rows]

    def consensus_tally(self) -> tuple:
        """Когда все модели сказали одно и то же — насколько они были правы."""
        rows = self.conn.execute("""
            SELECT r.id, r.outcome,
                   GROUP_CONCAT(DISTINCT c.direction) dirs,
                   COUNT(c.id) n
            FROM rounds r JOIN calls c ON c.round_id = r.id
            WHERE r.outcome IN ('UP','DOWN') AND c.direction IS NOT NULL
            GROUP BY r.id
        """).fetchall()
        hits = total = 0
        for r in rows:
            dirs = (r["dirs"] or "").split(",")
            if len(dirs) != 1 or int(r["n"] or 0) < 2:
                continue
            total += 1
            if dirs[0] == r["outcome"]:
                hits += 1
        return hits, total

    def confidence_split(self, model: str, threshold: float = 70.0) -> dict:
        """Лучше ли модель угадывает, когда заявляет высокую уверенность."""
        out = {}
        for label, cond in (("уверенные", ">="), ("неуверенные", "<")):
            row = self.conn.execute(f"""
                SELECT
                  SUM(CASE WHEN c.direction = r.outcome THEN 1 ELSE 0 END) hits,
                  COUNT(*) n
                FROM calls c JOIN rounds r ON r.id = c.round_id
                WHERE c.model = ? AND c.direction IS NOT NULL
                  AND r.outcome IN ('UP','DOWN')
                  AND c.confidence {cond} ?
            """, (model, threshold)).fetchone()
            out[label] = (int(row["hits"] or 0), int(row["n"] or 0))
        return out

    def totals(self) -> dict:
        row = self.conn.execute("""
            SELECT COUNT(*) rounds,
                   SUM(CASE WHEN outcome IS NOT NULL THEN 1 ELSE 0 END) resolved,
                   SUM(CASE WHEN outcome = 'FLAT' THEN 1 ELSE 0 END) flat
            FROM rounds""").fetchone()
        cost = self.conn.execute(
            "SELECT SUM(cost) c, COUNT(*) n FROM calls").fetchone()
        return {
            "rounds": int(row["rounds"] or 0),
            "resolved": int(row["resolved"] or 0),
            "flat": int(row["flat"] or 0),
            "pending": int(row["rounds"] or 0) - int(row["resolved"] or 0),
            "cost": float(cost["c"] or 0.0),
            "calls": int(cost["n"] or 0),
        }
