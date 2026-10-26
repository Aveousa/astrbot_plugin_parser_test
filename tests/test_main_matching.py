from __future__ import annotations

import asyncio
import importlib
import sys
import types
from pathlib import Path
from types import SimpleNamespace

from core.data import ParseResult, Platform


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
        self.message_obj = SimpleNamespace(raw_message={"sender": {"role": sender_role}})

    @property
    def unified_msg_origin(self):
        return self._umo

    def get_messages(self):
        return self._chain

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


def test_shortcut_command_requires_group_admin_and_persists_toggle():
    main_module = _load_main_module()
    plugin = main_module.ParserPlugin.__new__(main_module.ParserPlugin)
    mutations = []

    async def set_group_parser(platform_id, group_id, parser_key, enabled):
        mutations.append((platform_id, group_id, parser_key, enabled))
        return ["netease"] if enabled else []

    plugin.group_parser_config = SimpleNamespace(set_group_parser=set_group_parser)

    async def collect(result):
        return [item async for item in result]

    member = _Event(
        main_module, "/关闭bilibili解析", [], message_str="/关闭bilibili解析",
        umo="Napcat_V5:GroupMessage:10002_90001", sender_role="member",
    )
    assert asyncio.run(collect(main_module.ParserPlugin.toggle_group_parser(plugin, member))) == [
        "此指令仅限群主或群管理员使用"
    ]
    assert member.stopped is True
    assert mutations == []

    admin = _Event(
        main_module, "/关闭bilibili解析", [], message_str="/关闭bilibili解析",
        umo="Napcat_V5:GroupMessage:10002_90001", sender_role="admin",
    )
    assert asyncio.run(collect(main_module.ParserPlugin.toggle_group_parser(plugin, admin))) == [
        "当前群聊的Bilibili解析已关闭"
    ]
    assert admin.stopped is True
    assert mutations == [("Napcat_V5", "90001", "bilibili", False)]


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
