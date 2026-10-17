"""Обойти страницы панели и сказать, какие из них мертвы.

Сюита проверяет домен и ручки, но не отвечает на вопрос «а человек это
увидит?». Между зелёной сюитой и работающей страницей лежит слой, где живут
самые неприятные дефекты: ошибка JS рвёт модуль на середине (разметка уже
нарисована — страница выглядит живой и просто не отвечает), панель остаётся
пустой при полной базе, «Загрузка…» висит навсегда.

```python
from uvicorn_.uvicorn_sweep import uvicorn_sweep

got = uvicorn_sweep(['/admin/orders', '/admin/data'])
for page, marks in got['pages'].items():
    print(page, marks or 'цела')
```

## ⚠⚠ Вкладки находятся САМИ, списком их не передают

Список вкладок в проверке — вторая копия разметки, и расходится она молча: на
странице завели вкладку, проверка о ней не знает и говорит «цела». Обходятся
все узлы с `data-tab`, какие на странице есть.

## ⚠ Три находки, и каждая поймана живьём

| Находка | Как выглядит человеку |
|---------|-----------------------|
| ошибка JS | страница нарисована и не отвечает на нажатия |
| «Загрузка…» | висит навсегда; сервер при этом отвечает за полсекунды |
| пустая панель | вкладка открывается в ничто |

Четвёртой находки — «долго» — здесь нет намеренно: медленная страница это
отдельный вопрос с отдельными порогами, а смешанные в одну ведомость они
дадут шум, в котором мёртвую страницу не разглядеть.

## ⚠ Открывается каждая страница своим заходом

Браузер поднимается на страницу и гасится; десять страниц — десять заходов,
полторы минуты. Один заход с переходами был бы быстрее, но первая же
упавшая страница уносила бы с собой остальные: ошибка модуля рвёт и
навигацию.
"""

import argparse
import json
import sys

# Сколько ждать, пока страница оживёт, прежде чем щёлкать вкладки.
#
# ⚠ Две секунды, а не «до готовности DOM»: страницы панели рисуются своим
# запросом после загрузки модуля, и `DOMContentLoaded` наступает задолго до
# того, как в панели появится хоть что-то.
UVICORN_SWEEP_SETTLE_MS = 2000

# Сколько ждать после переключения вкладки.
#
# ⚠ Полторы секунды: вкладка со своей ручкой (сводка, история) успевает
# сходить на сервер, а обход десяти вкладок не растягивается на минуту.
UVICORN_SWEEP_TAB_MS = 1500

# Короче этого содержимое панели считается пустым.
#
# ⚠ Двадцать знаков, а не ноль: пустая панель редко бывает совсем пустой —
# в ней остаётся заголовок или «—». Двадцать отсекает их и не трогает
# честное «Карточек нет».
UVICORN_SWEEP_EMPTY_CHARS = 20

# Чьи битые ответы не считаем находкой — всегда.
#
# ⚠ Фавикон у зеркала свой: его там нет по построению, и 404 на него
# приходит на КАЖДОЙ целой странице. Без этого обход ругался бы всегда и
# перестал бы что-либо значить.
#
# ⚠⚠ Остальное проект называет САМ (`skip` у `uvicorn_sweep`). У панели
# бывают ручки, которых у неё нет намеренно: `optionalList` в общем слое
# глотает их отказ, и страница живёт без них. Знать, какие именно, —
# свойство установки, и список таких адресов в общем слое был бы знанием
# проекта не на своём месте.
UVICORN_SWEEP_SKIP = ('/favicon.ico',)

# Что считаем зависшей загрузкой. ⚠ Многоточие — часть подписи: без него
# совпало бы и обычное слово в тексте страницы.
UVICORN_SWEEP_LOADING = 'Загрузка…'

# Сколько всего дать странице на обход. ⚠ Считается из числа вкладок: у
# страницы с шестью вкладками честный обход дольше, чем у страницы без них.
UVICORN_SWEEP_BUDGET_MS = 20000

# JS обхода: открыть вкладки по очереди и вернуть, что с каждой не так.
#
# ⚠⚠ Слушатели вешаются ПЕРВЫМИ строками. Ошибка модуля случается при его
# разборе — то есть до того, как мы сюда дошли; но `unhandledrejection` от
# запросов страницы прилетает позже, и его мы ловим.
UVICORN_SWEEP_JS = """
const out = {tabs: [], errors: []};
window.addEventListener('error', e => out.errors.push(String(e.message)));
window.addEventListener('unhandledrejection',
                        e => out.errors.push('promise: ' + String(e.reason)));

await new Promise(r => setTimeout(r, %(settle)d));

const look = () => {
    const pane = [...document.querySelectorAll('.page-pane')]
        .find(p => !p.hidden);
    const text = (pane || document.body).innerText || '';

    return {chars: text.trim().length, loading: text.includes(%(loading)s)};
};

// ⚠⚠ Битые ресурсы — ОТСЮДА, а не из текста консоли. Консоль говорит
// «Failed to load resource: 404» и не называет адрес; отличить рухнувший
// скрипт страницы от фавикона зеркала по такой строке нельзя, и обход
// ругался на каждую целую страницу.
out.broken = performance.getEntriesByType('resource')
    .filter(r => r.responseStatus >= 400)
    .map(r => r.name.replace(location.origin, ''))
    .filter(name => !%(skip)s.some(one => name.includes(one)));

const tabs = [...document.querySelectorAll('[data-tab]')];
if (!tabs.length) {
    out.tabs.push({tab: '', ...look()});
} else {
    for (const btn of tabs) {
        btn.click();
        await new Promise(r => setTimeout(r, %(tab)d));
        out.tabs.push({tab: btn.dataset.tab || '?', ...look()});
    }
}

return out;
"""


def uvicorn_sweep(pages: list, base: str = '', user: str = '',
                  password: str = '', size: tuple = (1700, 900),
                  skip: tuple = ()) -> dict:
    """Обойти страницы панели; вернуть, какие из них мертвы.

    Args:
        pages: пути страниц (`['/admin/orders', '/']`).
        base: адрес панели; пусто — из `.env`.
        user, password: под кем входить; пусто — из `.env`.
        size: вьюпорт обхода.
        skip: адреса, чей 404 не считать находкой, — ручки, которых у этой
            установки нет намеренно (`/node/list` у панели без узлов).

    Returns:
        `{'pages': {путь: [находки словами]}, 'bad': сколько страниц с
        находками}`. Пустой список находок — страница цела.

    ⚠ Не бросает на упавшей странице: её отказ — это находка, а не беда
    обхода. Одна мёртвая страница не должна прятать состояние остальных.
    """
    out, bad = {}, 0
    for page in pages:
        marks = uvicorn_sweep_page(page, base, user, password, size, skip)
        out[page] = marks
        bad += 1 if marks else 0

    return {'pages': out, 'bad': bad}


def uvicorn_sweep_page(page: str, base: str = '', user: str = '',
                       password: str = '', size: tuple = (1700, 900),
                       skip: tuple = ()) -> list:
    """Обойти одну страницу и её вкладки; вернуть находки словами.

    Returns:
        Список находок (`['stat: ЗАСТЫЛО на «Загрузка…»']`); пусто — цела.

    ⚠ Отказ браузера приходит находкой, а не исключением: «страница не
    открылась» — такой же ответ о её состоянии, как «вкладка пуста».
    """
    from uvicorn_.uvicorn_mirror import uvicorn_mirror
    from web_.web_drive import web_drive_eval

    script = UVICORN_SWEEP_JS % {'settle': UVICORN_SWEEP_SETTLE_MS,
                                 'tab': UVICORN_SWEEP_TAB_MS,
                                 'loading': json.dumps(UVICORN_SWEEP_LOADING),
                                 'skip': json.dumps(
                                     list(UVICORN_SWEEP_SKIP) + list(skip))}
    try:
        with uvicorn_mirror(page, base, user, password) as url:
            got = web_drive_eval(url, script, size=size,
                                 wait_ms=UVICORN_SWEEP_BUDGET_MS)
    except Exception as fault:  # noqa: BLE001 — отказ страницы это находка
        return [f'не открылась: {str(fault).splitlines()[0][:120]}']

    return _sweep_marks(got)


# ── детали реализации ──────────────────────────────────────────────────────

def _sweep_marks(got: dict) -> list:
    """Ответ браузера — в находки словами; пусто, когда всё цело.

    ⚠⚠ Пустой `value` сам по себе находка: скрипт обхода не доработал, то
    есть страница умерла до того, как успела о себе рассказать. Молчание
    здесь легко принять за «цела» — и мы бы принимали.
    """
    value = got.get('value')
    if not isinstance(value, dict):
        where = got.get('where') or ''
        return [f'обход не доработал{f" ({where})" if where else ""}']

    marks = []
    for row in value.get('tabs') or []:
        name = row.get('tab') or '(одна)'
        if row.get('loading'):
            marks.append(f'{name}: ЗАСТЫЛО на «{UVICORN_SWEEP_LOADING}»')
        elif int(row.get('chars') or 0) < UVICORN_SWEEP_EMPTY_CHARS:
            marks.append(f'{name}: ПУСТО')

    marks += [f'JS: {one[:100]}' for one in value.get('errors') or []]
    marks += [f'не загрузилось: {one}' for one in value.get('broken') or []]
    marks += _sweep_crashed(got)

    return marks


def _sweep_crashed(got: dict) -> list:
    """Модуль страницы, упавший при разборе; пусто — таких нет.

    ⚠⚠ Это ГЛАВНАЯ находка обхода, и ловится она только здесь. Ошибка
    случается до того, как наш скрипт повесил слушатель: `window.onerror`
    её не видит, а разметка к тому моменту нарисована — вкладки и панели на
    месте, текст есть, и страница проходит все прочие проверки как целая.
    Поймано на себе: нарочно сломанная страница получила «цела».

    ⚠ Уровень `page-error`, а не `error`. Второй — это битые ресурсы
    («Failed to load resource»), и отбор по нему пропускал ровно то, ради
    чего обход затевался.
    """
    return [f'МОДУЛЬ УПАЛ: {_sweep_short(one.get("text"))}'
            for one in got.get('console') or []
            if one.get('level') == 'page-error']


def _sweep_short(text) -> str:
    """Первая строка сообщения — в ней суть, дальше идёт адрес и стек."""
    return str(text or '').strip().splitlines()[0][:120]


def _sweep_main() -> int:
    """CLI: обойти перечисленные страницы и напечатать ведомость."""
    ap = argparse.ArgumentParser(
        description='Обойти страницы панели: какие из них мертвы.')
    ap.add_argument('pages', nargs='+', help='пути страниц')
    ap.add_argument('--base', default='', help='адрес панели')
    ap.add_argument('--size', default='1700x900', help='вьюпорт, ШxВ')
    ap.add_argument('--skip', action='append', default=[], metavar='АДРЕС',
                    help='ручка, которой у установки нет намеренно; повторять')
    ns = ap.parse_args()

    width, _, height = ns.size.partition('x')
    got = uvicorn_sweep(ns.pages, ns.base,
                        size=(int(width), int(height or 900)),
                        skip=tuple(ns.skip))

    for page, marks in got['pages'].items():
        print(('✗ ' if marks else '✓ ') + page
              + ('  ' + '; '.join(marks) if marks else ''))

    print(f'\nстраниц с находками: {got["bad"]} из {len(got["pages"])}')

    return 1 if got['bad'] else 0


if __name__ == '__main__':
    sys.exit(_sweep_main())
