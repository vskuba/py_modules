"""
Состояние кругов-расписаний поверх `setting_`: JSON с дефолтами и `dynamic=1`.

Всё, что круг помнит между тиками (расписание поиска: активна ли вещь, смены,
последний прогон и почему он не состоялся), живёт одной строкой `setting`.
Голый `setting_get` про это молчит: значение — строка, дефолты каждый домен
разводит руками, `dynamic=1` забывают — и состояние человека выползает на
страницу «Настройки → Значения», как будто это настройка.

Здесь приём один раз: прочитать → разобрать JSON (словом сказать, если там не
он) → долить дефолтами → отдать dict; патч — поверх состояния, запись — с
`dynamic=1`. Кругам остаётся своя работа: что означает день и смена, что
считать состоянием — это домен, а не слой.
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

if __package__ in (None, ''):
    _here = str(Path(__file__).resolve().parent)
    sys.path[:] = [item for item in sys.path if item not in ('', '.', _here)]
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from setting_.setting_ import setting_get, setting_set, setting_delete


async def setting_state(key: str, defaults: dict | None = None) -> dict:
    """Состояние ключа: разобранный JSON поверх дефолтов.

    Args:
        key: ключ строки в `setting`.
        defaults: чего не хватает в строке; нет строки — ответ они целиком.

    Returns:
        Соединённый dict (значение из базы поверх дефолтов, ключ в ключ).

    Raises:
        ValueError: в строке лежит не JSON-объект (или не объект вовсе).
    """
    raw = await setting_get(key, '')
    if not raw:
        return dict(defaults or {})
    try:
        saved = json.loads(raw)
    except (TypeError, ValueError):
        raise ValueError(f'настройка {key!r} хранит не JSON: {raw[:80]!r}')
    if not isinstance(saved, dict):
        raise ValueError(f'настройка {key!r} хранит не объект JSON, '
                         f'а {type(saved).__name__}')
    return {**(defaults or {}), **saved}


async def setting_state_patch(key: str, patch: dict,
                              defaults: dict | None = None) -> dict:
    """Правка состояния ключом: прочитано → долито патчем → записано с dynamic=1.

    Возвращает итоговое состояние — звено круга не должно перечитывать то, что
    только что положило.

    Args:
        key: ключ строки.
        patch: что переписать (пересёк дефолты, не трогая остальных ключей).
        defaults: дефолты недостающих ключей (см. `setting_state`).

    Returns:
        Итоговое состояние после правки.
    """
    state = await setting_state(key, defaults)
    state.update(patch)
    await setting_set(key, json.dumps(state, ensure_ascii=False), dynamic=1)
    return state


async def setting_state_clear(key: str) -> None:
    """Снять состояние с полки: строка снята — круг снова живёт по дефолтам."""
    await setting_delete(key)


if __name__ == '__main__':
    ap = argparse.ArgumentParser(
        description='JSON-состояние в setting: read / patch (dynamic=1). '
                    'С хоста базу видно опубликованным портом — MYSQL_HOST/'
                    'MYSQL_PORT называет тот, кто зовёт.')
    ap.add_argument('command', choices=['read', 'patch', 'clear'])
    ap.add_argument('key', help='ключ строки в setting')
    ap.add_argument('--defaults', default='{}', help='дефолты JSON-объектом')
    ap.add_argument('--patch', default='{}', help='правка JSON-объектом')
    ns = ap.parse_args()
    try:
        if ns.command == 'read':
            out = asyncio.run(setting_state(ns.key, json.loads(ns.defaults)))
        elif ns.command == 'patch':
            out = asyncio.run(setting_state_patch(
                ns.key, json.loads(ns.patch), json.loads(ns.defaults)))
        else:
            out = None
            asyncio.run(setting_state_clear(ns.key))
        print(json.dumps(out, ensure_ascii=False, indent=1) if out is not None
              else 'снято')
    except (ValueError, TypeError) as err:
        raise SystemExit(f'ошибка: {err}')
