from types import SimpleNamespace

import pytest

from core.config import PluginConfig
from core.debounce import Debouncer
from core.session_scope import (
    is_group_session,
    normalize_session_scope,
    session_scope_in,
)


@pytest.mark.parametrize(
    ("umo", "expected"),
    [
        (
            "Napcat_V5:GroupMessage:123456_987654",
            "Napcat_V5:GroupMessage:987654",
        ),
        (
            "other_adapter:GroupMessage:1_2",
            "other_adapter:GroupMessage:2",
        ),
        (
            "Napcat_V5:GroupMessage:987654",
            "Napcat_V5:GroupMessage:987654",
        ),
        (
            "Napcat_V5:FriendMessage:123456_987654",
            "Napcat_V5:FriendMessage:123456_987654",
        ),
        (
            "Napcat_V5:GroupMessage:user_987654",
            "Napcat_V5:GroupMessage:user_987654",
        ),
        (
            "Napcat_V5:GroupMessage:123456_987654_extra",
            "Napcat_V5:GroupMessage:123456_987654_extra",
        ),
    ],
)
def test_normalize_session_scope_is_strict_and_backward_compatible(umo, expected):
    assert normalize_session_scope(umo) == expected


def test_session_scope_membership_matches_legacy_and_canonical_group_entries():
    member = "Napcat_V5:GroupMessage:20002_90001"

    assert session_scope_in(member, ["Napcat_V5:GroupMessage:10001_90001"])
    assert session_scope_in(member, ["Napcat_V5:GroupMessage:90001"])
    assert not session_scope_in(member, ["Napcat_V5:GroupMessage:90002"])
    assert is_group_session(member)
    assert not is_group_session("Napcat_V5:FriendMessage:20002")


def test_blacklist_mutations_canonicalize_and_remove_all_legacy_group_entries():
    saves: list[list[str]] = []
    cfg = SimpleNamespace(
        blacklist=[
            "Napcat_V5:GroupMessage:10001_90001",
            "Napcat_V5:GroupMessage:10002_90001",
            "Napcat_V5:GroupMessage:10003_90002",
            "Napcat_V5:FriendMessage:10001",
        ]
    )
    cfg.save_config = lambda: saves.append(list(cfg.blacklist))

    PluginConfig.add_blacklist(
        cfg, "Napcat_V5:GroupMessage:10004_90001"
    )

    assert cfg.blacklist == [
        "Napcat_V5:GroupMessage:90001",
        "Napcat_V5:GroupMessage:10003_90002",
        "Napcat_V5:FriendMessage:10001",
    ]
    assert saves == [list(cfg.blacklist)]

    PluginConfig.remove_blacklist(
        cfg, "Napcat_V5:GroupMessage:10005_90001"
    )

    assert cfg.blacklist == [
        "Napcat_V5:GroupMessage:10003_90002",
        "Napcat_V5:FriendMessage:10001",
    ]
    assert len(saves) == 2


def test_debouncer_reuses_group_scope_across_isolated_members():
    debouncer = Debouncer(SimpleNamespace(debounce_interval=300))

    assert debouncer.hit_link(
        "Napcat_V5:GroupMessage:10001_90001", "https://example.com/video"
    ) is False
    assert debouncer.hit_link(
        "Napcat_V5:GroupMessage:10002_90001", "https://example.com/video"
    ) is True
    assert debouncer.hit_link(
        "Napcat_V5:GroupMessage:10002_90002", "https://example.com/video"
    ) is False
