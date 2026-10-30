"""Goods list pages: the same cursor contract over a list or a database slice."""
from __future__ import annotations

from typing import Any

from accounts.goods_api import decode_cursor, paginate


class _Sliced:
    """A queryset stand-in: answers slices only, and records each one asked for."""

    def __init__(self, items: list[int]) -> None:
        self.items = items
        self.asked: list[slice] = []

    def __getitem__(self, window: slice) -> list[int]:
        self.asked.append(window)
        return self.items[window]

    def __len__(self) -> int:
        raise AssertionError("a page must not count or load the whole set")


def _walk(items: Any) -> list[list[int]]:
    pages, cursor = [], None
    while True:
        window, cursor = paginate(items, {"limit": "2", **({"cursor": cursor} if cursor else {})})
        pages.append(window)
        if cursor is None:
            return pages


def test_pages_keep_order_and_end_exactly_at_the_last_row() -> None:
    assert _walk(list(range(5))) == [[0, 1], [2, 3], [4]]
    assert _walk(list(range(4))) == [[0, 1], [2, 3]]
    assert _walk([]) == [[]]


def test_a_queryset_is_read_one_page_and_one_row_at_a_time() -> None:
    sliced = _Sliced(list(range(5)))
    assert _walk(sliced) == [[0, 1], [2, 3], [4]]
    assert sliced.asked == [slice(0, 3), slice(2, 5), slice(4, 7)]


def test_the_next_cursor_points_at_the_first_row_of_the_next_page() -> None:
    _, cursor = paginate(list(range(5)), {"limit": "2"})
    assert cursor is not None and decode_cursor(cursor) == 2
