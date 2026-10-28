from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from core.apple_music_backend import AppleMusicBackendError
from core.group_parser_config import (
    GROUP_PARSER_SETTINGS_KEY,
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


def test_group_configuration_reuses_backend_lock():
    cfg, _ = _config()
    lock = asyncio.Lock()
    service = GroupParserConfigService(cfg, SimpleNamespace(), lock=lock)

    assert service.lock is lock


def test_parser_keyword_mapping_checks_qishui_before_douyin_host():
    assert parser_key_for_keyword("music.douyin.com/qishui/share/track") == "qishui"
    assert parser_key_for_keyword("qishui.douyin.com/s") == "qishui"
    assert parser_key_for_keyword("v.douyin.com") == "douyin"
