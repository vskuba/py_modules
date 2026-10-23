"""
Доменный код **в контейнере прода**: тем же окружением панели, на той машине.

Вопрос «дожила ли правка до живого стека» до сих пор решался заклинанием:
`ssh хост 'cd ~/app && docker exec контейнер python -c "from x import y;
print(...)"'`. Кавычки в нём съедаются дважды (ssh-оболочка, потом sh
контейнера), многострочный код в `-c` не влезает вовсе, а `project_probe` —
двойник этого приёма — поднимает код **своей** машины, наружу из контейнера, с
подменой адресов; к чужому хосту у него дороги нет.

Здесь обратная половина приёма: исполнение **внутри** поднятого контейнера,
его же окружением — `PYTHONPATH`, `.env`, имена сервисов из внутренней сети.
Ничего не подменяется и не угадывается: код видит стек ровно так, как его
видит панель. Этим и проверяют реестры настройки, состав импортов и «есть ли
у функции новый параметр» — не глазами по диффу, одним выражением.

Contract — как у `project_probe`: кусок кода, stdout ответом, чужой код
возврата — `RuntimeError` с хвостом stderr. Тело с `await` оборачивается в
коручину само, тем же правилом, что у двойника.
"""
import argparse
import shlex
import sys
from pathlib import Path

if __package__ in (None, ''):
    _here = str(Path(__file__).resolve().parent)
    sys.path[:] = [item for item in sys.path if item not in ('', '.', _here)]
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ssh_.ssh_x import ssh_x


def project_probe_ssh(code: str, target: str, container: str = 'uvicorn') -> str:
    """Выполнить кусок доменного кода внутри контейнера на том хосте.

    Args:
        code: текст программы (обычно импорт + print); тело с `await`
            оборачивается в корутину само.
        target: куда ходить — алиас ssh или `пользователь@хост`.
        container: контейнер, чьим окружением запускать; точное имя или
            подстрока среди поднятых (`uvicorn` найдёт `dating_uvicorn`).

    Returns:
        stdout процесса.

    Raises:
        RuntimeError: контейнер не поднят или код вернулся с ошибкой — хвост
        stderr в тексте.
    """
    code = _wrap(code)
    script = [
        'TMP=$(mktemp)',
        'printf %s "$DSH_CODE" > "$TMP"',
        f'NAME=$(docker inspect -f "{{{{.Name}}}}" {shlex.quote(container)} '
        '2>/dev/null | tr -d /)',
        f'[ -n "$NAME" ] || NAME=$(docker ps --format "{{{{.Names}}}}" | '
        f'grep -m1 -- {shlex.quote(container)})',
        '[ -n "$NAME" ] || { echo "контейнер не поднят" >&2; rm -f "$TMP"; '
        'exit 3; }',
        'docker exec -i "$NAME" sh -c \'command -v python >/dev/null 2>&1 '
        '&& exec python -u -; exec python3 -u -\' < "$TMP"',
        'rc=$?',
        'rm -f "$TMP"',
        'exit $rc',
    ]
    answer = ssh_x(target, script, args={'DSH_CODE': _dollar(code)})
    if answer['code'] != 0:
        raise RuntimeError(f"прод ответил {answer['code']}: "
                           f"{(answer['error'] or answer['output']).strip()[-500:]}")
    return answer['output']


def _dollar(text: str) -> str:
    """Код с `$` на начале `ssh_x` счёл бы ссылкой на локальное окружение;
    перевод строки на начале безвреден для Python.
    """
    return '\n' + text if text.startswith('$') else text


def _wrap(code: str) -> str:
    """Тело корутины — корутиной; то же правило, что в `project_probe`
    (`code_rules.md`, §4.2: две копии ещё не паттерн).
    """
    if 'await ' not in code:
        return code
    body = '\n'.join('    ' + line for line in code.splitlines()) or '    pass'
    return ('import asyncio\n\n\nasync def _main():\n' + body +
            '\n\n\nasyncio.run(_main())\n')


if __name__ == '__main__':
    ap = argparse.ArgumentParser(
        description='кусок доменного кода внутри контейнера на том хосте')
    ap.add_argument('-a', '--async-code', required=True, metavar='КОД',
                    help='код (тело корутины оборачивается само)')
    ap.add_argument('--target', required=True, help='куда ходить: алиас ssh')
    ap.add_argument('--container', default='uvicorn',
                    help='контейнер на том хосте (точное имя или подстрока)')
    ns = ap.parse_args()
    try:
        print(project_probe_ssh(ns.async_code, ns.target, ns.container),
              end='')
    except (RuntimeError, ValueError) as err:
        raise SystemExit(f'ошибка: {err}')
