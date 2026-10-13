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
