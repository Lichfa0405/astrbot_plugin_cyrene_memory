"""Cyrene Memory 官方插件页面 API。"""

from __future__ import annotations

PLUGIN_NAME = "astrbot_plugin_cyrene_memory"
PAGE_API_PREFIX = f"/{PLUGIN_NAME}/page"


class CyrenePageApi:
    def __init__(self, plugin) -> None:
        self.plugin = plugin

    def register_routes(self) -> None:
        register = self.plugin.context.register_web_api
        register(f"{PAGE_API_PREFIX}/stats", self.get_stats, ["GET"], "Cyrene stats")
        register(f"{PAGE_API_PREFIX}/l0", self.get_l0, ["GET"], "Cyrene L0 list")
        register(f"{PAGE_API_PREFIX}/l1", self.get_l1, ["GET"], "Cyrene L1 list")
        register(f"{PAGE_API_PREFIX}/l2", self.get_l2, ["GET"], "Cyrene L2 list")
        register(f"{PAGE_API_PREFIX}/relationship", self.get_relationship, ["GET"], "Cyrene Relationship")
        register(f"{PAGE_API_PREFIX}/tasks", self.get_tasks, ["GET"], "Cyrene Tasks")
        register(f"{PAGE_API_PREFIX}/l0/delete", self.del_l0, ["POST"], "Cyrene L0 delete")
        register(f"{PAGE_API_PREFIX}/l1/delete", self.del_l1, ["POST"], "Cyrene L1 delete")
        register(f"{PAGE_API_PREFIX}/l2/delete", self.del_l2, ["POST"], "Cyrene L2 delete")

    async def get_stats(self, *args, **kwargs):
        storage = self.plugin.storage
        import sqlite3
        conn = sqlite3.connect(storage.db_path)
        try:
            n0 = conn.execute("SELECT COUNT(*) FROM l0_profile").fetchone()[0]
            n1 = conn.execute("SELECT COUNT(*) FROM l1_working").fetchone()[0]
            n2 = conn.execute("SELECT COUNT(*) FROM l2_events").fetchone()[0]
            try:
                nrel = conn.execute("SELECT COUNT(*) FROM relationship_state").fetchone()[0]
            except Exception:
                nrel = 0
        finally:
            conn.close()
        return {"l0": n0, "l1": n1, "l2": n2, "rel": nrel}

    async def get_l0(self, *args, **kwargs):
        return {"items": self.plugin.storage.get_l0()}

    async def get_l1(self, *args, **kwargs):
        import sqlite3
        conn = sqlite3.connect(self.plugin.storage.db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT id, user_id, role, substr(content,1,200) AS preview, created_at FROM l1_working ORDER BY id DESC LIMIT 200"
            ).fetchall()
            return {"items": [dict(r) for r in rows]}
        finally:
            conn.close()

    async def get_l2(self, *args, **kwargs):
        import sqlite3
        conn = sqlite3.connect(self.plugin.storage.db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT id, user_id, content, sentiment, tags, importance, created_at, emotion_note FROM l2_events ORDER BY id DESC LIMIT 200"
            ).fetchall()
            return {"items": [dict(r) for r in rows]}
        finally:
            conn.close()

    def _get_id(self, payload):
        if isinstance(payload, dict):
            return payload.get("id")
        return None

    async def del_l0(self, *args, **kwargs):
        payload = kwargs.get("payload") or (args[0] if args else {})
        rid = self._get_id(payload) if isinstance(payload, dict) else payload
        import sqlite3
        conn = sqlite3.connect(self.plugin.storage.db_path)
        try:
            conn.execute("DELETE FROM l0_profile WHERE id = ?", (rid,))
            conn.commit()
        finally:
            conn.close()
        return {"ok": True, "deleted": rid}

    async def del_l1(self, *args, **kwargs):
        payload = kwargs.get("payload") or (args[0] if args else {})
        rid = self._get_id(payload) if isinstance(payload, dict) else payload
        import sqlite3
        conn = sqlite3.connect(self.plugin.storage.db_path)
        try:
            conn.execute("DELETE FROM l1_working WHERE id = ?", (rid,))
            conn.commit()
        finally:
            conn.close()
        return {"ok": True, "deleted": rid}

    async def del_l2(self, *args, **kwargs):
        payload = kwargs.get("payload") or (args[0] if args else {})
        rid = self._get_id(payload) if isinstance(payload, dict) else payload
        import sqlite3
        conn = sqlite3.connect(self.plugin.storage.db_path)
        try:
            conn.execute("DELETE FROM l2_events WHERE id = ?", (rid,))
            conn.commit()
        finally:
            conn.close()
        return {"ok": True, "deleted": rid}

    async def get_relationship(self, *args, **kwargs):
        import sqlite3
        conn = sqlite3.connect(self.plugin.storage.db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("SELECT * FROM relationship_state ORDER BY updated_at DESC").fetchall()
            return {"items": [dict(r) for r in rows]}
        finally:
            conn.close()

    async def get_tasks(self, *args, **kwargs):
        return {"items": self.plugin.storage.get_all_tasks("110016396")}
