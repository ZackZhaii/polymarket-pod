"""Issue #131: a market's category is fail closed, from its Gamma tags.

Gamma's own ``category`` is null for nearly every active market (100/100 of the top 100 by 24h volume on
2026-10-10), so the category is derived from the tag slugs instead:

* ``classify(tags)`` gives the market's buckets. Tags that are missing, not a list, empty, or with no usable
  slug give no buckets and the problem ``missing_category`` (veto in intent_builder and RiskGate alike).
  Tags that match no bucket of ``CATEGORY_BUCKETS`` are the bucket ``other`` (classified, not blocked).
* ``CATEGORY_BUCKETS`` is the minimal mapping: empty for now, so every tagged market is ``other``. The
  real bucket table (politics, macro, sports, ...), its caps and any sizing are KEY #135, not this module.
* ``BANNED_CATEGORIES`` is the one explicit source of banned categories (empty: nothing is banned yet).
  A bucket, a tag slug or the explicit category in it vetoes ``category_banned``. A source that cannot
  be read as a set of non-blank strings vetoes ``banned_categories_unreadable``; it is never read as empty.

Nothing here is in config/risk.toml and no threshold is involved. No network, no orders.
"""
from __future__ import annotations

from collections.abc import Mapping

MISSING_CATEGORY = "missing_category"
CATEGORY_BANNED = "category_banned"
BANNED_CATEGORIES_UNREADABLE = "banned_categories_unreadable"
OTHER = "other"

# bucket -> the tag slugs (lower case) that put a market in it. Minimal on purpose; filled by #135 (KEY).
CATEGORY_BUCKETS: Mapping[str, frozenset[str]] = {}

# The explicit list of banned categories (bucket names, tag slugs or an explicit category; lower case).
BANNED_CATEGORIES: frozenset[str] = frozenset()


def _norm(value: object) -> str | None:
    return value.strip().lower() if isinstance(value, str) and value.strip() else None


def tag_slugs(tags: object) -> tuple[str, ...] | None:
    """The usable tag slugs (stripped, lower case, de-duplicated, in order), or None when there are none.

    None for missing tags, a non-list / non-tuple (a bare string is not a tag list), an empty list, or a
    list with no non-blank string in it.
    """
    if not isinstance(tags, (list, tuple)):
        return None
    out: list[str] = []
    for tag in tags:
        slug = _norm(tag)
        if slug is not None and slug not in out:
            out.append(slug)
    return tuple(out) or None


def classify(tags: object, buckets: object = None) -> tuple[tuple[str, ...], str | None]:
    """``(buckets, problem)``: the sorted buckets the tags hit (``("other",)`` when none), or ``((), problem)``.

    problem is ``missing_category`` for unusable tags, ``invalid_category_buckets`` for a mapping that is
    not ``{non-blank name: set of strings}``. ``buckets`` defaults to CATEGORY_BUCKETS (read at call time).
    """
    slugs = tag_slugs(tags)
    if slugs is None:
        return (), MISSING_CATEGORY
    table = CATEGORY_BUCKETS if buckets is None else buckets
    if not isinstance(table, Mapping):
        return (), "invalid_category_buckets"
    hit: list[str] = []
    for name, members in table.items():
        key = _norm(name)
        if key is None or key == OTHER or not isinstance(members, (set, frozenset)) or \
                not all(isinstance(m, str) for m in members):
            return (), "invalid_category_buckets"
        if any(slug in {m.strip().lower() for m in members} for slug in slugs):
            hit.append(key)
    return (tuple(sorted(set(hit))) or (OTHER,)), None


def banned_categories(source: object = None) -> tuple[frozenset[str] | None, str | None]:
    """``(banned, problem)``. ``source`` defaults to BANNED_CATEGORIES (read at call time).

    Only a set / frozenset of non-blank strings is readable; anything else (None, a list, a string, a
    blank entry) is ``(None, banned_categories_unreadable)``, never an empty set.
    """
    value = BANNED_CATEGORIES if source is None else source
    if not isinstance(value, (set, frozenset)):
        return None, BANNED_CATEGORIES_UNREADABLE
    out = set()
    for item in value:
        key = _norm(item)
        if key is None:
            return None, BANNED_CATEGORIES_UNREADABLE
        out.add(key)
    return frozenset(out), None


def category_problems(category: object, tags: object, *, banned: object = None,
                      buckets: object = None) -> list[str]:
    """Veto reasons for one market: missing_category, invalid_category_buckets, category_banned and/or
    banned_categories_unreadable. [] means the market is classified and nothing it carries is banned."""
    problems: list[str] = []
    found, problem = classify(tags, buckets)
    if problem is not None:
        problems.append(problem)
    names, banned_problem = banned_categories(banned)
    if banned_problem is not None:
        problems.append(banned_problem)
    elif names:
        labels = set(found) | set(tag_slugs(tags) or ())
        explicit = _norm(category)
        if explicit is not None:
            labels.add(explicit)
        if labels & names:
            problems.append(CATEGORY_BANNED)
    return problems


def bucket_label(tags: object) -> list[str] | None:
    """What a fill records (``category_buckets``): the buckets, or None when unclassified. Never raises."""
    try:
        found, problem = classify(tags)
    except Exception:  # noqa: BLE001 - recording only
        return None
    return None if problem is not None else list(found)
