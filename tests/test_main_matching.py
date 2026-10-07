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
    ):
        self.message_str = text if message_str is None else message_str
        self._chain = [main_module.Plain(text)]
        self.reactions: list[str] = []
        self.calls = calls
        self.fail_react = fail_react

    @property
    def unified_msg_origin(self):
        return "test:FriendMessage:1"

    def get_messages(self):
        return self._chain

    def get_self_id(self):
        return "bot"

    def get_message_type(self):
        return object()

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
        **({"cache_dir": cache_dir} if cache_dir is not None else {}),
    )
    plugin.key_pattern_list = [
        ("163cn.tv", main_module.re.compile(r"163cn\.tv/[A-Za-z0-9_-]+"))
    ]
    plugin.parser_map = {"163cn.tv": _Parser(calls)}
    plugin.debouncer = _Debouncer(hit_link=hit_link)
    plugin.sender = _Sender(calls)
    plugin.arbiter = object()
    return plugin


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
