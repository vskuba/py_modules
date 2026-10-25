"""Снимок таблицы «ключ→значение» и дифф против прошлого захода.

Наблюдение за сетью живёт сравнениями: сериал SOA вчера и сегодня, набор PTR
имён на фронте, хеши страниц. Руками это каждый раз одинаково: снять таблицу,
прочитать старую, руками разложить «что сдвинулось / появилось / пропало» —
и каждый раз есть шанс напутать по памяти («в прошлый раз у ariete был
2015101325, кажется»). Здесь память — файл: один вызов кладёт снимок, второй
снимает и тут же отвечает диффом. Формат — плоский dict строк; для вложенного
хранилища есть `setting_/setting_state`, здесь — ровно таблица.

## Что здесь есть

* `file_snap_save` — положить таблицу в файл (атомарно, с меткой времени);
* `file_snap_diff` — снять новую и ответить, что сдвинулось с прошлого раза;
* `file_snap_load` — просто последний снимок.

⚠ Дифф считает изменения только по ключам обеих таблиц; ключ, пропавший из
новой, — `removed`, а не «изменился на пусто» — различай consciously.
"""
import json
import os
import tempfile
from datetime import datetime, timezone


def file_snap_save(path: str, table: dict) -> dict:
    """Положить таблицу снимком: атомарно, прежняя копия — рядом с .bak.

    Args:
        path: файл снимка (JSON).
        table: плоский dict «ключ → значение-строка».

    Returns:
        {'ok': bool, 'path': …, 'count': int, 'saved_at': ISO,
         'error': словами (пусто при удаче)}
    """
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)) or '.', exist_ok=True)
        if os.path.exists(path):
            _snap_bak(path)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)),
                                   suffix='.tmp')
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump({'saved_at': datetime.now(timezone.utc)
                       .strftime('%Y-%m-%dT%H:%M:%SZ'),
                       'table': {k: str(v) for k, v in table.items()}},
                      f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
        return {'ok': True, 'path': path, 'count': len(table),
                'saved_at': datetime.now(timezone.utc)
                .strftime('%Y-%m-%dT%H:%M:%SZ'), 'error': ''}
    except OSError as e:
        if os.path.exists(tmp):
            os.unlink(tmp)
        return {'ok': False, 'path': path, 'count': 0, 'saved_at': '',
                'error': f'{type(e).__name__}: {e}'}


def file_snap_diff(path: str, table: dict, *, save: bool = True) -> dict:
    """Снять таблицу и ответить диффом против последнего снимка.

    Args:
        path: файл снимка; нет файла — первый снимок, пустой diff.
        table: новая таблица.
        save: положить новый снимок после сравнения (иначе — только сверка).

    Returns:
        {'ok': bool, 'first': нет прежнего снимка,
         'changed': {ключ: {'was', 'now'}}, 'added': {ключ: значение},
         'removed': {ключ: прежнее}, 'same': int,
         'prev_saved_at': ISO|'', 'saved_at': ISO|'' (если save),
         'error': словами}
    """
    prev = file_snap_load(path)
    if prev['table'] is None:
        saved = file_snap_save(path, table) if save else {'saved_at': ''}
        return {'ok': True, 'first': True, 'changed': {},
                'added': {}, 'removed': {}, 'same': 0,
                'prev_saved_at': '',
                'saved_at': saved['saved_at'], 'error': prev['error']}
    old = prev['table']
    new = {k: str(v) for k, v in table.items()}
    changed = {k: {'was': old[k], 'now': new[k]}
               for k in old.keys() & new.keys() if old[k] != new[k]}
    added = {k: v for k, v in new.items() if k not in old}
    removed = {k: v for k, v in old.items() if k not in new}
    same = sum(1 for k in old.keys() & new.keys() if old[k] == new[k])
    saved_at = file_snap_save(path, table)['saved_at'] if save else ''
    return {'ok': True, 'first': False, 'changed': changed, 'added': added,
            'removed': removed, 'same': same,
            'prev_saved_at': prev['saved_at'], 'saved_at': saved_at,
            'error': ''}


def file_snap_load(path: str) -> dict:
    """Последний снимок: {'table': dict|None, 'saved_at': ISO|'', 'error'}.

    Нет файла — table None без ошибки (первый заход — не поломка).
    """
    try:
        with open(path, encoding='utf-8') as f:
            js = json.load(f)
        return {'table': js.get('table'), 'saved_at': js.get('saved_at', ''),
                'error': ''}
    except FileNotFoundError:
        return {'table': None, 'saved_at': '', 'error': ''}
    except (OSError, ValueError) as e:
        return {'table': None, 'saved_at': '',
                'error': f'{type(e).__name__}: {e}'}


def _snap_bak(path: str) -> None:
    """Прежний снимок — в .bak; порченый файл не должен остаться единственным."""
    try:
        os.replace(path, path + '.bak')
    except OSError:
        pass


if __name__ == '__main__':
    import argparse

    _P = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    _P.add_argument('cmd', choices=('show', 'diff'))
    _P.add_argument('path')
    _P.add_argument('--json', help='новая таблица JSON-файлом (для diff)')
    _C = _P.parse_args()
    if _C.cmd == 'show':
        _got = file_snap_load(_C.path)
        for _k, _v in sorted((_got['table'] or {}).items()):
            print(f'{_k:<32} {_v}')
        print(f'saved_at={_got["saved_at"]} err={_got["error"]}')
        _res = {'ok': not _got['error']}
    else:
        _table = json.load(open(_C.json, encoding='utf-8')) if _C.json else {}
        _res = file_snap_diff(_C.path, _table)
        print(f"первый={_res['first']} сдвинулось={len(_res['changed'])} "
              f"новых={len(_res['added'])} пропало={len(_res['removed'])} "
              f"тех=же={_res['same']} (был от {_res['prev_saved_at']})")
        for _k, _c in _res['changed'].items():
            print(f'  ~ {_k}: {_c["was"]} -> {_c["now"]}')
    raise SystemExit(0 if _res.get('ok') else 1)
