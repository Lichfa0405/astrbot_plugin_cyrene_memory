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
        register(f"{PAGE_API_PREFIX}/raw_logs", self.get_raw_logs, ["GET"], "Cyrene Raw Logs")
        register(f"{PAGE_API_PREFIX}/l2", self.get_l2, ["GET"], "Cyrene L2 list")
        register(f"{PAGE_API_PREFIX}/relationship", self.get_relationship, ["GET"], "Cyrene Relationship")
        register(f"{PAGE_API_PREFIX}/tasks", self.get_tasks, ["GET"], "Cyrene Tasks")
        register(f"{PAGE_API_PREFIX}/l0/delete", self.del_l0, ["POST"], "Cyrene L0 delete")
        register(f"{PAGE_API_PREFIX}/l0/update", self.update_l0, ["POST"], "Cyrene L0 update")
        register(f"{PAGE_API_PREFIX}/l0/simplify", self.simplify_l0, ["POST"], "Cyrene L0 simplify")
        register(f"{PAGE_API_PREFIX}/l0/summarize", self.summarize_l0, ["POST"], "Cyrene L0 summarize")
        register(f"{PAGE_API_PREFIX}/l0/update", self.update_l0, ["POST"], "Cyrene L0 update")
        register(f"{PAGE_API_PREFIX}/l2/update", self.update_l2, ["POST"], "Cyrene L2 update")
        register(f"{PAGE_API_PREFIX}/archives", self.get_archives, ["GET"], "Cyrene Archives")
        register(f"{PAGE_API_PREFIX}/archives/download", self.download_archive, ["GET"], "Cyrene Archive download")
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
            try:
                ntasks = conn.execute("SELECT COUNT(*) FROM tasks WHERE status != 'done'").fetchone()[0]
            except Exception:
                ntasks = 0
            try:
                nraw = conn.execute("SELECT COUNT(*) FROM raw_logs").fetchone()[0]
            except Exception:
                nraw = 0
        finally:
            conn.close()
        return {"l0": n0, "l1": n1, "l2": n2, "rel": nrel, "tasks": ntasks, "raw": nraw}

    async def get_l0(self, *args, **kwargs):
        return {"items": self.plugin.storage.get_l0()}

    async def get_l1(self, *args, **kwargs):
        return {"items": self.plugin.storage.get_l1_working("110016396")}

    async def get_raw_logs(self, *args, **kwargs):
        import sqlite3
        conn = sqlite3.connect(self.plugin.storage.db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT id, user_id, role, substr(content,1,200) AS preview, created_at FROM raw_logs ORDER BY created_at DESC LIMIT 200"
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
                "SELECT id, user_id, content, sentiment, tags, importance, created_at, emotion_note FROM l2_events ORDER BY created_at DESC LIMIT 200"
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

    async def update_l0(self, *args, **kwargs):
        payload = kwargs.get("payload") or (args[0] if args else {})
        rid = payload.get("id")
        val = payload.get("value")
        if not rid: return {"ok": False}
        import sqlite3, time
        conn = sqlite3.connect(self.plugin.storage.db_path)
        try:
            conn.execute("UPDATE l0_profile SET value = ?, updated_at = ? WHERE id = ?", (val, time.time(), rid))
            conn.commit()
            return {"ok": True}
        finally:
            conn.close()

    async def update_l2(self, *args, **kwargs):
        payload = kwargs.get("payload") or (args[0] if args else {})
        rid = payload.get("id")
        val = payload.get("content")
        if not rid: return {"ok": False}
        import sqlite3
        conn = sqlite3.connect(self.plugin.storage.db_path)
        try:
            conn.execute("UPDATE l2_events SET content = ? WHERE id = ?", (val, rid))
            conn.commit()
            return {"ok": True}
        finally:
            conn.close()

    async def get_archives(self, *args, **kwargs):
        return {"items": self.plugin.storage.get_archives()}

    async def download_archive(self, *args, **kwargs):
        from starlette.responses import FileResponse
        import os
        payload = kwargs.get("payload") or (args[0] if args else {})
        filename = payload.get("filename") if isinstance(payload, dict) else None
        
        # 兼容前端 GET 传参
        if not filename and hasattr(self, 'plugin') and hasattr(self.plugin, 'context'):
            pass # 这里由 AstrBot 核心决定怎么接参，如果拿不到，前端就通过 a 标签直接请求静态资源
            
        if not filename:
            return {"ok": False, "msg": "Missing filename"}
            
        archive_dir = os.path.join(os.path.dirname(self.plugin.storage.db_path), "archives")
        file_path = os.path.join(archive_dir, filename)
        
        if not os.path.exists(file_path):
            return {"ok": False, "msg": "File not found"}
            
        return FileResponse(file_path, filename=filename)

    async def update_l0(self, *args, **kwargs):
        payload = kwargs.get("payload") or (args[0] if args else {})
        rid = payload.get("id")
        val = payload.get("value")
        if not rid or val is None: return {"ok": False, "msg": "Missing params"}
        import sqlite3, time
        conn = sqlite3.connect(self.plugin.storage.db_path)
        try:
            conn.execute("UPDATE l0_profile SET value = ?, updated_at = ? WHERE id = ?", (val, time.time(), rid))
            conn.commit()
            return {"ok": True, "value": val}
        finally:
            conn.close()

    async def _get_llm_response(self, prompt: str):
        # 借用 AstrBot 核心的 Provider
        provider = self.plugin.context.get_using_provider()
        if not provider:
            return "错误：未找到可用模型"
        try:
            resp = await provider.text_chat(prompt=prompt)
            return resp.completion_text.strip()
        except Exception as e:
            return f"错误：{str(e)}"

    async def simplify_l0(self, *args, **kwargs):
        payload = kwargs.get("payload") or (args[0] if args else {})
        rid = payload.get("id")
        text = payload.get("value")
        if not rid or not text: return {"ok": False, "msg": "Missing params"}
        
        prompt = f"请将以下用户画像或环境配置精简到 30 字以内，保留核心事实，去除多余的修饰和流水账：\n\n{text}"
        new_val = await self._get_llm_response(prompt)
        
        import sqlite3, time
        conn = sqlite3.connect(self.plugin.storage.db_path)
        try:
            conn.execute("UPDATE l0_profile SET value = ?, updated_at = ? WHERE id = ?", (new_val, time.time(), rid))
            conn.commit()
            return {"ok": True, "value": new_val}
        finally:
            conn.close()

    async def summarize_l0(self, *args, **kwargs):
        payload = kwargs.get("payload") or (args[0] if args else {})
        rid = payload.get("id")
        category = payload.get("category")
        key = payload.get("key")
        old_val = payload.get("value")
        if not rid: return {"ok": False, "msg": "Missing params"}
        
        import sqlite3
        conn = sqlite3.connect(self.plugin.storage.db_path)
        conn.row_factory = sqlite3.Row
        try:
            # 1. 去数据库里找相关的 L2 和最近的 L1
            l2_rows = conn.execute("SELECT content FROM l2_events WHERE content LIKE ? ORDER BY created_at DESC LIMIT 5", (f"%{key}%",)).fetchall()
            l1_rows = conn.execute("SELECT role, content FROM l1_working WHERE content LIKE ? ORDER BY created_at DESC LIMIT 10", (f"%{key}%",)).fetchall()
            
            # 2. 去 Zip 归档里找
            archive_hits = self.plugin.storage.search_archives(key, limit=10)
            
            context = "【相关长期事件(L2)】\n" + "\n".join([r["content"] for r in l2_rows]) + "\n\n"
            context += "【相关近期对话(L1)】\n" + "\n".join([f"{r['role']}: {r['content']}" for r in l1_rows]) + "\n\n"
            context += "【相关历史归档(旧对话)】\n" + "\n".join(archive_hits)
            
            prompt = f"""你的任务是重新总结一条记忆。
当前记忆分类：{category}
当前记忆键名：{key}
当前记忆旧值：{old_val}

请根据以下为你提供的历史检索上下文，重新提炼并更新这个键名对应的值。
要求：保留所有有用的客观事实，修正错误的旧信息，字数不超过 100 字。

{context}

请直接输出更新后的值，不要包含任何解释。"""
            
            new_val = await self._get_llm_response(prompt)
            if new_val.startswith("错误"):
                return {"ok": False, "msg": new_val}
                
            conn.execute("UPDATE l0_profile SET value = ?, updated_at = ? WHERE id = ?", (new_val, time.time(), rid))
            conn.commit()
            return {"ok": True, "value": new_val}
        finally:
            conn.close()
