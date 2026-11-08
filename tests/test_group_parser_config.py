from __future__ import annotations

import asyncio
import copy
import json
import threading
import time
from types import SimpleNamespace

import pytest

from core.apple_music_backend import (
    AppleMusicBackend,
    AppleMusicBackendError,
    BackendSettings,
)
from core.group_parser_config import (
    GROUP_PARSER_SETTINGS_KEY,
    QQ_OFFICIAL_KNOWN_GROUPS_KEY,
    GroupParserConfigService,
    group_enabled_parsers,
    parser_key_for_keyword,
)


class _Adapter:
    def __init__(self, platform_id="Napcat_V5", groups=None, name="aiocqhttp"):
        self.meta = SimpleNamespace(
            id=platform_id,
            name=name,
            adapter_display_name="NapCat",
        )
        self.groups = groups or []
        self.calls = []
        self.client = SimpleNamespace(call_action=self.call_action)

    def get_client(self):
        return self.client

    async def call_action(self, action, **kwargs):
        assert action == "get_group_list"
        self.calls.append((action, kwargs))
        return self.groups


def _config(*, disabled=(), save_error=None):
    keys = (
        "bilibili", "douyin", "xhs", "pixiv", "qqmusic", "netease",
        "kugou", "qishui", "kuwo", "applemusic",
    )
    parser = SimpleNamespace(
        _nodes={key: SimpleNamespace(enable=key not in disabled) for key in keys}
    )
    data = {}
    saves = []

    def save_config():
        saves.append(True)
        if save_error is not None:
            raise save_error

    cfg = SimpleNamespace(
        parser=parser,
        _data=data,
        save_config=save_config,
        cleanup_blacklist_on_group_leave=True,
    )
    return cfg, saves


def _service(cfg, groups=None, *, platform_id="Napcat_V5"):
    adapter = _Adapter(platform_id, groups or [
        {"group_id": "90001", "group_name": "解析测试群"},
        {"group_id": "90002", "group_name": "第二个群"},
    ])
    context = SimpleNamespace(
        platform_manager=SimpleNamespace(platform_insts=[adapter])
    )
    return GroupParserConfigService(cfg, context)


def _multi_service(cfg, *adapters):
    return GroupParserConfigService(cfg, SimpleNamespace(
        platform_manager=SimpleNamespace(platform_insts=list(adapters))
    ))


def _failing_adapter(platform_id="Unavailable"):
    adapter = _Adapter(platform_id)

    async def fail(*_args, **_kwargs):
        raise RuntimeError("private token must not reach the management page")

    adapter.client.call_action = fail
    return adapter


def test_group_policy_is_isolated_by_instance_and_group_id():
    cfg, _ = _config()
    cfg._data[GROUP_PARSER_SETTINGS_KEY] = {
        "v1:Napcat_V5:90001": {"disabled_parsers": ["netease"]}
    }

    assert "netease" not in group_enabled_parsers(cfg, "Napcat_V5", "90001")
    assert "netease" in group_enabled_parsers(cfg, "Napcat_V5", "90002")
    assert "netease" in group_enabled_parsers(cfg, "AnotherBot", "90001")


def test_group_policy_reads_schema_stable_list_and_ignores_invalid_entries():
    cfg, _ = _config()
    cfg._data[GROUP_PARSER_SETTINGS_KEY] = [
        {
            "platform_id": "Napcat_V5",
            "group_id": "90001",
            "disabled_parsers": ["netease"],
        },
        {"platform_id": "invalid/id", "group_id": "90002", "disabled_parsers": ["pixiv"]},
        "not-an-entry",
    ]

    assert "netease" not in group_enabled_parsers(cfg, "Napcat_V5", "90001")
    assert "pixiv" in group_enabled_parsers(cfg, "Napcat_V5", "90002")


def test_group_list_reports_live_groups_and_effective_parser_status():
    cfg, _ = _config()
    cfg._data[GROUP_PARSER_SETTINGS_KEY] = {
        "v1:Napcat_V5:90001": {"disabled_parsers": ["bilibili", "netease"]}
    }
    service = _service(cfg, [
        {"group_id": "90001", "group_name": "解析测试群"},
        {"group_id": "not-an-id", "group_name": "忽略无效群"},
    ])

    result = asyncio.run(service.get_groups())

    assert len(result["groups"]) == 1
    group = result["groups"][0]
    assert group["name"] == "解析测试群"
    assert group["parsing_enabled"] is True
    assert "bilibili" not in group["enabled"]
    assert "netease" not in group["enabled"]
    assert {item["key"] for item in result["parsers"]} >= {"bilibili", "netease"}
    adapter = service._adapters()[0]
    assert adapter.calls == [("get_group_list", {})]

    asyncio.run(service.get_groups(force_refresh=True))
    assert adapter.calls[-1] == ("get_group_list", {"no_cache": True})


@pytest.mark.parametrize("failure", ["exception", "invalid", "invalid_items", "no_client", "broken_client"])
def test_group_query_failure_is_isolated_from_other_onebot_and_qq_instances(failure):
    cfg, saves = _config()
    cfg._data[QQ_OFFICIAL_KNOWN_GROUPS_KEY] = [{
        "platform_id": "QQBot", "group_id": "openid-A", "name": "官方测试群",
    }]
    failed = _failing_adapter()
    if failure in {"invalid", "invalid_items"}:
        failed.client.call_action = failed.call_action
        failed.groups = {} if failure == "invalid" else [{"bad": "response"}]
    elif failure == "no_client":
        failed.client = None
    elif failure == "broken_client":
        def broken_client():
            raise RuntimeError("private token")
        failed.get_client = broken_client
    good = _Adapter("Working", [{"group_id": "90001", "group_name": "正常群"}])
    official = _Adapter("QQBot", name="qq_official")
    result = asyncio.run(_multi_service(cfg, failed, good, official).get_groups())
    assert {group["platform_id"] for group in result["groups"]} == {"Working", "QQBot"}
    assert len(result["instance_errors"]) == 1
    assert result["instance_errors"][0]["platform_id"] == "Unavailable"
    assert "private" not in result["instance_errors"][0]["error"]
    assert official.calls == []
    assert saves == []


def test_onebot_query_timeout_cancels_request_and_preserves_other_instances(monkeypatch):
    monkeypatch.setattr("core.group_parser_config.GROUP_QUERY_TIMEOUT", 0.02)
    cfg, _ = _config()
    slow = _Adapter("Slow")
    cancelled = []

    async def hang(*_args, **_kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    slow.client.call_action = hang
    good = _Adapter("Working", [{"group_id": "90001"}])

    async def run():
        return await asyncio.wait_for(_multi_service(cfg, slow, good).get_groups(), 1)

    result = asyncio.run(run())
    assert cancelled == [True]
    assert [group["platform_id"] for group in result["groups"]] == ["Working"]
    assert "超时" in result["instance_errors"][0]["error"]


def test_cancelled_group_query_propagates_and_drains_all_instance_tasks():
    cfg, saves = _config()

    async def run():
        started = asyncio.Event()
        active = 0
        finished = 0

        async def hang(*_args, **_kwargs):
            nonlocal active, finished
            active += 1
            if active == 2:
                started.set()
            try:
                await asyncio.Event().wait()
            finally:
                finished += 1

        adapters = [_Adapter("A"), _Adapter("B")]
        for adapter in adapters:
            adapter.client.call_action = hang
        task = asyncio.create_task(_multi_service(cfg, *adapters).save_group_settings([]))
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished == 2
        assert not [item for item in asyncio.all_tasks() if item is not asyncio.current_task() and not item.done()]

    asyncio.run(run())
    assert cfg._data == {}
    assert saves == []


def test_group_query_concurrency_is_bounded(monkeypatch):
    monkeypatch.setattr("core.group_parser_config.GROUP_QUERY_CONCURRENCY", 2)
    cfg, _ = _config()
    active = peak = 0

    async def query(*_args, **_kwargs):
        nonlocal active, peak
        active += 1
        peak = max(active, peak)
        try:
            await asyncio.sleep(0.005)
            return []
        finally:
            active -= 1

    adapters = [_Adapter(f"Bot{index}") for index in range(7)]
    for adapter in adapters:
        adapter.client.call_action = query
    result = asyncio.run(_multi_service(cfg, *adapters).get_groups())
    assert peak == 2
    assert active == 0
    assert result["instance_errors"] == []


@pytest.mark.parametrize("disk_error", [False, True])
def test_partial_save_preserves_failed_instance_and_qq_history_and_can_retry(disk_error):
    cfg, saves = _config(save_error=OSError("disk full") if disk_error else None)
    cfg._data[GROUP_PARSER_SETTINGS_KEY] = [
        {"platform_id": platform, "group_id": group, "disabled_parsers": ["pixiv"]}
        for platform, group in [("Failed", "90001"), ("Failed", "90099"),
                                ("Working", "90099"), ("QQBot", "historic-openid")]
    ]
    cfg._data["blacklist"] = [f"{platform}:GroupMessage:{group}" for platform, group in [
        ("Failed", "90001"), ("Failed", "90099"), ("Working", "90099"), ("QQBot", "historic-openid")
    ]]
    cfg._data[QQ_OFFICIAL_KNOWN_GROUPS_KEY] = [{
        "platform_id": "QQBot", "group_id": "openid-A", "name": "官方测试群",
    }]
    original = copy.deepcopy(cfg._data)
    failed = _failing_adapter("Failed")
    good = _Adapter("Working", [{"group_id": "90001"}])
    official = _Adapter("QQBot", name="qq_official")
    service = _multi_service(cfg, failed, good, official)
    drafts = [{"platform_id": platform, "group_id": group, "enabled": ["bilibili"], "parsing_enabled": False}
              for platform, group in [("Failed", "90001"), ("Working", "90001"), ("QQBot", "openid-A")]]
    if disk_error:
        with pytest.raises(AppleMusicBackendError, match="保存失败"):
            asyncio.run(service.save_group_settings(drafts))
        assert cfg._data == original
        return
    result = asyncio.run(service.save_group_settings(drafts))
    assert {item["platform_id"] for item in result["saved_groups"]} == {"Working", "QQBot"}
    assert [item["platform_id"] for item in result["unsaved_groups"]] == ["Failed"]
    assert result["cleaned_group_count"] == result["cleaned_blacklist_group_count"] == 1
    assert group_enabled_parsers(cfg, "Working", "90001") == {"bilibili"}
    assert group_enabled_parsers(cfg, "QQBot", "openid-A") == {"bilibili"}
    failed_entries = [item for item in cfg._data[GROUP_PARSER_SETTINGS_KEY] if item["platform_id"] == "Failed"]
    assert failed_entries == original[GROUP_PARSER_SETTINGS_KEY][:2]
    assert "QQBot:GroupMessage:historic-openid" in cfg._data["blacklist"]
    assert "Failed:GroupMessage:90099" in cfg._data["blacklist"]
    assert cfg._data[QQ_OFFICIAL_KNOWN_GROUPS_KEY] == original[QQ_OFFICIAL_KNOWN_GROUPS_KEY]
    assert saves == [True]
    failed.client.call_action = failed.call_action
    failed.groups = [{"group_id": "90001"}, {"group_id": "90099"}]
    retried = asyncio.run(service.save_group_settings(drafts[:1]))
    assert retried["unsaved_groups"] == []
    assert group_enabled_parsers(cfg, "Failed", "90001") == {"bilibili"}
    assert group_enabled_parsers(cfg, "Working", "90001") == {"bilibili"}
    assert saves == [True, True]


def test_all_failed_instances_report_errors_and_do_not_persist():
    cfg, saves = _config()
    cfg._data["blacklist"] = ["Failed:GroupMessage:90001"]
    original = copy.deepcopy(cfg._data)
    service = _multi_service(cfg, _failing_adapter("Failed"))
    result = asyncio.run(service.get_groups())
    assert result["groups"] == []
    assert result["instance_errors"][0]["platform_id"] == "Failed"
    with pytest.raises(AppleMusicBackendError, match="未保存任何修改"):
        asyncio.run(service.save_group_settings([{
            "platform_id": "Failed", "group_id": "90001", "enabled": [], "parsing_enabled": True,
        }]))
    assert cfg._data == original
    assert saves == []


@pytest.mark.parametrize("platform_id", ["Failed", "Removed", "QQBot"])
def test_skipped_submission_does_not_persist_or_claim_success(platform_id):
    cfg, saves = _config()
    service = _multi_service(cfg, _failing_adapter("Failed"), _Adapter("QQBot", name="qq_official"))
    result = asyncio.run(service.save_group_settings([{
        "platform_id": platform_id, "group_id": "90001", "enabled": [], "parsing_enabled": True,
    }]))
    assert result["saved_groups"] == []
    assert len(result["unsaved_groups"]) == 1
    assert cfg._data == {}
    assert saves == []


def test_successfully_empty_roster_confirms_departure_but_failure_does_not():
    cfg, _ = _config()
    cfg._data[GROUP_PARSER_SETTINGS_KEY] = [
        {"platform_id": platform, "group_id": "90001", "disabled_parsers": ["pixiv"]}
        for platform in ["Empty", "Failed"]
    ]
    cfg._data["blacklist"] = ["Empty:GroupMessage:90001", "Failed:GroupMessage:90001"]
    service = _multi_service(cfg, _Adapter("Empty"), _failing_adapter("Failed"))
    result = asyncio.run(service.save_group_settings([{
        "platform_id": "Empty", "group_id": "90001", "enabled": [], "parsing_enabled": True,
    }]))
    assert result["departed_groups"] == [{"platform_id": "Empty", "group_id": "90001"}]
    assert result["saved_groups"] == []
    assert [item["platform_id"] for item in cfg._data[GROUP_PARSER_SETTINGS_KEY]] == ["Failed"]
    assert cfg._data["blacklist"] == ["Failed:GroupMessage:90001"]


@pytest.mark.parametrize("platform_name", ["aiocqhttp", "qq_official"])
def test_group_remark_roundtrips_and_can_be_edited_or_cleared(platform_name):
    cfg, saves = _config()
    adapter = _Adapter("Bot", [{"group_id": "90001", "group_name": "真实群名"}], name=platform_name)
    service = _multi_service(cfg, adapter)
    if platform_name == "qq_official":
        asyncio.run(service.remember_qq_official_group("Bot", "90001", "真实群名"))
    draft = {
        "platform_id": "Bot", "group_id": "90001",
        "enabled": sorted(group_enabled_parsers(cfg, "Bot", "90001")),
        "parsing_enabled": True, "remark": "  自定义备注🍰  ",
    }
    before = copy.deepcopy(asyncio.run(service.get_groups()))
    result = asyncio.run(service.save_group_settings([draft]))
    assert result["saved_groups"] == [{"platform_id": "Bot", "group_id": "90001"}]
    assert cfg._data[GROUP_PARSER_SETTINGS_KEY] == [{
        "platform_id": "Bot", "group_id": "90001", "disabled_parsers": [], "remark": "自定义备注🍰",
    }]
    # Emulate a restart using the serialized config, not page-local state.
    cfg._data = json.loads(json.dumps(cfg._data, ensure_ascii=False))
    service = _multi_service(cfg, adapter)
    group = asyncio.run(service.get_groups())["groups"][0]
    assert group["remark"] == "自定义备注🍰"
    assert group["name"] == before["groups"][0]["name"] == "真实群名"
    assert group["enabled"] == before["groups"][0]["enabled"]
    assert group["parsing_enabled"] is True
    if platform_name == "qq_official":
        asyncio.run(service.remember_qq_official_group("Bot", "90001", "新真实群名"))
        assert asyncio.run(service.get_groups())["groups"][0]["remark"] == "自定义备注🍰"
        assert cfg._data[QQ_OFFICIAL_KNOWN_GROUPS_KEY][0]["name"] == "新真实群名"
    draft["remark"] = "更新后的备注"
    asyncio.run(service.save_group_settings([draft]))
    assert asyncio.run(service.get_groups())["groups"][0]["remark"] == "更新后的备注"
    draft["remark"] = "   "
    asyncio.run(service.save_group_settings([draft]))
    assert cfg._data[GROUP_PARSER_SETTINGS_KEY] == []
    assert asyncio.run(service.get_groups())["groups"][0]["remark"] == ""
    assert saves


@pytest.mark.parametrize("platform_name", ["aiocqhttp", "qq_official"])
def test_group_commands_and_legacy_requests_preserve_remarks(platform_name):
    cfg, _ = _config()
    adapter = _Adapter("Bot", [{"group_id": "90001"}], name=platform_name)
    cfg._data[GROUP_PARSER_SETTINGS_KEY] = [{
        "platform_id": "Bot", "group_id": "90001", "disabled_parsers": [], "remark": "保留的备注",
    }]
    service = _multi_service(cfg, adapter)
    if platform_name == "qq_official":
        asyncio.run(service.remember_qq_official_group("Bot", "90001"))
    for enabled in (False, True):
        asyncio.run(service.set_group_parser("Bot", "90001", "bilibili", enabled))
        stored = cfg._data[GROUP_PARSER_SETTINGS_KEY][0]
        assert stored["remark"] == "保留的备注"
        assert ("bilibili" in stored["disabled_parsers"]) is not enabled
    asyncio.run(service.save_group_settings([{
        "platform_id": "Bot", "group_id": "90001",
        "enabled": ["bilibili"], "parsing_enabled": False,
    }]))
    assert cfg._data[GROUP_PARSER_SETTINGS_KEY][0]["remark"] == "保留的备注"
    # Clearing a remark must keep the group policy and total switch intact.
    asyncio.run(service.save_group_settings([{
        "platform_id": "Bot", "group_id": "90001", "remark": "",
        "enabled": ["bilibili"], "parsing_enabled": False,
    }]))
    assert "remark" not in cfg._data[GROUP_PARSER_SETTINGS_KEY][0]
    group = asyncio.run(service.get_groups())["groups"][0]
    assert group["remark"] == ""
    assert group["enabled"] == ["bilibili"]
    assert group["parsing_enabled"] is False


@pytest.mark.parametrize("remark", [None, 10, [], {}, "长" * 129, "a\nb", "\ra", "a\t", "a\x00b", "a\x7fb"])
def test_invalid_group_remark_is_rejected_without_writes(remark):
    cfg, saves = _config()
    service = _service(cfg)
    with pytest.raises(AppleMusicBackendError, match="群聊备注名"):
        asyncio.run(service.save_group_settings([{
            "platform_id": "Napcat_V5", "group_id": "90001", "remark": remark,
            "enabled": [], "parsing_enabled": True,
        }]))
    assert cfg._data == {}
    assert saves == []
    assert service._adapters()[0].calls == []


def test_concurrent_remark_saves_are_isolated_by_instance_and_group():
    cfg, saves = _config()
    adapters = [_Adapter(platform, [{"group_id": "90001"}, {"group_id": "90002"}])
                for platform in ["A", "B"]]
    service = _multi_service(cfg, *adapters)

    async def run():
        await asyncio.gather(*(service.save_group_settings([{
            "platform_id": platform, "group_id": group, "remark": remark,
            "enabled": sorted(group_enabled_parsers(cfg, platform, group)), "parsing_enabled": True,
        }]) for platform, group, remark in [("A", "90001", "A的备注"), ("B", "90001", "B的备注"),
                                           ("A", "90002", "长" * 128)]))

    asyncio.run(run())
    remarks = {(group["platform_id"], group["group_id"]): group["remark"]
               for group in asyncio.run(service.get_groups())["groups"]}
    assert remarks == {("A", "90001"): "A的备注", ("B", "90001"): "B的备注",
                       ("A", "90002"): "长" * 128, ("B", "90002"): ""}
    assert len(saves) == 3


def test_remark_partial_save_preserves_failed_drafts_and_rolls_back_disk_errors():
    cfg, saves = _config()
    cfg._data[GROUP_PARSER_SETTINGS_KEY] = [
        {"platform_id": platform, "group_id": "90001", "disabled_parsers": [], "remark": "旧备注"}
        for platform in ["Failed", "Working"]
    ]
    service = _multi_service(cfg, _failing_adapter("Failed"), _Adapter("Working", [{"group_id": "90001"}]))
    drafts = [{"platform_id": platform, "group_id": "90001", "remark": "新备注",
               "enabled": sorted(group_enabled_parsers(cfg, platform, "90001")), "parsing_enabled": True}
              for platform in ["Failed", "Working"]]
    result = asyncio.run(service.save_group_settings(drafts))
    assert [item["platform_id"] for item in result["unsaved_groups"]] == ["Failed"]
    assert [item["remark"] for item in cfg._data[GROUP_PARSER_SETTINGS_KEY]] == ["旧备注", "新备注"]
    original = copy.deepcopy(cfg._data)

    def fail_save():
        raise OSError("disk full")

    cfg.save_config = fail_save
    drafts[1]["remark"] = "不应保存"
    drafts[1]["parsing_enabled"] = False
    with pytest.raises(AppleMusicBackendError, match="保存失败"):
        asyncio.run(service.save_group_settings(drafts))
    assert cfg._data == original
    assert saves == [True]


def test_qq_official_group_list_uses_observed_groups_without_roster_api():
    cfg, saves = _config()
    cfg._data[QQ_OFFICIAL_KNOWN_GROUPS_KEY] = [{
        "platform_id": "QQBot_1",
        "group_id": "group-openid-A1",
        "name": "官方测试群",
    }]
    adapter = _Adapter("QQBot_1", name="qq_official")
    service = GroupParserConfigService(
        cfg, SimpleNamespace(platform_manager=SimpleNamespace(platform_insts=[adapter]))
    )

    result = asyncio.run(service.get_groups())

    assert result["groups"][0]["platform_id"] == "QQBot_1"
    assert result["groups"][0]["group_id"] == "group-openid-A1"
    assert result["groups"][0]["name"] == "官方测试群"
    assert adapter.calls == []
    assert saves == []


def test_qq_official_group_can_be_remembered_and_toggled():
    cfg, saves = _config()
    adapter = _Adapter("QQBot_1", name="qq_official")
    service = GroupParserConfigService(
        cfg, SimpleNamespace(platform_manager=SimpleNamespace(platform_insts=[adapter]))
    )

    assert asyncio.run(
        service.remember_qq_official_group("QQBot_1", "group-openid-A1", "官方测试群")
    ) is True
    enabled = asyncio.run(
        service.set_group_parser("QQBot_1", "group-openid-A1", "netease", False)
    )

    assert "netease" not in enabled
    assert cfg._data[QQ_OFFICIAL_KNOWN_GROUPS_KEY][0]["group_id"] == "group-openid-A1"
    assert saves == [True, True]


def test_known_qq_official_group_uses_lock_free_fast_path():
    cfg, saves = _config()
    cfg._data[QQ_OFFICIAL_KNOWN_GROUPS_KEY] = [{
        "platform_id": "QQBot_1",
        "group_id": "group-openid-A1",
        "name": "官方测试群",
    }]
    service = GroupParserConfigService(cfg, SimpleNamespace())

    class FailingLock:
        async def __aenter__(self):
            raise AssertionError("已记录群不应进入群配置锁")

        async def __aexit__(self, *_args):
            return None

    service.lock = FailingLock()

    assert asyncio.run(
        service.remember_qq_official_group("QQBot_1", "group-openid-A1")
    ) is False
    assert saves == []


def test_concurrent_first_observation_persists_one_qq_official_group():
    cfg, saves = _config()
    adapter = _Adapter("QQBot_1", name="qq_official")
    service = GroupParserConfigService(
        cfg, SimpleNamespace(platform_manager=SimpleNamespace(platform_insts=[adapter]))
    )

    async def run():
        return await asyncio.gather(*(
            service.remember_qq_official_group(
                "QQBot_1", "group-openid-A1", "官方测试群"
            )
            for _ in range(8)
        ))

    results = asyncio.run(run())

    assert results.count(True) == 1
    assert results.count(False) == 7
    assert saves == [True]
    assert cfg._data[QQ_OFFICIAL_KNOWN_GROUPS_KEY] == [{
        "platform_id": "QQBot_1",
        "group_id": "group-openid-A1",
        "name": "官方测试群",
    }]


def test_cancelled_group_record_waits_for_save_and_then_updates_index():
    cfg, _ = _config()
    started = threading.Event()
    release = threading.Event()

    def save_config():
        started.set()
        assert release.wait(timeout=2)

    cfg.save_config = save_config
    adapter = _Adapter("QQBot_1", name="qq_official")
    service = GroupParserConfigService(
        cfg, SimpleNamespace(platform_manager=SimpleNamespace(platform_insts=[adapter]))
    )

    async def run():
        task = asyncio.create_task(service.remember_qq_official_group(
            "QQBot_1", "group-openid-A1", "官方测试群"
        ))
        await asyncio.wait_for(asyncio.to_thread(started.wait, 1), timeout=2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())

    assert service._qq_official_group_index[
        ("QQBot_1", "group-openid-A1")
    ] == "官方测试群"
    assert cfg._data[QQ_OFFICIAL_KNOWN_GROUPS_KEY][0]["group_id"] == "group-openid-A1"


def test_failed_group_record_rolls_back_without_updating_index():
    cfg, _ = _config(save_error=OSError("disk full"))
    adapter = _Adapter("QQBot_1", name="qq_official")
    service = GroupParserConfigService(
        cfg, SimpleNamespace(platform_manager=SimpleNamespace(platform_insts=[adapter]))
    )

    with pytest.raises(AppleMusicBackendError, match="记录保存失败"):
        asyncio.run(service.remember_qq_official_group(
            "QQBot_1", "group-openid-A1", "官方测试群"
        ))

    assert QQ_OFFICIAL_KNOWN_GROUPS_KEY not in cfg._data
    assert ("QQBot_1", "group-openid-A1") not in service._qq_official_group_index


def test_group_index_refresh_rebuilds_from_reloaded_configuration():
    cfg, saves = _config()
    service = _service(cfg)
    cfg._data[QQ_OFFICIAL_KNOWN_GROUPS_KEY] = [{
        "platform_id": "QQBot_1",
        "group_id": "group-reloaded",
        "name": "Reloaded group",
    }]

    service.refresh_qq_official_group_index()

    assert service._qq_official_group_index == {
        ("QQBot_1", "group-reloaded"): "Reloaded group"
    }
    assert asyncio.run(service.remember_qq_official_group(
        "QQBot_1", "group-reloaded"
    )) is False
    assert saves == []


def test_group_list_reports_total_switch_from_canonicalized_blacklist_scope():
    cfg, _ = _config()
    cfg.blacklist = ["Napcat_V5:GroupMessage:10001_90001"]
    service = _service(cfg)

    result = asyncio.run(service.get_groups())

    assert result["groups"][0]["parsing_enabled"] is False
    assert result["groups"][1]["parsing_enabled"] is True


def test_explicit_save_persists_group_draft_and_only_changes_target_group():
    cfg, saves = _config()
    service = _service(cfg)
    assert saves == []

    result = asyncio.run(service.save_group_settings([
        {
            "platform_id": "Napcat_V5", "group_id": "90001",
            "enabled": ["bilibili"], "parsing_enabled": False,
        }
    ]))

    assert result["message"] == "群聊总开关及解析器设置已确认并保存"
    assert result["cleaned_group_count"] == 0
    assert result["cleaned_blacklist_group_count"] == 0
    assert saves == [True]
    assert service._adapters()[0].calls[-1] == ("get_group_list", {"no_cache": True})
    assert group_enabled_parsers(cfg, "Napcat_V5", "90001") == {"bilibili"}
    assert len(group_enabled_parsers(cfg, "Napcat_V5", "90002")) == 10
    assert cfg._data["blacklist"] == ["Napcat_V5:GroupMessage:90001"]


def test_partial_submission_does_not_reset_another_live_group():
    cfg, _ = _config()
    asyncio.run(GroupParserConfigService(cfg, _service(cfg).context).set_group_parser(
        "Napcat_V5", "90002", "pixiv", False
    ))
    service = _service(cfg)

    asyncio.run(service.save_group_settings([
        {
            "platform_id": "Napcat_V5", "group_id": "90001",
            "enabled": ["bilibili"], "parsing_enabled": True,
        }
    ]))

    assert group_enabled_parsers(cfg, "Napcat_V5", "90001") == {"bilibili"}
    assert "pixiv" not in group_enabled_parsers(cfg, "Napcat_V5", "90002")


def test_failed_save_rolls_back_group_configuration():
    cfg, _ = _config(save_error=OSError("disk full"))
    original = {"v1:Napcat_V5:90002": {"disabled_parsers": ["pixiv"]}}
    cfg._data[GROUP_PARSER_SETTINGS_KEY] = original.copy()
    service = _service(cfg)

    with pytest.raises(AppleMusicBackendError, match="保存失败"):
        asyncio.run(service.save_group_settings([
            {
                "platform_id": "Napcat_V5", "group_id": "90001",
                "enabled": ["bilibili"], "parsing_enabled": True,
            }
        ]))

    assert cfg._data[GROUP_PARSER_SETTINGS_KEY] == original


def test_group_cleanup_does_not_prune_settings_for_disconnected_adapter():
    cfg, _ = _config()
    cfg._data[GROUP_PARSER_SETTINGS_KEY] = [
        {
            "platform_id": "Napcat_V5",
            "group_id": "90003",
            "disabled_parsers": ["bilibili"],
        }
    ]
    adapter = _Adapter("Napcat_V5", [{"group_id": "90001", "group_name": "在群"}])

    async def call_action(_action, **_kwargs):
        raise RuntimeError("adapter unavailable")

    adapter.client.call_action = call_action
    context = SimpleNamespace(
        platform_manager=SimpleNamespace(platform_insts=[adapter])
    )
    service = GroupParserConfigService(cfg, context)

    with pytest.raises(AppleMusicBackendError, match="无法读取 OneBot"):
        asyncio.run(service.save_group_settings([]))

    assert cfg._data[GROUP_PARSER_SETTINGS_KEY][0]["group_id"] == "90003"


def test_confirmed_group_total_switch_updates_blacklist_and_preserves_unrelated_scopes():
    cfg, saves = _config()
    cfg.blacklist = ["Napcat_V5:GroupMessage:70001", "Other:GroupMessage:90001"]
    service = _service(cfg)

    asyncio.run(service.save_group_settings([
        {
            "platform_id": "Napcat_V5", "group_id": "90001",
            "enabled": ["bilibili"], "parsing_enabled": False,
        },
        {
            "platform_id": "Napcat_V5", "group_id": "90002",
            "enabled": ["bilibili"], "parsing_enabled": True,
        },
    ]))

    assert saves == [True]
    assert cfg._data["blacklist"] == [
        "Other:GroupMessage:90001",
        "Napcat_V5:GroupMessage:90001",
    ]
    assert group_enabled_parsers(cfg, "Napcat_V5", "90001") == {"bilibili"}
    assert asyncio.run(service.get_groups())["groups"][0]["parsing_enabled"] is False


@pytest.mark.parametrize("cleanup_enabled", [True, False])
def test_confirmed_save_cleans_departed_group_settings_and_optionally_blacklist(
    cleanup_enabled,
):
    cfg, saves = _config()
    cfg.cleanup_blacklist_on_group_leave = cleanup_enabled
    cfg._data[GROUP_PARSER_SETTINGS_KEY] = [
        {
            "platform_id": "Napcat_V5",
            "group_id": "90001",
            "disabled_parsers": ["netease"],
        },
        {
            "platform_id": "Napcat_V5",
            "group_id": "90003",
            "disabled_parsers": ["bilibili"],
        },
        {
            "platform_id": "OtherBot",
            "group_id": "90004",
            "disabled_parsers": ["pixiv"],
        },
    ]
    cfg._data["blacklist"] = [
        "Napcat_V5:GroupMessage:90001",
        "Napcat_V5:GroupMessage:10001_90003",
        "OtherBot:GroupMessage:90004",
        "Napcat_V5:FriendMessage:12345",
    ]
    service = _service(cfg, [
        {"group_id": "90001", "group_name": "仍在群"},
        {"group_id": "90002", "group_name": "新群"},
    ])

    result = asyncio.run(service.save_group_settings([
        {
            "platform_id": "Napcat_V5", "group_id": "90001",
            "enabled": ["bilibili"], "parsing_enabled": False,
        },
        {
            # This is a stale browser draft: it must not block save or be re-added.
            "platform_id": "Napcat_V5", "group_id": "90003",
            "enabled": ["netease"], "parsing_enabled": True,
        },
    ]))

    assert result["cleaned_group_count"] == 1
    assert result["cleaned_blacklist_group_count"] == (1 if cleanup_enabled else 0)
    assert [entry["group_id"] for entry in cfg._data[GROUP_PARSER_SETTINGS_KEY]] == [
        "90001", "90004",
    ]
    assert "Napcat_V5:GroupMessage:90001" in cfg._data["blacklist"]
    assert "OtherBot:GroupMessage:90004" in cfg._data["blacklist"]
    assert "Napcat_V5:FriendMessage:12345" in cfg._data["blacklist"]
    if cleanup_enabled:
        assert "Napcat_V5:GroupMessage:10001_90003" not in cfg._data["blacklist"]
    else:
        assert "Napcat_V5:GroupMessage:10001_90003" in cfg._data["blacklist"]
    assert saves == [True]


def test_group_total_switch_save_requires_boolean_and_persistence_rolls_back_blacklist():
    cfg, _ = _config(save_error=OSError("disk full"))
    cfg.blacklist = ["Napcat_V5:GroupMessage:10001_90001", "Other:GroupMessage:1"]
    original_blacklist = list(cfg.blacklist)
    service = _service(cfg)

    with pytest.raises(AppleMusicBackendError, match="总解析开关"):
        asyncio.run(service.save_group_settings([
            {
                "platform_id": "Napcat_V5", "group_id": "90001",
                "enabled": ["bilibili"], "parsing_enabled": 1,
            }
        ]))

    with pytest.raises(AppleMusicBackendError, match="保存失败"):
        asyncio.run(service.save_group_settings([
            {
                "platform_id": "Napcat_V5", "group_id": "90001",
                "enabled": ["bilibili"], "parsing_enabled": False,
            }
        ]))

    assert "blacklist" not in cfg._data
    assert cfg.blacklist == original_blacklist
    assert GROUP_PARSER_SETTINGS_KEY not in cfg._data


def test_session_total_switch_persists_once_per_real_change():
    cfg, saves = _config()
    service = _service(cfg)
    scope = "Napcat_V5:GroupMessage:90001"

    assert asyncio.run(service.set_session_parsing_enabled(scope, False)) is True
    assert cfg._data["blacklist"] == [scope]
    assert asyncio.run(service.set_session_parsing_enabled(scope, False)) is False
    assert asyncio.run(service.set_session_parsing_enabled(scope, True)) is True
    assert cfg._data["blacklist"] == []
    assert saves == [True, True]


def test_failed_session_total_switch_save_restores_blacklist_and_key_presence():
    cfg, _ = _config(save_error=OSError("disk full"))
    cfg.blacklist = ["Other:GroupMessage:1"]
    service = _service(cfg)

    with pytest.raises(AppleMusicBackendError, match="会话解析设置保存失败"):
        asyncio.run(service.set_session_parsing_enabled(
            "Napcat_V5:GroupMessage:90001", False
        ))

    assert "blacklist" not in cfg._data
    assert cfg.blacklist == ["Other:GroupMessage:1"]


def test_failed_save_restores_schema_stable_storage_and_key_presence():
    cfg, _ = _config(save_error=OSError("disk full"))
    original = [
        {
            "platform_id": "Napcat_V5",
            "group_id": "90002",
            "disabled_parsers": ["pixiv"],
        }
    ]
    cfg._data[GROUP_PARSER_SETTINGS_KEY] = original
    service = _service(cfg)

    with pytest.raises(AppleMusicBackendError, match="保存失败"):
        asyncio.run(service.set_group_parser("Napcat_V5", "90001", "bilibili", False))

    assert cfg._data[GROUP_PARSER_SETTINGS_KEY] == original


def test_group_toggle_serializes_policy_as_schema_stable_list():
    cfg, _ = _config()
    service = _service(cfg)

    asyncio.run(service.set_group_parser("Napcat_V5", "90001", "bilibili", False))

    assert cfg._data[GROUP_PARSER_SETTINGS_KEY] == [
        {
            "platform_id": "Napcat_V5",
            "group_id": "90001",
            "disabled_parsers": ["bilibili"],
        }
    ]


def test_global_disable_cannot_be_overridden_by_group_save_or_command():
    cfg, _ = _config(disabled={"pixiv"})
    service = _service(cfg)

    with pytest.raises(AppleMusicBackendError, match="全局关闭"):
        asyncio.run(service.save_group_settings([
            {
                "platform_id": "Napcat_V5", "group_id": "90001",
                "enabled": ["pixiv"], "parsing_enabled": True,
            }
        ]))
    with pytest.raises(AppleMusicBackendError, match="全局配置中关闭"):
        asyncio.run(service.set_group_parser("Napcat_V5", "90001", "pixiv", True))


def test_quick_toggle_persists_only_the_selected_group():
    cfg, saves = _config()
    service = _service(cfg)

    disabled = asyncio.run(
        service.set_group_parser("Napcat_V5", "90001", "bilibili", False)
    )

    assert "bilibili" not in disabled
    assert "bilibili" in group_enabled_parsers(cfg, "Napcat_V5", "90002")
    assert saves == [True]
    enabled = asyncio.run(
        service.set_group_parser("Napcat_V5", "90001", "bilibili", True)
    )
    assert "bilibili" in enabled
    assert cfg._data[GROUP_PARSER_SETTINGS_KEY] == []


def test_quick_toggle_rejects_group_not_joined_to_target_instance():
    cfg, _ = _config()
    service = _service(cfg, [{"group_id": "90002", "group_name": "另一个群"}])

    with pytest.raises(AppleMusicBackendError, match="不在当前 OneBot 实例"):
        asyncio.run(service.set_group_parser("Napcat_V5", "90001", "bilibili", False))


def test_group_configuration_accepts_a_dedicated_group_lock():
    cfg, _ = _config()
    lock = asyncio.Lock()
    service = GroupParserConfigService(cfg, SimpleNamespace(), lock=lock)

    assert service.lock is lock


def test_group_and_apple_business_locks_are_separate_but_config_writes_are_shared():
    cfg, _ = _config()
    config_lock = asyncio.Lock()
    backend = AppleMusicBackend(
        cfg,
        BackendSettings(enabled=False),
        config_write_lock=config_lock,
    )
    service = GroupParserConfigService(
        cfg, SimpleNamespace(), config_lock=backend.config_write_lock
    )

    assert service.lock is not backend.lock
    assert service.config_lock is backend.config_write_lock


def test_group_and_apple_config_saves_cannot_overlap():
    cfg, _ = _config()
    cfg.parser.get = lambda key: cfg.parser._nodes.get(key)
    adapter = _Adapter("QQBot_1", name="qq_official")
    config_lock = asyncio.Lock()
    active_saves = 0
    maximum_active_saves = 0
    save_count = 0
    counter_lock = threading.Lock()

    def save_config():
        nonlocal active_saves, maximum_active_saves, save_count
        with counter_lock:
            active_saves += 1
            maximum_active_saves = max(maximum_active_saves, active_saves)
        time.sleep(0.03)
        with counter_lock:
            save_count += 1
            active_saves -= 1

    cfg.save_config = save_config
    backend = AppleMusicBackend(
        cfg,
        BackendSettings(enabled=False),
        config_write_lock=config_lock,
    )
    service = GroupParserConfigService(
        cfg,
        SimpleNamespace(platform_manager=SimpleNamespace(platform_insts=[adapter])),
        config_lock=config_lock,
    )

    async def run():
        try:
            await asyncio.gather(
                service.remember_qq_official_group(
                    "QQBot_1", "group-openid-A1", "官方测试群"
                ),
                backend.update_music_parser_settings(
                    "admin", {"qqmusic": {"enable": False}}
                ),
            )
        finally:
            await backend.close()

    asyncio.run(run())

    assert maximum_active_saves == 1
    assert save_count == 2
    assert cfg._data[QQ_OFFICIAL_KNOWN_GROUPS_KEY][0]["group_id"] == "group-openid-A1"
    assert cfg._data["__parser_management_history_v1"]["qqmusic"]["settings"] == {
        "enable": False
    }


def test_failed_apple_config_save_restores_shared_history_object_in_place():
    cfg, _ = _config(save_error=OSError("disk full"))
    cfg.parser.get = lambda key: cfg.parser._nodes.get(key)
    history = {"qqmusic": {"settings": {"enable": True}}}
    cfg._data["__parser_management_history_v1"] = history
    cfg._parser_management_history = history
    backend = AppleMusicBackend(cfg, BackendSettings(enabled=False))

    async def run():
        try:
            with pytest.raises(AppleMusicBackendError, match="音乐解析设置保存失败"):
                await backend.update_music_parser_settings(
                    "admin", {"qqmusic": {"enable": False}}
                )
        finally:
            await backend.close()

    asyncio.run(run())

    assert cfg._data["__parser_management_history_v1"] is history
    assert cfg._parser_management_history is history
    assert history == {"qqmusic": {"settings": {"enable": True}}}
    assert cfg.parser._nodes["qqmusic"].enable is True


def test_failed_transaction_cannot_roll_back_a_later_group_write():
    cfg, _ = _config()
    cfg.parser.get = lambda key: cfg.parser._nodes.get(key)
    config_lock = asyncio.Lock()
    adapter = _Adapter("QQBot_1", name="qq_official")
    started = threading.Event()
    release = threading.Event()
    saved = []
    attempts = 0

    def save_config():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            started.set()
            assert release.wait(timeout=2)
            raise OSError("disk full")
        saved.append(copy.deepcopy(cfg._data))

    cfg.save_config = save_config
    backend = AppleMusicBackend(
        cfg, BackendSettings(enabled=False), config_write_lock=config_lock
    )
    service = GroupParserConfigService(
        cfg,
        SimpleNamespace(platform_manager=SimpleNamespace(platform_insts=[adapter])),
        config_lock=config_lock,
    )

    async def run():
        first = asyncio.create_task(backend.update_music_parser_settings(
            "admin", {"qqmusic": {"enable": False}}
        ))
        try:
            assert await asyncio.to_thread(started.wait, 1)
            second = asyncio.create_task(service.remember_qq_official_group(
                "QQBot_1", "group-openid-A1"
            ))
            await asyncio.sleep(0)
            assert not second.done()
            release.set()
            with pytest.raises(AppleMusicBackendError):
                await first
            assert await second is True
        finally:
            release.set()
            await backend.close()

    asyncio.run(run())

    assert attempts == 2
    assert saved == [cfg._data]
    assert "__parser_management_history_v1" not in saved[0]
    assert saved[0][QQ_OFFICIAL_KNOWN_GROUPS_KEY][0]["group_id"] == "group-openid-A1"
    assert cfg.parser._nodes["qqmusic"].enable is True


def test_parser_keyword_mapping_checks_qishui_before_douyin_host():
    assert parser_key_for_keyword("music.douyin.com/qishui/share/track") == "qishui"
    assert parser_key_for_keyword("qishui.douyin.com/s") == "qishui"
    assert parser_key_for_keyword("v.douyin.com") == "douyin"
