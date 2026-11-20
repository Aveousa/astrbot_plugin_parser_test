"""Resolve a consistent Kugou browser identity without mixing cookie aliases."""

from urllib.parse import parse_qs


def cookie_values(header: str) -> dict[str, str]:
    values = {}
    for part in header.split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name:
            value = value.strip()
            # Differently scoped identity cookies must not be silently selected.
            if (
                name in values
                and values[name] != value
                and name in {"KuGoo", "KugooID", "t", "userid", "token"}
            ):
                raise ValueError("Conflicting account cookies")
            values.setdefault(name, value)
    return values


def nested_values(value: str) -> dict[str, str]:
    # Decode only delimiters until the compound structure is visible. Decoding
    # the whole token repeatedly would alter escaped characters in its value.
    import re

    for _ in range(3):
        value = re.sub(r"%3[dD]", "=", value)
        value = re.sub(r"%26", "&", value, flags=re.IGNORECASE)
        fields = parse_qs(value, keep_blank_values=True)
        if "KugooID" in fields or "t" in fields:
            return {k: entries[0] for k, entries in fields.items() if entries}
        value = re.sub(r"%25", "%", value, flags=re.IGNORECASE)
    return {}


def account_credentials(header: str) -> tuple[str, str]:
    try:
        values = cookie_values(header)
    except ValueError:
        return "", ""
    nested = nested_values(values.get("KuGoo", ""))
    nested_pair = nested.get("KugooID", "").strip(), nested.get("t", "").strip()
    top_pair = values.get("KugooID", ""), values.get("t", "")
    if all(nested_pair):
        # Browser exports commonly mirror only KugooID at the top level.
        # Accept such a partial mirror only when every supplied field agrees.
        if any(
            value and value != expected
            for value, expected in zip(top_pair, nested_pair)
        ):
            return "", ""
        return nested_pair
    if any(nested_pair):
        return "", ""
    return top_pair if all(top_pair) else ("", "")
