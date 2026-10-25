"""Слайдовый дек — приёмка целиком: влезают ли слайды во вьюпорт, ходит ли навигация, кадр каждого.

Сосед — `web_shot`: тот снимает один кадр одной страницы. Деки же ломаются
молча: слайд с таблицей на экран длиннее вьюпорта и нижняя строка отрезана
без единой ошибки в консоли; кнопка «дальше» после правки перестаёт работать,
и дек показывает слайд 1 семь раз подряд. Глазами семь слайдов не принимаются —
здесь каждый слайд получает кадр, геометрию и ответ навигации за один прогон.

## Что здесь есть

* `web_deck_check` — по файлу дека: число слайдов, для каждого — кадр PNG,
  влезает ли (scroll против вьюпорта) и действительно ли переключился.

⚠ Навигация ищем по двум конвенциям: глобальная `show(k)` (что типично для
самописных) или клик по `<body>` на k (что типично для click-деков). Дек с
хэш-навигацией без обеих попадёт под клик-ветку и может показывать слайд не тот —
строка с `visible` ≠ `k` это покажет, правь вызов под свой дек.
⚠ Консоль и сети — не сюда: они к `web_drive`, тут только геометрия и кадры.
⚠ Вердикт «влезает» считается по нижней кромке отрисованных листьев, не по
`scrollHeight` документа: пустые боксы (svg `height:auto` пережиток) раздувают
высоту дока на сотни px, не добавляя в кадр ни пикселя — по документу такие
слайды вечно «лезут» за экран. Строка `phantom` честно это различает.
"""
import argparse
import json
import os
import tempfile

from web_.web_probe import web_probe
from web_.web_shot import web_shot_capture

# Вьюпорт приёмки дека: 16:9 проектор/монитор; слайд, влезший сюда, влезет и в
# презентацию, а не только в мой монитор.
WEB_DECK_SIZE = (1280, 720)

# Дать transition догореть до замера: восемьдесят мс хватает с запасом.
WEB_DECK_SETTLE = 80


def web_deck_check(deck: str, size=WEB_DECK_SIZE, shots: bool = True,
                   out_dir: str = '', chrome: str = '') -> dict:
    """Проверить дек: каждый слайд открыт, замерен и снят; вердикт построчно.

    Args:
        deck: путь к HTML-файлу дека.
        size: (ширина, высота) вьюпорта, пиксель в пиксель.
        shots: снимать ли кадры (сними — быстрее, но шов не увидишь).
        out_dir: куда класть PNG; пусто — временный каталог (путь вернётся).
        chrome: путь к бинарю браузера; пусто — поиск `web_shot`.

    Returns:
        {'ok': все слайды влезли и переключились, 'deck': путь, 'slides': n,
         'size': …, 'out': каталог кадров, 'rows': [{'k', 'visible', 'fits',
         'content_bottom', 'overflow': {'down','right'}, 'phantom', 'shot',
         'error'}]}
    """
    if not os.path.isfile(deck):
        return {'ok': False, 'deck': deck, 'slides': 0, 'size': list(size),
                'out': '', 'rows': [], 'error': 'файл дека не найден'}
    first = web_probe(deck, _deck_go(0) + _deck_measure(0), size=size,
                      chrome=chrome)
    n = int(first.get('n', 0)) if isinstance(first, dict) else 0
    if not n:
        return {'ok': False, 'deck': deck, 'slides': 0, 'size': list(size),
                'out': '', 'rows': [], 'error': 'ни одного <section> — это дек?'}
    out_dir = out_dir or tempfile.mkdtemp(prefix='deck_')
    rows = []
    for k in range(n):
        row = {'k': k, 'visible': -1, 'fits': False, 'content_bottom': 0,
               'overflow': {'down': 0, 'right': 0}, 'phantom': False,
               'shot': '', 'error': ''}
        try:
            got = web_probe(deck, _deck_go(k) + _deck_measure(k), size=size,
                            chrome=chrome)
            row['visible'] = int(got.get('vis', -1))
            row['content_bottom'] = int(got.get('cb', 0))
            down = int(got.get('sh', 0)) - int(got.get('ih', 0))
            right = int(got.get('sw', 0)) - int(got.get('iw', 0))
            row['overflow'] = {'down': max(down, 0), 'right': max(right, 0)}
            # Вердикт по нижней отрисованной кромке, а не по высоте документа:
            # фантомные боксы (например svg height:auto) раздувают scrollHeight,
            # не раздувая кадр — на глаз контент влезает, а scrollHeight кричит.
            row['fits'] = row['content_bottom'] <= int(got.get('ih', 0)) + 2 \
                and right <= 2
            row['phantom'] = down > 2 and row['fits']
        except (FileNotFoundError, RuntimeError) as err:
            row['error'] = str(err)
        if shots and not row['error']:
            png = os.path.join(out_dir, f'slide-{k + 1:02d}.png')
            try:
                web_shot_capture(deck, png, size=size, inject_js=_deck_go(k, sync=True),
                                 chrome=chrome)
                row['shot'] = png
            except (FileNotFoundError, RuntimeError, ValueError) as err:
                row['error'] = str(err)
        rows.append(row)
    ok = all(not r['error'] and r['fits'] and r['visible'] == r['k'] for r in rows)
    return {'ok': ok, 'deck': deck, 'slides': n, 'size': list(size),
            'out': out_dir, 'rows': rows}


# ── детали реализации ──

def _deck_go(k: int, sync: bool = False) -> str:
    """JS перехода к слайду k: show(k) где есть, иначе k кликов по телу.

    sync — без await (для inject_js, он встраивается как обычный скрипт).
    """
    go = (f"var S=[].slice.call(document.querySelectorAll('section'));"
          f"if (typeof show==='function') show({k});"
          f"else for (var i=0;i<{k};i++) document.body.click();")
    if sync:
        return go
    return go + f"window.scrollTo(0,0);await new Promise(function (r) " \
                f"{{ setTimeout(r, {WEB_DECK_SETTLE}); }});"


def _deck_measure(k: int) -> str:
    """JS замера: видимый слайд, нижняя отрисованная кромка и геометрия дока."""
    return ("var vis=-1;S.forEach(function (s, i) {"
            "if (vis < 0 && s.offsetParent !== null && s.offsetHeight > 0) vis = i;});"
            "var cb=0;document.querySelectorAll('*').forEach(function (e) {"
            "if (e.childElementCount > 0) return;"
            "var r=e.getBoundingClientRect();"
            "if (r.height > 0 && r.bottom > cb && r.top < innerHeight) cb=r.bottom;});"
            "var de=document.documentElement;"
            f"return {{k: {k}, n: S.length, vis: vis, cb: Math.round(cb),"
            f" sh: de.scrollHeight, sw: de.scrollWidth, ih: innerHeight,"
            f" iw: innerWidth}};")


if __name__ == '__main__':
    _P = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    _P.add_argument('deck', help='HTML-файл дека')
    _P.add_argument('--size', default='1280,720', help='Ш x В вьюпорта')
    _P.add_argument('--no-shots', action='store_true', help='не снимать кадры')
    _P.add_argument('--out', default='', help='каталог для PNG')
    _C = _P.parse_args()
    _res = web_deck_check(_C.deck, size=tuple(int(x) for x in _C.size.split(',')),
                          shots=not _C.no_shots, out_dir=_C.out)
    if 'error' in _res:
        raise SystemExit(f"ошибка: {_res['error']}")
    for _r in _res['rows']:
        if _r['error']:
            print(f"  слайд {_r['k'] + 1}: 🔴 {_r['error']}")
        else:
            mark = 'ок' if _r['fits'] and _r['visible'] == _r['k'] else '🔴'
            why = ('' if _r['fits'] else
                   f" вылезает на {_r['content_bottom'] - _res['size'][1]}px вниз,"
                   f" {_r['overflow']['right']}px вбок")
            if _r['phantom']:
                why += f" (бок дока раздут на {_r['overflow']['down']}px," \
                       f" рисунка там нет)"
            nav = ('' if _r['visible'] == _r['k'] else
                   f" навигация показала слайд {_r['visible'] + 1}")
            print(f"  слайд {_r['k'] + 1}: {mark} виден={_r['visible'] + 1}"
                  f"{why}{nav}")
    print(f"{_res['slides']} слайд(ов), кадры: {_res['out']}")
    raise SystemExit(min(sum(1 for r in _res['rows']
                             if r['error'] or not r['fits']
                             or r['visible'] != r['k']), 125))
