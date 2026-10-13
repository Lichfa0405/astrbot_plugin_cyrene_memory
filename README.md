# Cyrene Memory

> LivingMemory 的**个人向补全插件**。群聊交给 LivingMemory，私聊交给它。

---

## 定位

AstrBot 上已有的记忆插件（如 LivingMemory）走的是**通用社交记忆**路线：按轮次压缩、面向群聊、适配多人共享。但用于**单一用户的深度陪伴**时，它有三个天然短板：压缩丢细节、无分层、无画像。

Cyrene Memory 就是为了补这三个洞而存在。它借鉴 Cyrene Agent 的 DMAE 设计：
- **群聊**继续用 LivingMemory 记录，作为背景信息源
- **私聊**切换到本插件，走**无损分层记忆**
- 本插件**只读** LivingMemory，不写回、不触发向量计算

两套系统各司其职，互不污染。

## 三层记忆设计

| 层级 | 内容 | 生命周期 | 写入方式 |
|---|---|---|---|
| **L0 画像** | 用户称呼、长期偏好、禁忌、关系 | 永久，覆写 | LLM 工具 `cyrene_update_profile` |
| **L1 工作记忆** | 每轮对话原文（user + assistant） | 默认 7 天 TTL | 自动钩子 `on_llm_response` |
| **L2 长期事件** | 独立事件（含情感、标签、重要度） | 长期 | LLM 工具 `cyrene_record_event` |

对比 LivingMemory 的“10~15 轮压缩一次”，本插件**不压缩**。L1 保留原文、L2 按事件存、L0 只覆写不衰减。

## 关键特性

### 1. 保护 prompt 缓存
注入的记忆块**不修改 `system_prompt`**，而是作为本轮 prompt 前缀追加。对 DeepSeek 等支持前缀缓存的模型，命中率保持 100%，成本几乎不增加。

### 2. 双轨只读兼容
读取 LivingMemory 时走三条轨道：私聊轨 `session_id` 片段匹配；群聊轨在群聊 session 中做文本别名匹配；语义轨 `/cyrene_recall 关键词` 直接全文检索。全部纯 SQL，不调 Ollama，不占用本地算力。

### 3. L2 三级降级召回
1. 当前输入的所有关键词都命中 → 注入
2. 任一关键词命中 → 注入
3. 完全无命中 → 退化为“最近高重要度事件”

### 4. 人格工具隔离
配合 AstrBot 的“一人格一工具白名单”机制：私聊人格只勾 `cyrene_*` 工具，群聊人格只勾 LivingMemory 工具。

### 5. WebUI Dashboard
在 AstrBot 后台直接查看三张表，支持单条删除。无需 Node 构建，纯静态 HTML + 原生 JS。

## 安装

把插件目录放入：

`<astrbot-data>/data/plugins/astrbot_plugin_cyrene_memory/`

重启 AstrBot，插件自动加载。首次启动会自动建库：

`<astrbot-data>/data/plugin_data/astrbot_plugin_cyrene_memory/cyrene_memory.db`

依赖：无额外 pip 包，使用 Python 标准库 `sqlite3`。

## 配置

在 AstrBot 后台插件配置页填写 LivingMemory 兼容层与记忆注入配置。主要字段包括 `enabled`、`db_path`、`identity_mappings`、`text_aliases`、`inject_l0`、`inject_l2`、`inject_livingmemory` 和 `max_chars`。

## 命令

| 命令 | 作用 |
|---|---|
| `/cyrene_status` | 查看插件状态与配置摘要 |
| `/cyrene_recall [关键词]` | 从 LivingMemory 召回记忆 |
| `/cyrene_remember 分类\|键\|值` | 手动写一条 L0 画像 |
| `/cyrene_profile` | 查看当前用户的 L0 画像 |
| `/cyrene_inject_preview` | 预览当前会注入的记忆块 |
| `/cyrene_l0_clear` / `/cyrene_l1_clear` / `/cyrene_l2_clear` | 清空对应层（调试用） |
| `/cyrene_webui` | 提示 Dashboard 访问路径 |

## LLM 工具

- `cyrene_update_profile(category, key, value)`：写入 L0 画像，分类限定 `identity` / `preference` / `taboo` / `relationship`。只记录用户本人的信息。
- `cyrene_record_event(content, sentiment, tags, importance)`：写入 L2 长期事件。`sentiment` 为 `positive` / `neutral` / `negative`；`tags` 为逗号分隔；`importance` 范围 0.0–1.0。

## Dashboard

入口：AstrBot 后台 → 插件列表 → `astrbot_plugin_cyrene_memory` → 页面。后端 API 前缀：`/astrbot_plugin_cyrene_memory/page`。

## 目录结构

- `main.py`：插件入口、命令、LLM 工具、钩子
- `data_reader.py`：LivingMemory 只读适配器
- `storage.py`：SQLite 存储层（L0/L1/L2）
- `page_api.py`：Dashboard 后端 API
- `pages/dashboard/index.html`：Dashboard 前端
- `_conf_schema.json`：配置面板定义
- `metadata.yaml`：插件元信息

## 设计原则

1. 所有可变配置走 `_conf_schema.json`
2. 对 LivingMemory 数据库永远以只读模式打开
3. 召回走 SQL，不调用 LLM 或 embedding
4. 人格工具隔离依赖工具白名单
5. 记忆注入不修改 `system_prompt`

## 版本

`v0.1.0` — 首版。实现 L0/L1/L2 闭环、只读兼容、注入、Dashboard。

## 许可

MIT
