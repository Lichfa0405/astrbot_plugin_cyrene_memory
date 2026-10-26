# Changelog

## v0.1.0 — 首版

### 新增
- L0 / L1 / L2 三层记忆存储（SQLite）
- LivingMemory 只读适配器：私聊轨 + 群聊身份轨 + 语义轨
- 记忆注入钩子（保护 prompt 缓存，不改 system_prompt）
- L1 自动写入钩子（`on_llm_response`）
- LLM 工具：`cyrene_update_profile`、`cyrene_record_event`
- L2 三级降级召回（严格 AND → OR → 高重要度兜底）
- Dashboard 单页（纯 HTML + 原生 JS）
- WebUI 配置面板

## v0.3.0 — 架构进化版 (2026-10-26)
### 新增
- **任务状态机 (Task Board)**：长线工程断片终结者，未完成任务强制置顶注入。
- **本地向量语义检索 (P1)**：接入 Ollama qwen3-embedding，支持跨词法盲区召回记忆。
- **后台反思引擎 (Reflection)**：L1 阈值触发，带主备模型切换与重试，将流水账自动提炼为 L0/L2。
- **情感化标签**：L2 事件由模型自主生成 `emotion_note`，让记忆有温度。
- **关系状态演进**：高优事件会自动覆盖当前关系底色，持久化到 `relationship_state` 表。

### 优化
- 强制记忆指令：在注入块头部增加强指令，逼迫模型主动调用记忆工具。
- WebUI Dashboard 升级：增加关系状态与任务看板面板。
- 完美保护 DeepSeek Flash 的 Prompt 前缀缓存。
