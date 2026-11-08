from __future__ import annotations

import asyncio
import copy
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
