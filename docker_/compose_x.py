"""Команда в контейнере стека: учётка из `.env` в аргументах, вывод без шума.

Когда база панели живёт в контейнере, вопрос «что в строке базы» превращается в
`docker compose exec -T <сервис> mysql -p"$(grep '^MYSQL_PASSWORD=' .env |
cut -d= -f2-)" --default-character-set=utf8mb4 …`: пароль грепается руками в
каждом вызове и светится в списке процессов, кодировка забывается — и Cyrillic
ключи JSON падают «Invalid JSON path expression», а клиентный `[Warning]`
загрязняет вывод, который читают числа.

`compose_x` берёт это на себя: значение вида `'$MYSQL_PASSWORD'` подставляется
из окружения проекта (`.env` уже в нём — его положил `config`), `[Warning]`
гасится, код возврата возвращается, а не выбрасывается: команда, не пустившая
сервис, — тоже ответ.
"""
import os
import subprocess

# Потолок на команду контейнеру. Дамп базы и миграция идут минутами, поэтому
# он щедрый; смысл его не в скорости, а в том, что зов из автоматики обязан
# когда-нибудь вернуться: `docker compose exec` без потолка висит вечно, если
# сервис не поднялся и демон ждёт его.
COMPOSE_X_TIMEOUT = 600.0


def compose_x(service, cmd, *, args=None, quiet=('[Warning]',),
              timeout: float = COMPOSE_X_TIMEOUT) -> dict:
    """Выполнить команду в сервисе compose-стека из корня проекта.

    Args:
        service: имя сервиса (`persona_mysql`, `persona_uvicorn`, …).
        cmd: команда контейнеру: строка (одна, с аргументами через пробел
            не делится — это один элемент) или список `[бинарь, арг...]`.
        args: флаги к бинарю: {'-p': '$MYSQL_PASSWORD', '--db': True} —
            значение-строка `'$ИМЯ'` подставляется из окружения проекта
            (`.env`); `True` — голый флаг без значения.
        quiet: подстроки-шум, такие строки вывода выбрасываются.
        timeout: потолок в секундах; исчерпан — `code` = -1 и слово в `error`.

    Returns:
        {'code': rc, 'output': stdout без шума, 'error': stderr без шума}.

    ⚠ Таймаут — такой же ответ, как ненулевой код: `{'code': -1}` и строка
    в `error`, а не исключение. Иначе цикл, зовущий контейнер по кругу,
    обрывается на первом же неподнявшемся сервисе.
    """
    argv = ['docker', 'compose', 'exec', '-T', service]
    parts = list(cmd) if isinstance(cmd, (list, tuple)) else [cmd]
    for flag, value in (args or {}).items():
        if value is True:
            argv.append(flag)
            continue
        v = str(value)
        argv += [flag, os.environ.get(v[1:], '') if v.startswith('$') else v]
    argv += parts
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {'code': -1, 'output': '',
                'error': f'не ответил за {timeout:g} c: сервис «{service}» '
                         f'не поднят или команда не завершается сама'}
    except FileNotFoundError:
        return {'code': -1, 'output': '', 'error': 'docker не найден в PATH'}
    return {'code': r.returncode,
            'output': _quiet(r.stdout, quiet), 'error': _quiet(r.stderr, quiet)}


def _quiet(text: str, marks) -> str:
    """Выбросить шумовые строки вывода.

    Копия такой же в `ssh_/ssh_x.py` осознанная: копий ровно две, и общий файл
    ради трёх строк связал бы docker с ssh, которые друг о друге знать не
    должны. Появится третий такой вызывающий — вот тогда паттерн
    (`code_rules.md`, §4.2).
    """
    return '\n'.join(line for line in text.splitlines()
                     if not any(m in line for m in marks))


if __name__ == '__main__':
    import argparse
    import json
    ap = argparse.ArgumentParser(
        description='команда в сервисе compose-стека из корня проекта; '
                    'вывод — JSON без клиентского шума (--no-quiet не гасить).')
    ap.add_argument('service', help='имя сервиса (persona_mysql, …)')
    ap.add_argument('-a', '--arg', action='append', default=[],
                    metavar='флаг=значение',
                    help='флаг контейнеру; значение $ИМЯ берётся из окружения; '
                         'без «=» — голый флаг; повторять')
    ap.add_argument('cmd', nargs='+', help='бинарь и аргументы контейнеру')
    ap.add_argument('--no-quiet', action='store_true',
                    help='не выбрасывать шумовые строки из вывода')
    ap.add_argument('--timeout', type=float, default=COMPOSE_X_TIMEOUT,
                    help='потолок в секундах; исчерпан — code -1 и слово в error')
    ns = ap.parse_args()
    args = {}
    for a in ns.arg:
        flag, _, value = a.partition('=')
        args[flag] = value or True
    out = compose_x(ns.service, ns.cmd, args=args, timeout=ns.timeout,
                    quiet=() if ns.no_quiet else ('[Warning]',))
    print(json.dumps(out, ensure_ascii=False))
    raise SystemExit(0 if out['code'] == 0 else 1)
