import os
import time
import sqlite3

try:
    from astrbot.api import logger
except ImportError:
    import logging
    logger = logging.getLogger("cyrene_memory")

DB_PATH = "/root/astrbot-data/data/plugin_data/astrbot_plugin_cyrene_memory/cyrene_memory.db"


class CyreneStorage:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        if not os.path.exists(db_path):
            logger.warning(f"[Cyrene Memory] 新库不存在: {db_path}")

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # ---------- L0 ----------
    def upsert_l0(self, user_id: str, category: str, key: str, value: str, confidence: float = 0.8):
        now = time.time()
        conn = self._conn()
        try:
            conn.execute("""
                INSERT INTO l0_profile (user_id, category, key, value, confidence, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, category, key) DO UPDATE SET
                    value = excluded.value,
                    confidence = excluded.confidence,
                    updated_at = excluded.updated_at
            """, (user_id, category, key, value, confidence, now, now))
            conn.commit()
        finally:
            conn.close()

    def get_l0(self, user_id: str = None):
        conn = self._conn()
        try:
            if user_id:
                rows = conn.execute(
                    "SELECT * FROM l0_profile WHERE user_id = ? ORDER BY category, key",
                    (user_id,)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM l0_profile ORDER BY user_id, category, key"
                ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ---------- L1 ----------
    def append_l1(self, user_id: str, role: str, content: str, ttl_days: float = 7.0):
        now = time.time()
        conn = self._conn()
        try:
            conn.execute(
                "INSERT INTO l1_working (user_id, role, content, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, role, content, now, now + ttl_days * 86400)
            )
            conn.commit()
        finally:
            conn.close()

    def get_l1(self, user_id: str, limit: int = 20):
        now = time.time()
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT * FROM l1_working WHERE user_id = ? AND (expires_at IS NULL OR expires_at > ?) ORDER BY created_at DESC LIMIT ?",
                (user_id, now, limit)
            ).fetchall()
            return [dict(r) for r in rows][::-1]
        finally:
            conn.close()

    # ---------- L2 ----------
    def append_l2(self, user_id: str, content: str, importance: float = 0.5,
                  sentiment: str = None, tags: str = "[]", entities: str = "[]", event_time: float = None):
        now = time.time()
        conn = self._conn()
        try:
            conn.execute("""
                INSERT INTO l2_events (user_id, event_time, content, entities, sentiment, tags, importance, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (user_id, event_time or now, content, entities, sentiment, tags, importance, now))
            conn.commit()
        finally:
            conn.close()

    def search_l2(self, user_id: str, keyword: str = None, limit: int = 20):
        conn = self._conn()
        try:
            if keyword:
                rows = conn.execute(
                    "SELECT * FROM l2_events WHERE user_id = ? AND content LIKE ? ORDER BY importance DESC, created_at DESC LIMIT ?",
                    (user_id, f"%{keyword}%", limit)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM l2_events WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
                    (user_id, limit)
                ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
