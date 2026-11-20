import time
import sqlite3

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest, LLMResponse
from astrbot.api.star import Context, Star, register

from .data_reader import LivingMemoryReader
from .storage import CyreneStorage
from .embedding_engine import EmbeddingEngine, vector_to_blob, blob_to_vector, cosine_similarity
from .page_api import CyrenePageApi


@register("astrbot_plugin_cyrene_memory", "Lichfa0405", "基于 Cyrene Agent 理念的分层记忆插件，只读兼容 LivingMemory", "v1.0.0", "https://github.com/Lichfa0405/astrbot_plugin_cyrene_memory")
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
        block = await self._build_memory_block(user_id)
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

    def _spawn_l1_embed(self, rid, key: str, value: str):
        """发起 L1 向量化，不阻塞调用方；embedder 未启用或不在事件循环里就跳过"""
        if not rid or not getattr(self, 'embedder', None) or not getattr(self.embedder, 'enabled', False):
            return
        import asyncio
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._async_embed_and_save(rid, 'l1', f"{key}: {value}"))

    def _spawn_l2_embed(self, rid, content: str):
        """发起 L2 向量化，不阻塞调用方；embedder 未启用或不在事件循环里就跳过"""
        if not rid or not getattr(self, 'embedder', None) or not getattr(self.embedder, 'enabled', False):
            return
        import asyncio
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._async_embed_and_save(rid, 'l2', content))

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

    @staticmethod
    def _freshness(last_used_at, halflife_days: float) -> float:
        """P3 久不用渐淡：返回 0.30~1.00 的权重系数。

        刻意留 0.3 的地板 —— 再久不用也只是在排序里沉下去，
        权重永不为 0，所以强相关话题出现时它仍然能被召回。
        不做物理删除，只让它安静地待着。
        """
        if not last_used_at:
            return 1.0
        try:
            age_days = max(0.0, (time.time() - float(last_used_at)) / 86400.0)
        except Exception:
            return 1.0
        return 0.30 + 0.70 * (0.5 ** (age_days / float(halflife_days)))

    async def _rank_l0(self, rows, query: str):
        """按与当前 query 的语义相关度给 L0 排序。

        P1 改动：原来 L0 是按 rowid 顺序灌进去的，排在后面的条目永远见不到光。
        现在改成按相关度浮现——与当前话题无关的偏好自然沉下去，
        相关的那几条自己浮上来。无向量条给中位分，避免永久沉底。
        """
        if not rows or not query:
            return rows
        # 极短消息（"好""嗯""继续"）的向量基本是噪声，此时排序反而打乱记忆，退回原序
        if len(query) < 4:
            return rows
        if not (getattr(self, 'embedder', None) and self.embedder.enabled):
            return rows
        try:
            q_vec = await self.embedder.get_embedding(query)
            if not q_vec:
                return rows
            embs = self.storage.get_all_embeddings('l0')
            if not embs:
                return rows
            scored = []
            _miss = 0
            _err = None
            for r in rows:
                rid = r.get('id')
                # 注意：get_all_embeddings 返回的 key 是整数 record_id，
                # 这里必须两种类型都试，否则全部 miss、全部退化成兜底分
                blob = embs.get(rid)
                if blob is None and rid is not None:
                    blob = embs.get(str(rid))
                    if blob is None and str(rid).lstrip('-').isdigit():
                        blob = embs.get(int(rid))
                if blob is None:
                    _miss += 1
                    scored.append((r, 0.45))
                    continue
                try:
                    sim = cosine_similarity(q_vec, blob_to_vector(blob))
                except Exception as _e:
                    _err = _err or f"{type(_e).__name__}: {_e}"
                    sim = 0.45
                scored.append((r, sim))
            scored.sort(key=lambda x: x[1], reverse=True)
            if scored:
                logger.info(f"[Cyrene Memory] L0 相关度排序 top={scored[0][1]:.3f} min={scored[-1][1]:.3f} 共 {len(scored)} 条 | miss={_miss} qdim={len(q_vec)} embs={len(embs)} err={_err}")
            return [r for r, _ in scored]
        except Exception as e:
            logger.warning(f"[Cyrene Memory] L0 相关度排序失败，退回原序: {e}")
            return rows

    async def _recall_l2(self, user_id: str, query: str):
        """混合召回：关键词 + 向量语义"""
        import json, urllib.request
        tokens = self._extract_tokens(query)
        cand_sim = {}

        # 1. 向量语义检索
        vec_hits = []
        if getattr(self, 'embedder', None) and self.embedder.enabled and query:
            try:
                q_vec = await self.embedder.get_embedding(query)
                if not q_vec:
                    raise RuntimeError("query embedding empty")
                
                all_embs = self.storage.get_all_embeddings('l2')
                scored = []
                for rid, blob in all_embs.items():
                    sim = cosine_similarity(q_vec, blob_to_vector(blob))
                    if sim > 0.4:
                        scored.append((rid, sim))
                scored.sort(key=lambda x: x[1], reverse=True)
                # P3：多留几个候选，给渐淡重排腾挪空间
                for rid, sim in scored[:8]:
                    cand_sim[rid] = sim
                vec_hits = list(cand_sim.keys())
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

            # P3：语义相关度 × 新鲜度（L2 半衰期 45 天，比 L1 宽容得多）
            final = [dict(r) for r in rows]
            if final:
                for d in final:
                    base = cand_sim.get(d.get('id'), 0.40)
                    d['_score'] = base * self._freshness(d.get('last_used_at'), 45.0)
                final.sort(key=lambda x: -x['_score'])
                used_ids = [x['id'] for x in final[:5]]
                if used_ids:
                    try:
                        conn.execute(
                            f"UPDATE l2_events SET last_used_at = ?, "
                            f"use_count = COALESCE(use_count, 0) + 1 "
                            f"WHERE id IN ({','.join('?' * len(used_ids))})",
                            [time.time()] + used_ids
                        )
                        conn.commit()
                        logger.info("[Cyrene Memory] L2 渐淡重排 " + " | ".join(
                            f"{str(x.get('content'))[:14]}={x['_score']:.3f}" for x in final[:3]))
                    except Exception as e:
                        logger.warning(f"[Cyrene Memory] L2 使用痕迹回写失败: {e}")
            return final
        finally:
            conn.close()

    async def _recall_l1(self, user_id: str, query: str, top: int = 2):
        """L1 工作记忆召回：向量优先，关键词兜底。工作记忆是过程性事实，阈值放宽到 0.35

        P3 改动：算出候选后不再直接截断，而是用「语义相关度 × 新鲜度」重排。
        久没被用到的条目会往下沉，但权重有 0.3 的地板，永远不会被剔除。
        """
        if not query:
            return []
        tokens = self._extract_tokens(query)
        cand_sim = {}
        if getattr(self, 'embedder', None) and self.embedder.enabled:
            try:
                q_vec = await self.embedder.get_embedding(query)
                if q_vec:
                    all_embs = self.storage.get_all_embeddings('l1')
                    scored = []
                    for rid, blob in all_embs.items():
                        sim = cosine_similarity(q_vec, blob_to_vector(blob))
                        if sim > 0.35:
                            scored.append((rid, sim))
                    scored.sort(key=lambda x: x[1], reverse=True)
                    for rid, sim in scored[:top + 6]:
                        cand_sim[rid] = sim
                    logger.info(f"[Cyrene Memory] L1 召回 向量命中 {len(cand_sim)}/{len(all_embs)} 条")
            except Exception as e:
                logger.warning(f"[Cyrene Memory] L1 向量检索失败: {e}")

        conn = sqlite3.connect(self.storage.db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = []
            if cand_sim:
                ph = ','.join('?' * len(cand_sim))
                rows = conn.execute(
                    f"SELECT id, key, value, last_used_at, use_count FROM l1_working "
                    f"WHERE user_id = ? AND id IN ({ph})",
                    [user_id] + list(cand_sim.keys())
                ).fetchall()
            if not rows and tokens:
                where = " OR ".join(["(key LIKE ? OR value LIKE ?)"] * len(tokens))
                params = []
                for t in tokens:
                    params.extend([f"%{t}%", f"%{t}%"])
                rows = conn.execute(
                    f"SELECT id, key, value, last_used_at, use_count FROM l1_working "
                    f"WHERE user_id = ? AND ({where}) LIMIT ?",
                    [user_id] + params + [top + 4]
                ).fetchall()

            out = []
            for r in rows:
                d = dict(r)
                base = cand_sim.get(d['id'])
                if base is None:
                    base = 0.40
                d['_score'] = base * self._freshness(d.get('last_used_at'), 7.0)
                out.append(d)
            out.sort(key=lambda r: (1 if str(r.get('key', '')).startswith('[L0降级]') else 0,
                                    -r['_score']))
            picked = out[:top]

            used_ids = [r['id'] for r in picked]
            if used_ids:
                now = time.time()
                conn.execute(
                    f"UPDATE l1_working SET last_used_at = ?, use_count = COALESCE(use_count, 0) + 1 "
                    f"WHERE id IN ({','.join('?' * len(used_ids))})",
                    [now] + used_ids
                )
                conn.commit()
                logger.info("[Cyrene Memory] L1 渐淡重排 " + " | ".join(
                    f"{str(x['key'])[:16]}={x['_score']:.3f}" for x in picked))
            return picked
        finally:
            conn.close()


    _SYSTEM_WAKE_MARKERS = (
        "[CronJob]",
        "【重要】结论必须",
        "结论必须用 send_message_to_user",
        # cron note 里的指令关键词：用户不会在聊天里说出工具名，几乎零误伤
        "send_message_to_user",
        "send_message_to_user(",
        "步骤：①",
        "本轮由定时任务",
        "定时任务触发",
        # cron 收尾时 persist_agent_history 写进历史的总结前缀
        "I finished this job, here is the result",
    )

    def _is_cron_event(self, event: AstrMessageEvent) -> bool:
        """按事件来源判断是否为 cron 定时任务唤醒——比猜关键词可靠得多。

        AstrBot 的 cron 会用 CronMessageEvent 构造合成事件：
          · platform_meta.name == "cron"
          · _extras 里带 "cron_job"
        只要命中任一条，这轮就一定不是他说的话，与正文长什么样无关。
        """
        try:
            name = (event.get_platform_name() or "").lower()
            if name == "cron":
                return True
        except Exception:
            pass
        try:
            pm = getattr(event, "platform_meta", None)
            if pm is not None and (getattr(pm, "name", "") or "").lower() == "cron":
                return True
        except Exception:
            pass
        try:
            extras = getattr(event, "_extras", None)
            if isinstance(extras, dict) and "cron_job" in extras:
                return True
        except Exception:
            pass
        return False

    def _is_system_wake(self, text: str) -> bool:
        """识别 cron 定时任务注入的文本。这类内容不是他说的话，
        喂进反思器只会提炼出「用户调整了阈值」这种流水账，必须在入口拦住。"""
        if not text:
            return False
        head = text[:300]
        return any(m in head for m in self._SYSTEM_WAKE_MARKERS)

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

    async def _build_memory_block(self, user_id: str) -> str:
        header = (
            "【系统注入的记忆上下文】\n"
            "以下内容是背景参考，帮助你知道对方是谁、在意什么。\n"
            "使用规则：\n"
            "1. 自然地运用这些信息，不要主动复述、不要暴露\"我有一份记忆清单\"。\n"
            "2. 只在与当前对话相关时才使用；不相关就忽略。\n"
            "3. 标记为[群聊]的信息来自群聊场景，不要当作私聊原话引用。\n"
            "【强制记忆规则】在回复的同时，必须审视本轮对话：\n"
            "- 【L1 冲突检查】：如果发现新的技术事实与上述 L1 冲突（如路径变化），必须使用 cyrene_update_working_memory，填写 old_key 进行覆盖融合！\n"
            "- 如果用户透露了长期偏好、习惯、身份，【必须调用】cyrene_update_profile。\n"
            "- 如果用户完成了某事、发生了情绪波动，【必须调用】cyrene_record_event。\n"
            "- 如果涉及复杂的排查、开发、配置，或者之前挂起的任务有进展，【必须调用】cyrene_update_task。\n"
            "请静默调用工具，不要在回复中提及。\n"
        )
        budget = max(self.max_chars - len(header), 240)
        remaining = [budget]

        def take(lines_in, share):
            """按总预算的 share 比例取材，且不超过剩余预算。返回保留下来的行"""
            quota = min(max(int(budget * share), 80), remaining[0])
            out, used = [], 0
            for ln in lines_in:
                if used + len(ln) + 1 > quota:
                    break
                out.append(ln)
                used += len(ln) + 1
            remaining[0] -= used
            return out

        parts = []

        # 1. 任务黑板（最高优先级，15%）
        active_tasks = self.storage.get_active_tasks(user_id, limit=3)
        if active_tasks:
            ls = ["### 当前挂起的核心任务"]
            for t in active_tasks:
                ls.append(f"- [{t['status'].upper()}] {t['title']}：{t['summary']}")
            kept = take(ls, 0.15)
            if len(kept) > 1:
                parts.append("\n".join(kept))

        # 2. 关系状态（6%）
        rel = self.storage.get_relationship(user_id)
        if rel:
            kept = take([f"### 关系状态\n- 当前阶段：{rel['stage']}\n- 近期氛围：{rel['recent_mood']}"], 0.06)
            if kept:
                parts.append(kept[0])

        # 3. 骨架层：identity 类永远在场，不参与排序（通常极短）
        # 4. 浮现层：其余 L0 按与当前话题的相关度排序后填充
        if self.inject_l0:
            l0 = self.storage.get_l0(user_id)
            if l0:
                ident = [r for r in l0 if r['category'] == 'identity']
                if ident:
                    ls = ["### 关于他（恒定）"]
                    for r in ident:
                        ls.append(f"- {r['key']}: {r['value']}")
                    kept = take(ls, 0.10)
                    if len(kept) > 1:
                        parts.append("\n".join(kept))

                floating = [r for r in l0 if r['category'] != 'identity']
                if floating:
                    ranked = await self._rank_l0(floating, self._current_query)
                    ls = ["### 长期记忆（按与当前话题的相关度浮现）"]
                    for r in ranked:
                        ls.append(f"- [{r['category']}] {r['key']}: {r['value']}")
                    kept = take(ls, 0.38)
                    if len(kept) > 1:
                        parts.append("\n".join(kept))

        # 3.5 L1 工作记忆（12%，向量召回）——这部分原来 100% 只写不读
        if getattr(self, 'inject_l1', True):
            try:
                l1 = await self._recall_l1(user_id, self._current_query, top=2)
                if l1:
                    ls = ["### 工作记忆（最近在做的事）"]
                    for r in l1:
                        ls.append(f"- {r['key']}: {r['value']}")
                    kept = take(ls, 0.12)
                    if len(kept) > 1:
                        parts.append("\n".join(kept))
            except Exception as e:
                logger.warning(f"[Cyrene Memory] L1 召回段失败: {e}")


        # 4. L2 相关事件（20%，最多 3 条）
        if self.inject_l2:
            l2 = await self._recall_l2(user_id, self._current_query)
            if l2:
                ls = ["### L2 长期事件（相关）"]
                for r in l2[:3]:
                    note = (r.get("emotion_note") or "").strip().strip("（）()　 ")
                    if not note:
                        senti = r.get("sentiment") or "neutral"
                        if senti == "positive":
                            note = "他当时心情不错"
                        elif senti == "negative":
                            note = "他当时情绪不太好"
                    tag = f"（{note}）" if note else ""
                    ls.append(f"- {r['content']}{tag}")
                kept = take(ls, 0.20)
                if len(kept) > 1:
                    parts.append("\n".join(kept))

        # 5. LivingMemory 只读补充（7%）
        if self.inject_lm:
            try:
                memories = self.reader.read_memories(limit=5, min_importance=0.5)
                if memories:
                    ls = ["### 历史记忆（只读补充）"]
                    for m in memories:
                        tag = " [群聊]" if "global" in m["session_id"] else ""
                        ls.append(f"-{tag} {m['content']}")
                    kept = take(ls, 0.07)
                    if len(kept) > 1:
                        parts.append("\n".join(kept))
            except Exception as e:
                logger.warning(f"[Cyrene Memory] 读取 LivingMemory 失败: {e}")

        if not parts:
            return ""

        body = "\n\n".join(parts)
        block = header + "\n" + body + "\n【记忆上下文结束】"
        if len(block) > self.max_chars:
            block = block[: self.max_chars] + "\n【记忆上下文已截断】"
        logger.info(f"[Cyrene Memory] 注入块 {len(block)}/{self.max_chars} 字符，剩余预算 {remaining[0]}")
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

        # 系统唤醒轮次（cron 定时任务）不是真实对话：不写原料，也不触发反思
        if (self._is_cron_event(event)
                or self._is_system_wake(user_text)
                or self._is_system_wake(assistant_text)):
            logger.info("[Cyrene Memory] 跳过系统唤醒轮次（来源=cron 或 命中唤醒标记），不写入反思原料")
            return

        try:
            self.storage.append_raw_log(user_id, "user", user_text)
            if assistant_text:
                self.storage.append_raw_log(user_id, "assistant", assistant_text)
            
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
        block = await self._build_memory_block(user_id)
        if not block:
            yield event.plain_result("当前无可注入内容。")
            return
        yield event.plain_result(block)


    @filter.llm_tool(name="cyrene_update_profile")
    async def cyrene_update_profile(self, event: AstrMessageEvent,
                                     category: str, key: str, value: str):
        '''更新"用户本人"的核心画像（L0）或关系状态。
        你的职责是提炼【长期的、静态的、未来每次对话都可能用到的事实】，作为常驻背景。

        **分类指南**：
        1. identity/preference/taboo/relationship：记录用户的性格、称呼、禁忌、关系状态。
        2. environment：**工程基建字典**。记录项目的绝对路径、容器的名称、IP 地址、端口号、API Key 等环境事实。

        **严格约束**：
        - **单条 value 必须控制在 40 字以内**（硬限制）。超长内容会被自动降级到 L1 工作记忆，无法进入 L0。需要写长内容时，改用 cyrene_update_working_memory。
        - environment 必须写成极其简练的【键值对】事实。
        - 绝不要把流水账写进 L0，只提取不变的客观事实。
        - **如何修正旧事实**：当你发现旧事实发生变更（如 IP 变了、路径改了），**必须使用完全相同的 key 再次调用本工具**。系统底层会自动覆盖旧值，不要新建一个似是而非的 key。

        Args:
            category (string): 分类，从 identity/preference/taboo/relationship/environment 中选一个
            key (string): 简短键名，例如 "用户称呼"、"网关IP"、"C项目路径"
            value (string): 具体值，用简洁的自然语言描述
        '''
        user_id = event.get_sender_id()
        if not user_id:
            yield "无法识别用户，未写入。"
            return

        value = (value or "").strip()
        if not value:
            yield "值为空，未写入。"
            return
        # 硬性守卫：L0 只装静态短事实，超长内容一律降级到 L1，防止分级模糊
        LIMITS = {"environment": 120, "identity": 300, "preference": 300,
                  "relationship": 300, "taboo": 200}
        reason = None
        if category not in LIMITS:
            reason = f"未知类目 {category}"
        elif value.count("\n") >= 2:
            reason = "含多段落（L1 特征）"
        elif len(value) > LIMITS[category]:
            reason = f"{len(value)}字 超过 {category} 上限 {LIMITS[category]}"

        if reason:
            _rid = self.storage.upsert_l1_working(user_id, f"[L0降级]{key}", value)
            self._spawn_l1_embed(_rid, f"[L0降级]{key}", value)
            logger.info(f"[Cyrene Memory] L0 拒收({reason})，降级至 L1: {key}")
            yield f"超出 L0 画像分级标准（{reason}），已存入 L1 工作记忆：{key}"
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


    async def _find_duplicate_l2(self, user_id: str, content: str, precomputed_vec=None):
        """写入前查重：返回相似度最高的既有 L2 条目（仅当超过阈值 0.80）。

        阈值依据（qwen3-embedding:0.6b 实测）：
          同事件不同表述   0.849 / 0.700
          不同事件同领域   上限 0.737
          无关内容         0.34 ~ 0.37
        0.80 落在 0.737~0.849 安全区中央，既抓得住换说法的重复，
        又不会把相邻事件误合并。
        """
        if not self.embedder.enabled:
            return None
        vec = precomputed_vec or await self.embedder.get_embedding(content)
        if not vec:
            return None
        conn = sqlite3.connect(self.storage.db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("""
                SELECT e.record_id AS rid, e.vector AS vec, l.content AS content
                  FROM embeddings e
                  JOIN l2_events l ON l.id = e.record_id
                 WHERE e.record_type = 'l2' AND l.user_id = ?
            """, (user_id,)).fetchall()
        finally:
            conn.close()
        best = None
        for r in rows:
            s = cosine_similarity(blob_to_vector(r["vec"]), vec)
            if best is None or s > best["score"]:
                best = {"id": r["rid"], "content": r["content"], "score": s}
        if best and best["score"] >= 0.80:
            return best
        return None

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

        # ---- 语义查重：命中则并入既有条目，不再新增 ----
        try:
            dup = await self._find_duplicate_l2(user_id, content)
        except Exception as e:
            logger.warning(f"[Cyrene Memory] L2 查重异常，按新增处理: {e}")
            dup = None
        if dup:
            self.storage.merge_l2_duplicate(dup["id"], imp)
            logger.info(
                f"[Cyrene Memory] L2 去重合并 score={dup['score']:.3f} 并入#{dup['id']} "
                f"| 新: {content[:36]} | 原: {dup['content'][:36]}"
            )
            if emotion_note:
                self.storage.update_relationship(user_id, recent_mood=emotion_note[:20])
            yield f"已并入既有记忆（相似度 {dup['score']:.2f}）：{dup['content'][:40]}"
            return

        rid = self.storage.append_l2(
            user_id=user_id,
            content=content,
            importance=imp,
            sentiment=senti,
            tags=json.dumps(tag_list, ensure_ascii=False),
            emotion_note=(emotion_note or "").strip()[:40],
        )
        # 新写入的 L2 必须拿到向量，否则下次查重永远抓不到它
        self._spawn_l2_embed(rid, content)
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
        # 把现有 L0 键名喂给模型：否则它看不见已有事实，必然重复写入
        try:
            _existing = self.storage.get_l0(user_id)
            _keys = sorted({r['key'] for r in _existing}) if _existing else []
        except Exception:
            _keys = []
        _key_block = "\n".join(f"- {k}" for k in _keys) if _keys else "（暂无）"
        prompt = f"""你在维护一份「长期人格记忆」。你要判断的不是「这段对话发生了什么」，而是：这么多条消息里，有没有哪一句，能让人在一年后更懂这个人。

【最重要的一条：默认什么都不写】
l0 默认返回 []。只有当对话里出现了明确的新信息——用户说了自己喜欢什么、做什么工作、有什么习惯、在意什么——才写。
凑数是这里最严重的错误。写进一条假的，比漏掉十条真的糟糕一百倍。绝大多数对话都应该返回空数组，这是正常结果，不是你失败了。

【绝对不写进 l0】
x 系统参数、阈值、字数、条数、端口、版本号、路径 —— 这些是配置，不是人
x 「L0机制充当流水账回收站」这类对工具的评论或吐槽
x 进度、结果、完成情况、本次改了什么
x 你和用户一起干的活本身
x assistant（昔涟）提出的方案、规则、承诺、代码设计 —— 哪怕再好，那也不是关于「他」的事实。只看 user 说了什么。

【可以写进 l0】
v 稳定习惯：习惯先备份再动手 / 偏好实用够用即止
v 身份与环境：大二电子信息学院 / WSL 测试机在 100.64.0.2
v 偏好与忌讳：渲染图一律浅色主题 / 讨厌被反复追问

【关于 l2 —— 记人，不记活】
l2 记的是「这个人身上发生了什么」，不是「你们一起把什么事做完了」。

动笔前先过一道口语检查：
把你写的这句话，换成一个朋友转述给他听，会不会显得很怪？
  不会说：「我朋友提出了重启前任务管理习惯」——这话不像人说的
  会说：「我朋友现在每次重启前，都先设个闹钟叫自己醒」
写到能像后者这样自然讲出口，才算数。

【l2 四条禁令】
x 空洞概括：「考虑调整作息」「状态不太好」「有进步」——没有具体的事，等于没写
x 动作转述：「用户提出了 X」「用户调整了 Y」「完成了 Z」——这是在写会议记录，不是在记人
x 只在这次成立的事：临时决定、一次性操作
x 你做的事：「帮他改好了配置」「给他生成了图」

【l2 怎么写才立得住】至少占住一样：
v 具体的锚：数字、时间、地点、他的原话（「设了 22:30 的闹钟」「跑到第 41 次」）
v 反差或矛盾：明明达标了还去跑 / 嘴上说随便，实际很在意
v 一个关于他的推断：这件事说明他是什么样的人

【对照】
好：「跑满 41 次还去操场——把任务过成了习惯」
好：「早八第一节全程划水，第二节才听明白，课后说要改作息」
差：「用户提出了重启前任务管理习惯」（动作转述）
差：「考虑调整作息」（空洞，等于没写）
差：「插件 bug 修复，报错清零」（这是活，不是人）

【importance 怎么给】
0.9 以上：人生转折、长期关系的建立或破裂
0.7-0.8：能改变你怎么看他的一件事
0.5-0.6：有画面但不算转折的日常
0.3-0.4：琐碎，但至少有具体内容
写不出具体内容就不要写，绝不用 0.5 凑数。

l2 默认返回 []。一条写得好的，胜过三条凑数的。

【判断三问】三条全为「是」才写 l0：
1. 一年后还成立吗？
2. 说的是这个人，还是这次这件事？
3. 跟当前话题无关时，它还有用吗？

【已有长期事实的键名】（不要重复这些；确需修正时用完全相同的 key 覆盖）
{_key_block}

【上限】
l0 最多 2 条，默认 0 条。l2 最多 3 条，默认 0 条。value 控制在 25 汉字以内。

严格按 JSON 输出，不要包 Markdown 代码块。
{{
  "l0": [{{"category": "environment/preference/identity", "key": "简短键名", "value": "具体事实"}}],
  "l2": [{{"content": "事件描述", "sentiment": "positive/negative/neutral", "importance": 0.6}}]
}}
没有内容就返回空数组 [].

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
                    for item in data["l0"][:3]:
                        _k = item.get("key", "未知")
                        _v = item.get("value", "")
                        _c = item.get("category", "preference")
                        # category 白名单：只认四类。脏值（如 "preference/quality"）先尝试归一化，
                        # 归不了的直接丢弃——宁可不写，也不能让非法类别污染 L0。
                        if _c not in ("environment", "preference", "identity", "relationship"):
                            _low = str(_c).lower()
                            if "pref" in _low:
                                _c = "preference"
                            elif "ident" in _low:
                                _c = "identity"
                            elif "env" in _low:
                                _c = "environment"
                            elif "relat" in _low:
                                _c = "relationship"
                            else:
                                logger.info(f"[反思] 非法 category({_c})，已丢弃: {_k}")
                                continue
                        # 双保险：prompt 之外再兜一层，拦掉明显的操作流水账
                        if re.search(r'(结果|本次|这次|进度|覆盖率|总结|完成情况)', _k) or \
                           re.search(r'覆盖率\s*\d+\s*%|\d+\s*条\s*/?\s*\d+\s*字', _v):
                            logger.info(f"[反思] 疑似流水账，已丢弃: {_k} = {_v[:20]}")
                            continue
                        # 硬性拦截：超过 40 字属于工作记忆，不配进 L0，自动降级到 L1
                        if len(_v) > 40:
                            _rid = self.storage.upsert_l1_working(user_id, _k, _v)
                            self._spawn_l1_embed(_rid, _k, _v)
                            logger.info(f"[反思] 拦截过长 L0，已自动降级至 L1: {_k}")
                        else:
                            self.storage.upsert_l0(user_id, _c, _k, _v)
                if "l2" in data:
                    _pending_l2 = []   # 本批已写入条目，用于批内自去重（它们尚未入库，查不到）
                    for item in data["l2"][:5]:
                        _c2 = (item.get("content") or "").strip()
                        if not _c2:
                            continue
                        try:
                            _imp2 = max(0.0, min(1.0, float(item.get("importance", 0.5))))
                        except Exception:
                            _imp2 = 0.5
                        # 向量只算一次：查重、入库、批内比对都用它
                        _vec2 = None
                        if getattr(self, 'embedder', None) and getattr(self.embedder, 'enabled', False):
                            try:
                                _vec2 = await self.embedder.get_embedding(_c2)
                            except Exception as e:
                                logger.warning(f"[Cyrene Memory] 反思 L2 向量化失败: {e}")
                        _dup2 = None
                        if _vec2:
                            # ① 与库中既有 L2 比对
                            try:
                                _dup2 = await self._find_duplicate_l2(user_id, _c2, precomputed_vec=_vec2)
                            except Exception as e:
                                logger.warning(f"[Cyrene Memory] 反思 L2 查重异常，按新增处理: {e}")
                            # ② 与本批已接受的条目比对
                            if not _dup2 and _pending_l2:
                                for _p in _pending_l2:
                                    _s = cosine_similarity(_p["vec"], _vec2)
                                    if _s >= 0.80:
                                        _dup2 = {"id": _p["id"], "content": _p["content"], "score": _s}
                                        break
                        if _dup2:
                            self.storage.merge_l2_duplicate(_dup2["id"], _imp2)
                            logger.info(
                                f"[Cyrene Memory] L2 去重合并 score={_dup2['score']:.3f} 并入#{_dup2['id']} "
                                f"| 来源=反思 | 新: {_c2[:36]} | 原: {_dup2['content'][:36]}"
                            )
                            continue
                        _rid2 = self.storage.append_l2(
                            user_id, _c2,
                            importance=_imp2,
                            sentiment=item.get("sentiment", "neutral")
                        )
                        if _rid2:
                            if _vec2:
                                self.storage.save_embedding(_rid2, 'l2', vector_to_blob(_vec2))
                                _pending_l2.append({"id": _rid2, "content": _c2, "vec": _vec2})
                            else:
                                self._spawn_l2_embed(_rid2, _c2)
                logger.info(f"[Cyrene Memory] 反思完成: L0 写入 {len(data.get('l0', []))} 条, L2 写入 {len(data.get('l2', []))} 条")
                
            # 标记已反思
            self.storage.mark_l1_reflected([m['id'] for m in messages])
            
            # 归档清理逻辑
            import sqlite3, json, os, zipfile, time
            conn = sqlite3.connect(self.storage.db_path)
            conn.row_factory = sqlite3.Row
            try:
                # l1_working 表没有 is_reflected 列（只有 id/user_id/key/value/updated_at），
                # 原 SQL 必然抛 no such column 并被外层吞掉。改为按活跃度归档：
                # 超过 7 天未更新、且已跌出最近 50 条的条目才切片打包。
                cutoff = time.time() - 7 * 86400
                rows = conn.execute("""
                    SELECT * FROM l1_working 
                    WHERE updated_at < ?
                    AND id NOT IN (SELECT id FROM l1_working ORDER BY updated_at DESC LIMIT 50)
                """, (cutoff,)).fetchall()
                
                if rows:
                    archive_dir = os.path.join(os.path.dirname(self.storage.db_path), "archives")
                    os.makedirs(archive_dir, exist_ok=True)
                    date_str = time.strftime("%Y%m%d_%H%M%S")
                    jsonl_path = os.path.join(archive_dir, f"l1_archive_{date_str}.jsonl")
                    zip_path = os.path.join(archive_dir, f"l1_archive_{date_str}.zip")
                    
                    with open(jsonl_path, "w", encoding="utf-8") as f_json:
                        for r in rows:
                            f_json.write(json.dumps(dict(r), ensure_ascii=False) + "\n")
                    
                    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                        zipf.write(jsonl_path, os.path.basename(jsonl_path))
                    
                    os.remove(jsonl_path)
                    
                    ids_to_delete = [str(r["id"]) for r in rows]
                    conn.execute(f"DELETE FROM l1_working WHERE id IN ({','.join(ids_to_delete)})")
                    conn.commit()
                    logger.info(f"[Cyrene Memory] 已归档并清理 {len(rows)} 条 L1 数据至 {zip_path}")
            except Exception as e:
                logger.warning(f"[Cyrene Memory] L1 归档失败: {e}")
            finally:
                conn.close()
        except Exception as e:
            logger.error(f"[Cyrene Memory] 解析反思结果失败: {e}, 原始输出: {result_text}")

    async def _call_llm_with_retry(self, prompt: str, primary: str, fallback: str, max_retries: int):
        """带重试和回退的模型调用。
        限流 / 超时 / 服务端不可用属于不可恢复错误，立即切下一个 provider，不做无谓等待。
        """
        import asyncio
        providers = []
        if primary:
            p = self.context.get_provider_by_id(primary)
            if p: providers.append(p)
        if fallback:
            p = self.context.get_provider_by_id(fallback)
            if p: providers.append(p)

        # 如果都没配，用默认
        if not providers:
            p = self.context.get_using_provider()
            if p: providers.append(p)

        CALL_TIMEOUT = 90  # 单次调用硬超时（秒）

        def _hopeless(msg: str) -> bool:
            """这些错误重试无意义，直接换 provider"""
            m = msg.lower()
            keys = ('429', 'rate limit', 'ratelimit', 'too many requests', 'quota',
                    'timeout', 'timed out', 'overloaded', '502', '503', '504',
                    '401', '403', 'insufficient')
            return any(k in m for k in keys)

        for provider in providers:
            try:
                pid = provider.meta().id
            except Exception:
                pid = "unknown"
            for attempt in range(max_retries):
                try:
                    resp = await asyncio.wait_for(provider.text_chat(prompt=prompt), timeout=CALL_TIMEOUT)
                    if resp and resp.completion_text:
                        return resp.completion_text
                    logger.warning(f"[Cyrene Memory] {pid} 返回空内容，切换下一个候选")
                    break
                except asyncio.TimeoutError:
                    logger.warning(f"[Cyrene Memory] {pid} 调用超时 {CALL_TIMEOUT}s，立即切换候选")
                    break
                except Exception as e:
                    msg = str(e)
                    if _hopeless(msg):
                        logger.warning(f"[Cyrene Memory] {pid} 限流/不可用，立即切换候选: {msg[:100]}")
                        break
                    logger.warning(f"[Cyrene Memory] {pid} 调用失败 (第 {attempt+1}/{max_retries} 次): {msg[:100]}")
                    if attempt < max_retries - 1:
                        await asyncio.sleep(1)
        logger.error("[Cyrene Memory] 所有候选模型均失败，本次反思跳过")
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

    @filter.llm_tool(name="cyrene_update_working_memory")
    async def cyrene_update_working_memory(self, event: AstrMessageEvent, key: str, value: str, old_key: str = ""):
        '''更新当前的工作记忆（L1）。
        
        当用户在讨论具体的技术排查、项目文件路径、正在修改的代码、临时配置时，【必须调用本工具记录工作状态】。

        **重要纠错功能**：
        如果你在对话中发现新事实与旧的记忆发生了冲突（比如路径变了、IP 换了），请使用融合逻辑：
        1. 提取旧记忆中有用的其他信息，与新信息合并。
        2. 将合并后的最新完整事实填入 `value`。
        3. 将过时的旧记忆的键名填入 `old_key`。系统会自动将旧条目删除，避免冲突。
        4. 如果没有冲突，或者只是新增，请将 `old_key` 留空。

        Args:
            key (string): 新记忆的键名，如 "C项目路径"、"当前报错"
            value (string): 融合了新旧信息后最准确的值
            old_key (string): 需要被覆盖/删除的旧冲突键名（如果没有冲突则不填）
        '''
        user_id = event.get_sender_id()
        if not user_id:
            yield "无法识别用户。"
            return
            
        # 如果传了 old_key，且与 key 不同，执行融合纠错（删掉旧条目）
        if old_key and old_key.strip() != "" and old_key != key:
            self.storage.delete_l1_working_by_key(user_id, old_key.strip())
            logger.info(f"[Cyrene Memory] L1 融合纠错: 已删除过时记忆 {old_key}")
            
        _rid = self.storage.upsert_l1_working(user_id, key, value)
        self._spawn_l1_embed(_rid, key, value)
        logger.info(f"[Cyrene Memory] L1 工作记忆写入: {key} = {value}")
        yield f"已更新工作记忆：{key}"
