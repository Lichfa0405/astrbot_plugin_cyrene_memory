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
            return conn.execute("SELECT id FROM l0_profile WHERE user_id=? AND category=? AND key=?", (user_id, category, key)).fetchone()["id"]
        finally:
            conn.close()

    def get_l0(self, user_id: str = None):
        conn = self._conn()
        try:
            if user_id:
                rows = conn.execute(
                    "SELECT * FROM l0_profile WHERE user_id = ? ORDER BY updated_at DESC",
                    (user_id,)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM l0_profile ORDER BY updated_at DESC"
                ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ---------- L1 ----------
    def append_raw_log(self, user_id: str, role: str, content: str, ttl_days: float = 7.0):
        now = time.time()
        conn = self._conn()
        try:
            conn.execute(
                "INSERT INTO raw_logs (user_id, role, content, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, role, content, now, now + ttl_days * 86400)
            )
            conn.commit()
            
            # 自动归档检查
            count = conn.execute("SELECT COUNT(*) FROM raw_logs").fetchone()[0]
            if count >= 100:
                self._archive_l1_batch(conn)
        finally:
            conn.close()

    def _archive_l1_batch(self, conn):
        import os, zipfile, json, time
        try:
            # 每次只切最老的 50 条
            rows = conn.execute("SELECT * FROM raw_logs ORDER BY created_at ASC LIMIT 50").fetchall()
            if not rows:
                return
            
            start_ts = rows[0]["created_at"]
            end_ts = rows[-1]["created_at"]
            fmt = "%Y%m%d_%H%M%S"
            start_str = time.strftime(fmt, time.localtime(start_ts))
            end_str = time.strftime(fmt, time.localtime(end_ts))
            start_id = rows[0]["id"]
            end_id = rows[-1]["id"]
            
            archive_dir = os.path.join(os.path.dirname(self.db_path), "archives")
            os.makedirs(archive_dir, exist_ok=True)
            
            zip_name = f"l1_{start_str}__{end_str}_ID{start_id}-{end_id}.zip"
            zip_path = os.path.join(archive_dir, zip_name)
            jsonl_path = os.path.join(archive_dir, f"temp_{start_id}_{end_id}.jsonl")
            
            # 使用 print 写入，彻底避免换行符转义问题
            with open(jsonl_path, "w", encoding="utf-8") as f_out:
                for r in rows:
                    print(json.dumps(dict(r), ensure_ascii=False), file=f_out)
            
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                zf.write(jsonl_path, os.path.basename(jsonl_path).replace("temp_", "l1_data_"))
            
            os.remove(jsonl_path)
            
            ids = [str(r["id"]) for r in rows]
            conn.execute(f"DELETE FROM raw_logs WHERE id IN ({','.join(ids)})")
            conn.commit()
            print(f"[Cyrene Memory] 切片归档完成: {zip_name}")
        except Exception as e:
            print(f"[Cyrene Memory] 切片归档失败: {e}")

    def get_archives(self):
        import os
        archive_dir = os.path.join(os.path.dirname(self.db_path), "archives")
        if not os.path.exists(archive_dir): return []
        return [f for f in os.listdir(archive_dir) if f.endswith('.zip')]


    def get_raw_logs(self, user_id: str, limit: int = 20):
        now = time.time()
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT * FROM raw_logs WHERE user_id = ? AND (expires_at IS NULL OR expires_at > ?) ORDER BY created_at DESC LIMIT ?",
                (user_id, now, limit)
            ).fetchall()
            return [dict(r) for r in rows][::-1]
        finally:
            conn.close()

    # ---------- L2 ----------
    def append_l2(self, user_id: str, content: str, importance: float = 0.5,
                  sentiment: str = None, tags: str = "[]", entities: str = "[]",
                  event_time: float = None, emotion_note: str = ""):
        now = time.time()
        conn = self._conn()
        try:
            conn.execute("""
                INSERT INTO l2_events (user_id, event_time, content, entities, sentiment, tags, importance, created_at, emotion_note)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (user_id, event_time or now, content, entities, sentiment, tags, importance, now, emotion_note or ""))
            conn.commit()
            return conn.execute("SELECT last_insert_rowid() as id").fetchone()["id"]
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

    # ---------- Relationship ----------
    def get_relationship(self, user_id: str):
        conn = self._conn()
        try:
            row = conn.execute("SELECT * FROM relationship_state WHERE user_id = ?", (user_id,)).fetchone()
            if row:
                return dict(row)
            # 如果没记录，返回默认值
            return {"user_id": user_id, "stage": "初识", "recent_mood": "平稳", "updated_at": time.time()}
        finally:
            conn.close()

    def update_relationship(self, user_id: str, stage: str = None, recent_mood: str = None):
        now = time.time()
        conn = self._conn()
        try:
            conn.execute("INSERT OR IGNORE INTO relationship_state (user_id, updated_at) VALUES (?, ?)", (user_id, now))
            if stage is not None:
                conn.execute("UPDATE relationship_state SET stage = ?, updated_at = ? WHERE user_id = ?", (stage, now, user_id))
            if recent_mood is not None:
                conn.execute("UPDATE relationship_state SET recent_mood = ?, updated_at = ? WHERE user_id = ?", (recent_mood, now, user_id))
            conn.commit()
        finally:
            conn.close()

    # ---------- Reflection 支持 ----------
    def get_unreflected_l1(self, user_id: str, limit: int = 50):
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT id, role, content FROM l1_working WHERE user_id = ? AND is_reflected = 0 ORDER BY id ASC LIMIT ?",
                (user_id, limit)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def mark_l1_reflected(self, ids: list):
        if not ids: return
        conn = self._conn()
        try:
            placeholders = ','.join('?' * len(ids))
            conn.execute(f"UPDATE l1_working SET is_reflected = 1 WHERE id IN ({placeholders})", ids)
            conn.commit()
        finally:
            conn.close()

    # ---------- Tasks (状态机) ----------
    def upsert_task(self, user_id: str, title: str, status: str = "active", summary: str = ""):
        now = time.time()
        conn = self._conn()
        try:
            # 检查是否存在同名且未完成的任务
            row = conn.execute("SELECT id FROM tasks WHERE user_id = ? AND title = ? AND status != 'done'", (user_id, title)).fetchone()
            if row:
                conn.execute(
                    "UPDATE tasks SET status = ?, summary = ?, updated_at = ? WHERE id = ?",
                    (status, summary, now, row["id"])
                )
            else:
                conn.execute(
                    "INSERT INTO tasks (user_id, title, status, summary, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (user_id, title, status, summary, now, now)
                )
            conn.commit()
        finally:
            conn.close()

    def get_active_tasks(self, user_id: str, limit: int = 5):
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT title, status, summary FROM tasks WHERE user_id = ? AND status IN ('active', 'paused') ORDER BY updated_at DESC LIMIT ?",
                (user_id, limit)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_all_tasks(self, user_id: str):
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT id, title, status, summary, updated_at FROM tasks WHERE user_id = ? ORDER BY updated_at DESC",
                (user_id,)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ---------- Embeddings ----------
    def save_embedding(self, record_id: int, record_type: str, vector_bytes: bytes):
        conn = self._conn()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO embeddings (record_id, record_type, vector, updated_at) VALUES (?, ?, ?, ?)",
                (record_id, record_type, vector_bytes, time.time())
            )
            conn.commit()
        finally:
            conn.close()

    def get_all_embeddings(self, record_type: str):
        conn = self._conn()
        try:
            rows = conn.execute("SELECT record_id, vector FROM embeddings WHERE record_type = ?", (record_type,)).fetchall()
            return {r["record_id"]: r["vector"] for r in rows}
        finally:
            conn.close()

    def search_archives(self, keyword: str, limit: int = 15):
        """去压缩包冷数据里搜索相关对话"""
        import os, zipfile, json
        archive_dir = os.path.join(os.path.dirname(self.db_path), "archives")
        if not os.path.exists(archive_dir):
            return []
            
        results = []
        # 按时间倒序（最新的包先搜）
        files = sorted([f for f in os.listdir(archive_dir) if f.endswith('.zip')], reverse=True)
        
        for file in files:
            if len(results) >= limit: break
            file_path = os.path.join(archive_dir, file)
            try:
                with zipfile.ZipFile(file_path, 'r') as zf:
                    for name in zf.namelist():
                        if name.endswith('.jsonl'):
                            with zf.open(name) as f:
                                for line in f:
                                    try:
                                        row = json.loads(line.decode('utf-8'))
                                        content = row.get('content', '')
                                        if keyword.lower() in content.lower():
                                            results.append(f"[来自归档 {file}] {row.get('role')}: {content}")
                                            if len(results) >= limit:
                                                return results
                                    except Exception:
                                        pass
            except Exception as e:
                pass
        return results

    # ---------- True L1 Working Memory ----------
    def upsert_l1_working(self, user_id: str, key: str, value: str):
        now = time.time()
        conn = self._conn()
        try:
            # 检查是否存在该 key
            row = conn.execute("SELECT id FROM l1_working WHERE user_id = ? AND key = ?", (user_id, key)).fetchone()
            if row:
                conn.execute("UPDATE l1_working SET value = ?, updated_at = ? WHERE id = ?", (value, now, row["id"]))
            else:
                conn.execute("INSERT INTO l1_working (user_id, key, value, updated_at) VALUES (?, ?, ?, ?)", (user_id, key, value, now))
            conn.commit()
        finally:
            conn.close()

    def get_l1_working(self, user_id: str, limit: int = 20):
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT id, key, value, updated_at FROM l1_working WHERE user_id = ? ORDER BY updated_at DESC LIMIT ?",
                (user_id, limit)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def delete_l1_working(self, row_id: int):
        conn = self._conn()
        try:
            conn.execute("DELETE FROM l1_working WHERE id = ?", (row_id,))
            conn.commit()
        finally:
            conn.close()

    def delete_l1_working_by_key(self, user_id: str, key: str):
        conn = self._conn()
        try:
            conn.execute("DELETE FROM l1_working WHERE user_id = ? AND key = ?", (user_id, key))
            conn.commit()
        finally:
            conn.close()
