"""
Распря указателя `py_modules`: что принесёт bump и как слить линии без потерь.

`py_modules` живёт двумя чекаутами одного репозитория: самостоятельный (где
правят общий слой) и submodule `<корень>/py_modules` (где его читает панель).
Линии расходятся молча: gitlink указывает на старый кончик, пока человек не
вспомнил про указатель, а правка уехала в другой чекаут. Симптом — панель
крутится без свежей правки и молчит; проверить без инструмента нельзя, потому
что «чего не хватает» у detached HEAD не спрашивают.

Здесь два вопроса, на которые модуль отвечает словами, и один танец делает сам:

* **что сейчас**: gitlink (что зафиксировано проектом), кончик чекаута, их родство
  (кто кому предок — `merge-base --is-ancestor`), долетел ли кончик до origin
  (`pushed`) — вслепую сдвинутый на незапушенное указатель валит выкладку на
  `upload-pack: not our ref`, — несохранённая правка в чекауте (`dirty`), отставание
  от бара origin (`behind`) и чем именно отличается файл;
* **что принесёт bump**: список файлов между gitlink и целью; если кончик чекаута
  не предок цели, bump без слияния **украдёт** строку чекаута — модуль это
  говорит до, а не после;
* **слить и встать**: `project_submodule_bump` fetch'ит цель из второго чекаута
  (`--from`), вливает неслитое линией через временную ветку, как это делают
  руками, и оставляет кончик на объединении.

Коммит указателя остаётся человеком: модуль называет sha, а `git add py_modules`
и сообщение — дело того, кто коммитит.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

# Порядок строк тут значим: модуль запускают и прямым путём
# (`python py_modules/project_/project_submodule.py`), а тогда в `sys.path`
# лежит его каталог, а не корень слоя, — импорт соседа обязан идти ПОСЛЕ
# починки пути, иначе `project_` не пакет и ModuleNotFoundError.
if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from project_.project_ import project_git


def project_submodule_state(fetch: bool = True) -> dict:
    """Что сейчас: gitlink, чекаут, родство линий, долетел ли push, чем отличаются.

    Вне submodule-контекста (самостоятельный чекаут общего слоя) gitlink пуст —
    это не ошибка, а факт: здесь правят линию сразу, а не указатель.

    Args:
        fetch: взглянуть на origin свежим взглядом (`fetch -q`, тихий); сеть
            могла не доехать — тогда вердикт по последним привезённым линиям.

    Returns:
        {'gitlink': sha из дерева проекта ('' — submodule-контекста нет),
         'checkout': HEAD чекаута, 'synced': равны, 'ancestor': gitlink предок
         чекаута (без слияния bump ничего не украдёт), 'pushed': кончик
         чекаута нашёлся на барах origin (False — push не долетел, и указатель
         на такой sha валит выкладку в `upload-pack: not our ref`), 'remote':
         имя бара, где кончик нашёлся ('' — нигде), 'dirty': правка в рабочем
         дереве чекаута, которой нет ни в линии, ни в указателе — выкладка её
         не увидит, 'behind': на сколько коммитов кончик отстал от своего бара
         origin, 'files': `diff --stat gitlink..checkout` — что приедет в панель
         при bump}

    ⚠⚠ **`synced` отвечает про указатель, а не про диск**, и этого мало: линий
    здесь **три**, а не две. Третья — рабочее дерево чекаута. Правка слоя,
    лежащая в нём несохранённой, в выкладку не попадёт вовсе (подмодуль тянется
    по gitlink), а `gitlink == checkout` при этом честно скажет «сошлось».
    Поэтому смотреть надо на `dirty` и `behind` вместе с `synced`; живой случай
    — в докстринге `_dirty`.
    """
    root, sub = _repo()
    parts = project_git(root, 'ls-tree', 'HEAD', sub.name).split() if root != sub else []
    # «160000 commit <sha>\t<путь>»: полем больше — и это не gitlink
    gitlink = parts[2] if len(parts) == 4 and parts[1] == 'commit' else ''
    checkout = project_git(sub, 'rev-parse', 'HEAD')
    ancestor = bool(gitlink and checkout) and _run_ok(
        sub, 'merge-base', '--is-ancestor', gitlink, checkout)
    remote = _on_origin(sub, checkout, fetch)
    files = project_git(sub, 'diff', '--stat', gitlink, checkout).splitlines() \
        if gitlink and checkout else []
    return {'gitlink': gitlink, 'checkout': checkout,
            'synced': bool(gitlink) and gitlink == checkout, 'ancestor': ancestor,
            'pushed': bool(remote), 'remote': remote,
            'dirty': _dirty(sub), 'behind': _behind(sub, remote, checkout),
            'files': files}


def project_submodule_bump(line: str = 'master', from_repo: str = '') -> dict:
    """Встать на кончик линии, влив в него неслитое; вернуть, на что встали.

    Танец целиком: `--from` (второй чекаут) — fetch цели оттуда; если HEAD
    чекаута не предок цели, HEAD вливается в `<line>` слиянием и итог —
    объединение обеих линий. Указатель проекта после этого правят руками
    (`git add py_modules`), модуль лишь называет sha.

    Args:
        line: цель — имя ветки или sha; с `from_repo` — та же ветка **в том**
            чекауте (её же имя fetch заберёт оттуда).

            ⚠⚠ Без `from_repo` имя разрешается в **самом подмодуле**, а локальные
            ветки там устаревшие по природе: подмодуль живёт отсоединённым, и
            `master` остаётся там, где его оставил `clone`. Такой bump вернёт
            прежний sha с честным `merged: true` — цель была предком кончика, —
            и это выглядит как ложь инструмента. Целиться в `origin/<ветка>`
            после `fetch` либо звать с `from_repo`. Подробности и мера —
            `docs/project_submodule.md`, §3.
        from_repo: путь второго чекаута, откуда взять линию (самостоятельный
            чекаут для проекта и наоборот).

    Returns:
        {'sha': итоговый кончик, 'merged': было ли слияние,
         'files': diff --stat старый gitlink..новый sha, пусто — линия полная}

    Raises:
        ValueError: цель не читается (нет такой ветки/sha нигде из названных),
            либо линии разошлись **содержанием** и слияние упёрлось в конфликт.

    ⚠⚠ **Конфликт слияния разрешает человек, и чекаут при этом не трогается.**
    Автоматически слить можно только то, что не спорит; спорное — это две
    осмысленные правки одного места, и выбрать между ними может лишь тот, кто
    знает, зачем они сделаны. Прежде конфликт ронял модуль исключением
    `CalledProcessError` **посреди слияния**: чекаут оставался с конфликтом в
    дереве, на чужом коммите и с висящей веткой `_psm-tmp`, а человек видел
    `Command '[...]' returned non-zero exit status 128` и не знал ни что
    случилось, ни где теперь его линия. Измерено на живом расхождении
    DatingAi ↔ RhapsodyAi 01.10.2026.
    """
    root, sub = _repo()
    head = project_git(sub, 'rev-parse', 'HEAD')
    target = line
    if from_repo:
        # та же линия в двух чекаутах живёт по-разному: свежее там, где правят
        other = _layer_of(Path(from_repo))
        here = project_git(sub, 'rev-parse', '--verify', line)
        there = project_git(other, 'rev-parse', '--verify', line)
        if there and (not here or here != there):
            _run(sub, 'fetch', '-q', str(other), line)
            target = 'FETCH_HEAD'
    if not project_git(sub, 'rev-parse', '--verify', target):
        raise ValueError(f'цель {line!r} не читается в {sub.name}: ни ветка, ни sha')
    sha = project_git(sub, 'rev-parse', target)

    merged = bool(sha) and bool(head) and not _run_ok(
        sub, 'merge-base', '--is-ancestor', head, sha)
    if merged:
        # линия чекаута не предок цели — вливаем её в цель временной веткой,
        # как это делают руками: цель победит, но не потеряет чужое.
        #
        # ⚠ Весь танец — под откат: любой шаг может не удаться (конфликт,
        # грязное дерево), и бросить человека посреди слияния нельзя.
        _run_ok(sub, 'branch', '-q', '-D', '_psm-tmp')   # хвост прошлой неудачи
        _run(sub, 'branch', '-q', '_psm-tmp', head)
        _run(sub, 'checkout', '-q', sha)

        if not _run_ok(sub, 'merge', '-q', '--no-edit', '_psm-tmp'):
            # ⚠ Возврат ровно туда, где человек стоял: прерванное слияние
            # отменяем, чекаут ставим обратно на его линию, временную ветку
            # убираем. Оставь мы хоть что-то — следующий заход упал бы на
            # занятом имени ветки, и причина выглядела бы совсем иной.
            why = _merge_why(sub, line)
            _run_ok(sub, 'merge', '--abort')
            _run_ok(sub, 'checkout', '-q', '--force', head)
            _run_ok(sub, 'branch', '-q', '-D', '_psm-tmp')
            raise ValueError(why)

        _run_ok(sub, 'branch', '-q', '-D', '_psm-tmp')
    else:
        _run(sub, 'checkout', '-q', sha)

    parts = project_git(root, 'ls-tree', 'HEAD', sub.name).split() if root != sub else []
    gitlink = parts[2] if len(parts) == 4 and parts[1] == 'commit' else ''
    final = project_git(sub, 'rev-parse', 'HEAD')
    return {'sha': final, 'merged': merged,
            'files': project_git(sub, 'diff', '--stat', gitlink, final).splitlines()
            if gitlink else []}


# ── детали реализации ──

def _dirty(sub: Path) -> list:
    """Правка в рабочем дереве чекаута: путями, как их называет `status`.

    ⚠⚠ **Третья линия, которой модуль не видел вовсе до 27.10.2026, и цена
    слепоты измерена.** Правка общего слоя лежала в рабочем дереве подмодуля
    несохранённой; gitlink и кончик при этом совпадали, и `state` отвечал
    `synced: true`, `files: []` — то есть «всё в порядке». Коммит проекта,
    который этой правкой пользовался, уехал на прод один; выкладка тянет
    подмодуль **по gitlink** и про диск не знает. Девять кругов DatingAi падали
    там `TypeError: unexpected keyword argument 'every'` каждым тиком десять
    часов — входящие письма не забирались, — а инструмент, заведённый ровно
    против расхождения линий, молчал.

    ⚠ Неотслеживаемые файлы считаются тоже: новый файл слоя, не попавший в
    коммит, не доедет до выкладки ровно так же, как несохранённая правка.

    ⚠ Пустой ответ при незапущенном git неотличим от чистого дерева — как и
    везде в модуле (`project_git`). Цена ошибки здесь невелика: `pushed` и
    `synced` спрашивают тот же git и ответят тем же молчанием.

    ⚠⚠ Путь берётся **разбором по пробелу, а не срезом с третьего знака**.
    У `--porcelain` метка ровно двузначная (` M `, `?? `), но `project_git`
    отдаёт ответ `strip`-нутым — и у **первой** строки ведущий пробел метки
    `' M'` уже съеден. Срез по месту отрезал тогда первую букву пути
    (`roject_/project_submodule.py`), и выглядело это опечаткой в имени файла,
    а не сдвигом на знак.
    """
    return [row.strip().split(maxsplit=1)[-1] for row in
            project_git(sub, 'status', '--porcelain').splitlines() if row.strip()]


def _behind(sub: Path, remote: str, sha: str) -> int:
    """На сколько коммитов кончик чекаута отстал от своего бара origin.

    ⚠ Отставание — **не поломка**: указатель держат на проверенной строке
    осознанно, и подмодуль живёт отсоединённым по природе. Но вопрос «а слой
    уже уехал?» задаётся всякий раз, когда правки не видно, и отвечать на него
    вторым походом в git человеку незачем.

    ⚠ Кончик не долетел до origin (`remote` пуст) — считать не от чего: отвечаем
    нулём, а про незапушенное говорит `pushed`.
    """
    if not remote or not sha:
        return 0

    count = project_git(sub, 'rev-list', '--count', f'{sha}..{remote}')

    return int(count) if count.isdigit() else 0


def _on_origin(sub: Path, sha: str, fetch: bool) -> str:
    """Имя бара origin, который содержит кончик; '' — не содержит нигде.

    ⚠ Вердикт нужен словами: push #1 при сборке phone молча умер на
    `Failed to connect to github.com port 443 after 134386 ms`, указатель
    сдвинули вслепую — и выкладка упала в `upload-pack: not our ref`.
    Молчаливый fetch тут же за молчанием: сеть не доехала — вердикт по
    последним привезённым линиям, а не отказ отвечать.
    """
    if not sha:
        return ''
    if fetch:
        _run_ok(sub, 'fetch', '-q', 'origin')
    for line in project_git(sub, 'branch', '-r', '--contains', sha).splitlines():
        # «remotes/origin/master», без звёздочек текущей ветки; `origin/HEAD ->`
        # — символка, не отдельный бар
        ref = line.strip().removeprefix('remotes/').lstrip('*+ ').strip()
        if ref and '->' not in ref:
            return ref
    return ''


def _repo() -> tuple[Path, Path]:
    """(корень проекта, чекаут py_modules).

    Чекаут — каталог этого модуля с точностью до папки пакета: он сам знает,
    куда его подключили; `--show-superproject-working-tree` отвечает, есть ли
    над ним проект с gitlink (в standalone-чекауте его нет — возвращаем сам
    чекаут, gitlink тогда пуст).
    """
    here = Path(__file__).resolve().parents[1]
    super_ = project_git(here, 'rev-parse', '--show-superproject-working-tree')
    return (Path(super_) if super_ else here), here


def _layer_of(where: Path) -> Path:
    """Чекаут общего слоя по пути, который назвал человек.

    ⚠⚠ **Называют обычно корень проекта**, а не сам слой: «возьми линию из
    DatingAi» — естественная формулировка, и путь к `…/DatingAi/py_modules`
    человек держать в голове не обязан. Берём подпапку, если она есть.

    Без этого `fetch` тянул линию **самого проекта**: приезжала чужая история,
    `merge-base --is-ancestor` отвечал «не предок» даже у совпавших линий, а
    слияние упиралось в `refusing to merge unrelated histories`. Снаружи это
    выглядело как «линии разошлись» там, где они одинаковы.
    """
    here = where.resolve()
    inner = here / Path(__file__).resolve().parents[1].name

    return inner if (inner / '.git').exists() else here


def _merge_why(sub: Path, line: str) -> str:
    """Отказ слияния словами: какие файлы спорят и что с этим делать.

    Голый `CalledProcessError` человеку не говорит ничего — ни что слияние
    вообще было, ни где теперь его линия. Называем спорные файлы и путь
    руками: выбирать между двумя осмысленными правками одного места модуль
    не вправе.
    """
    files = [row.split('\t')[-1] for row in
             project_git(sub, 'diff', '--name-only', '--diff-filter=U').splitlines()
             if row.strip()]

    # ⚠ Спорящих файлов нет — значит слияние не началось вовсе, и причина
    # другая: чаще всего привезли линию **не того репозитория** (`--from`
    # указывает на проект, у которого свой `py_modules`). Говорить про
    # «разошлись содержанием» тут значило бы послать человека разбирать
    # конфликт, которого нет.
    if not files:
        return (f'слить не вышло, и спорящих файлов нет — похоже, привезена '
                f'линия другого репозитория. Чекаут возвращён на прежнюю линию. '
                f'Проверить: cd {sub} && git merge {line}')

    shown = ', '.join(files[:6]) + (' …' if len(files) > 6 else '')

    return (f'линии разошлись содержанием, слить само не вышло — спорят: '
            f'{shown}. Чекаут возвращён на прежнюю линию, ничего не потеряно. '
            f'Разрешать руками: cd {sub} && git merge {line} — и выбрать, '
            f'чья правка верна')


def _run_ok(cwd: Path, *args: str) -> bool:
    try:
        return subprocess.run(['git', *args], cwd=str(cwd), capture_output=True,
                              timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _run(cwd: Path, *args: str) -> None:
    """Команда git, обязанная удаться: чужой текст наружу как есть."""
    subprocess.run(['git', *args], cwd=str(cwd), check=True, capture_output=True,
                   text=True, timeout=20)


if __name__ == '__main__':
    ap = argparse.ArgumentParser(
        description='Указатель py_modules: state — что и как расходится, '
                    'bump — встать на линию, влив неслитое.')
    ap.add_argument('command', choices=['state', 'bump'])
    ap.add_argument('--line', default='master', help='цель: ветка или sha')
    ap.add_argument('--from', dest='from_repo', default='',
                    help='путь второго чекаута, откуда взять линию')
    ap.add_argument('--no-fetch', action='store_true',
                    help='state: вердикт про push по уже привезённым барам, без сети')
    ns = ap.parse_args()
    try:
        out = (project_submodule_state(fetch=not ns.no_fetch) if ns.command == 'state'
               else project_submodule_bump(ns.line, ns.from_repo))
        print(json.dumps(out, ensure_ascii=False, indent=1))
    except (ValueError, subprocess.CalledProcessError) as err:
        raise SystemExit(f'ошибка: {err}')
