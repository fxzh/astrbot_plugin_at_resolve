# astrbot_plugin_at_resolve（QQ群at映射解析）

QQ 官方机器人（qq_official 平台）群聊 **@提及 解析**插件。

## 解决什么问题

QQ 官方群机器人下发的消息文本里，群友 A @ 群友 B 时是：

```
[群友A/12:34:56]: <@A1B2C3D4A1B2C3D4A1B2C3D4A1B2C3D4> xxxyyy
```

`<@HEX>` 是被@者的 member_openid，AI 无法分辨谁 at 了谁。

本插件在消息进入管道最前端（priority 最大，先于内置群聊上下文收集/消息存档）：

1. **学习**：从每条群消息的发送者（member_openid + 昵称）自动维护映射；
   若 QQ 事件 `mentions` 数组携带其他被@用户的昵称也一并学习（以 debug 日志观察为准）。
2. **改写**：`<@HEX>` → `@昵称`；未学到的显示 `@群友XXXXXX`（openid 前 6 位，稳定可追踪）；
   @机器人自己 → `@机器人`；`<@everyone>` → `@全体成员`。

改写覆盖 `message_str`、消息链 Plain、Reply 引用文本——因此群聊上下文注入块、
对话历史、消息存档（platform_message_history）全部同步净化。

> 明确不做：不回改插件安装前已存的历史消息；不重写历史上下文。

## 映射数据

按群一个 JSON：`data/plugin_data/astrbot_plugin_at_resolve/groups/<group_openid>.json`

```json
{
  "group_id": "C3D4A1B2…",
  "group_name": "某群",
  "last_active": "2026-11-01T12:00:00",
  "members": { "A1B2C3D4…": { "name": "someone", "updated": "…" } }
}
```

写入为原子替换（tmp + rename）；nickname 取最新观察值。

## 管理方式

**WebUI**（推荐）：插件管理 → 本插件（QQ群at映射解析）→ **QQ群at映射管理** 页面
（查看所有群 / 搜索 / 改昵称 / 删单条 / 添加映射 / 删除整群 / 重扫目录）。
后端接口走 `register_web_api`（`/api/v1/plugins/extensions/…`，带面板鉴权），
页面经插件视图 iframe + bridge SDK 加载。

**指令**（管理员）：`/atmap`（别名 `/提及映射`）

```
/atmap                        查看已学习的群与人数
/atmap show <序号|群ID>       查看某群映射明细
/atmap del <序号|群ID>        删除某群持久化数据
/atmap set <群ID> <openid> <昵称>   手动添加/修正
/atmap deluser <群ID> <openid>      删除单条
/atmap reload                 重新扫描数据目录
```

退群清理：直接删该群的 JSON 文件、WebUI 删除整群、或 `/atmap del`；
也可在配置里开 `auto_clean_days`（N 天未活跃自动清理，插件加载时执行）。

## 配置项（WebUI 插件配置页）

| 键 | 默认 | 说明 |
|---|---|---|
| enabled | true | 总开关 |
| learn_enabled | true | 自动学习 |
| rewrite_enabled | true | 提及改写（关=只学不改） |
| apply_platforms | ["qq_official"] | 生效平台 |
| mention_format | @{name} | 已解析提及模板（{name}/{short}/{id}） |
| unknown_format | @群友{short} | 未学到昵称时的模板 |
| self_format | @机器人 | @机器人 自己 |
| everyone_format | @全体成员 | 全体提及 |
| auto_clean_days | 0 | 自动清理天数（0=关） |
| debug_log | false | 输出 mentions 原始字段与改写计数 |

## 第一轮 @ 能否立刻解析？

- 被@者在本群发过言（映射已学到）→ 立刻
- 被@者是本条消息发送者（自@）→ 立刻（先学习后改写）
- 被@者从未发言 → 首次显示 `@群友XXXXXX`，其开口后**之后的新消息**即可解析
- 若 QQ 事件 mentions 带非 bot 用户昵称 → 当轮即可解析（观察 debug_log）

## 环境要求

AstrBot ≥ v4.29（`register_web_api` / 插件视图 `views/` 机制）。
仅对 qq_official 群消息生效（可配置）。
