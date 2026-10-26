"""Lists related humanization."""

from __future__ import annotations

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Iterable
    from typing import Any

__all__ = ["natural_list"]


def natural_list(
    items: Iterable[Any],
    conjunction: str = "and",
    oxford_comma: bool = False,
) -> str:
    """Natural list.

    Convert an iterable of items into a human-readable string with commas and a
    conjunction before the last item.

    Examples:
        >>> natural_list(["one", "two", "three"])
        'one, two and three'
        >>> natural_list(["one", "two"])
        'one and two'
        >>> natural_list(["one"])
        'one'
        >>> natural_list(["a", "b", "c"], conjunction="or")
        'a, b or c'
        >>> natural_list(["a", "b", "c"], oxford_comma=True)
        'a, b, and c'
        >>> natural_list(["a", "b", "c"], conjunction="or", oxford_comma=True)
        'a, b, or c'

    Args:
        items (Iterable): An iterable of items.
        conjunction (str): The word placed before the last item.
        oxford_comma (bool): Put a comma before the conjunction when there are
            three or more items.

    Returns:
        str: A string with commas and the conjunction in the right places.
    """
    item_list = [str(item) for item in items]
    if not item_list:
        return ""
    if len(item_list) == 1:
        return item_list[0]
    elif len(item_list) == 2:
        return f"{item_list[0]} {conjunction} {item_list[1]}"
    else:
        prefix = ", ".join(item_list[:-1])
        if oxford_comma:
            prefix += ","
        return prefix + f" {conjunction} {item_list[-1]}"
