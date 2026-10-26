from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest, LLMResponse
from astrbot.api.star import Context, Star, register

from .data_reader import LivingMemoryReader
from .storage import CyreneStorage
from .embedding_engine import EmbeddingEngine, vector_to_blob, blob_to_vector, cosine_similarity
from .page_api import CyrenePageApi


@register("astrbot_plugin_cyrene_memory", "EGO", "基于 Cyrene Agent 理念的分层记忆插件，只读兼容 LivingMemory", "v0.1.0", "")
class CyreneMemoryPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self.reader = LivingMemoryReader(config)
        self.storage = CyreneStorage()
        self.embedder = EmbeddingEngine(config)

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



    async def _async_embed_and_save(self, record_id: int, record_type: str, text: str):
        vec = await self.embedder.get_embedding(text)
        if vec:
            self.storage.save_embedding(record_id, record_type, vector_to_blob(vec))
            logger.info(f"[Cyrene Memory] 向量化成功: {record_type} ID={record_id}")

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
        """混合召回：关键词 + 向量语义"""
        import sqlite3, json, urllib.request
        tokens = self._extract_tokens(query)
        
        # 1. 向量语义检索
        vec_hits = []
        if getattr(self, 'embedder', None) and self.embedder.enabled and query:
            try:
                payload = json.dumps({"model": self.embedder.model_name, "input": [query]}).encode('utf-8')
                req = urllib.request.Request(self.embedder.api_url, data=payload, headers={'Content-Type': 'application/json'})
                resp = json.loads(urllib.request.urlopen(req, timeout=10).read().decode('utf-8'))
                q_vec = resp["embeddings"][0]
                
                all_embs = self.storage.get_all_embeddings('l2')
                scored = []
                for rid, blob in all_embs.items():
                    sim = cosine_similarity(q_vec, blob_to_vector(blob))
                    if sim > 0.4:
                        scored.append((rid, sim))
                scored.sort(key=lambda x: x[1], reverse=True)
                vec_hits = [x[0] for x in scored[:3]]
            except Exception as e:
                logger.warning(f"[Cyrene Memory] 向量检索失败: {e}")

        conn = sqlite3.connect(self.storage.db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = []
            # 如果向量命中了，优先返回
            if vec_hits:
                ph = ','.join('?' * len(vec_hits))
                rows = conn.execute(f"SELECT * FROM l2_events WHERE id IN ({ph})", vec_hits).fetchall()

            # 如果向量没命中，回退到关键词逻辑
            if not rows:
                # Level 1
                if tokens:
                    where = " AND ".join(["(content LIKE ? OR tags LIKE ?)"] * len(tokens))
                    params = []
                    for t in tokens:
                        params.extend([f"%{t}%", f"%{t}%"])
                    rows = conn.execute(
                        f"SELECT * FROM l2_events WHERE user_id = ? AND {where} ORDER BY importance DESC LIMIT 5",
                        [user_id] + params
                    ).fetchall()

                # Level 2
                if not rows and len(tokens) >= 2:
                    where = " OR ".join(["(content LIKE ? OR tags LIKE ?)"] * len(tokens))
                    params = []
                    for t in tokens:
                        params.extend([f"%{t}%", f"%{t}%"])
                    candidates = conn.execute(
                        f"SELECT * FROM l2_events WHERE user_id = ? AND ({where}) ORDER BY importance DESC LIMIT 20",
                        [user_id] + params
                    ).fetchall()
                    filtered = []
                    for r in candidates:
                        haystack = ((r["content"] or "") + " " + (r["tags"] or "")).lower()
                        hit = sum(1 for t in tokens if t in haystack)
                        if hit >= 2:
                            filtered.append(dict(r))
                    rows = filtered[:5]

                # Level 3
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

        # 注入任务黑板（优先级最高，放在最前面）
        active_tasks = self.storage.get_active_tasks(user_id, limit=3)
        if active_tasks:
            task_lines = ["### 当前挂起的核心任务"]
            for t in active_tasks:
                task_lines.append(f"- [{t['status'].upper()}] {t['title']}：{t['summary']}")
            parts.insert(0, "\n".join(task_lines))

        # 注入关系状态
        rel = self.storage.get_relationship(user_id)
        if rel:
            parts.append(f"### 关系状态\n- 当前阶段：{rel['stage']}\n- 近期氛围：{rel['recent_mood']}")

        if self.inject_l0:
            l0 = self.storage.get_l0(user_id)
            if l0:
                env_lines = ["### 核心基建与环境（常驻事实）"]
                user_lines = ["### L0 用户画像"]
                has_env = False
                has_user = False
                for r in l0:
                    if r['category'] == 'environment':
                        env_lines.append(f"- {r['key']}: {r['value']}")
                        has_env = True
                    else:
                        user_lines.append(f"- [{r['category']}] {r['key']}: {r['value']}")
                        has_user = True
                
                # 环境事实放在最前面，绝对不会漏
                if has_env:
                    parts.insert(0, "\n".join(env_lines))
                if has_user:
                    parts.append("\n".join(user_lines))

        if self.inject_l2:
            l2 = self._recall_l2(user_id, self._current_query)
            if l2:
                lines = ["### L2 长期事件（相关）"]
                for r in l2:
                    note = (r.get("emotion_note") or "").strip().strip("（）()　 ")
                    if not note:
                        senti = r.get("sentiment") or "neutral"
                        if senti == "positive":
                            note = "他当时心情不错"
                        elif senti == "negative":
                            note = "他当时情绪不太好"
                    tag = f"（{note}）" if note else ""
                    lines.append(f"- {r['content']}{tag}")
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
            "【强制记忆规则】在回复的同时，必须审视本轮对话：\n"
            "- 如果用户透露了长期偏好、习惯、身份，【必须调用】cyrene_update_profile。\n"
            "- 如果用户完成了某事、发生了情绪波动，【必须调用】cyrene_record_event。\n"
            "- 如果涉及复杂的排查、开发、配置，或者之前挂起的任务有进展，【必须调用】cyrene_update_task。\n"
            "请静默调用工具，不要在回复中提及。\n"
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
            
            # 检查是否需要触发后台反思
            refl_cfg = self.config.get("reflection", {})
            if refl_cfg.get("enabled", True):
                trigger_count = refl_cfg.get("trigger_count", 15)
                unreflected = self.storage.get_unreflected_l1(user_id, limit=trigger_count + 5)
                if len(unreflected) >= trigger_count:
                    import asyncio
                    asyncio.create_task(self._run_reflection(user_id, unreflected, refl_cfg))
        except Exception as e:
            logger.warning(f"[Cyrene Memory] L1 写入或反思触发失败: {e}")

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
        '''更新"用户本人"的核心画像（L0）或关系状态。
        只要用户的发言中透露了他们的习惯、偏好、技术栈、身份背景，或者关系出现变化，【必须立即调用本工具记录】。
        不要等用户明确说"请记住"，主动提炼信息是你的职责。

        Args:
            category (string): 分类，从 identity/preference/taboo/relationship/environment 中选一个。环境配置（如代理网关、特定路径、服务器设置）必须用 environment。
            key (string): 简短键名，例如 "用户称呼"、"喜欢的食物"、"关系阶段"
            value (string): 具体值，用简洁的自然语言描述
        '''
        user_id = event.get_sender_id()
        if not user_id:
            yield "无法识别用户，未写入。"
            return
        rid = self.storage.upsert_l0(user_id, category, key, value)
        logger.info(f"[Cyrene Memory] L0 自动写入: user={user_id} [{category}] {key} = {value}")
        if rid and getattr(self, 'embedder', None):
            import asyncio
            asyncio.create_task(self._async_embed_and_save(rid, 'l0', f"{key}: {value}"))
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
                                   tags: str = "", importance: str = "0.5",
                                   emotion_note: str = ""):
        '''记录一条长期事件到 L2（Episodic Memory）。
        只要用户提到了【做成了某件事、遇到了挫折、情绪有明显的起伏】，就可以调用。
        重点记录【这件事对人的意味着什么】以及【他当时的感受】，避免纯技术流水账。

        Args:
            content (string): 一句话事件描述，聚焦于"人和故事"，例如"用户终于解决困扰三天的 bug"
            sentiment (string): 情感倾向，从 positive/neutral/negative 中选
            tags (string): 逗号分隔的标签，如"开发,里程碑"
            importance (float): 重要度 0.0-1.0
            emotion_note (string): 用一句话记录"用户当时是什么心情"，带点画面感，20字以内。
        '''
        user_id = event.get_sender_id()
        if not user_id:
            yield "无法识别用户，未记录。"
            return
        import json
        tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
        senti = sentiment if sentiment in ("positive", "neutral", "negative") else "neutral"
        imp = max(0.0, min(1.0, float(importance) if importance else 0.5))
        rid = self.storage.append_l2(
            user_id=user_id,
            content=content,
            importance=imp,
            sentiment=senti,
            tags=json.dumps(tag_list, ensure_ascii=False),
            emotion_note=(emotion_note or "").strip()[:40],
        )
        # 只要有情绪标签，立刻更新近期关系氛围
        if emotion_note:
            self.storage.update_relationship(user_id, recent_mood=emotion_note[:20])
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

    # ================= 反思引擎 =================
    async def _run_reflection(self, user_id: str, messages: list, config: dict):
        """后台异步反思任务"""
        logger.info(f"[Cyrene Memory] 启动后台反思, 用户={user_id}, 消息数={len(messages)}")
        
        text_dump = "\n".join([f"{m['role']}: {m['content']}" for m in messages])
        prompt = f"""请分析以下对话记录，提取值得长期记忆的事实。
严格按照 JSON 格式输出，不要包含 Markdown 代码块以外的任何内容。
格式：
{{
  "l0": [{{"category": "environment/preference/identity", "key": "简短键名", "value": "具体事实"}}],
  "l2": [{{"content": "事件描述", "sentiment": "positive/negative/neutral", "importance": 0.6}}]
}}
如果某类没有内容，返回空数组 []。

对话记录：
{text_dump}
"""
        primary = config.get("primary_provider_id", "")
        fallback = config.get("fallback_provider_id", "")
        max_retries = config.get("max_retries", 3)

        result_text = await self._call_llm_with_retry(prompt, primary, fallback, max_retries)
        if not result_text:
            logger.error("[Cyrene Memory] 反思失败，无法获取模型输出")
            return

        # 解析 JSON 并写入
        import json, re
        try:
            # 提取 JSON 块
            match = re.search(r'\{.*\}', result_text, re.DOTALL)
            if match:
                data = json.loads(match.group(0))
                if "l0" in data:
                    for item in data["l0"]:
                        self.storage.upsert_l0(user_id, item.get("category", "preference"), item.get("key", "未知"), item.get("value", ""))
                if "l2" in data:
                    for item in data["l2"]:
                        self.storage.append_l2(user_id, item.get("content", ""), importance=float(item.get("importance", 0.5)), sentiment=item.get("sentiment", "neutral"))
                logger.info(f"[Cyrene Memory] 反思完成: L0 写入 {len(data.get('l0', []))} 条, L2 写入 {len(data.get('l2', []))} 条")
                
            # 标记已反思
            self.storage.mark_l1_reflected([m['id'] for m in messages])
        except Exception as e:
            logger.error(f"[Cyrene Memory] 解析反思结果失败: {e}, 原始输出: {result_text}")

    async def _call_llm_with_retry(self, prompt: str, primary: str, fallback: str, max_retries: int):
        """带重试和回退的模型调用"""
        import asyncio
        providers = []
        if primary:
            p = self.context.get_provider(primary)
            if p: providers.append(p)
        if fallback:
            p = self.context.get_provider(fallback)
            if p: providers.append(p)
        
        # 如果都没配，用默认
        if not providers:
            p = self.context.get_using_provider()
            if p: providers.append(p)

        for provider in providers:
            for attempt in range(max_retries):
                try:
                    resp = await provider.text_chat(prompt=prompt)
                    if resp and resp.completion_text:
                        return resp.completion_text
                except Exception as e:
                    logger.warning(f"[Cyrene Memory] 反思调用失败 ({provider.meta().id}), 第 {attempt+1} 次重试: {e}")
                    await asyncio.sleep(2)
        return ""

    @filter.llm_tool(name="cyrene_update_task")
    async def cyrene_update_task(self, event: AstrMessageEvent, 
                                 title: str, summary: str, status: str = "active"):
        '''更新或创建一个长线任务（Task Board）。
        
        触发条件：当用户要求进行复杂的、多步骤的排查、写代码、配置环境时，【必须立即调用本工具创建任务】。
        当任务有新的进展、或者遇到阻碍时，更新 summary 并保持 active/paused。
        当任务彻底完成时，将 status 设为 "done"。

        Args:
            title (string): 任务核心标题，如 "配置 MCP 通信"、"排查安卓构建"
            summary (string): 当前最新的进度摘要，包含卡点或关键结论
            status (string): 状态，从 active/paused/done 中选一个
        '''
        user_id = event.get_sender_id()
        if not user_id:
            yield "无法识别用户，未记录。"
            return
        
        self.storage.upsert_task(user_id, title, status, summary)
        logger.info(f"[Cyrene Memory] Task 更新: {title} [{status}]")
        yield f"已更新任务：[{status}] {title}"
