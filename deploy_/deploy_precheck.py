"""
Что проверить перед выкладкой: быстрые статические проверки одной командой.

    PYTHONPATH=py_modules python -m deploy_.deploy_precheck          # весь проект
    PYTHONPATH=py_modules python -m deploy_.deploy_precheck --only js,migrations

## ⚠⚠ Зачем отдельный вход, когда есть сюита

Сюита проверяет **поведение**, и на то, что ломается ровно при выкладке, у неё нет
ни глаз, ни повода: кириллическое имя работает, порядок функций работает,
`.js`-файл с опечаткой ломает страницу молча (сюита его не грузит), а миграция в
подкаталоге просто не едет — `yoyo` не спускается в подпапки. Всё это видно только
разбором файлов и находится за секунды.

Отсюда набор: проверки **статические, быстрые и без сети с базой**. Живое —
прогон сюиты, состояние прода, применённые миграции — сюда намеренно не входит:
проверка, длящаяся минуты, перестаёт запускаться перед каждой выкладкой, а нужна
она именно каждый раз.

## Что проверяется

| Проверка       | Чем               | Почему это ломает выкладку |
|----------------|-------------------|----------------------------|
| `cyrillic`     | `variable_cyrillic` | имена латиницей — правило `code_rules.md` |
| `order`        | `function_order`  | публичное выше приватного: файл читают сверху |
| `js`           | `js_check`        | опечатка в модуле = мёртвая страница, и сюита молчит |
| `migrations`   | `mysql_migration_check_check` | `ALTER` раньше `CREATE`, подкаталоги вне глаз `yoyo` |
| `collate`      | `mysql_collate_risks` | разные сличения у прода и локально — падение на проде |
| `pages`        | `page_wire_check` | шаблон, которого нет, и страница вне навигации |
| `instead`      | `tool_instead`    | своё поверх готового в общем слое |

## ⚠ Две строгости, и разница существенна

`стоп` — то, что ломает выкладку или нарушает правило: чинится до выкладки.
`взгляд` — догадки: `collate` ищет по образцу, `pages` не знает, что страница
скрыта намеренно, `instead` отдаёт кандидатов. Они печатаются, но выкладку не
держат — иначе набор быстро научатся пропускать целиком.

## ⚠⚠ Пути берутся по договору, а чего нет — молча пропускается

`src/`, `migrations/`, `data/**/public/js`, `data/**/template` — соглашение этих
проектов, а не настройка. Нет каталога — проверка пропущена с пометкой `нет`, и
это не ошибка: библиотеке без `migrations/` нечего там смотреть.

⚠ `js` требует `node` в системе. Нет его — проверка честно скажется
непроведённой, а не тихо пройдёт: «проверено» и «проверить не смогли» — разное.
"""
import subprocess
import sys
import warnings

from pathlib import Path

# Файл запускают и путём. Тогда первым в путях лежит каталог файла, и `import deploy_`
# находит соседний модуль вместо пакета.
if __package__ in (None, ''):
    _here = str(Path(__file__).resolve().parent)
    sys.path[:] = [item for item in sys.path if item not in ('', '.', _here)]
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from file_.file_walk import file_walk
from function_.function_order import function_order
from js_.js_check import js_check
from mysql_.migration.mysql_migration_check import mysql_migration_check_check
from mysql_.mysql_collate import mysql_collate_risks
from page_.page_wire import page_wire_check
from tool_.tool_instead import tool_instead
from variable_.variable_cyrillic import variable_cyrillic

# Строгости. ⚠ Держать выкладку вправе только `стоп`: набор, срабатывающий на
# догадках, отключают целиком — и вместе с догадками уходят настоящие находки.
DEPLOY_PRECHECK_STOP = 'стоп'
DEPLOY_PRECHECK_LOOK = 'взгляд'

# Порядок проверок в отчёте. ⚠ Имена короткие: ими же пользуются `--only`/`--skip`.
DEPLOY_PRECHECK_ORDER = ('cyrillic', 'order', 'js', 'migrations', 'collate',
                         'pages', 'instead')

# Каталог кода по договору проектов.
DEPLOY_PRECHECK_SRC = 'src'
DEPLOY_PRECHECK_MIGRATIONS = 'migrations'

# Сколько находок показываем на проверку: остальное — числом. ⚠ Длинный отчёт
# читают по диагонали, а пропущенная первая находка обычно объясняет остальные.
DEPLOY_PRECHECK_SHOW = 8

# Ветка, против которой смотрим «что уезжает». ⚠ Нет её — остаётся рабочее дерево:
# свежий клон и своё имя у ветки не должны ронять набор.
DEPLOY_PRECHECK_BASE = 'origin/master'

# Виды находок миграций, вправду ломающие выкладку. ⚠⚠ Остальные виды проверка
# отдаёт как `взгляд`, и это не мягкость: `unguarded`, `no-stage-markers` и
# `non-latin-in-query` — про оформление, а `alter-before-create` и `shared-label`
# означают, что миграция не проедет или проедет не та.
DEPLOY_PRECHECK_MIGRATION_STOP = ('alter-before-create', 'shared-label',
                                  're-runs-on-start')

# Виды находок миграций, которые не показываются вовсе.
#
# ⚠⚠ `non-latin-in-query` — про кириллицу в тексте запроса, а в этих проектах
# отчётные подписи миграций пишутся по-русски намеренно (`SELECT 'ДО' AS этап`,
# `AS осталось_старых`): их читает человек в выводе миграции. Проверка честна, но
# для принятого здесь стиля срабатывает на каждой миграции — 313 строк «взгляда»
# на один заход, в которых тонет всё остальное.
#
# ⚠ Это решение **показа**, а не самой проверки: `mysql_migration_check` оставлен
# как есть, и проекту с другим соглашением довод пригодится.
DEPLOY_PRECHECK_MIGRATION_MUTE = ('non-latin-in-query',)


def deploy_precheck(root='.', only=(), skip=(), whole=False) -> list[dict]:
    """Быстрые проверки кода перед выкладкой: кириллица, порядок, js, миграции.

    Args:
        root: корень проекта.
        only: только эти проверки (имена из `DEPLOY_PRECHECK_ORDER`); пусто — все.
        skip: эти пропустить.
        whole: смотреть весь проект, а не только уезжающее.

    Returns:
        list[dict]: записи `{check, level, where, what}` — имя проверки, строгость
        (`стоп`/`взгляд`), место и суть. Записи со `level` пустым — сообщения о
        самой проверке («каталога нет», «нет node»), а не находки.

    ⚠⚠ По умолчанию проверяется **то, что уезжает**, а не весь проект. На живом
    проекте разница решающая: полный проход дал 642 держащих находки — весь
    накопленный долг разом, включая замороженную начальную схему. Набор, который
    в первый же день требует починить 642 места, не запускают во второй.

    ⚠ `whole=True` — для разового разбора долга, а не для выкладки.

    ⚠ Порядок — как в `DEPLOY_PRECHECK_ORDER`, а не по строгости: отчёт читают
    сверху вниз, и одна проверка не должна быть размазана по нему в двух местах.

    ⚠⚠ Своя ошибка проверки не роняет набор: она становится записью `взгляд` с
    текстом исключения. Иначе одна сломанная проверка отменяла бы все остальные —
    ровно перед выкладкой, когда набор и нужен.
    """
    base = Path(root).resolve()
    want = [one for one in DEPLOY_PRECHECK_ORDER
            if (not only or one in only) and one not in skip]
    going = None if whole else _changed(base)
    found = []

    # ⚠ Разбор чужих файлов сыплет `SyntaxWarning` (устаревшая экранировка в чужих
    # строках), и предупреждения уходили в stderr **вперемешку с отчётом заставы**.
    # К находкам они отношения не имеют: чинить их — не дело проверки перед выкладкой.
    warnings.simplefilter('ignore', SyntaxWarning)

    for name in want:
        try:
            found += _CHECKS[name](base, going)
        except Exception as bad:      # noqa: BLE001 — см. ⚠⚠ выше
            found.append({'check': name, 'level': DEPLOY_PRECHECK_LOOK,
                          'where': '', 'what': f'проверка сорвалась: {bad}'})

    return found


def deploy_precheck_format(found, show=DEPLOY_PRECHECK_SHOW) -> str:
    """Отчёт словами. Пусто — проверки прошли."""
    if not found:
        return 'Проверки перед выкладкой прошли.'

    out = []

    for name in DEPLOY_PRECHECK_ORDER:
        rows = [one for one in found if one['check'] == name]
        if not rows:
            continue

        notes = [one for one in rows if not one['level']]
        real = [one for one in rows if one['level']]

        if not real:
            out.append(f"  {name}: {notes[0]['what']}")
            continue

        stop = sum(1 for one in real if one['level'] == DEPLOY_PRECHECK_STOP)
        mark = '✗' if stop else '·'
        out.append(f"\n{mark} {name} — находок {len(real)}"
                   + (f', из них стоп {stop}' if stop else ''))

        for one in real[:show]:
            place = f"{one['where']}: " if one['where'] else ''
            out.append(f"    {one['level']:6} {place}{one['what']}")

        if len(real) > show:
            out.append(f"    … и ещё {len(real) - show}")

    stopped = deploy_precheck_stopped(found)
    out.append(f"\n{'✗ Выкладку держит' if stopped else '· Держащих находок нет'}"
               f": стоп-находок {stopped}.")

    return '\n'.join(out)


def deploy_precheck_stopped(found) -> int:
    """Сколько находок держат выкладку. Ноль — можно выкладывать."""
    return sum(1 for one in found if one.get('level') == DEPLOY_PRECHECK_STOP)


def main() -> int:
    """CLI: код возврата 1 — есть находки `стоп`; 0 — можно выкладывать."""
    import argparse

    ap = argparse.ArgumentParser(
        description='Быстрые статические проверки перед выкладкой.')
    ap.add_argument('root', nargs='?', default='.', help='корень проекта')
    ap.add_argument('--only', default='', help='только эти проверки, через запятую')
    ap.add_argument('--skip', default='', help='пропустить эти')
    ap.add_argument('--full', action='store_true', help='все находки, без сокращения')
    ap.add_argument('--whole', action='store_true',
                    help='весь проект, а не только уезжающее (разбор долга)')
    ns = ap.parse_args()

    found = deploy_precheck(ns.root,
                            only=tuple(one for one in ns.only.split(',') if one),
                            skip=tuple(one for one in ns.skip.split(',') if one),
                            whole=ns.whole)
    print(deploy_precheck_format(found, show=10_000 if ns.full
                                 else DEPLOY_PRECHECK_SHOW))

    return 1 if deploy_precheck_stopped(found) else 0


def _check_cyrillic(base: Path, going) -> list[dict]:
    """Кириллица в именах кода: правило — имена латиницей, русский в прозе."""
    src = base / DEPLOY_PRECHECK_SRC
    if not src.exists():
        return [_note('cyrillic', f'{DEPLOY_PRECHECK_SRC}/ нет — пропущено')]

    return [{'check': 'cyrillic', 'level': DEPLOY_PRECHECK_STOP,
             'where': f"{_short(base, one['file'])}:{one['line']}",
             'what': f"имя `{one['name']}`"}
            for one in variable_cyrillic(str(src))
            if _going(base, one['file'], going)]


def _check_order(base: Path, going) -> list[dict]:
    """Публичные функции выше приватных: файл читают сверху вниз."""
    src = base / DEPLOY_PRECHECK_SRC
    if not src.exists():
        return [_note('order', f'{DEPLOY_PRECHECK_SRC}/ нет — пропущено')]

    return [{'check': 'order', 'level': DEPLOY_PRECHECK_STOP,
             'where': f"{_short(base, one['file'])}:{one['line']}",
             'what': f"{one['name']} после {one['after']} "
                     f"(стр. {one['after_line']})"}
            for one in function_order(str(src))
            if _going(base, one['file'], going)]


def _check_js(base: Path, going) -> list[dict]:
    """Каждый модуль страницы обязан разбираться: опечатка — мёртвая страница.

    ⚠⚠ Именно здесь `node` — системная зависимость. Нет его — отчёт говорит
    «не проверено»: это не то же, что «проверено и цело».
    """
    files = [one for one in file_walk(base / 'data', ('.js',))
             if '/public/js/' in str(one).replace('\\', '/')
             and _going(base, one, going)]
    if not files:
        return [_note('js', 'уезжающих модулей страниц нет — пропущено')]

    out = []
    for one in files:
        try:
            got = js_check(one)
        except FileNotFoundError:
            return [_note('js', 'нет node — НЕ ПРОВЕРЕНО')]

        if not got['intact']:
            out.append({'check': 'js', 'level': DEPLOY_PRECHECK_STOP,
                        'where': got['where'] or _short(base, one),
                        'what': got['what']})

    return out


def _check_migrations(base: Path, going) -> list[dict]:
    """Каталог миграций глазами yoyo: порядок, метки, опасные места.

    ⚠⚠ Разбирается **весь** каталог, а отбираются находки уезжающих файлов:
    «`ALTER` раньше `CREATE`» и «метка занята» — свойства набора, и по одному
    файлу их не увидеть вовсе.
    """
    where = base / DEPLOY_PRECHECK_MIGRATIONS
    if not where.is_dir():
        return [_note('migrations', f'{DEPLOY_PRECHECK_MIGRATIONS}/ нет — пропущено')]

    out = []
    for one in mysql_migration_check_check(where):
        if one['kind'] in DEPLOY_PRECHECK_MIGRATION_MUTE:
            continue
        if not _going(base, where / one['file'], going):
            continue

        level = (DEPLOY_PRECHECK_STOP
                 if one['kind'] in DEPLOY_PRECHECK_MIGRATION_STOP
                 else DEPLOY_PRECHECK_LOOK)
        out.append({'check': 'migrations', 'level': level, 'where': one['file'],
                    'what': f"{one['kind']}: {one['detail']}"})

    return out


def _check_collate(base: Path, going) -> list[dict]:
    """Места миграций, падающие при разных сличениях у прода и локально.

    ⚠ `взгляд`, а не `стоп`: ищется по образцу, и у баз с одним сличением
    находка ничего не значит. Но стоило проверки на копии прода — дважды.
    """
    where = base / DEPLOY_PRECHECK_MIGRATIONS
    if not where.is_dir():
        return [_note('collate', f'{DEPLOY_PRECHECK_MIGRATIONS}/ нет — пропущено')]

    out = []
    for one in sorted(where.glob('*.sql')):
        if not _going(base, one, going):
            continue

        text = one.read_text(encoding='utf-8', errors='replace')
        for line, chunk, why in mysql_collate_risks(text):
            out.append({'check': 'collate', 'level': DEPLOY_PRECHECK_LOOK,
                        'where': f'{one.name}:{line}', 'what': f'{chunk} — {why}'})

    return out


def _check_pages(base: Path, going) -> list[dict]:
    """Три места страницы: точка роутера, файл шаблона, строка навигации.

    ⚠ `взгляд`: страница вне навигации бывает намеренной (служебная, по ссылке).
    А вот шаблона, которого нет, не бывает намеренно — но и он тут `взгляд`:
    литерал шаблона роутер иногда собирает по частям, и разбор видит обрывок.
    """
    routers = base / DEPLOY_PRECHECK_SRC / 'uvicorn' / 'routers'
    templates = base / 'data' / 'uvicorn' / 'template'
    nav = templates / 'base.html'

    if not (routers.exists() and templates.is_dir() and nav.is_file()):
        return [_note('pages', 'роутеров и шаблонов по договору нет — пропущено')]

    # ⚠ Отбор по уезжающему здесь другой: `page_wire_check` отдаёт страницы, а не
    # файлы, и какой роутер её объявил — не говорит. Поэтому проверка либо идёт
    # целиком (роутеры или шапка тронуты), либо не идёт вовсе.
    if going is not None and not any(
            one.startswith(('src/uvicorn/routers', 'data/uvicorn/template'))
            for one in going):
        return [_note('pages', 'роутеры и шаблоны не тронуты — пропущено')]

    out = []
    for one in page_wire_check(routers, templates, nav):
        holes = [name for name, ok in (('шаблона нет', one['file']),
                                       ('нет в навигации', one['nav'])) if not ok]
        if holes:
            out.append({'check': 'pages', 'level': DEPLOY_PRECHECK_LOOK,
                        'where': one['url'],
                        'what': f"{one['template']}: {', и '.join(holes)}"})

    return out


def _check_instead(base: Path, going) -> list[dict]:
    """Своё поверх готового: что из уезжающего кода уже умеет общий слой.

    ⚠ `взгляд` по устройству: `tool_instead` отдаёт кандидатов, а подходят они
    или нет — решает человек. Держать выкладку догадкой нельзя.

    ⚠⚠ Эта проверка смотрит только уезжающее **всегда**, даже при `whole`: по
    всему проекту она отвечала бы страницами кандидатов на код, написанный годы
    назад и давно работающий.
    """
    files = going if going is not None else _changed(base)
    if not files:
        return [_note('instead', 'уезжающих файлов не видно — пропущено')]

    out = []
    for one in files:
        path = base / one
        if not path.is_file():
            continue

        for got in tool_instead(path.read_text(encoding='utf-8', errors='replace')):
            best = got['tools'][0]
            out.append({'check': 'instead', 'level': DEPLOY_PRECHECK_LOOK,
                        'where': one,
                        'what': f"{got['mark']} → {best['where']}:{best['name']}"})

    return out


def _changed(base: Path) -> list[str]:
    """Файлы, уезжающие этой выкладкой: против базовой ветки плюс рабочее дерево.

    ⚠ Своя пара вызовов `git`, а не `commit_scope_changes`: тот показывает
    изменённое **в дереве**, а выкладка уносит и то, что уже закоммичено, но
    ещё не на origin. Вопросы разные, и ответы тоже.

    ⚠ Нет базовой ветки (свежий клон, своё имя у ветки) — остаётся рабочее
    дерево. Пустой ответ законен: выкладывают и без правок в коде.
    """
    out = []

    for args in ((['diff', '--name-only', f'{DEPLOY_PRECHECK_BASE}...HEAD'],),
                 (['diff', '--name-only', 'HEAD'],),
                 (['diff', '--name-only', '--cached'],)):
        got = subprocess.run(['git', '-C', str(base), *args[0]],
                             capture_output=True, text=True, check=False)
        if got.returncode == 0:
            out += [one for one in got.stdout.split('\n') if one.strip()]

    # ⚠ Только код, который проверки умеют читать: снимки, дампы и картинки в
    # выдаче `git` тоже есть, и `tool_instead` нашёл бы в них приметы текстом.
    return sorted({one for one in out if one.endswith(('.py', '.sh', '.sql', '.js'))})


def _going(base: Path, path, going) -> bool:
    """Уезжает ли этот файл. `going` пустой список — не уезжает ничего.

    ⚠ `None` и `[]` здесь разное: `None` значит «смотрим весь проект» (флаг
    `whole`), пустой список — «выкладка ничего не меняет в коде». Сведи их в одно
    `if not going`, и `--whole` перестал бы работать, показывая пусто.
    """
    if going is None:
        return True

    return _short(base, path) in going


def _note(check: str, what: str) -> dict:
    """Сообщение о самой проверке, а не находка: `level` пуст намеренно."""
    return {'check': check, 'level': '', 'where': '', 'what': what}


def _short(base: Path, path) -> str:
    """Путь от корня проекта — в отчёте абсолютные пути только мешают."""
    try:
        return str(Path(path).resolve().relative_to(base))
    except ValueError:
        return str(path)


# ⚠ Ниже словаря — потому что он ссылается на функции: собрать его выше значило
# бы держать имена строками и терять проверку опечаток при запуске.
_CHECKS = {'cyrillic': _check_cyrillic, 'order': _check_order, 'js': _check_js,
           'migrations': _check_migrations, 'collate': _check_collate,
           'pages': _check_pages, 'instead': _check_instead}


if __name__ == '__main__':
    sys.exit(main())
