from __future__ import annotations

import asyncio
import importlib
import sys
import types
from pathlib import Path
from types import SimpleNamespace

from core.data import ParseResult, Platform
from core.group_parser_config import (
    GroupParserConfigService,
    group_enabled_parsers,
    group_parsing_enabled,
)


def _load_main_module():
    package = types.ModuleType("parser_plugin_test")
    package.__path__ = ["."]
    sys.modules.setdefault("parser_plugin_test", package)
    return importlib.import_module("parser_plugin_test.main")


class _Event:
    def __init__(
        self,
        main_module,
        text: str,
        calls: list[str],
        *,
        message_str: str | None = None,
        fail_react: bool = False,
        umo: str = "test:FriendMessage:1",
        sender_role: str = "member",
    ):
        self.message_str = text if message_str is None else message_str
        self._chain = [main_module.Plain(text)]
        self.reactions: list[str] = []
        self.calls = calls
        self.fail_react = fail_react
        self._umo = umo
        self.sender_role = sender_role
        self.stopped = False
        self.role = "admin" if sender_role == "bot_admin" else "member"
        self.is_at_or_wake_command = True
        self.extras = {}
        self.message_obj = SimpleNamespace(raw_message={"sender": {"role": sender_role}})

    @property
    def unified_msg_origin(self):
        return self._umo

    def get_messages(self):
        return self._chain

    def get_message_str(self):
        return self.message_str

    def is_admin(self):
        return self.role == "admin"

    def set_extra(self, key, value):
        self.extras[key] = value

    def get_self_id(self):
        return "bot"

    def get_group_id(self):
        if "GroupMessage:" not in self._umo:
            return ""
        return self._umo.rsplit("_", 1)[-1]

    def get_platform_name(self):
        return "aiocqhttp" if self._umo.startswith("Napcat_V5:") else "test"

    def get_platform_id(self):
        return "Napcat_V5" if self._umo.startswith("Napcat_V5:") else "test"

    def get_message_type(self):
        return object()

    def stop_event(self):
        self.stopped = True

    def plain_result(self, message):
        return message

    async def react(self, emoji: str):
        self.reactions.append(emoji)
        self.calls.append("react")
        if self.fail_react:
            raise RuntimeError("reaction unavailable")


class _Debouncer:
    def __init__(self, hit_link: bool = False, hit_resource: bool = False):
        self._hit_link = hit_link
        self._hit_resource = hit_resource

    def hit_link(self, session: str, link: str):
        return self._hit_link

    def hit_resource(self, session: str, resource_id: str):
        return self._hit_resource


class _Parser:
    def __init__(self, calls: list[str]):
        self.calls = calls
        self.results: list[ParseResult] = []
        self.platform = SimpleNamespace(name="netease")

    async def parse(self, keyword, searched):
        self.calls.append("parse")
        result = ParseResult(platform=Platform("test", "Test"), url=searched.group(0))
        self.results.append(result)
        return result


class _Sender:
    def __init__(self, calls: list[str]):
        self.calls = calls

    async def send_parse_result(self, event, result):
        self.calls.append("send")


def _plugin(
    main_module,
    calls: list[str],
    *,
    hit_link: bool = False,
    cache_dir: Path | None = None,
):
    plugin = main_module.ParserPlugin.__new__(main_module.ParserPlugin)
    plugin.cfg = SimpleNamespace(
        whitelist=[],
        blacklist=[],
        require_at_in_group=False,
        parser=SimpleNamespace(
            _nodes={"netease": SimpleNamespace(enable=True)}
        ),
        **({"cache_dir": cache_dir} if cache_dir is not None else {}),
    )
    plugin.key_pattern_list = [
        ("163cn.tv", main_module.re.compile(r"163cn\.tv/[A-Za-z0-9_-]+"))
    ]
    plugin.parser_map = {"163cn.tv": _Parser(calls)}
    plugin.parser_map["music.163.com/song"] = plugin.parser_map["163cn.tv"]
    plugin.key_pattern_list.append(
        ("music.163.com/song", main_module.re.compile(r"music\.163\.com/song\?id=\d+"))
    )
    plugin.debouncer = _Debouncer(hit_link=hit_link)
    plugin.sender = _Sender(calls)
    plugin.arbiter = object()
    return plugin


def _set_group_policy(plugin, *, enabled=None, disabled=None):
    plugin.cfg._data = {}
    if enabled is not None:
        all_keys = {
            "bilibili", "douyin", "xhs", "pixiv", "qqmusic", "netease",
            "kugou", "qishui", "kuwo", "applemusic",
        }
        plugin.cfg.parser._nodes = {
            key: SimpleNamespace(enable=key in enabled) for key in all_keys
        }
    if disabled:
        plugin.cfg._data["group_parser_settings"] = disabled


def test_matched_link_parses_without_reaction():
    main_module = _load_main_module()
    calls: list[str] = []
    plugin = _plugin(main_module, calls)
    event = _Event(main_module, "推荐歌单 https://163cn.tv/AbC_123", calls)

    asyncio.run(main_module.ParserPlugin.on_message(plugin, event))

    assert event.reactions == []
    assert calls == ["parse", "send"]


def test_debounced_link_does_not_react_or_parse():
    main_module = _load_main_module()
    calls: list[str] = []
    plugin = _plugin(main_module, calls, hit_link=True)
    event = _Event(main_module, "重复歌单 https://163cn.tv/AbC_123", calls)

    asyncio.run(main_module.ParserPlugin.on_message(plugin, event))

    assert event.reactions == []
    assert calls == []


def test_member_isolated_group_blacklist_applies_to_every_group_member():
    main_module = _load_main_module()
    blocked_calls: list[str] = []
    plugin = _plugin(main_module, blocked_calls)
    plugin.cfg.blacklist = ["Napcat_V5:GroupMessage:10001_90001"]
    member_event = _Event(
        main_module,
        "群成员链接 https://163cn.tv/AbC_123",
        blocked_calls,
        umo="Napcat_V5:GroupMessage:10002_90001",
    )

    asyncio.run(main_module.ParserPlugin.on_message(plugin, member_event))

    assert blocked_calls == []

    other_group_calls: list[str] = []
    other_plugin = _plugin(main_module, other_group_calls)
    other_plugin.cfg.blacklist = list(plugin.cfg.blacklist)
    other_group_event = _Event(
        main_module,
        "其他群链接 https://163cn.tv/AbC_123",
        other_group_calls,
        umo="Napcat_V5:GroupMessage:10002_90002",
    )

    asyncio.run(main_module.ParserPlugin.on_message(other_plugin, other_group_event))

    assert other_group_calls == ["parse", "send"]


def test_member_isolated_group_whitelist_allows_every_group_member_only():
    main_module = _load_main_module()
    allowed_calls: list[str] = []
    plugin = _plugin(main_module, allowed_calls)
    plugin.cfg.whitelist = ["Napcat_V5:GroupMessage:10001_90001"]
    member_event = _Event(
        main_module,
        "同群成员链接 https://163cn.tv/AbC_123",
        allowed_calls,
        umo="Napcat_V5:GroupMessage:10002_90001",
    )

    asyncio.run(main_module.ParserPlugin.on_message(plugin, member_event))

    assert allowed_calls == ["parse", "send"]

    blocked_calls: list[str] = []
    other_plugin = _plugin(main_module, blocked_calls)
    other_plugin.cfg.whitelist = list(plugin.cfg.whitelist)
    other_group_event = _Event(
        main_module,
        "其他群链接 https://163cn.tv/AbC_123",
        blocked_calls,
        umo="Napcat_V5:GroupMessage:10002_90002",
    )

    asyncio.run(main_module.ParserPlugin.on_message(other_plugin, other_group_event))

    assert blocked_calls == []


def test_group_disabled_parser_is_filtered_before_matching_and_arbitration():
    main_module = _load_main_module()
    calls: list[str] = []
    plugin = _plugin(main_module, calls)
    _set_group_policy(plugin, disabled={
        "v1:Napcat_V5:90001": {"disabled_parsers": ["netease"]}
    })
    event = _Event(
        main_module, "https://163cn.tv/AbC_123", calls,
        umo="Napcat_V5:GroupMessage:10002_90001",
    )

    asyncio.run(main_module.ParserPlugin.on_message(plugin, event))

    assert calls == []


def test_netease_shortcut_updates_current_group_state_and_skips_arbitration_parse_and_send():
    main_module = _load_main_module()
    saves = []
    cfg = SimpleNamespace(
        whitelist=[], blacklist=[], require_at_in_group=False,
        parser=SimpleNamespace(
            _nodes={key: SimpleNamespace(enable=True) for key in main_module.PARSER_LABELS}
        ),
        _data={}, save_config=lambda: saves.append(True),
    )

    async def get_group_list():
        return [{"group_id": "90001", "group_name": "解析测试群"}]

    adapter = SimpleNamespace(
        meta=SimpleNamespace(id="Napcat_V5", name="aiocqhttp", adapter_display_name="NapCat"),
        get_client=lambda: SimpleNamespace(call_action=lambda action: get_group_list()),
    )
    service = GroupParserConfigService(
        cfg, SimpleNamespace(platform_manager=SimpleNamespace(platform_insts=[adapter]))
    )
    plugin = main_module.ParserPlugin.__new__(main_module.ParserPlugin)
    plugin.cfg = cfg
    plugin.group_parser_config = service

    handler_registry = importlib.import_module("astrbot.core.star.star_handler").star_handlers_registry
    handler = next(
        item for item in handler_registry.get_handlers_by_module_name(main_module.__name__)
        if item.handler_name == "toggle_group_parser"
    )
    command_filter_cls = importlib.import_module("astrbot.core.star.filter.command").CommandFilter
    permission_module = importlib.import_module("astrbot.core.star.filter.permission")
    permission_filter = next(
        item for item in handler.event_filters
        if isinstance(item, permission_module.PermissionTypeFilter)
    )
    command_filter = next(item for item in handler.event_filters if isinstance(item, command_filter_cls))
    assert permission_filter.permission_type == permission_module.PermissionType.ADMIN

    async def collect(result):
        return [item async for item in result]

    command = "关闭网易云解析"
    command_event = _Event(
        main_module, command, [], message_str=command,
        umo="Napcat_V5:GroupMessage:10002_90001", sender_role="bot_admin",
    )
    assert command_filter.filter(command_event, None)
    assert permission_filter.filter(command_event, None)
    assert asyncio.run(collect(main_module.ParserPlugin.toggle_group_parser(plugin, command_event))) == [
        "当前群聊的网易云音乐解析已关闭"
    ]
    assert saves == [True]
    assert "netease" not in group_enabled_parsers(cfg, "Napcat_V5", "90001")

    management_state = asyncio.run(service.get_groups())
    assert "netease" not in management_state["groups"][0]["enabled"]
    assert group_parsing_enabled(cfg, "Napcat_V5", "90001") is True

    calls: list[str] = []
    parser = _Parser(calls)
    parser.platform = SimpleNamespace(name="netease")
    plugin.parser_map = {
        "music.163.com/song": parser,
        "163cn.tv": parser,
    }
    plugin.key_pattern_list = [
        ("music.163.com/song", main_module.re.compile(r"music\.163\.com/song\?id=\d+")),
        ("163cn.tv", main_module.re.compile(r"163cn\.tv/[A-Za-z0-9_-]+")),
    ]
    plugin.debouncer = _Debouncer()
    plugin.sender = _Sender(calls)
    async def compete(**kwargs):
        calls.append("arbitrate")
        return True

    plugin.arbiter = SimpleNamespace(compete=compete)
    link_event = _Event(
        main_module, "https://music.163.com/song?id=123456", calls,
        umo="Napcat_V5:GroupMessage:10002_90001",
    )
    link_event.message_obj.raw_message.update({"message_id": 123, "time": 456, "self_id": 789})
    link_event.bot = object()
    link_event.is_private_chat = lambda: False
    aiocqhttp_event_type = main_module.AiocqhttpMessageEvent
    main_module.AiocqhttpMessageEvent = (aiocqhttp_event_type, _Event)
    try:
        asyncio.run(main_module.ParserPlugin.on_message(plugin, link_event))
    finally:
        main_module.AiocqhttpMessageEvent = aiocqhttp_event_type
    assert calls == []


def test_group_policy_leaves_unmatched_enabled_parser_and_other_groups_untouched():
    main_module = _load_main_module()
    calls: list[str] = []
    plugin = _plugin(main_module, calls)
    _set_group_policy(plugin, disabled={
        "v1:Napcat_V5:90001": {"disabled_parsers": ["bilibili"]}
    })
    event = _Event(
        main_module, "https://163cn.tv/AbC_123", calls,
        umo="Napcat_V5:GroupMessage:10002_90002",
    )

    asyncio.run(main_module.ParserPlugin.on_message(plugin, event))

    assert calls == ["parse", "send"]


def test_group_policy_filters_all_bilibili_keywords_by_parser_platform():
    main_module = _load_main_module()
    calls: list[str] = []
    plugin = _plugin(main_module, calls)
    bilibili = _Parser(calls)
    bilibili.platform = SimpleNamespace(name="bilibili")
    plugin.parser_map = {"/BV": bilibili}
    plugin.key_pattern_list = [
        ("/BV", main_module.re.compile(r"bilibili.com/video/(?P<bvid>BV[0-9A-Za-z]{10})"))
    ]
    _set_group_policy(plugin, disabled={
        "v1:Napcat_V5:90001": {"disabled_parsers": ["bilibili"]}
    })
    event = _Event(
        main_module,
        "https://www.bilibili.com/video/BV1xx411c7mD",
        calls,
        umo="Napcat_V5:GroupMessage:10002_90001",
    )

    asyncio.run(main_module.ParserPlugin.on_message(plugin, event))

    assert calls == []


def test_registered_shortcut_command_requires_bot_admin_and_persists_toggle():
    main_module = _load_main_module()
    plugin = main_module.ParserPlugin.__new__(main_module.ParserPlugin)
    mutations = []

    async def set_group_parser(platform_id, group_id, parser_key, enabled):
        mutations.append((platform_id, group_id, parser_key, enabled))
        return ["netease"] if enabled else []

    plugin.group_parser_config = SimpleNamespace(set_group_parser=set_group_parser)

    async def collect(result):
        return [item async for item in result]

    handler_registry = importlib.import_module("astrbot.core.star.star_handler").star_handlers_registry
    handler = next(
        item for item in handler_registry.get_handlers_by_module_name(main_module.__name__)
        if item.handler_name == "toggle_group_parser"
    )
    command_filter_cls = importlib.import_module("astrbot.core.star.filter.command").CommandFilter
    permission_module = importlib.import_module("astrbot.core.star.filter.permission")
    permission_filter = next(
        item for item in handler.event_filters
        if isinstance(item, permission_module.PermissionTypeFilter)
    )
    command_filter = next(item for item in handler.event_filters if isinstance(item, command_filter_cls))

    group_admin = _Event(
        main_module, "关闭bilibili解析", [], message_str="关闭bilibili解析",
        umo="Napcat_V5:GroupMessage:10002_90001", sender_role="admin",
    )
    assert command_filter.filter(group_admin, None)
    assert not permission_filter.filter(group_admin, None)
    assert asyncio.run(collect(main_module.ParserPlugin.toggle_group_parser(plugin, group_admin))) == [
        "此指令仅限 AstrBot Bot 管理员使用"
    ]
    assert group_admin.stopped is True
    assert mutations == []

    owner = _Event(
        main_module, "关闭bilibili解析", [], message_str="关闭bilibili解析",
        umo="Napcat_V5:GroupMessage:10002_90001", sender_role="owner",
    )
    assert command_filter.filter(owner, None)
    assert not permission_filter.filter(owner, None)
    assert asyncio.run(collect(main_module.ParserPlugin.toggle_group_parser(plugin, owner))) == [
        "此指令仅限 AstrBot Bot 管理员使用"
    ]
    assert owner.stopped is True
    assert mutations == []

    admin = _Event(
        main_module, "关闭bilibili解析", [], message_str="关闭bilibili解析",
        umo="Napcat_V5:GroupMessage:10002_90001", sender_role="bot_admin",
    )
    assert command_filter.filter(admin, None)
    assert permission_filter.filter(admin, None)
    assert asyncio.run(collect(main_module.ParserPlugin.toggle_group_parser(plugin, admin))) == [
        "当前群聊的Bilibili解析已关闭"
    ]
    assert admin.stopped is True
    assert mutations == [("Napcat_V5", "90001", "bilibili", False)]


def test_standard_shortcut_commands_accept_astrbot_wake_prefix_stripping_and_all_parser_labels():
    main_module = _load_main_module()
    plugin = main_module.ParserPlugin.__new__(main_module.ParserPlugin)
    mutations = []

    async def set_group_parser(platform_id, group_id, parser_key, enabled):
        mutations.append((parser_key, enabled))
        return [parser_key] if enabled else []

    plugin.group_parser_config = SimpleNamespace(set_group_parser=set_group_parser)
    handler_registry = importlib.import_module("astrbot.core.star.star_handler").star_handlers_registry
    handler = next(
        item for item in handler_registry.get_handlers_by_module_name(main_module.__name__)
        if item.handler_name == "toggle_group_parser"
    )
    command_filter_cls = importlib.import_module("astrbot.core.star.filter.command").CommandFilter
    permission_module = importlib.import_module("astrbot.core.star.filter.permission")
    permission_filter = next(
        item for item in handler.event_filters
        if isinstance(item, permission_module.PermissionTypeFilter)
    )
    command_filters = [item for item in handler.event_filters if isinstance(item, command_filter_cls)]
    assert len(command_filters) == 1
    assert not any(hasattr(item, "regex") for item in handler.event_filters)
    command_filter = command_filters[0]
    assert permission_filter.permission_type == permission_module.PermissionType.ADMIN

    async def collect(result):
        return [item async for item in result]

    for parser_key, label in main_module.PARSER_LABELS.items():
        if parser_key == "music_base":
            continue
        command = f"关闭{parser_key}解析"
        event = _Event(
            main_module, command, [], message_str=command,
            umo="Napcat_V5:GroupMessage:10002_90001", sender_role="bot_admin",
        )
        # AstrBot removes the configured wake prefix before CommandFilter.
        assert command_filter.filter(event, None), label
        assert permission_filter.filter(event, None), label
        assert asyncio.run(collect(main_module.ParserPlugin.toggle_group_parser(plugin, event))) == [
            f"当前群聊的{label}解析已关闭"
        ]
        open_command = f"开启{parser_key}解析"
        open_event = _Event(
            main_module, open_command, [], message_str=open_command,
            umo="Napcat_V5:GroupMessage:10002_90001", sender_role="bot_admin",
        )
        assert command_filter.filter(open_event, None), open_command
        assert permission_filter.filter(open_event, None), open_command
        assert asyncio.run(collect(main_module.ParserPlugin.toggle_group_parser(plugin, open_event))) == [
            f"当前群聊的{label}解析已启用"
        ]

    assert len(mutations) == len(main_module.PARSER_LABELS) * 2
    assert {key for key, enabled in mutations if enabled is False} == set(main_module.PARSER_LABELS)
    assert {key for key, enabled in mutations if enabled is True} == set(main_module.PARSER_LABELS)

    unstripped = _Event(
        main_module, "/关闭bilibili解析", [], message_str="/关闭bilibili解析",
        umo="Napcat_V5:GroupMessage:10002_90001", sender_role="admin",
    )
    assert not command_filter.filter(unstripped, None)
    not_woken = _Event(
        main_module, "关闭bilibili解析", [], message_str="关闭bilibili解析",
        umo="Napcat_V5:GroupMessage:10002_90001", sender_role="admin",
    )
    not_woken.is_at_or_wake_command = False
    assert not command_filter.filter(not_woken, None)


def test_registered_shortcut_immediately_updates_management_and_matching_state():
    main_module = _load_main_module()
    saves = []
    cfg = SimpleNamespace(
        whitelist=[], blacklist=[], require_at_in_group=False,
        parser=SimpleNamespace(
            _nodes={key: SimpleNamespace(enable=True) for key in main_module.PARSER_LABELS}
        ),
        _data={}, save_config=lambda: saves.append(True),
    )

    async def get_group_list():
        return [{"group_id": "90001", "group_name": "解析测试群"}]

    adapter = SimpleNamespace(
        meta=SimpleNamespace(id="Napcat_V5", name="aiocqhttp", adapter_display_name="NapCat"),
        get_client=lambda: SimpleNamespace(call_action=lambda action: get_group_list()),
    )
    service = GroupParserConfigService(
        cfg, SimpleNamespace(platform_manager=SimpleNamespace(platform_insts=[adapter]))
    )
    plugin = main_module.ParserPlugin.__new__(main_module.ParserPlugin)
    plugin.cfg = cfg
    plugin.group_parser_config = service

    async def collect(result):
        return [item async for item in result]

    command = "关闭bilibili解析"
    command_event = _Event(
        main_module, command, [], message_str=command,
        umo="Napcat_V5:GroupMessage:10002_90001", sender_role="bot_admin",
    )
    handler_registry = importlib.import_module("astrbot.core.star.star_handler").star_handlers_registry
    handler = next(
        item for item in handler_registry.get_handlers_by_module_name(main_module.__name__)
        if item.handler_name == "toggle_group_parser"
    )
    command_filter_cls = importlib.import_module("astrbot.core.star.filter.command").CommandFilter
    permission_module = importlib.import_module("astrbot.core.star.filter.permission")
    permission_filter = next(
        item for item in handler.event_filters
        if isinstance(item, permission_module.PermissionTypeFilter)
    )
    command_filter = next(item for item in handler.event_filters if isinstance(item, command_filter_cls))
    assert permission_filter.permission_type == permission_module.PermissionType.ADMIN
    assert command_filter.filter(command_event, None)
    assert permission_filter.filter(command_event, None)
    assert asyncio.run(collect(main_module.ParserPlugin.toggle_group_parser(plugin, command_event))) == [
        "当前群聊的Bilibili解析已关闭"
    ]

    assert saves == [True]
    assert "bilibili" not in group_enabled_parsers(cfg, "Napcat_V5", "90001")
    management_state = asyncio.run(service.get_groups())
    assert "bilibili" not in management_state["groups"][0]["enabled"]

    netease_command = "关闭网易云解析"
    netease_event = _Event(
        main_module, netease_command, [], message_str=netease_command,
        umo="Napcat_V5:GroupMessage:10002_90001", sender_role="bot_admin",
    )
    assert command_filter.filter(netease_event, None)
    assert permission_filter.filter(netease_event, None)
    assert asyncio.run(collect(main_module.ParserPlugin.toggle_group_parser(plugin, netease_event))) == [
        "当前群聊的网易云音乐解析已关闭"
    ]
    assert saves == [True, True]
    assert "netease" not in group_enabled_parsers(cfg, "Napcat_V5", "90001")
    management_state = asyncio.run(service.get_groups())
    assert "netease" not in management_state["groups"][0]["enabled"]

    calls: list[str] = []
    parser = _Parser(calls)
    parser.platform = SimpleNamespace(name="netease")
    plugin.parser_map = {"bilibili": parser}
    plugin.parser_map = {"music.163.com/song": parser}
    plugin.key_pattern_list = [(
        "music.163.com/song", main_module.re.compile(r"music\.163\.com/song\?id=\d+")
    )]
    plugin.debouncer = _Debouncer()
    plugin.sender = _Sender(calls)
    plugin.arbiter = object()
    link_event = _Event(
        main_module, "https://music.163.com/song?id=123456", calls,
        umo="Napcat_V5:GroupMessage:10002_90001",
    )
    asyncio.run(main_module.ParserPlugin.on_message(plugin, link_event))
    assert calls == []


def test_parser_toggle_commands_write_group_scope_and_report_group_effect():
    main_module = _load_main_module()
    mutations: list[tuple[str, str]] = []
    plugin = main_module.ParserPlugin.__new__(main_module.ParserPlugin)
    plugin.cfg = SimpleNamespace(
        add_blacklist=lambda umo: mutations.append(("add", umo)),
        remove_blacklist=lambda umo: mutations.append(("remove", umo)),
    )
    event = SimpleNamespace(
        unified_msg_origin="Napcat_V5:GroupMessage:10001_90001",
        plain_result=lambda message: message,
    )

    async def collect(result):
        return [item async for item in result]

    closed = asyncio.run(
        collect(main_module.ParserPlugin.close_parser(plugin, event))
    )
    opened = asyncio.run(
        collect(main_module.ParserPlugin.open_parser(plugin, event))
    )

    assert mutations == [
        ("add", "Napcat_V5:GroupMessage:90001"),
        ("remove", "Napcat_V5:GroupMessage:90001"),
    ]
    assert closed == ["当前群聊的解析已关闭"]
    assert opened == ["当前群聊的解析已开启"]


def test_group_total_open_command_runs_before_blacklist_and_close_keeps_parser_settings():
    main_module = _load_main_module()
    plugin = main_module.ParserPlugin.__new__(main_module.ParserPlugin)
    plugin._ensure_parser_registration = lambda: None
    blacklist = ["Napcat_V5:GroupMessage:90001"]
    plugin.cfg = SimpleNamespace(
        whitelist=[], blacklist=blacklist,
        require_at_in_group=False,
        parser=SimpleNamespace(_nodes={"netease": SimpleNamespace(enable=True)}),
        remove_blacklist=lambda scope: blacklist.remove(scope) if scope in blacklist else None,
        add_blacklist=lambda scope: blacklist.append(scope) if scope not in blacklist else None,
    )
    plugin.key_pattern_list = []
    plugin.parser_map = {}

    async def collect(result):
        return [item async for item in result]

    open_event = _Event(
        main_module, "/开启解析", [], message_str="/开启解析",
        umo="Napcat_V5:GroupMessage:10002_90001", sender_role="admin",
    )
    assert asyncio.run(collect(main_module.ParserPlugin.open_parser(plugin, open_event))) == [
        "当前群聊的解析已开启"
    ]
    assert blacklist == []

    close_event = _Event(
        main_module, "/关闭解析", [], message_str="/关闭解析",
        umo="Napcat_V5:GroupMessage:10002_90001", sender_role="admin",
    )
    assert asyncio.run(collect(main_module.ParserPlugin.close_parser(plugin, close_event))) == [
        "当前群聊的解析已关闭"
    ]
    assert blacklist == ["Napcat_V5:GroupMessage:90001"]


def test_matching_does_not_call_reaction():
    main_module = _load_main_module()
    calls: list[str] = []
    plugin = _plugin(main_module, calls)
    event = _Event(
        main_module,
        "推荐歌单 https://163cn.tv/AbC_123",
        calls,
        fail_react=True,
    )

    asyncio.run(main_module.ParserPlugin.on_message(plugin, event))

    assert event.reactions == []
    assert calls == ["parse", "send"]


def test_plain_message_chain_is_used_when_adapter_message_text_is_empty():
    main_module = _load_main_module()
    calls: list[str] = []
    plugin = _plugin(main_module, calls)
    event = _Event(
        main_module,
        "推荐歌单 https://163cn.tv/AbC_123",
        calls,
        message_str="",
    )

    asyncio.run(main_module.ParserPlugin.on_message(plugin, event))

    assert event.reactions == []
    assert calls == ["parse", "send"]


def test_matching_creates_a_parse_cache_directory(tmp_path: Path):
    main_module = _load_main_module()
    calls: list[str] = []
    plugin = _plugin(main_module, calls, cache_dir=tmp_path / "cache")
    event = _Event(main_module, "推荐歌单 https://163cn.tv/AbC_123", calls)

    asyncio.run(main_module.ParserPlugin.on_message(plugin, event))

    result = plugin.parser_map["163cn.tv"].results[0]
    assert result.cache_dir is not None
    assert result.cache_dir.parent == tmp_path / "cache"
    assert result.cache_dir.is_dir()


def test_initialize_checks_kugou_cookie_after_registering_parsers(monkeypatch):
    main_module = _load_main_module()
    calls: list[str] = []

    async def complete(name: str):
        calls.append(name)

    plugin = main_module.ParserPlugin.__new__(main_module.ParserPlugin)
    plugin.cleaner = SimpleNamespace(
        clean_stale_playwright_profiles=lambda: complete("clean")
    )
    plugin.renderer = SimpleNamespace(start=lambda: complete("renderer"))
    plugin._register_parser = lambda: calls.append("register")
    plugin._check_qqmusic_cookie = lambda: complete("qq-cookie")
    plugin._check_kugou_cookie = lambda: complete("kugou-cookie")
    monkeypatch.setattr(
        main_module.Renderer, "load_resources", staticmethod(lambda: None)
    )

    asyncio.run(plugin.initialize())

    assert calls == ["clean", "renderer", "register", "qq-cookie", "kugou-cookie"]


def test_platform_account_statuses_distinguish_login_cookies_and_disabled_parsers():
    main_module = _load_main_module()

    class _AccountParser:
        def __init__(self, name, *, auth=False, cookie=False, bilibili=False):
            self.platform = SimpleNamespace(name=name)
            self._auth = auth
            self._cookie = cookie
            if bilibili:
                self.login = SimpleNamespace(has_cookie=lambda: cookie)

        def has_login_cookie(self):
            return self._auth

        def has_auth_credentials(self):
            return self._auth

        def has_cookie(self):
            return self._cookie

    plugin = main_module.ParserPlugin.__new__(main_module.ParserPlugin)
    plugin.cfg = SimpleNamespace(parser=SimpleNamespace(
        douyin=SimpleNamespace(enable=True),
        bilibili=SimpleNamespace(enable=True),
        qqmusic=SimpleNamespace(enable=True),
        kugou=SimpleNamespace(enable=False),
    ))
    douyin = _AccountParser("douyin", auth=False, cookie=True)
    bilibili = _AccountParser("bilibili", cookie=True, bilibili=True)
    qqmusic = _AccountParser("qqmusic", auth=False, cookie=True)
    kugou = _AccountParser("kugou", auth=True, cookie=True)
    plugin.parser_map = {
        "douyin.com": douyin,
        "v.douyin.com": douyin,
        "bilibili.com": bilibili,
        "y.qq.com": qqmusic,
        "kugou.com": kugou,
    }

    statuses = {
        item["key"]: item
        for item in main_module.ParserPlugin._platform_account_statuses(plugin)
    }

    assert statuses["douyin"] == {
        "key": "douyin", "available": False, "text": "登录 Cookie 未配置"
    }
    assert statuses["bilibili"]["available"] is True
    assert statuses["bilibili"]["text"] == "登录 Cookie 已配置"
    assert statuses["qqmusic"] == {
        "key": "qqmusic", "available": False, "text": "账号 Cookie 不完整"
    }
    assert statuses["kugou"]["text"] == "解析器未启用"
