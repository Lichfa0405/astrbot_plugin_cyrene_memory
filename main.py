from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest, LLMResponse
from astrbot.api.star import Context, Star, register

from .data_reader import LivingMemoryReader
from .storage import CyreneStorage
from .page_api import CyrenePageApi


@register("astrbot_plugin_cyrene_memory", "EGO", "基于 Cyrene Agent 理念的分层记忆插件，只读兼容 LivingMemory", "v0.1.0", "")
class CyreneMemoryPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self.reader = LivingMemoryReader(config)
        self.storage = CyreneStorage()

        inj = config.get("memory_injection", {})
        self.inject_enabled = inj.get("enabled", True)
        self.inject_l0 = inj.get("inject_l0", True)
        self.inject_l2 = inj.get("inject_l2", True)
        self.inject_lm = inj.get("inject_livingmemory", True)
        self.max_chars = inj.get("max_chars", 1200)
        logger.info(f"[Cyrene Memory] 注入: enabled={self.inject_enabled} max_chars={self.max_chars}")

        self._current_query = ""
        self.page_api = None
        if hasattr(self.context, "register_web_api"):
            try:
                self.page_api = CyrenePageApi(self)
                self.page_api.register_routes()
                logger.info("[Cyrene Memory] 官方页面 API 已注册")
            except Exception as e:
                logger.warning(f"[Cyrene Memory] 页面 API 注册失败: {e}")
                self._current_query = ""
        self.page_api = None

    # ---------- 记忆注入钩子 ----------
    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        if not self.inject_enabled:
            return
        if not self._is_private_chat(event):
            return
        user_id = event.get_sender_id()
        if not user_id:
            return

        self._current_query = (event.message_str or "").strip()
        block = self._build_memory_block(user_id)
        if not block:
            return

        try:
            # 优先拼到本轮 prompt 前缀，保护 system_prompt 的缓存
            if hasattr(req, "prompt") and isinstance(req.prompt, str):
                req.prompt = block + "\n\n" + req.prompt
                logger.info(f"[Cyrene Memory] 已注入记忆块 ({len(block)} 字符)")
            else:
                logger.warning("[Cyrene Memory] req.prompt 不可用，跳过注入")
        except Exception as e:
            logger.warning(f"[Cyrene Memory] 注入失败: {e}")


    def _extract_tokens(self, text: str):
        """从当前输入里粗提关键词：去标点、去单字、去重，保留 2 字以上"""
        import re
        if not text:
            return []
        # 中英文混切：中文按字，英文数字按词
        raw = re.findall(r"[a-zA-Z0-9_\-]{2,}|[\u4e00-\u9fa5]{2,}", text)
        stop = {"什么", "怎么", "这个", "那个", "现在", "可以", "今天", "明天", "我们", "你们", "他们", "如何", "是不是"}
        seen, out = set(), []
        for t in raw:
            t = t.strip().lower()
            if t and t not in stop and t not in seen:
                seen.add(t)
                out.append(t)
        return out[:8]

    def _recall_l2(self, user_id: str, query: str):
        """三级降级召回 L2：严格 AND → OR → 近期高重要度"""
        import sqlite3, json
        tokens = self._extract_tokens(query)
        conn = sqlite3.connect(self.storage.db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = []
            # Level 1: 所有 token 都要出现在 content 或 tags 里
            if tokens:
                where = " AND ".join(
                    ["(content LIKE ? OR tags LIKE ?)"] * len(tokens)
                )
                params = []
                for t in tokens:
                    params.extend([f"%{t}%", f"%{t}%"])
                params.append(user_id)
                rows = conn.execute(
                    f"SELECT * FROM l2_events WHERE user_id = ? AND {where} ORDER BY importance DESC LIMIT 5",
                    [user_id] + params[:-1]
                ).fetchall()

            # Level 2: 任一 token 命中
            if not rows and tokens:
                where = " OR ".join(["(content LIKE ? OR tags LIKE ?)"] * len(tokens))
                params = []
                for t in tokens:
                    params.extend([f"%{t}%", f"%{t}%"])
                rows = conn.execute(
                    f"SELECT * FROM l2_events WHERE user_id = ? AND ({where}) ORDER BY importance DESC LIMIT 5",
                    [user_id] + params
                ).fetchall()

            # Level 3: 无命中，退化为最近高重要度
            if not rows:
                rows = conn.execute(
                    "SELECT * FROM l2_events WHERE user_id = ? AND importance >= 0.5 ORDER BY created_at DESC LIMIT 3",
                    (user_id,)
                ).fetchall()

            return [dict(r) for r in rows]
        finally:
            conn.close()

    def _is_private_chat(self, event: AstrMessageEvent) -> bool:
        """只对私聊注入，避免污染群聊"""
        try:
            umo = getattr(event, "unified_msg_origin", "") or ""
            if "GroupMessage" in umo or ":group:" in umo.lower():
                return False
            if "FriendMessage" in umo or "private" in umo.lower():
                return True
        except Exception:
            pass
        try:
            mt = event.get_message_type()
            s = str(mt).lower()
            if "group" in s:
                return False
            if "friend" in s or "private" in s:
                return True
        except Exception:
            pass
        return False

    def _build_memory_block(self, user_id: str) -> str:
        parts = []

        if self.inject_l0:
            l0 = self.storage.get_l0(user_id)
            if l0:
                lines = ["### L0 用户画像"]
                for r in l0:
                    lines.append(f"- [{r['category']}] {r['key']}: {r['value']}")
                parts.append("\n".join(lines))

        if self.inject_l2:
            l2 = self._recall_l2(user_id, self._current_query)
            if l2:
                lines = ["### L2 长期事件（相关）"]
                for r in l2:
                    lines.append(f"- {r['content']}")
                parts.append("\n".join(lines))

        if self.inject_lm:
            try:
                memories = self.reader.read_memories(limit=5, min_importance=0.5)
                if memories:
                    lines = ["### 历史记忆（只读补充）"]
                    for m in memories:
                        tag = " [群聊]" if "global" in m["session_id"] else ""
                        lines.append(f"-{tag} {m['content']}")
                    parts.append("\n".join(lines))
            except Exception as e:
                logger.warning(f"[Cyrene Memory] 读取 LivingMemory 失败: {e}")

        if not parts:
            return ""

        header = (
            "【系统注入的记忆上下文】\n"
            "以下内容是背景参考，帮助你知道对方是谁、在意什么。\n"
            "使用规则：\n"
            "1. 自然地运用这些信息，不要主动复述、不要暴露\"我有一份记忆清单\"。\n"
            "2. 只在与当前对话相关时才使用；不相关就忽略。\n"
            "3. 标记为[群聊]的信息来自群聊场景，不要当作私聊原话引用。\n"
        )
        body = "\n\n".join(parts)
        block = header + "\n" + body + "\n【记忆上下文结束】"

        if len(block) > self.max_chars:
            block = block[: self.max_chars] + "\n【记忆上下文已截断】"
        return block

    # ---------- 命令 ----------
    @filter.command("cyrene_status")
    async def cyrene_status(self, event: AstrMessageEvent):
        compat = self.config.get("livingmemory_compat", {})
        inj = self.config.get("memory_injection", {})
        yield event.plain_result(
            "【Cyrene Memory 状态】\n"
            f"兼容层: {compat.get('enabled', True)}\n"
            f"UMO 数: {len(self.reader.umos)}\n"
            f"别名数: {len(self.reader.aliases)}\n"
            f"群聊模式: {self.reader.group_pattern}\n"
            f"注入启用: {inj.get('enabled', True)}\n"
            f"注入上限: {inj.get('max_chars', 1200)} 字符"
        )

    @filter.command("cyrene_recall")
    async def cyrene_recall(self, event: AstrMessageEvent):
        msg = event.message_str.strip()
        parts = msg.split(maxsplit=1)
        keyword = parts[1].strip() if len(parts) > 1 else ""
        memories = self.reader.read_memories(limit=10, keyword=keyword)
        yield event.plain_result(self.reader.format_for_prompt(memories, keyword))

    @filter.command("cyrene_remember")
    async def cyrene_remember(self, event: AstrMessageEvent):
        msg = event.message_str.strip()
        parts = msg.split(maxsplit=1)
        if len(parts) < 2:
            yield event.plain_result("用法: /cyrene_remember 分类|键|值")
            return
        seg = [s.strip() for s in parts[1].split("|")]
        if len(seg) != 3:
            yield event.plain_result("格式错误，应为: 分类|键|值")
            return
        category, key, value = seg
        user_id = event.get_sender_id()
        self.storage.upsert_l0(user_id, category, key, value)
        yield event.plain_result(f"已写入 L0: [{category}] {key} = {value}")

    @filter.command("cyrene_profile")
    async def cyrene_profile(self, event: AstrMessageEvent):
        user_id = event.get_sender_id()
        rows = self.storage.get_l0(user_id)
        if not rows:
            yield event.plain_result("L0 画像为空。")
            return
        lines = [f"【L0 画像】user={user_id}"]
        for r in rows:
            lines.append(f"- [{r['category']}] {r['key']} = {r['value']}")
        yield event.plain_result("\n".join(lines))

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, response: LLMResponse):
        """每轮 LLM 回复后写入 L1 工作记忆（仅私聊，非命令）"""
        if not self.inject_enabled:
            return
        if not self._is_private_chat(event):
            return

        user_id = event.get_sender_id()
        if not user_id:
            return

        user_text = (event.message_str or "").strip()
        if not user_text or user_text.startswith("/"):
            return

        assistant_text = ""
        try:
            assistant_text = (response.completion_text or "").strip()
        except Exception:
            pass

        try:
            self.storage.append_l1(user_id, "user", user_text)
            if assistant_text:
                self.storage.append_l1(user_id, "assistant", assistant_text)
        except Exception as e:
            logger.warning(f"[Cyrene Memory] L1 写入失败: {e}")

    @filter.command("cyrene_inject_preview")
    async def cyrene_inject_preview(self, event: AstrMessageEvent):
        """预览当前会注入的记忆块"""
        user_id = event.get_sender_id()
        self._current_query = (event.message_str or "").strip()
        block = self._build_memory_block(user_id)
        if not block:
            yield event.plain_result("当前无可注入内容。")
            return
        yield event.plain_result(block)


    @filter.llm_tool(name="cyrene_update_profile")
    async def cyrene_update_profile(self, event: AstrMessageEvent,
                                     category: str, key: str, value: str):
        '''更新"用户本人"的核心画像（L0）。

        注意：这里记录的是【用户自己】的信息，不是机器人的信息。
        如果用户说"以后叫你某某"、"你的名字是某某"，那是描述机器人，不要调用本工具。
        只有当用户明确表达以下关于【他自己】的信息时，才调用：
        - 用户希望被怎么称呼（category=identity, key=用户称呼）
        - 用户的全名或别名（category=identity, key=用户全名）
        - 用户长期偏好或习惯（category=preference, key=具体偏好名）
        - 用户禁忌话题或雷区（category=taboo, key=具体禁忌名）
        - 用户与机器人的关系状态（category=relationship, key=关系）

        不要在闲聊、打招呼、无明确表述时滥用本工具。

        Args:
            category (string): 分类，从 identity/preference/taboo/relationship 中选一个
            key (string): 简短键名，例如 "用户称呼"、"喜欢的食物"、"禁忌话题"
            value (string): 具体值，用简洁的自然语言描述
        '''
        user_id = event.get_sender_id()
        if not user_id:
            yield "无法识别用户，未写入。"
            return
        self.storage.upsert_l0(user_id, category, key, value)
        logger.info(f"[Cyrene Memory] L0 自动写入: user={user_id} [{category}] {key} = {value}")
        yield f"已记录 L0：{category}/{key}"

    @filter.command("cyrene_l0_clear")
    async def cyrene_l0_clear(self, event: AstrMessageEvent):
        """清空当前用户的 L0 画像（仅用于调试）"""
        user_id = event.get_sender_id()
        import sqlite3
        conn = sqlite3.connect(self.storage.db_path)
        try:
            conn.execute("DELETE FROM l0_profile WHERE user_id = ?", (user_id,))
            conn.commit()
            yield event.plain_result(f"已清空 L0: user={user_id}")
        finally:
            conn.close()


    @filter.command("cyrene_webui")
    async def cyrene_webui(self, event: AstrMessageEvent):
        """提示 WebUI 页面访问路径"""
        if not getattr(self, "page_api", None):
            yield event.plain_result("页面 API 未注册（AstrBot 版本不支持或注册失败）。")
            return
        yield event.plain_result(
            "Cyrene Memory 页面已挂载。\n"
            "入口：AstrBot 后台 → 插件 → astrbot_plugin_cyrene_memory → 页面/dashboard\n"
            "API 前缀：/astrbot_plugin_cyrene_memory/page"
        )


    @filter.command("cyrene_l1_clear")
    async def cyrene_l1_clear(self, event: AstrMessageEvent):
        """清空当前用户的 L1（调试用）"""
        import sqlite3
        user_id = event.get_sender_id()
        conn = sqlite3.connect(self.storage.db_path)
        try:
            n = conn.execute("DELETE FROM l1_working WHERE user_id = ?", (user_id,)).rowcount
            conn.commit()
            yield event.plain_result(f"已清空 L1: user={user_id}, 删除 {n} 条")
        finally:
            conn.close()


    @filter.llm_tool(name="cyrene_record_event")
    async def cyrene_record_event(self, event: AstrMessageEvent,
                                   content: str, sentiment: str = "neutral",
                                   tags: str = "", importance: float = 0.5):
        '''记录一条长期事件到 L2（Episodic Memory）。

        只在用户明确提到"发生了某件事"时调用，且该事件有长期价值。
        符合条件：完成了某个任务、开始了新项目、发生了重要变化、情绪波动大、
                  做出了决定、经历了失败或成功。
        不符合条件：日常寒暄、打招呼、聊天气、临时询问、没内容的闲聊。
        不确定时，不要调用。

        Args:
            content (string): 一句话事件描述，客观简洁，如"用户完成了 AstrBot 记忆插件第一版"
            sentiment (string): 情感倾向，从 positive/neutral/negative 中选
            tags (string): 逗号分隔的标签，如"开发,里程碑"，无标签传空字符串
            importance (float): 重要度 0.0-1.0，日常事件 0.3-0.5，重大事件 0.7-1.0
        '''
        user_id = event.get_sender_id()
        if not user_id:
            yield "无法识别用户，未记录。"
            return
        import json
        tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
        senti = sentiment if sentiment in ("positive", "neutral", "negative") else "neutral"
        imp = max(0.0, min(1.0, float(importance)))
        self.storage.append_l2(
            user_id=user_id,
            content=content,
            importance=imp,
            sentiment=senti,
            tags=json.dumps(tag_list, ensure_ascii=False),
        )
        logger.info(f"[Cyrene Memory] L2 写入: user={user_id} [{senti}] imp={imp} {content}")
        yield f"已记录 L2 事件：{content}"

    @filter.command("cyrene_l2_clear")
    async def cyrene_l2_clear(self, event: AstrMessageEvent):
        """清空当前用户的 L2（调试用）"""
        import sqlite3
        user_id = event.get_sender_id()
        conn = sqlite3.connect(self.storage.db_path)
        try:
            n = conn.execute("DELETE FROM l2_events WHERE user_id = ?", (user_id,)).rowcount
            conn.commit()
            yield event.plain_result(f"已清空 L2: user={user_id}, 删除 {n} 条")
        finally:
            conn.close()
