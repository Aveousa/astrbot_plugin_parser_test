"""Stable policy scope for AstrBot unified message origins (UMOs)."""

from __future__ import annotations

import re
from collections.abc import Iterable


_ISOLATED_GROUP_UMO = re.compile(
    r"^(?P<prefix>.+:GroupMessage:)(?P<member_id>\d+)_(?P<group_id>\d+)$"
)


def normalize_session_scope(umo: str) -> str:
    """Collapse a member-isolated group UMO to the stable group UMO.

    AstrBot 4.26.5 can emit ``...:GroupMessage:<member>_<group>`` when
    conversation isolation is enabled. Policies such as parser allow/deny
    lists must apply to the group, while private and unknown UMO formats must
    remain untouched.
    """

    if not isinstance(umo, str):
        return umo
    matched = _ISOLATED_GROUP_UMO.fullmatch(umo)
    if matched is None:
        return umo
    return f"{matched.group('prefix')}{matched.group('group_id')}"


def session_scope_in(umo: str, configured: Iterable[object]) -> bool:
    """Whether a UMO has an equivalent scope in a configured list."""

    target = normalize_session_scope(umo)
    return any(
        isinstance(value, str) and normalize_session_scope(value) == target
        for value in configured
    )


def is_group_session(umo: str) -> bool:
    """Return whether the UMO identifies any group-message conversation."""

    if not isinstance(umo, str):
        return False
    prefix, separator, _ = umo.rpartition(":")
    return bool(separator and prefix.endswith(":GroupMessage"))
