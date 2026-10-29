# BBR的记忆插件 v1.0.5

一个给 AstrBot 用的分层长期记忆插件：**每天归档 → 按话题提炼 → 原子记忆项 → 7/30 天迁移 → 检索/到期注入**。

v1.0.5 在 v0.1.2 的基础上：

- 提醒相关操作改为**模型工具调用**，不再靠正则和硬编码话术；
- 不主动注入 pending items，模型需要时调用 `memory_reminder_list`；
- 归档条目仍然可检索，只是不再主动注入；
- 新增增量记忆整理、`--rebuild`、`/mem reschedule`。

---

## 1. 它怎么工作

### 1.1 每天

1. 消息产生时就写入 `messages`（原文、时间、发送者、稳定 UID）。
2. 到达记忆整理时间且**最近 N 分钟没有消息**时，按 `(日期, 会话)` 提炼：
   - LLM 按话题切分；
   - 每个话题生成 30% 检索摘要、70% 周摘要、关键词、重要度 1–300；
   - 同时输出 `items`：提醒、待办、事实、偏好、事件、episode；
   - 写入 `topics` 和 `memory_items`。
3. **7 天**后，未过期的周记忆从**原始消息重新生成**月摘要（约 70%），改 `layer='month'`；**30 天**后同理进 `year`。
4. 归档（`expired`）条目仍可检索，只是不再注入；只有 `/mem forget` 才不可见。
5. 失败日期保留 job，下一次记忆整理重试；迁移包按 `(day, umo, stage)` 幂等。

### 1.2 记忆整理时间 + 空闲

- 默认 `04:00`（`Asia/Shanghai`）。
- 真正执行要求：**到点 + 最近 `idle_minutes` 分钟无消息**。
- 到点后一直不空闲：超过 `force_after_hours` 强制跑；`0` 表示不强跑。
- 启动后如果发现更早的日期没处理，会在空闲时补跑，不必等第二天。
- 手动 `/mem consolidate [日期]` 跳过空闲判定。

### 1.3 回答前

- 在 `on_llm_request` 里检索，**不整表读**。
- 普通请求取 top 4；命中“记得/上次/之前/我说过/继续”等取 top 8。
- 排序 = 相关度 + 新近度 + 重要度；同一天最多 2 条；低于门槛不注入。
- 预算默认 1200 字，带日期、层级、重要度、关键词、原话指针。
- 到期队列独立：`due_window_hours` 内到期的 reminder/task 先注入，不参与语义打分。
- 归档条目可 `/mem search`，但不会注入。
- **被动注入只计数，不续期**；只有显式 `memory_search_v01` / `memory_open_v01` 或 `/mem open` 才续期。
- 同一条 7 天内不重复被动注入（可配）。

### 1.4 items 生命周期

| 类型 | 注入到 | 检索 |
|---|---|---|
| reminder 未完成 | due + 1 天 | 永久 |
| reminder 已完成 | done 时刻 | done + 30 天 |
| task | 14 天 | 永久 |
| event | 30 天 | 永久 |
| preference / fact | 180 / 365 天 | 永久 |
| episode | 30–90 天 | 永久 |

`importance` 只参与排序，不再直接决定寿命。

---

## 2. 安装

1. 把整个 `astrbot_plugin_bbrsmemory_plugin` 目录复制到 AstrBot 的 `data/plugins/` 下。
2. 确认目录名是 `astrbot_plugin_bbrsmemory_plugin`（不能有空格/点）。
3. 安装依赖：

   ```bash
   pip install -r requirements.txt
   ```

4. 重启 AstrBot，在 WebUI 插件页确认加载；配置在插件配置里。
5. 数据在 `AstrBot/data/plugin_data/astrbot_plugin_bbrsmemory_plugin/`：
   - `memory.db`：SQLite（WAL）
   - `exports/`：迁移包
   - `media/`：v0.2 用，v0.1 为空

> 目录名必须是 `astrbot_plugin_bbrsmemory_plugin`，否则 `StarTools.get_data_dir()` 和插件名会对不上。

---

## 3. 配置（`_conf_schema.json`）

| 配置 | 默认 | 说明 |
|---|---|---|
| `enabled` | true | 总开关 |
| `timezone` | Asia/Shanghai | 判断“哪一天”和记忆整理时间 |
| `run_at` | 04:00 | 每日记忆整理时间 |
| `idle_minutes` | 30 | 空闲多久才跑；0 不等待 |
| `force_after_hours` | 6 | 到点后最多等多久就强制跑 |
| `summary_provider_id` | 空 | 提炼用模型；空 = 当前会话模型 |
| `importance_min/max` | 1/300 | 重要度范围 |
| `ttl_unit_days` | 1.0 | 遗忘天数 = 重要度 × 本值 |
| `ttl_min_days` / `ttl_max_days` | 2 / 365 | 遗忘天数截断 |
| `promote_week_days` | 7 | 周→月 |
| `promote_month_days` | 30 | 月→年 |
| `max_promotions_per_run` | 20 | 单次迁移上限，控制成本 |
| `retrieve_top_k` / `deep_top_k` | 4 / 8 | 注入条数 |
| `inject_budget_chars` | 1200 | 注入预算 |
| `suppress_days` | 7 | 被动注入后的静默期 |
| `renew_factor` / `max_renewals` | 1.3 / 3 | 显式回忆续期 |
| `expired_retention_days` | 30 | 已遗忘内容的保留期；归档不受影响 |
| `admin_only` | true | 管理命令仅管理员 |
| `reminder_backend` | auto | auto / memory / future_task |
| `scope_resolver` | scope_adapter.py | 自定义平台 scope 适配器文件 |
| `due_window_hours` | 24 | 到期队列提前多久开始注入 |
| `due_max_per_turn` | 3 | 每轮最多注入几条到期项 |
| `due_budget_chars` | 300 | 到期提醒字符预算 |
| `reminder_repeat_hours` | 6 | 同一提醒重复注入间隔 |
| `media_enabled` | false | v0.2 才实现 |
| `wait_for_media` | false | v0.1 不要开；否则 pending 附件会让当天一直等待 |

---

## 4. 命令

```text
/mem                         帮助
/mem stats                   统计
/mem whoami                  当前会话的 scope / 是否绑定
/mem scopes                  列出所有 scope（管理员）
/mem bind <scope_id>         把当前会话绑到某段记忆（管理员）
/mem unbind                  取消绑定
/mem alias list|set|del      管理群聊 scope 映射（管理员）
/mem merge <from> <to>       合并两段同类型记忆（管理员）
/mem items [scope]           查看原子记忆
/mem due [小时]              查看即将到期
/mem done <item_id>          标记完成，停止注入
/mem cancel <item_id>        取消提醒
/mem reschedule <item_id> <时间>  修改提醒时间
/mem search <词>             搜索（包含归档）
/mem open <id>               回看某条记忆的原文
/mem why <id>                为什么记/何时过期/命中几次
/mem pin|unpin <id>          固定/取消固定（固定不自动过期）
/mem mute <id> [天]          暂停主动注入
/mem forget <id>             软删除
/mem consolidate [YYYY-MM-DD] [--rebuild] 手动记忆整理/重建当天
/mem idle                    空闲/记忆整理状态
/mem export [--md]           导出迁移包（--md 额外生成 Obsidian 友好的 Markdown）
/mem import <zip路径>        导入迁移包
/mem rebuild                 重建 FTS 索引
```

## 4.2 模型工具

模型在自然语言里处理提醒时使用这些工具，插件只校验和执行：

```text
memory_reminder_create(text, due_at)
memory_reminder_list()
memory_reminder_update(item, due_at)
memory_reminder_done(item)
memory_reminder_cancel(item)
```

- `memory_reminder_list` 是按需查询，不会每轮注入；
- 时间不明确时，模型应先追问“上午还是晚上”；
- 只有工具返回成功后，模型才能说“改好了”。

## 4.3 增量记忆整理

- 每次记忆整理记录 `jobs.last_message_id`；
- 同一天有新消息时，`/mem consolidate <日期>` 只处理新增部分；
- 要完全重跑某一天：`/mem consolidate <日期> --rebuild`，会先备份再删除当天 topics/items 后重建。

## 4.1 scope 与绑定

记忆的归属是 `scope_id`，不是 UMO：

- `scope_id`：由 `binding_key` 生成 `uuid5`，同一身份在任何机器上得到同一个 ID；旧库中只对应一个 UMO 的随机 ID 会在启动时自动迁移，绑定/多会话共享的 scope 保留原 ID；
- `(platform, kind, external_id)`：决定 scope 的身份，例如 `aiocqhttp / private / 10001`；
- `label`：昵称/群名，只用于展示，改名不影响记忆。

两个 key 的粒度不同：

| key | 含义 | 影响范围 | 谁设置 | 典型值 |
|---|---|---|---|---|
| `binding_key` | 当前**会话**键 | 只影响当前会话 | `/mem bind` | `主人格:FriendMessage:996F...` |
| `alias_key` | **身份/群**键 | 影响该身份下所有会话 | `/mem alias` | `qq_official:private:996F...` |

区别：

- `binding_key` 包含机器人实例 ID，所以不同机器人是不同 binding；
- `alias_key` 只用平台类型 + 类型 + 外部 ID，所以同一用户在不同机器人下 alias 相同；
- `bind` 是“这个会话换记忆”，`alias` 是“这个身份/群统一映射”；
- 优先级：`binding` > `alias` > 适配器 `scope_id` > 默认三元组。

对应表：

```text
session_bindings(binding_key) -> scope_id
scope_aliases(alias_key)      -> scope_id
```

跨机器人共享：

```text
/mem bind <旧scope>
```

只影响当前机器人的当前会话。

如果希望同一用户在多个机器人下共享，用 alias：

```text
/mem alias set qq_official:private:996F... <旧scope>
```

群聊换平台：

```text
/mem alias list
/mem alias set myapp:group:987654 <旧 group scope>
```

群映射是群级记忆，不按群成员逐个绑定。私聊只能绑私聊，群只能绑群；`/mem merge` 也只允许同类型合并。

`/mem whoami` 会同时显示两个 key，并注明各自影响范围。

### 4.1.1 新平台适配器

从 AstrBot 迁出时，把下面的示例复制到插件数据目录并改名为 `scope_adapter.py`：

```text
plugin_data/astrbot_plugin_bbrsmemory_plugin/scope_adapter.py
```

模板见插件目录的 `scope_adapter.example.py`。

适配器函数：

```python
def resolve_scope(ctx: dict) -> dict | None:
    ...
```

`ctx` 字段：

```text
umo / platform / kind / external_id
sender_id / group_id / sender_name
raw_event
```

新平台可以忽略 AstrBot 字段，只用自己的：

```python
def resolve_scope(ctx):
    usercode = ctx.get("usercode")
    if not usercode:
        return None
    return {
        "scope_id": f"myapp_{usercode}",
        "platform": "myapp",
        "kind": "private",
        "external_id": usercode,
        "binding_key": f"myapp:private:{usercode}",
        "alias_key": f"myapp:private:{usercode}",
        "label": "微信私聊",
    }
```

群聊：

```python
def resolve_scope(ctx):
    group_code = ctx.get("group_code")
    if not group_code:
        return None
    return {
        "platform": "myapp",
        "kind": "group",
        "external_id": group_code,
        "binding_key": f"myapp:group:{group_code}",
        "alias_key": f"myapp:group:{group_code}",
    }
```

返回 `None` 表示回退内置逻辑。适配器异常只记录日志，不会阻断消息入库。

配置项：

```json
"scope_resolver": "scope_adapter.py"
```

文件放在插件数据目录；不存在或留空就用内置 AstrBot 逻辑。修改适配器后需要重启插件。

`/mem export` 生成的包在 `data/plugin_data/astrbot_plugin_bbrsmemory_plugin/exports/`，需要手动复制到新机器；导入路径可以写绝对路径，也可以写相对于插件数据目录的路径。

---

## 5. 迁移包

```text
memory_export_YYYYmmdd_HHMMSS.zip
  manifest.json          # schema=3、版本、条数、分片、sha256
  scopes/
    part-0001.jsonl.gz   # scope_id、platform、kind、external_id、label
  messages/
    part-0001.jsonl.gz   # 每 50000 条一片
  topics/
    part-0001.jsonl.gz
  items/
    part-0001.jsonl.gz   # 原子记忆（提醒/事实/事件等）
  attachments/
    part-0001.jsonl.gz   # v0.1 只有引用
  jobs/
    part-0001.jsonl.gz
  bindings/
    part-0001.jsonl.gz   # binding_key -> scope_id
  aliases/
    part-0001.jsonl.gz   # alias_key -> scope_id（群聊映射）
  media/                 # v0.1 为空，v0.2 放语音/表情包
  markdown/              # 仅 --md 时生成
    2026/10/2026-10-20-周末开会-<id>.md
  meta.json
  README.txt
```

- 分片按 `export_shard_rows` 控制，manifest 记录每片行数、首尾日期、sha256。
- 导入流式处理，不把整包读进内存；按 `msg_uid` / topic UUID / scope_id 去重合并。
- 旧 v1 包没有 `scopes/`，导入时按 `umo` 现场建 scope，兼容。
- 导入后不会自动改 UMO：先 `/mem scopes`，再 `/mem bind <scope_id>`。
- 换成别的系统：直接读 JSONL；不再用 bot：读 `markdown/`。
- 迁移包不含绝对路径、不含模型私有状态；`media/` 是可选的大头。

---

## 6. 目录结构

```text
astrbot_plugin_bbrsmemory_plugin/
  main.py            # AstrBot 胶水：捕获、注入、调度、命令、工具
  memory_db.py       # SQLite schema、FTS、视图、jobs、TTL
  consolidation.py   # 切话题、摘要、迁移、过期
  retrieval.py       # 关键词/时间解析、排序、注入
  migration.py       # 分片导出/导入、Markdown
  media.py           # 附件协议 + Noop + 渲染
  textutil.py        # 分词/关键词/截断/slug
  timeparse.py       # 纯 Python 日期/时间解析
  tests/             # 离线冒烟测试（不需要 AstrBot）
```

核心模块（`memory_db` / `consolidation` / `retrieval` / `migration` / `media` / `textutil` / `timeparse`）**不 import astrbot**，以后换框架只改 `main.py`。

---

## 7. 设计参考

- [MemoryOS](https://github.com/BAI-LAB/MemoryOS)：短/中/长三层与热度迁移；本插件用 `layer` + 日期阈值实现。
- [MemoryBank](https://github.com/zhongwanjun/MemoryBank-SiliconFriend)：艾宾浩斯遗忘曲线；本插件离散成 `ttl_days = importance × unit`，并在显式回忆时续期。
- [Generative Agents](https://github.com/joonspk-research/generative_agents)：recency + importance + relevance 检索；本插件用 FTS + 三者加权排序。
- [Letta / MemGPT](https://github.com/letta-ai/letta)：后台巩固（sleep-time consolidation）；本插件用每日记忆整理。
- [Mem0](https://github.com/mem0ai/mem0)、[Graphiti](https://github.com/getzep/graphiti)：记忆抽取与时间有效性；v0.2 再考虑事实级 supersede 和图关系。

没有复制上述项目的代码；只借鉴了公开的算法思想。

---

## 8. v1.0.5 的边界

- 没有 embedding / 向量召回，只有 FTS5 + jieba 关键词；同义改写召回弱。
- 记忆按 `scope_id` 隔离；换 UMO 后需要显式 `/mem bind`，不会自动按昵称合并。
- 绑定是显式操作，绑错会串记忆；`/mem scopes` 和 `bound_by` 用于审计。
- 不做事实冲突消解；`facts` 只追加，新事实更新留给 v0.2。
- 摘要仍是 LLM 生成的，可能失真；靠“原文保留 + facts 不压缩 + 从原文重做 + 可回原文”压住，不能归零。
- 只存文本和附件引用；图片/语音/文件的内容 v0.2 才处理。
- 单进程 SQLite；多实例同时跑会锁库。
- `raw_retention_days` 目前只预留，v1.0.5 不自动删原文（避免把指针删断）。
- 待办主动推送不在本插件：本插件只写 `reminder_outbox`，由独立的待办插件消费。
- pending items 不自动注入；模型需要时调用 `memory_reminder_list`。
- 提醒修改/取消/完成必须走工具；模型空口说“改好了”不算数。
- `/mem itemize` 尚未实现，旧 topic 不会自动拆 items。
- 迁移包 schema=3；旧 v2 包导入时会按 `umo` 补 scope，items 为空。
- 从 v0.1 升级时，启动会自动加 `scope_id` 列并按 `umo` 回填 scope；旧库不需要手动迁移。

---

## 9. v0.2 的接口已经留好

- `message_attachments` 表：`transcript`、`caption`、`display_name`、`local_path`、`status` 字段已存在。
- `media.py`：`MediaStore` 协议 + `NoopMediaStore`；v0.2 实现 `save/transcribe/describe` 即可。
- `render_message_text()`：自动把转写/描述拼进提炼文本，历史消息不改。
- 配置：`media_enabled`、`stt_provider_id`、`vision_provider_id`、`media_max_mb`、`wait_for_media` 已占位。
- 迁移包：`attachments/`、`media/` 位置已留。

语音目标形态：`[语音#id] 转写：…`；表情包目标形态：`[表情包:初音未来_开心.jpg#id]`。

---

## 10. 测试

```bash
cd astrbot_plugin_bbrsmemory_plugin
python -m unittest discover -s tests -v
```

测试不需要 AstrBot、不需要网络；用假 LLM 覆盖提炼、检索、迁移、导出/导入。测试临时目录放在 `tests/.tmp_*`，跑完自动清理。
