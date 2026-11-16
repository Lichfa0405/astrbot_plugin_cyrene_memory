"""Cyrene Memory 官方插件页面 API。"""
from __future__ import annotations
import sqlite3
import time
import os

try:
    from quart import request
except ImportError:
    from fastapi import request

PLUGIN_NAME = "astrbot_plugin_cyrene_memory"
PAGE_API_PREFIX = f"/{PLUGIN_NAME}/page"


class CyrenePageApi:
    def __init__(self, plugin) -> None:
        self.plugin = plugin

    def register_routes(self) -> None:
        register = self.plugin.context.register_web_api
        # --- GET 查询类 ---
        register(f"{PAGE_API_PREFIX}/stats", self.get_stats, ["GET"], "Cyrene stats")
        register(f"{PAGE_API_PREFIX}/l0", self.get_l0, ["GET"], "Cyrene L0 list")
        register(f"{PAGE_API_PREFIX}/l1", self.get_l1, ["GET"], "Cyrene L1 list")
        register(f"{PAGE_API_PREFIX}/l2", self.get_l2, ["GET"], "Cyrene L2 list")
        register(f"{PAGE_API_PREFIX}/raw_logs", self.get_raw_logs, ["GET"], "Cyrene Raw Logs")
        register(f"{PAGE_API_PREFIX}/relationship", self.get_relationship, ["GET"], "Cyrene Rel")
        register(f"{PAGE_API_PREFIX}/tasks", self.get_tasks, ["GET"], "Cyrene Tasks")
        register(f"{PAGE_API_PREFIX}/archives", self.get_archives, ["GET"], "Cyrene Archives")
        
        # --- 修改类 (全部单层下划线，同时支持 POST/GET，双管齐下) ---
        register(f"{PAGE_API_PREFIX}/l0_update", self.update_l0, ["POST", "GET"], "Update L0")
        register(f"{PAGE_API_PREFIX}/l0_simplify", self.simplify_l0, ["POST", "GET"], "Simplify L0")
        register(f"{PAGE_API_PREFIX}/l0_summarize", self.summarize_l0, ["POST", "GET"], "Summarize L0")
        register(f"{PAGE_API_PREFIX}/l0_move", self.move_l0, ["POST", "GET"], "Move L0")
        register(f"{PAGE_API_PREFIX}/l0_delete", self.del_l0, ["POST", "GET"], "Delete L0")
        register(f"{PAGE_API_PREFIX}/l1_delete", self.del_l1, ["POST", "GET"], "Delete L1")
        register(f"{PAGE_API_PREFIX}/l2_delete", self.del_l2, ["POST", "GET"], "Delete L2")

    async def _extract_data(self):
        """最强参数提取器：不管 GET 还是 POST，绝不落空"""
        try:
            if request.method == "POST":
                try:
                    data = await request.get_json()
                    if data: return data
                except Exception:
                    try:
                        data = await request.json()
                        if data: return data
                    except Exception:
                        pass
            if request.args:
                return dict(request.args)
        except Exception:
            pass
        return {}

    # ---------------- 查询类 ----------------
    async def get_stats(self):
        conn = sqlite3.connect(self.plugin.storage.db_path)
        try:
            n0 = conn.execute("SELECT COUNT(*) FROM l0_profile").fetchone()[0]
            n1 = conn.execute("SELECT COUNT(*) FROM l1_working").fetchone()[0]
            n2 = conn.execute("SELECT COUNT(*) FROM l2_events").fetchone()[0]
            nrel = conn.execute("SELECT COUNT(*) FROM relationship_state").fetchone()[0]
            ntasks = conn.execute("SELECT COUNT(*) FROM tasks WHERE status != 'done'").fetchone()[0]
            nraw = conn.execute("SELECT COUNT(*) FROM raw_logs").fetchone()[0]
        finally:
            conn.close()
        return {"l0": n0, "l1": n1, "l2": n2, "rel": nrel, "tasks": ntasks, "raw": nraw}

    async def get_l0(self):
        return {"items": self.plugin.storage.get_l0()}

    async def get_l1(self):
        return {"items": self.plugin.storage.get_l1_working("110016396")}

    async def get_l2(self):
        conn = sqlite3.connect(self.plugin.storage.db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("SELECT id, user_id, content, sentiment, tags, importance, created_at, emotion_note FROM l2_events ORDER BY created_at DESC LIMIT 200").fetchall()
            return {"items": [dict(r) for r in rows]}
        finally:
            conn.close()

    async def get_raw_logs(self):
        conn = sqlite3.connect(self.plugin.storage.db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("SELECT id, user_id, role, substr(content,1,200) AS preview, created_at FROM raw_logs ORDER BY created_at DESC LIMIT 200").fetchall()
            return {"items": [dict(r) for r in rows]}
        finally:
            conn.close()

    async def get_relationship(self):
        conn = sqlite3.connect(self.plugin.storage.db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("SELECT * FROM relationship_state ORDER BY updated_at DESC").fetchall()
            return {"items": [dict(r) for r in rows]}
        finally:
            conn.close()

    async def get_tasks(self):
        return {"items": self.plugin.storage.get_all_tasks("110016396")}

    async def get_archives(self):
        return {"items": self.plugin.storage.get_archives()}

    # ---------------- 修改类 ----------------
    async def update_l0(self):
        data = await self._extract_data()
        rid, val = data.get("id"), data.get("value")
        if not rid or val is None: return {"ok": False, "msg": "缺少参数"}
        conn = sqlite3.connect(self.plugin.storage.db_path)
        try:
            conn.execute("UPDATE l0_profile SET value = ?, updated_at = ? WHERE id = ?", (val, time.time(), rid))
            conn.commit()
            return {"ok": True, "value": val}
        finally:
            conn.close()

    async def simplify_l0(self):
        data = await self._extract_data()
        rid, text = data.get("id"), data.get("value")
        prompt = "请将以下内容精简到30字以内，保留核心事实：" + chr(10) + chr(10) + text
        new_val = await self.plugin._get_llm_response(prompt)
        conn = sqlite3.connect(self.plugin.storage.db_path)
        try:
            conn.execute("UPDATE l0_profile SET value = ?, updated_at = ? WHERE id = ?", (new_val, time.time(), rid))
            conn.commit()
            return {"ok": True, "value": new_val}
        finally:
            conn.close()

    async def summarize_l0(self):
        data = await self._extract_data()
        rid, category, key, old_val = data.get("id"), data.get("category"), data.get("key"), data.get("value")
        conn = sqlite3.connect(self.plugin.storage.db_path)
        conn.row_factory = sqlite3.Row
        try:
            l2_rows = conn.execute("SELECT content FROM l2_events WHERE content LIKE ? ORDER BY created_at DESC LIMIT 5", ("%"+key+"%",)).fetchall()
            l1_rows = conn.execute("SELECT role, content FROM raw_logs WHERE content LIKE ? ORDER BY created_at DESC LIMIT 10", ("%"+key+"%",)).fetchall()
            archive_hits = self.plugin.storage.search_archives(key, limit=10)
            nl = chr(10)
            context = "【L2】" + nl + nl.join([r["content"] for r in l2_rows]) + nl
            context += "【L1】" + nl + nl.join([r["role"] + ": " + r["content"] for r in l1_rows]) + nl
            context += "【归档】" + nl + nl.join(archive_hits)
            prompt = "重新总结键名 '" + key + "'。旧值：" + old_val + nl + "上下文：" + nl + context + nl + nl + "直接输出更新后的值："
            new_val = await self.plugin._get_llm_response(prompt)
            conn.execute("UPDATE l0_profile SET value = ?, updated_at = ? WHERE id = ?", (new_val, time.time(), rid))
            conn.commit()
            return {"ok": True, "value": new_val}
        finally:
            conn.close()

    async def move_l0(self):
        data = await self._extract_data()
        rid, target = data.get("id"), data.get("target")
        if not rid or target not in ('l1', 'l2'): return {"ok": False, "msg": "参数错误"}
        conn = sqlite3.connect(self.plugin.storage.db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT * FROM l0_profile WHERE id = ?", (rid,)).fetchone()
            if not row: return {"ok": False, "msg": "记录不存在"}
            if target == 'l1':
                self.plugin.storage.upsert_l1_working(row['user_id'], row['key'], row['value'])
            else:
                self.plugin.storage.append_l2(row['user_id'], content=f"[{row['key']}] {row['value']}", importance=0.5, sentiment="neutral")
            conn.execute("DELETE FROM l0_profile WHERE id = ?", (rid,))
            conn.commit()
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "msg": str(e)}
        finally:
            conn.close()

    async def del_l0(self):
        data = await self._extract_data()
        rid = data.get("id")
        conn = sqlite3.connect(self.plugin.storage.db_path)
        try:
            conn.execute("DELETE FROM l0_profile WHERE id = ?", (rid,))
            conn.commit()
        finally:
            conn.close()
        return {"ok": True, "deleted": rid}

    async def del_l1(self):
        data = await self._extract_data()
        rid = data.get("id")
        conn = sqlite3.connect(self.plugin.storage.db_path)
        try:
            conn.execute("DELETE FROM l1_working WHERE id = ?", (rid,))
            conn.commit()
        finally:
            conn.close()
        return {"ok": True, "deleted": rid}

    async def del_l2(self):
        data = await self._extract_data()
        rid = data.get("id")
        conn = sqlite3.connect(self.plugin.storage.db_path)
        try:
            conn.execute("DELETE FROM l2_events WHERE id = ?", (rid,))
            conn.commit()
        finally:
            conn.close()
        return {"ok": True, "deleted": rid}
