r"""JSON наружу человеку: строки с кириллицей живой, а не `\uXXXX`.

Обратная половина `json_from_string`: та разбирает текст в объект, эта объекта
отдаёт строкой. Каждый модульный CLI кончается одним и тем же — `json.dumps`
с одними и теми же тремя аргументами, которые в каждом `__main__` пишут
заново и иногда забывают `ensure_ascii`.
"""
import json
from typing import Any


def json_out(obj: Any, indent: int = 1) -> str:
    r"""Объект Python человекочитаемым JSON: кириллица буквами, даты путём.

    Args:
        obj: что показать — dict, list, что угодно сериализуемое; Path,
            datetime и прочие уезжают `str()`-ом, а не отказом.
        indent: отступ вложенности; 0 — одной строкой.

    Returns:
        str: JSON-текст; `ensure_ascii=False` — `\u0410` вместо «А» не мусорит.
    """
    return json.dumps(obj, ensure_ascii=False, indent=indent or None, default=str)
