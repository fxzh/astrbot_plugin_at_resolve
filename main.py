"""astrbot_plugin_at_resolve — QQ 官方机器人群聊 @提及 解析插件

qq_official 平台下，群友 A @ 群友 B 时，QQ 下发的消息文本里是
`<@A1B2C3D4A1B2C3D4A1B2C3D4A1B2C3D4>`（member_openid），AI 无法分辨谁 at 了谁。

本插件在每条群消息进入管道最前端时：

  1. 学习：为每个成员维护两个字段，各跟各的来源、互不覆盖——
       name  ← 消息发送者 author.username（QQ 昵称）
       alias ← 事件 mentions[].username（群名片，被@时随事件下发）
     按群持久化为 JSON（data/plugin_data/ 下）。
  2. 改写：把文本中的 `<@HEX>` / `<@!HEX>` 替换为 `@名字`。名字按
     prefer_group_card 配置取：开=群名片优先、QQ 昵称回退；关=QQ 昵称
     优先、群名片回退；两者皆无显示 `@群友XXXXXX`（openid 前 6 位，
     稳定可追踪）。改写覆盖 message_str、消息链 Plain、Reply 引用文本，
     因此群聊上下文注入块 / 对话历史 / 消息存档全部同步净化。
  3. 名字空间统一（prefer_group_card 开时）：已学到发送者群名片时，把
     sender.nickname 一并改为群名片——内置群聊上下文的 [发言人/时间]
     标签、发言人标注与 @ 改写从此用同一套名字，AI 不会把同一人误认
     成两个。学到群名片前该成员仍以 QQ 昵称呈现，学到后自下次发言起
     切换（每人至多一次；群名片本身改了会再次学习更新）。

管理方式：
  · WebUI → 插件管理 → 本插件 → 「QQ群at映射管理」页面（增删改查、删除整群）
  · 群内指令（管理员）：/atmap list|show|del|set|deluser|reload

不做的事：不回改插件安装前已存的历史消息。
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import EventMessageType, PermissionType
from astrbot.api.message_components import Plain, Reply
from astrbot.api.star import Context, Star, register

try:
    from astrbot.api import logger
except Exception:
    import logging

    logger = logging.getLogger("astrbot")

try:  # 兼容不同版本的 AstrBot
    from astrbot.api import AstrBotConfig
except ImportError:
    try:
        from astrbot.api.config import AstrBotConfig
    except ImportError:
        AstrBotConfig = None

try:
    from astrbot.core.star.star_tools import StarTools
except ImportError:
    StarTools = None

try:  # 兼容不同版本的指令参数解析
    from astrbot.core.star.filter.command import GreedyStr
except ImportError:
    GreedyStr = str

PLUGIN_NAME = "astrbot_plugin_at_resolve"

# <@HEX32> / <@!HEX32>（频道风格）/ <@everyone>
MENTION_RE = re.compile(r"<@!?([0-9A-Fa-f]{32}|everyone|all)>")

USAGE = "\n".join(
    [
        "推荐在 WebUI → 插件管理 → 本插件 → QQ群at映射管理 页面直接编辑。",
        "以下为指令方式（管理员）：",
        "/atmap            查看已学习的群与人数",
        "/atmap show <序号|群ID>    查看某群的映射明细",
        "/atmap del <序号|群ID>     删除某群的持久化数据",
        "/atmap set <群ID> <openid> <昵称>    手动添加/修正昵称",
        "/atmap setcard <群ID> <openid> <群名片>    手动添加/修正群名片",
        "/atmap deluser <群ID> <openid>       删除一条映射",
        "/atmap reload     重新扫描数据目录",
    ]
)


def _now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def _short(openid: str) -> str:
    return openid[:6].upper()


class AtMapStore:
    """按群持久化的 openid → 名字 映射存储。

    每群一个 JSON 文件：data/plugin_data/<插件>/groups/<group_openid>.json
    结构：{"group_id", "group_name", "last_active",
           "members": {OPENID: {"name", "alias", "updated"}}}
    name  = QQ 昵称（来源：消息发送者 author.username）
    alias = 群名片（来源：事件 mentions[].username，可能一直没有）
    旧版文件（条目无 alias 字段）按 alias 为空兼容读取，无需迁移：
    被旧版覆盖错的条目，待该成员下次发言即自动纠正。
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.groups_dir = root / "groups"
        self.groups_dir.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, dict] = {}

    # ---------------- 基础读写 ----------------

    def _path(self, gid: str) -> Path:
        return self.groups_dir / f"{gid}.json"

    def _valid_gid(self, gid: str) -> bool:
        return bool(gid) and re.fullmatch(r"[0-9A-Za-z_\-]{1,128}", gid) is not None

    def load_group(self, gid: str) -> dict | None:
        """读入某群数据（带内存缓存）；文件损坏时视为不存在。"""
        if gid in self._cache:
            return self._cache[gid]
        path = self._path(gid)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning(f"at_resolve: 群映射文件损坏，已忽略: {path.name}")
            return None
        if not isinstance(data, dict) or not isinstance(data.get("members"), dict):
            return None
        self._cache[gid] = data
        return data

    def save_group(self, gid: str, data: dict) -> None:
        if not self._valid_gid(gid):
            raise ValueError(f"非法群 ID: {gid!r}")
        self._cache[gid] = data
        path = self._path(gid)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        os.replace(tmp, path)

    def delete_group(self, gid: str) -> bool:
        self._cache.pop(gid, None)
        path = self._path(gid)
        if path.is_file():
            path.unlink()
            return True
        return False

    def all_group_ids(self) -> list[str]:
        ids = {p.stem for p in self.groups_dir.glob("*.json")}
        ids.update(self._cache.keys())
        return sorted(ids)

    def reload(self) -> None:
        self._cache.clear()

    # ---------------- 业务操作 ----------------

    def touch_group(self, gid: str, group_name: str | None) -> dict:
        """取/建群数据并刷新活跃时间。调用方负责 save。"""
        data = self.load_group(gid)
        if data is None:
            data = {"group_id": gid, "group_name": "", "members": {}}
        data["last_active"] = _now_iso()
        if group_name:
            data["group_name"] = str(group_name)
        return data

    def _learn_field(
        self, gid: str, openid: str, field: str, value: str, group_name: str | None
    ) -> bool:
        """写入单一字段（name 或 alias）。返回是否有变更。"""
        openid = str(openid or "").strip().upper()
        value = str(value or "").strip()
        if not openid or not value:
            return False
        data = self.touch_group(gid, group_name)
        members: dict = data["members"]
        entry = members.get(openid)
        if not isinstance(entry, dict):
            entry = {"name": "", "alias": "", "updated": _now_iso()}
            members[openid] = entry
        if entry.get(field) == value:
            return False  # 该来源无变化，不落盘
        entry[field] = value
        entry["updated"] = _now_iso()
        self.save_group(gid, data)
        return True

    def learn_author(
        self, gid: str, openid: str, nickname: str, group_name: str | None
    ) -> bool:
        """学习 QQ 昵称（消息发送者本人，author.username）。"""
        return self._learn_field(gid, openid, "name", nickname, group_name)

    def learn_mention(
        self, gid: str, openid: str, card: str, group_name: str | None
    ) -> bool:
        """学习群名片（事件 mentions[].username，被@时下发）。"""
        return self._learn_field(gid, openid, "alias", card, group_name)

    def set_entry(
        self,
        gid: str,
        openid: str,
        name: str | None = None,
        alias: str | None = None,
    ) -> bool:
        """手动设置字段（WebUI/指令）：None=不动该字段，str（含空串）=设为该值。

        与自动学习不同，允许写空值——空串即手动清除该字段
        （清除后若再次被@观察到群名片，会照常自动学回）。返回是否有变更。
        """
        openid = str(openid or "").strip().upper()
        if not openid or (name is None and alias is None):
            return False
        data = self.touch_group(gid, None)
        members: dict = data["members"]
        entry = members.get(openid)
        if not isinstance(entry, dict):
            entry = {"name": "", "alias": "", "updated": _now_iso()}
            members[openid] = entry
        changed = False
        if name is not None and entry.get("name") != name:
            entry["name"] = name
            changed = True
        if alias is not None and entry.get("alias") != alias:
            entry["alias"] = alias
            changed = True
        if changed:
            entry["updated"] = _now_iso()
            self.save_group(gid, data)
        return changed

    def _entry(self, gid: str, openid: str) -> dict | None:
        data = self.load_group(gid)
        if not data:
            return None
        entry = (data.get("members") or {}).get(str(openid).strip().upper())
        return entry if isinstance(entry, dict) else None

    def resolve(self, gid: str, openid: str, prefer_card: bool = True) -> str | None:
        """按展示策略取名字：prefer_card=True 群名片优先、QQ 昵称回退；False 反之。"""
        entry = self._entry(gid, openid)
        if entry is None:
            return None
        name = str(entry.get("name") or "")
        alias = str(entry.get("alias") or "")
        if prefer_card:
            return alias or name or None
        return name or alias or None

    def lookup_alias(self, gid: str, openid: str) -> str | None:
        """取群名片（没学到则 None）。用于统一发送者标签的名字空间。"""
        entry = self._entry(gid, openid)
        if entry is None:
            return None
        return str(entry.get("alias") or "") or None

    def stats(self) -> list[dict]:
        out = []
        for gid in self.all_group_ids():
            data = self.load_group(gid)
            if not data:
                continue
            out.append(
                {
                    "group_id": gid,
                    "group_name": data.get("group_name") or "",
                    "members": len(data.get("members") or {}),
                    "last_active": data.get("last_active") or "",
                }
            )
        out.sort(key=lambda x: x["last_active"], reverse=True)
        return out

    def cleanup(self, days: int) -> int:
        """删除超过 N 天未活跃的群数据，返回删除数。"""
        if days <= 0:
            return 0
        deadline = datetime.now() - timedelta(days=days)
        removed = 0
        for gid in self.all_group_ids():
            data = self.load_group(gid)
            if not data:
                continue
            try:
                last = datetime.fromisoformat(data.get("last_active") or "")
            except ValueError:
                continue
            if last < deadline:
                self.delete_group(gid)
                removed += 1
        return removed


@register(
    PLUGIN_NAME,
    "97857",
    "QQ群at映射解析：自动学习 openid→QQ昵称/群名片，把 <@HEX> 改写为 @名字，"
    "并统一 @ 与群聊上下文发言人标签的名字空间，AI 从此认得谁 at 了谁；"
    "WebUI 页面与 /atmap 指令均可管理",
    "1.1.0",
)
class AtResolvePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config

        data_dir = (
            StarTools.get_data_dir(PLUGIN_NAME)
            if StarTools is not None
            else Path(__file__).parent / "data"
        )
        self.store = AtMapStore(data_dir)

        try:
            removed = self.store.cleanup(int(self._cfg("auto_clean_days", 0) or 0))
            if removed:
                logger.info(f"at_resolve: 自动清理了 {removed} 个长期未活跃的群映射")
        except Exception:
            logger.warning("at_resolve: 自动清理失败", exc_info=True)

        self._register_web_api()

    # ------------------------------------------------------------------ #
    # 工具
    # ------------------------------------------------------------------ #

    def _cfg(self, key: str, default=None):
        try:
            if self.config is not None and self.config.get(key, default) is not None:
                return self.config.get(key, default)
        except Exception:
            pass
        return default

    def _platform_ok(self, event: AstrMessageEvent) -> bool:
        plats = [str(x) for x in (self._cfg("apply_platforms", None) or [])]
        if not plats:
            return True
        return event.get_platform_name() in plats

    def _fmt(self, key: str, default: str, **kw) -> str:
        tmpl = str(self._cfg(key, default) or default)
        try:
            return tmpl.format(**kw)
        except (KeyError, IndexError, ValueError):
            return default.format(**kw)

    # ------------------------------------------------------------------ #
    # 核心：学习 + 改写（挂 event_message_type，priority 最大，
    # 抢在内置 persist_group_message / group_icl（maxsize-2）之前）
    # ------------------------------------------------------------------ #

    @filter.event_message_type(EventMessageType.GROUP_MESSAGE, priority=sys.maxsize)
    async def on_group_message(self, event: AstrMessageEvent):
        try:
            if not self._cfg("enabled", True):
                return
            if not self._platform_ok(event):
                return
            gid = str(event.get_group_id() or "")
            if not gid:
                return

            prefer_card = bool(self._cfg("prefer_group_card", True))

            if self._cfg("learn_enabled", True):
                self._learn(event, gid)

            if self._cfg("rewrite_enabled", True):
                self._rewrite(event, gid, prefer_card)

            # 名字空间统一：必须在 _learn 之后（学习读的是原始 QQ 昵称，
            # 提前改写 sender.nickname 会把群卡片污染进 name 字段）
            if prefer_card:
                self._apply_card_nickname(event, gid)
        except Exception:
            logger.error("at_resolve: 处理群消息异常，消息按原样放行", exc_info=True)

    def _learn(self, event: AstrMessageEvent, gid: str) -> None:
        group_name = None
        try:
            group_obj = getattr(event.message_obj, "group", None)
            group_name = getattr(group_obj, "group_name", None)
        except Exception:
            pass

        # ① 消息发送者本人 → QQ 昵称（name 字段）
        self.store.learn_author(
            gid, event.get_sender_id() or "", event.get_sender_name() or "", group_name
        )

        # ② 事件 mentions 数组携带的被@者 → 群名片（alias 字段）。
        #    被@者即使从未发言也能当轮解析；与 name 字段互不覆盖。
        raw = getattr(event.message_obj, "raw_message", None)
        mentions = getattr(raw, "mentions", None) or []
        for m in mentions:
            if not isinstance(m, dict):
                m = {
                    "id": getattr(m, "id", None),
                    "username": getattr(m, "username", None),
                    "member_openid": getattr(m, "member_openid", None),
                    "bot": getattr(m, "bot", None),
                    "is_you": getattr(m, "is_you", None),
                }
            if m.get("bot") or m.get("is_you"):
                continue
            mid = str(m.get("member_openid") or m.get("id") or "").strip()
            name = str(m.get("username") or "").strip()
            if mid and name:
                self.store.learn_mention(gid, mid, name, group_name)

        if self._cfg("debug_log", False) and mentions:
            logger.debug(
                f"at_resolve: mentions payload => {json.dumps(mentions, ensure_ascii=False, default=str)[:500]}"
            )

    def _rewrite(
        self, event: AstrMessageEvent, gid: str, prefer_card: bool = True
    ) -> None:
        self_id = str(event.get_self_id() or "")

        def resolve(openid: str) -> str:
            if openid.lower() in ("everyone", "all"):
                return self._fmt("everyone_format", "@全体成员")
            if self_id and openid.upper() == self_id.upper():
                return self._fmt("self_format", "@机器人")
            name = self.store.resolve(gid, openid, prefer_card)
            if name:
                return self._fmt("mention_format", "@{name}", name=name, short=_short(openid), id=openid)
            return self._fmt(
                "unknown_format", "@群友{short}", short=_short(openid), id=openid
            )

        def rewrite(text: str) -> str:
            return MENTION_RE.sub(lambda m: resolve(m.group(1)), text)

        changed = 0

        msg_str = getattr(event, "message_str", None)
        if isinstance(msg_str, str) and MENTION_RE.search(msg_str):
            event.message_str = rewrite(msg_str)
            changed += 1

        chain = getattr(event.message_obj, "message", None) or []
        for comp in chain:
            if isinstance(comp, Plain):
                text = getattr(comp, "text", None)
                if isinstance(text, str) and MENTION_RE.search(text):
                    comp.text = rewrite(text)
                    changed += 1
            elif isinstance(comp, Reply):
                for attr in ("message_str", "text"):
                    val = getattr(comp, attr, None)
                    if isinstance(val, str) and MENTION_RE.search(val):
                        setattr(comp, attr, rewrite(val))
                        changed += 1
                for sub in getattr(comp, "chain", None) or []:
                    if isinstance(sub, Plain):
                        text = getattr(sub, "text", None)
                        if isinstance(text, str) and MENTION_RE.search(text):
                            sub.text = rewrite(text)
                            changed += 1

        if changed and self._cfg("debug_log", False):
            logger.debug(f"at_resolve: 重写了 {changed} 处提及 gid={_short(gid)}")

    def _apply_card_nickname(self, event: AstrMessageEvent, gid: str) -> None:
        """prefer_group_card 开启时，把发送者显示名统一为群名片（已学到时）。

        内置群聊上下文标签 / 发言人标注 / 消息存档读取的都是
        sender.nickname；本插件优先级（sys.maxsize）高于它们（maxsize-2），
        在此提前改写即可让标签与 @ 改写同名。仅影响本次事件的展示层，
        映射数据不受影响。必须在 _learn 之后调用。
        """
        sid = str(event.get_sender_id() or "")
        if not sid:
            return
        card = self.store.lookup_alias(gid, sid)
        if not card:
            return
        sender = getattr(event.message_obj, "sender", None)
        if sender is None or getattr(sender, "nickname", None) == card:
            return
        try:
            sender.nickname = card
        except Exception:
            pass  # 个别适配器的 sender 可能禁止改写，跳过即可

    # ------------------------------------------------------------------ #
    # 管理指令（/atmap，仅管理员）
    # ------------------------------------------------------------------ #

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("atmap", alias={"提及映射"})
    async def atmap_cmd(self, event: AstrMessageEvent, rest: GreedyStr):
        """提及映射管理指令"""
        try:
            reply = self._handle_cmd(str(rest or "").strip())
        except Exception:
            logger.error("at_resolve: 指令处理异常", exc_info=True)
            reply = "指令执行出错，详见日志。"
        yield event.plain_result(reply)

    def _resolve_gid(self, arg: str) -> str | None:
        """序号（/atmap list 里的 #n）或完整群 ID → 群 ID。"""
        stats = self.store.stats()
        if arg.isdigit() and 1 <= int(arg) <= len(stats):
            return stats[int(arg) - 1]["group_id"]
        if self.store.load_group(arg):
            return arg
        return None

    def _handle_cmd(self, rest: str) -> str:
        parts = rest.split()
        if not parts:
            return self._cmd_list()
        sub, args = parts[0].lower(), parts[1:]
        if sub == "list":
            return self._cmd_list()
        if sub == "show":
            if not args:
                return "用法: /atmap show <序号|群ID>"
            gid = self._resolve_gid(args[0])
            if not gid:
                return f"找不到对应的群（序号或 ID: {args[0]}）。"
            return self._cmd_show(gid)
        if sub == "del":
            if not args:
                return "用法: /atmap del <序号|群ID>"
            gid = self._resolve_gid(args[0])
            if not gid:
                return f"找不到对应的群（序号或 ID: {args[0]}）。"
            self.store.delete_group(gid)
            return f"已删除群 {_short(gid)} 的持久化映射。"
        if sub == "set":
            if len(args) < 3:
                return "用法: /atmap set <群ID> <openid> <昵称>"
            gid, openid, name = args[0], args[1], " ".join(args[2:])
            if not self.store._valid_gid(gid):
                return "群 ID 不合法。"
            self.store.learn_author(gid, openid, name, None)
            return f"已写入: {_short(openid)} → {name}"
        if sub == "setcard":
            if len(args) < 3:
                return "用法: /atmap setcard <群ID> <openid> <群名片>"
            gid, openid, card = args[0], args[1], " ".join(args[2:])
            if not self.store._valid_gid(gid):
                return "群 ID 不合法。"
            self.store.set_entry(gid, openid, alias=card)
            return f"已写入群名片: {_short(openid)} → {card}"
        if sub == "deluser":
            if len(args) < 2:
                return "用法: /atmap deluser <群ID> <openid>"
            gid, openid = args[0], args[1].upper()
            data = self.store.load_group(gid)
            if not data:
                return "该群没有任何映射数据。"
            if data["members"].pop(openid, None) is None:
                return f"没有 {_short(openid)} 的映射。"
            self.store.save_group(gid, data)
            return f"已删除 {_short(openid)} 的映射。"
        if sub == "reload":
            self.store.reload()
            return f"数据目录已重扫，当前 {len(self.store.stats())} 个群。"
        return f"未知子指令: {sub}\n{USAGE}"

    def _cmd_list(self) -> str:
        stats = self.store.stats()
        if not stats:
            return "还没有学到任何映射。群友发言后自动学习；也可在 WebUI 插件页手动添加。"
        lines = ["已学习映射的群（按最后活跃排序）："]
        for i, s in enumerate(stats, 1):
            name = s["group_name"] or "未知名"
            lines.append(
                f"#{i} {name}（{_short(s['group_id'])}…） {s['members']} 人 "
                f"活跃 {s['last_active']}"
            )
        lines.append("提示: WebUI → 插件管理 → 提及映射管理 页面可直接编辑。")
        return "\n".join(lines)

    def _cmd_show(self, gid: str) -> str:
        data = self.store.load_group(gid)
        if not data:
            return "该群没有数据。"
        members = data.get("members") or {}
        lines = [
            f"群 {data.get('group_name') or '未知名'}（{gid}）共 {len(members)} 条映射："
        ]
        for i, (openid, entry) in enumerate(
            sorted(members.items(), key=lambda kv: kv[1].get("updated", ""), reverse=True), 1
        ):
            if i > 50:
                lines.append(f"…仅显示前 50 条，共 {len(members)} 条")
                break
            name = entry.get("name") or "—"
            alias = entry.get("alias") or ""
            lines.append(
                f"{i}. {_short(openid)} → {name}"
                + (f"（群名片:{alias}）" if alias else "")
                + f"（{entry.get('updated')}）"
            )
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # WebUI 后端接口（register_web_api，前端页面在 views/atmap/）
    # ------------------------------------------------------------------ #

    def _register_web_api(self) -> None:
        """注册 /api/v1/plugins/extensions/<本插件>/<endpoint> 系列接口。"""
        api = self.context.register_web_api
        base = f"/{PLUGIN_NAME}"
        api(f"{base}/atmap/overview", self._api_overview, ["GET"], "提及映射总览")
        api(f"{base}/atmap/group/<gid>", self._api_group_detail, ["GET"], "群映射明细")
        api(f"{base}/atmap/group/<gid>/delete", self._api_group_delete, ["POST"], "删除群映射")
        api(f"{base}/atmap/member", self._api_member_set, ["POST"], "添加/修改映射")
        api(f"{base}/atmap/member/delete", self._api_member_delete, ["POST"], "删除单条映射")
        api(f"{base}/atmap/reload", self._api_reload, ["POST"], "重扫数据目录")

    @staticmethod
    def _ok(data: Any = None) -> dict:
        return {"status": "ok", "data": data}

    @staticmethod
    def _err(message: str, status_code: int = 400) -> Any:
        try:
            from astrbot.api.web import error_response

            return error_response(message, status_code=status_code)
        except Exception:
            return {"status": "error", "message": message}

    async def _api_overview(self):
        try:
            stats = self.store.stats()
            total = sum(s["members"] for s in stats)
            return self._ok({"groups": stats, "total_members": total})
        except Exception:
            logger.error("at_resolve: overview 接口异常", exc_info=True)
            return self._err("读取映射数据失败", 500)

    async def _api_group_detail(self, gid: str):
        try:
            data = self.store.load_group(gid)
            if not data:
                return self._err("该群没有映射数据", 404)
            members = [
                {
                    "openid": openid,
                    "name": entry.get("name", ""),
                    "alias": entry.get("alias", "") or "",
                    "updated": entry.get("updated", ""),
                }
                for openid, entry in (data.get("members") or {}).items()
            ]
            members.sort(key=lambda x: x["updated"], reverse=True)
            return self._ok(
                {
                    "group_id": gid,
                    "group_name": data.get("group_name") or "",
                    "last_active": data.get("last_active") or "",
                    "members": members,
                }
            )
        except Exception:
            logger.error("at_resolve: group detail 接口异常", exc_info=True)
            return self._err("读取群映射失败", 500)

    async def _api_group_delete(self, gid: str):
        try:
            if not self.store.delete_group(gid):
                return self._err("该群没有映射数据", 404)
            logger.info(f"at_resolve: WebUI 删除了群映射 {_short(gid)}")
            return self._ok({"deleted": gid})
        except Exception:
            logger.error("at_resolve: group delete 接口异常", exc_info=True)
            return self._err("删除失败", 500)

    async def _api_member_set(self):
        try:
            from astrbot.api.web import request

            body = await request.json(default={}) or {}
            gid = str(body.get("gid") or "").strip()
            openid = str(body.get("openid") or "").strip()
            raw_name = body.get("name")
            raw_alias = body.get("alias")
            # None=请求未携带该字段（不动），空串=清除该字段
            name = None if raw_name is None else str(raw_name).strip()
            alias = None if raw_alias is None else str(raw_alias).strip()
            if not gid or not openid:
                return self._err("gid / openid 不能为空")
            if name is None and alias is None:
                return self._err("name / alias 至少提供一项")
            if not name and not alias:
                return self._err("昵称与群名片不能同时为空")
            if not self.store._valid_gid(gid):
                return self._err("群 ID 不合法")
            if not re.fullmatch(r"[0-9A-Fa-f]{32}", openid):
                return self._err("openid 应为 32 位十六进制字符串")
            self.store.set_entry(gid, openid, name=name, alias=alias)
            return self._ok(
                {"gid": gid, "openid": openid.upper(), "name": name, "alias": alias}
            )
        except Exception:
            logger.error("at_resolve: member set 接口异常", exc_info=True)
            return self._err("保存失败", 500)

    async def _api_member_delete(self):
        try:
            from astrbot.api.web import request

            body = await request.json(default={}) or {}
            gid = str(body.get("gid") or "").strip()
            openid = str(body.get("openid") or "").strip().upper()
            data = self.store.load_group(gid)
            if not data:
                return self._err("该群没有映射数据", 404)
            if data["members"].pop(openid, None) is None:
                return self._err("该 openid 不存在", 404)
            self.store.save_group(gid, data)
            return self._ok({"deleted": openid})
        except Exception:
            logger.error("at_resolve: member delete 接口异常", exc_info=True)
            return self._err("删除失败", 500)

    async def _api_reload(self):
        try:
            self.store.reload()
            return self._ok({"groups": len(self.store.stats())})
        except Exception:
            logger.error("at_resolve: reload 接口异常", exc_info=True)
            return self._err("重扫失败", 500)
