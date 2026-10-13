# Cyrene Memory

> LivingMemory 的**个人向补全插件**。群聊交给 LivingMemory，私聊交给它。

---

## 定位

AstrBot 上已有的记忆插件（如 LivingMemory）走的是**通用社交记忆**路线：
按轮次压缩、面向群聊、适配多人共享。但用于**单一用户的深度陪伴**时，
它有三个天然短板：压缩丢细节、无分层、无画像。

Cyrene Memory 就是为了补这三个洞而存在。它借鉴 Cyrene Agent 的 DMAE 设计：

- **群聊** 继续用 LivingMemory 记录，作为背景信息源
- **私聊** 切换到本插件，走**无损分层记忆**
- 本插件**只读** LivingMemory，不写回、不触发向量计算

两套系统各司其职，互不污染。

---

## 三层记忆设计

| 层级 | 内容 | 生命周期 | 写入方式 |
|---|---|---|---|
| **L0 画像** | 用户称呼、长期偏好、禁忌、关系 | 永久，覆写 | LLM 工具 `cyrene_update_profile` |
| **L1 工作记忆** | 每轮对话原文（user + assistant） | 默认 7 天 TTL | 自动钩子 `on_llm_response` |
| **L2 长期事件** | 独立事件（含情感、标签、重要度） | 长期 | LLM 工具 `cyrene_record_event` |

对比 LivingMemory 的"10~15 轮压缩一次"，本插件**不压缩**。
L1 保留原文、L2 按事件存、L0 只覆写不衰减。

---

## 关键特性

### 1. 保护 prompt 缓存
注入的记忆块**不修改 `system_prompt`**，而是作为本轮 prompt 前缀追加。
对 DeepSeek 等支持前缀缓存的模型，命中率保持 100%，成本几乎不增加。

### 2. 双轨只读兼容
读取 LivingMemory 时走三条轨道：

- **私聊轨**：`session_id` 片段匹配（如 `aiocqhttp:123456`）
- **群聊轨**：群聊 session 中做文本别名匹配（如“称呼A / 称呼B”）
- **语义轨**：`/cyrene_recall 关键词` 直接全文检索

全部纯 SQL，不调 Ollama，不占用本地算力。

### 3. L2 三级降级召回
注入 L2 时按需降级，避免噪音：

1. **Level 1**：当前输入的所有关键词都命中 → 注入
2. **Level 2**：任一关键词命中 → 注入
3. **Level 3**：完全无命中 → 退化为“最近高重要度事件”

### 4. 人格工具隔离
配合 AstrBot 的“一人格一工具白名单”机制，实现：

- 私聊人格：只勾 `cyrene_*` 工具
- 群聊人格：只勾 LivingMemory 工具

模型不可能调错，因为工具**物理上不存在**。

### 5. WebUI Dashboard
在 AstrBot 后台直接查看三张表，支持单条删除。
无需 Node 构建，纯静态 HTML + 原生 JS。

---

## 安装

把插件目录放入：

<astrbot-data>/data/plugins/astrbot_plugin_cyrene_memory/

重启 AstrBot，插件自动加载。首次启动会自动建库：

<astrbot-data>/data/plugin_data/astrbot_plugin_cyrene_memory/cyrene_memory.db

依赖：无额外 pip 包，使用 Python 标准库 `sqlite3`。

---

## 配置

在 AstrBot 后台插件配置页填写：

### LivingMemory 兼容层
| 字段 | 说明 |
|---|---|
| `enabled` | 是否启用只读兼容 |
| `db_path` | LivingMemory 的 `livingmemory.db` 绝对路径 |
| `identity_mappings` | session_id 匹配片段列表，每行一个（如 `aiocqhttp:123456`） |
| `text_aliases` | 群聊记忆中用于识别“关于你”的文本别名列表 |
| `group_session_pattern` | 群聊 session_id 匹配模式，默认 `livingmemory:global` |

### 记忆注入
| 字段 | 说明 |
|---|---|
| `enabled` | 总开关 |
| `inject_l0` | 注入 L0 画像 |
| `inject_l2` | 注入 L2 相关事件 |
| `inject_livingmemory` | 注入 LivingMemory 只读记忆 |
| `max_chars` | 注入块最大字符数，默认 1200 |

---

## 命令

| 命令 | 作用 |
|---|---|
| `/cyrene_status` | 查看插件状态与配置摘要 |
| `/cyrene_recall [关键词]` | 从 LivingMemory 召回记忆，可带关键词做语义检索 |
| `/cyrene_remember 分类\|键\|值` | 手动写一条 L0 画像 |
| `/cyrene_profile` | 查看当前用户的 L0 画像 |
| `/cyrene_inject_preview` | 预览当前会注入的记忆块 |
| `/cyrene_l0_clear` / `/cyrene_l1_clear` / `/cyrene_l2_clear` | 清空对应层（调试用） |
| `/cyrene_webui` | 提示 Dashboard 访问路径 |

---

## LLM 工具

以下工具需在对应人格的工具白名单里**手动勾选**，否则模型不会调用。

### `cyrene_update_profile(category, key, value)`
写入 L0 画像。分类限定：
`identity` / `preference` / `taboo` / `relationship`

**只记录用户本人的信息**。用户说“以后叫你某名字”描述的是机器人，不应触发。

### `cyrene_record_event(content, sentiment, tags, importance)`
写入 L2 长期事件。仅在用户明确提到“发生了某事”且该事件有长期价值时调用。

- `sentiment`：`positive` / `neutral` / `negative`
- `tags`：逗号分隔
- `importance`：0.0 ~ 1.0，日常 0.3~0.5，重大 0.7~1.0

---

## Dashboard

入口：AstrBot 后台 → 插件列表 → `astrbot_plugin_cyrene_memory` → 页面

- 顶部显示 L0 / L1 / L2 的条目数
- 三个标签页分别展示三张表
- 每条记录右侧有删除按钮

后端 API 前缀：`/astrbot_plugin_cyrene_memory/page`

---

## 架构

astrbot_plugin_cyrene_memory/
├── main.py 插件入口、命令、LLM 工具、钩子
├── data_reader.py LivingMemory 只读适配器（三轨召回）
├── storage.py 本地 SQLite 存储层（L0/L1/L2）
├── page_api.py Dashboard 后端 API
├── pages/
│ └── dashboard/
│ └── index.html 单文件 Dashboard 前端
├── _conf_schema.json 配置面板定义
├── metadata.yaml 插件元信息
├── requirements.txt 依赖（空）
└── README.md

---

## 设计原则

1. **不写死任何配置**：所有可变项走 `_conf_schema.json`
2. **只读不写回**：对 LivingMemory 数据库永远以 `mode=ro` 打开
3. **不占用额外算力**：所有召回走纯 SQL，不调 LLM、不调 embedding
4. **人格工具物理隔离**：靠工具白名单而非提示词软约束
5. **保护 prompt 缓存**：注入块作为 prompt 前缀，不改 `system_prompt`

---

## 与 LivingMemory 的关系

| 维度 | LivingMemory | Cyrene Memory |
|---|---|---|
| 适用场景 | 群聊、通用社交 | 私聊、单一用户深度陪伴 |
| 记忆分层 | 单层原子 | L0 / L1 / L2 |
| 压缩策略 | 按 N 轮压缩 | 不压缩 |
| 向量检索 | 依赖 embedding | 无（纯 SQL） |
| 写入时机 | 自动总结 | 工具调用 + 钩子 |
| 对另一个的态度 | — | **只读**，永不写入 |

**两插件不是替代关系，而是互补。** 群聊让 LivingMemory 写，私聊让 Cyrene Memory 写；
私聊时 Cyrene Memory 会顺带把群聊里与用户相关的记忆读出来，作为背景补充。

---

## 版本

`v0.1.0` — 首版。实现 L0/L1/L2 闭环、只读兼容、注入、Dashboard。

## 许可

MIT
