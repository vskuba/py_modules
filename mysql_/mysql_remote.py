"""
Разовый запрос к базе **прода** — учётки остаются на той машине.

Вопрос «что там на проде в таблице X» до сих пор писался каждый раз заново:
`cat probe.sql | ssh хост 'cd ~/app && set -a && . ./.env.prod && set +a &&
docker exec -i -e MYSQL_PWD="$MYSQL_PASSWORD" контейнер mysql -t
--default-character-set=utf8mb4 -u"$MYSQL_USER" база'`. В этом заклинании
ошибок на пять мест: забудь `--default-character-set` — и Cyrillic в JSON
колонке ответит «Invalid JSON path expression»; забудь `-t` — и ответ поедет
табами, которые никто не разберёт; `.env` без `set -a` не экспортирует
переменные; пароль, подставленный сюда с локальной машины, светится в `ps`
на двух машинах сразу.

Локальные двойники здесь не помогают: `mysql_query` стучится в свою базу через
`pymysql` по адресу из локального `.env`, а прод из разработчика не виден
вовсе — только через ssh. `ssh_x` — примитив дороги, он не знает ни про mysql,
ни про `.env` приложения.

`mysql_remote_sql` берёт круг на себя: SQL доезжает через stdin, учётки
читаются **на самой машине** из её `.env` (секрет не покидает прод и не
светится в аргументах), кодировка включена, шум клиента (`[Warning]`,
строки `bind [`) выбрасывается, а код возврата — часть ответа, а не
исключение. Разведка схемы (`tables`/`columns`/`find`) — те же три вопроса,
что у `mysql_schema`, только заданные проду: `Unknown column` в живой таблице
стоит ровно тот же круг, что и локально.

Как и у `mysql_query`, чтение отделено от правки: без `write=True` уходят
только читающие запросы.
"""
import argparse
import shlex
import sys
from pathlib import Path

# Файл запускают и путём; без этого соседние namespace не находятся.
if __package__ in (None, ''):
    _here = str(Path(__file__).resolve().parent)
    sys.path[:] = [item for item in sys.path if item not in ('', '.', _here)]
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ssh_.ssh_x import ssh_x
from sql_.sql_split import sql_split_statements

# Первые слова читающего запроса — те же, что `MYSQL_QUERY_READ_ONLY` у
# `mysql_query`; копия держится ею (code_rules.md, §4.2): общий файл ради
# кортежа связал бы ssh-инструмент с драйвером pymysql, который ему не нужен.
MYSQL_REMOTE_READ = ('select', 'show', 'describe', 'desc', 'explain', 'with')

# Шум, который mysql-клиент и ssh гонят на stderr, когда сам запрос в порядке:
# предупреждение сессии, `[Warning]` клиента и строки `bind [` — печатает их и
# живой прод (MariaDB в контейнере), и руки оператора вырезали их каждый раз.
MYSQL_REMOTE_QUIET = ('Warning: Permanently added', '[Warning]', 'bind [')


def mysql_remote_sql(sql: str, target: str, *, container: str = 'mysql',
                     env_file: str = '.env', app_dir: str = '',
                     rows: bool = False, write: bool = False) -> dict:
    """Один запрос к базе прод-стека; учётки читаются на том хосте.

    Args:
        sql: текст запроса; подстановок нет — значения вписываются прямо, это
            разовый взгляд оператора, а не путь приложения.
        target: куда ходить — алиас ssh или `пользователь@хост` (как в `ssh_x`).
        container: контейнер базы на том хосте (`prod_mysql`).
        env_file: файл окружения приложения на том хосте, откуда берутся
            `MYSQL_USER`/`MYSQL_PASSWORD`/`MYSQL_DATABASE` (продовые стенды
            держат его отдельным файлом, например `.env.prod`).
        app_dir: каталог приложения на том хосте, где лежит `env_file`;
            `~/…` разбирается.
        rows: True — голые строки табами (для разбора), False — таблица для
            человека.
        write: разрешить запросы, меняющие данные (как `--write` у
            `mysql_query`).

    Returns:
        {'code': rc, 'output': stdout без шума, 'error': stderr без шума};
        база, отвергнувшая запрос, — `code` ≠ 0, а не исключение.
    """
    if not write and not _mysql_remote_read_only(sql):
        return {'code': -2, 'output': '',
                'error': 'запрос меняет данные — прод берегут: повтори с '
                         'write=True'}
    return _mysql_remote_exec(sql, target, container=container,
                              env_file=env_file, app_dir=app_dir, rows=rows)


def mysql_remote_tables(target: str, like: str = '', **dest) -> list:
    """Какие таблицы есть на проде (маска — как в `LIKE`)."""
    sql = 'SHOW TABLES' + (f" LIKE {_quote_word(like)}" if like else '')
    return [row[0] for row in _mysql_remote_rows(sql, target, **dest)]


def mysql_remote_columns(target: str, table: str, **dest) -> list:
    """Что за колонки в таблице прода: [{'column', 'type', 'null', 'key'}].

    Это ответ на «Unknown column 'created_at'» — до правки запроса, а не после
    второго промаха.
    """
    sql = f'DESCRIBE `{_ident(table)}`'
    return [{'column': row[0], 'type': row[1],
             'null': row[2] if len(row) > 2 else '',
             'key': row[3] if len(row) > 3 else ''}
            for row in _mysql_remote_rows(sql, target, **dest)]


def mysql_remote_find(target: str, word: str, **dest) -> list:
    """Где вообще лежит имя: таблицы и колонки, в которых есть это слово.

    Возвращает `['таблица.колонка', …]`; чистое имя таблицы — с точкой на
    конце. Вопрос «в какой из сорока таблиц лежит время» до этого стоил
    перебора DESCRIBE.
    """
    needle = _ident(word).lower()
    sql = (
        'SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.COLUMNS '
        "WHERE TABLE_SCHEMA = DATABASE() AND LOWER(COLUMN_NAME) "
        f"LIKE '%{needle}%' "
        'UNION ALL '
        'SELECT TABLE_NAME, \'\' FROM information_schema.TABLES '
        "WHERE TABLE_SCHEMA = DATABASE() AND LOWER(TABLE_NAME) "
        f"LIKE '%{needle}%' "
        'ORDER BY 1, 2')
    return [f'{row[0]}.{row[1]}' for row in _mysql_remote_rows(sql, target,
                                                               **dest)]


def _mysql_remote_read_only(sql: str) -> bool:
    """Читает ли запрос — первым словом каждого выражения, как у оригинала.

    Делим общим `sql_split_statements` по той же причине, что там: наивное
    деление по `;` режет строку и честный запрос получает отказ.
    """
    words = [one.split(None, 1)[0].lower()
             for one in sql_split_statements(sql) if one]
    return bool(words) and all(word in MYSQL_REMOTE_READ for word in words)


def _mysql_remote_exec(sql: str, target: str, *, container: str = 'mysql',
                       env_file: str = '.env', app_dir: str = '',
                       rows: bool = False, quiet=MYSQL_REMOTE_QUIET) -> dict:
    """Собрать скрипт и донести его до хоста — общий ход запроса и разведки."""
    head = []
    if app_dir:
        head.append(_cd(app_dir))
    head += ['set -a', f'. {_dot(env_file)}', 'set +a']
    flags = '-N -B' if rows else '-t'
    tail = (f'printf %s "$DSH_SQL" | docker exec -i -e MYSQL_PWD="$MYSQL_PASSWORD" '
            f'{shlex.quote(container)} mysql --default-character-set=utf8mb4 '
            f'{flags} -u"$MYSQL_USER" "$MYSQL_DATABASE"')
    return ssh_x(target, head + [tail], args={'DSH_SQL': _dollar(sql)},
                 quiet=quiet)


def _mysql_remote_rows(sql: str, target: str, container='mysql',
                       env_file='.env', app_dir='', **ignored) -> list:
    """Запрос разведки как строки значений; отказ базы — исключение.

    Разведка вызывается ровно тогда, когда собрался читать, — молчаливый
    пустой ответ здесь хуже слова: оператор решит, что таблицы пусты.
    """
    answer = _mysql_remote_exec(sql, target, container=container,
                                env_file=env_file, app_dir=app_dir, rows=True)
    if answer['code'] != 0:
        raise RuntimeError(f"прод не ответил: {(answer['error'] or answer['output'])[-300:]}")
    return [line.split('\t') for line in answer['output'].splitlines() if line]


def _dollar(text: str) -> str:
    """Уберечь текст от толкования `ssh_x`: значение с `$` на начале сочтётся
    ссылкой на локальное окружение. Добавленный перевод строки безвреден и для
    SQL, и для Python.
    """
    return '\n' + text if text.startswith('$') else text


def _dot(path: str) -> str:
    """Путь для `. file`: POSIX ищет голое имя по PATH, а не в каталоге —
    относительному пути нужен `./` на начале. ⚠ Относительность судится по
    отсутствию `/`, а не по первой букве: `.env.prod` — голое имя с точкой,
    первая версия принимала его за готовый относительный путь.
    """
    return shlex.quote(path if '/' in path else './' + path)


def _cd(path: str) -> str:
    """`cd` с разбором `~/`: в кавычках тильда не раскрывается."""
    if path.startswith('~/'):
        return f'cd "$HOME/{shlex.quote(path[2:])}"'
    return f'cd {shlex.quote(path)}'


def _ident(name: str) -> str:
    """Имя для вшивки в запрос: кавычки и перевод строк из него вон."""
    return ''.join(ch for ch in name if ch.isalnum() or ch == '_')


def _quote_word(word: str) -> str:
    """Слово как SQL-литерал `LIKE`: буквы, подчёркивание и маски `%`, остальное — вон."""
    return "'" + ''.join(ch for ch in word if ch.isalnum() or ch in '_%') + "'"


if __name__ == '__main__':
    ap = argparse.ArgumentParser(
        description='разовый запрос к базе прода; учётки читаются на том хосте '
                    'из его .env')
    ap.add_argument('sql', help='запрос; «-» — прочитать со stdin')
    ap.add_argument('--target', required=True, help='куда ходить: алиас ssh')
    ap.add_argument('--container', default='mysql', help='контейнер базы')
    ap.add_argument('--env', default='.env', help='файл окружения на том хосте')
    ap.add_argument('--dir', default='', help='каталог приложения на том хосте')
    ap.add_argument('--rows', action='store_true',
                    help='голые строки табами вместо таблицы')
    ap.add_argument('--write', action='store_true',
                    help='разрешить меняющие запросы')
    ns = ap.parse_args()
    body = sys.stdin.read() if ns.sql == '-' else ns.sql
    out = mysql_remote_sql(body, ns.target, container=ns.container,
                           env_file=ns.env, app_dir=ns.dir, rows=ns.rows,
                           write=ns.write)
    print(out['output'], end='')
    if out['error']:
        print(f"⚠ {out['error']}", file=sys.stderr)
    raise SystemExit(0 if out['code'] == 0 else 1)
