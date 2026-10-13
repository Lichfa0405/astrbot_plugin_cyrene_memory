import os
import sqlite3

try:
    from astrbot.api import logger
except ImportError:
    import logging
    logger = logging.getLogger("cyrene_memory")
    logging.basicConfig(level=logging.INFO)


class LivingMemoryReader:
    def __init__(self, config: dict):
        compat = config.get("livingmemory_compat", {})
        self.enabled = compat.get("enabled", True)
        self.db_path = compat.get("db_path", "")
        self.umos = self._to_list(compat.get("identity_mappings", []))
        self.aliases = self._to_list(compat.get("text_aliases", []))
        self.group_pattern = compat.get("group_session_pattern", "livingmemory:global")

    def _to_list(self, raw):
        out = []
        for item in raw or []:
            if isinstance(item, str):
                out.extend(x.strip() for x in item.split(",") if x.strip())
            elif isinstance(item, dict):
                for v in item.values():
                    if isinstance(v, str):
                        out.extend(x.strip() for x in v.split(",") if x.strip())
        return out

    def _query(self, conds, limit, min_importance):
        if not conds:
            return []
        sql = f"""
            SELECT a.id, a.content, a.importance, a.created_at,
                   a.session_id, d.text AS summary_text
            FROM memory_atoms a
            LEFT JOIN documents d ON a.parent_memory_id = d.id
            WHERE ({' OR '.join(conds)})
              AND a.status = 'active'
              AND a.importance >= ?
            ORDER BY a.importance DESC, a.created_at DESC
            LIMIT ?
        """
        try:
            conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, (min_importance, limit)).fetchall()
            conn.close()
            cleaned = []
            for r in rows:
                content = (r["content"] or "").strip()
                if not content or content.strip(",") == "":
                    continue
                cleaned.append(dict(r))
            return cleaned
        except Exception as e:
            logger.error(f"[Cyrene Memory] 查询失败: {e}")
            return []

    def read_memories(self, limit: int = 10, min_importance: float = 0.3, keyword: str = ""):
        if not self.enabled:
            return []
        if not self.db_path or not os.path.exists(self.db_path):
            logger.error(f"[Cyrene Memory] 数据库不存在: {self.db_path}")
            return []

        # 语义轨：关键词搜索
        if keyword:
            conds = []
            for kw in keyword.split():
                conds.append(f"a.content LIKE '%{kw}%'")
                conds.append(f"d.text LIKE '%{kw}%'")
            rows = self._query(conds, limit, min_importance)
            return [self._normalize(r, keyword) for r in rows]

        # 私聊轨
        private_conds = [f"a.session_id LIKE '%{u}%'" for u in self.umos]
        private_rows = self._query(private_conds, limit, min_importance)

        # 群聊身份轨
        alias_conds = []
        for a in self.aliases:
            alias_conds.append(f"a.content LIKE '%{a}%'")
            alias_conds.append(f"d.text LIKE '%{a}%'")
        group_rows = []
        if alias_conds:
            group_conds = [
                f"(a.session_id LIKE '%{self.group_pattern}%' AND ({' OR '.join(alias_conds)}))"
            ]
            group_rows = self._query(group_conds, limit, min_importance)

        # 合并去重：私聊优先
        seen, merged = set(), []
        for r in private_rows + group_rows:
            if r["id"] in seen:
                continue
            seen.add(r["id"])
            merged.append(r)
            if len(merged) >= limit:
                break

        cleaned = [r for r in merged if r["content"] and r["content"].strip().strip(",")]
        return [self._normalize(r, "") for r in cleaned]

    def _normalize(self, row, keyword):
        sid = row["session_id"] or "unknown"
        platform, uid = self._parse_session(sid)
        return {
            "id": row["id"],
            "content": row["content"],
            "summary": row["summary_text"],
            "importance": row["importance"],
            "session_id": sid,
            "platform": platform,
            "user_id": uid,
        }

    def _parse_session(self, sid: str):
        if "global" in sid:
            return "群聊", "global"
        parts = sid.split(":")
        if len(parts) >= 4:
            return parts[-2], parts[-1]
        return "unknown", "unknown"

    def format_for_prompt(self, memories: list, keyword: str = ""):
        header = f"【LivingMemory 补充记忆{' | 关键词:' + keyword if keyword else ''}】"
        if not memories:
            return header + " 无相关记录。"
        lines = [header]
        for m in memories:
            tag = " [群聊]" if "global" in m["session_id"] else ""
            lines.append(
                f"- [平台:{m['platform']} | 用户:{m['user_id']}]{tag} {m['content']}"
            )
        return "\n".join(lines)
