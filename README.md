# astrbot_plugin_at_resolve（QQ群at映射解析）

QQ 官方机器人（qq_official 平台）群聊 **@提及 解析**插件。

## 解决什么问题

QQ 官方群机器人下发的消息文本里，群友 A @ 群友 B 时是：

```
[群友A/12:34:56]: <@A1B2C3D4A1B2C3D4A1B2C3D4A1B2C3D4> xxxyyy
```

`<@HEX>` 是被@者的 member_openid，AI 无法分辨谁 at 了谁。

本插件在消息进入管道最前端（priority 最大，先于内置群聊上下文收集/消息存档）：

1. **学习**：为每个成员学两个字段、各跟各的来源、互不覆盖——
   `name`（QQ 昵称 ← 消息发送者）与 `alias`（群名片 ← 被@时随 `mentions` 数组下发）。
2. **改写**：`<@HEX>` → `@名字`，名字按 `prefer_group_card` 开关取：
   开=群名片优先、QQ 昵称回退（默认）；关=反之；
   两者皆无时显示 `@群友XXXXXX`（openid 前 6 位，稳定可追踪）；
   @机器人自己 → `@机器人`；`<@everyone>` → `@全体成员`。
3. **名字空间统一**（开关开时）：已学到发送者群名片时，把 `sender.nickname`
   一并改为群名片，内置群聊上下文的 `[发言人/时间]` 标签、发言人标注与
   @ 改写从此用同一套名字，AI 不会把同一人误认成两个。
   学到群名片前该成员仍以 QQ 昵称呈现，学到后自下次发言起切换（每人至多一次）。

改写覆盖 `message_str`、消息链 Plain、Reply 引用文本——因此群聊上下文注入块、
对话历史、消息存档（platform_message_history）全部同步净化。

> 为什么需要第 3 步：上下文标签取自 QQ 下发的 `author.username`（QQ 昵称），
> 而 @ 的 mentions 数组下发的是群名片——同一人两套名字，AI 会误认成两个人。
> 统一到群名片后，AI 回复引用人名也与群成员在 QQ 里实际所见一致。

> 明确不做：不回改插件安装前已存的历史消息；不重写历史上下文。

## 映射数据

按群一个 JSON：`data/plugin_data/astrbot_plugin_at_resolve/groups/<group_openid>.json`

```json
{
  "group_id": "C3D4A1B2…",
  "group_name": "某群",
  "last_active": "2026-11-01T12:00:00",
  "members": {
    "A1B2C3D4…": { "name": "QQ昵称", "alias": "群名片", "updated": "…" }
  }
}
```

写入为原子替换（tmp + rename）；`name`/`alias` 各取其来源的最新观察值，互不覆盖；
`alias` 可能一直没有（该成员从未被@过）。旧版文件（无 `alias` 字段）直接兼容，
无需迁移——被旧版覆盖错的条目，待该成员下次发言即自动纠正。

## 管理方式

**WebUI**（推荐）：插件管理 → 本插件（QQ群at映射解析）→ **QQ群at映射管理** 页面
（查看所有群 / 搜索 / 改昵称与群名片 / 删单条 / 添加映射 / 删除整群 / 重扫目录）。
群名片与昵称一样可手动编辑：**清空输入框后保存即清除该字段**（此后被@观察到新值会照常自动学回）。
后端接口走 `register_web_api`（`/api/v1/plugins/extensions/…`，带面板鉴权），
页面经插件视图 iframe + bridge SDK 加载。

**指令**（管理员）：`/atmap`（别名 `/提及映射`）

```
/atmap                        查看已学习的群与人数
/atmap show <序号|群ID>       查看某群映射明细（含群名片）
/atmap del <序号|群ID>        删除某群持久化数据
/atmap set <群ID> <openid> <昵称>       手动添加/修正昵称
/atmap setcard <群ID> <openid> <群名片> 手动添加/修正群名片
/atmap deluser <群ID> <openid>          删除单条
/atmap reload                 重新扫描数据目录
```

退群清理：直接删该群的 JSON 文件、WebUI 删除整群、或 `/atmap del`；
也可在配置里开 `auto_clean_days`（N 天未活跃自动清理，插件加载时执行）。

## 配置项（WebUI 插件配置页）

| 键 | 默认 | 说明 |
|---|---|---|
| enabled | true | 总开关 |
| learn_enabled | true | 自动学习（QQ 昵称 + 群名片双字段） |
| rewrite_enabled | true | 提及改写（关=只学不改） |
| prefer_group_card | true | 优先以群名片呈现：@ 与发言人标签均群名片优先、QQ 昵称回退；关=反之 |
| apply_platforms | ["qq_official"] | 生效平台 |
| mention_format | @{name} | 已解析提及模板（{name}/{short}/{id}） |
| unknown_format | @群友{short} | 未学到任何名字时的模板 |
| self_format | @机器人 | @机器人 自己 |
| everyone_format | @全体成员 | 全体提及 |
| auto_clean_days | 0 | 自动清理天数（0=关） |
| debug_log | false | 输出 mentions 原始字段与改写计数 |

## 第一轮 @ 能否立刻解析？

- 被@者在本群发过言（QQ 昵称已学到）→ 立刻
- 被@者是本条消息发送者（自@）→ 立刻（先学习后改写）
- 被@者从未发言 → 仍可立刻：QQ 的 `mentions` 数组会随事件下发被@者的
  **群名片**（已实测），当轮即学到 `alias` 并解析
- 两个名字都没有的极端情况 → 首次显示 `@群友XXXXXX`

## 环境要求

AstrBot ≥ v4.29（`register_web_api` / 插件视图 `views/` 机制）。
仅对 qq_official 群消息生效（可配置）。
