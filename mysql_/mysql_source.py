"""
База, о которой спрашивают, описана **командой оператора** — здесь живёт умение её спросить.

Своя база доступна через `.env` (`mysql_query`), а чужая — нет: прод лежит за ssh, копия
в другом контейнере, репетиционная база заводится на время. Для них сложилось одно
соглашение: базу описывает **команда, принимающая SQL на вход и печатающая ответ**.

    docker exec -i db mysql -N base
    ssh prod 'docker exec -i db mysql -N base'

Этот модуль — владелец соглашения. До него умение спрашивать такую базу было
расписано по **шести** файлам (`mysql_collate`, `mysql_rehearse`, `mysql_snapshot`,
`mysql_carry`, `mysql_schema_diff`, `mysql_migration_pending`), и каждый решал заново,
что делать с кодом возврата, с `stderr` и с экранированием. Расхождения были не
теоретические: часть считала пустой ответ отказом, часть — нормой; предупреждение
`mysql` про пароль одни отбрасывали, другие печатали как ошибку базы.

## ⚠⚠ `shell=True` здесь намеренно и по существу

`source` — это команда **оператора**, и в ней `docker exec` с ssh, где кавычки идут в
три слоя. Разбирать её в список нельзя: она и есть строка оболочки. Чужого ввода тут
не бывает — строка приходит из своей же командной строки или из настроек.

⚠ Отсюда правило вызывающим: `source` **не собирается из пользовательских данных**.
Имя таблицы, пришедшее из запроса, в команду не подставляют.

## Три вопроса, три функции

| Нужно | Чем |
|-------|-----|
| ответ как есть, решать про отказ самому | `mysql_source_ask` |
| строки таблицей, отказ исключением | `mysql_source_rows` |
| ответ JSON-ом, не побитым экранированием | `mysql_source_json` |

## ⚠⚠ Почему JSON едет через base64

`mysql` в пакетном режиме экранирует в значениях табуляции, переводы строк и обратные
слеши — и ломает ими сам JSON на первой же записи с длинным текстом. Base64 не содержит
ни одного знака, который бы экранировался. ⚠ Переводы строк, которые `TO_BASE64`
вставляет каждые 76 знаков, снимаются **в SQL**: снять их после поздно — пакетный режим
успевает превратить каждый в двузнаковое `\\n` уже внутри base64, и разбор отдаёт мусор.
"""
import base64
import json
import subprocess
import sys

from pathlib import Path

# Файл запускают и путём. Тогда первым в путях лежит каталог файла, и `import mysql_`
# находит соседний модуль вместо пакета.
if __package__ in (None, ''):
    _here = str(Path(__file__).resolve().parent)
    sys.path[:] = [item for item in sys.path if item not in ('', '.', _here)]
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Сколько ждём ответа. ⚠ Не бесконечно: команда идёт через ssh, и повисшее соединение
# иначе останавливает круг или выкладку без объяснения.
MYSQL_SOURCE_TIMEOUT = 120

# Что в `stderr` не является сообщением об ошибке. ⚠ `mysql` печатает это всегда, когда
# пароль передан в командной строке, и в отчётах оно читалось как ответ базы
# («Записано: mysql: [Warning] …»).
MYSQL_SOURCE_NOISE = ('[Warning] Using a password',)


def mysql_source_ask(source, sql='', timeout=MYSQL_SOURCE_TIMEOUT) -> dict:
    """Спросить базу её командой; решение про отказ — за вызывающим.

    Args:
        source: команда, принимающая SQL на вход и печатающая ответ.
        sql: запрос; пусто — команда запускается как есть (она сама себе запрос).
        timeout: сколько ждать ответа.

    Returns:
        dict: `{ok, out, said}` — ответила ли база, её вывод и сообщение об отказе
        без служебного шума (`MYSQL_SOURCE_NOISE`).

    ⚠⚠ Исключение **не бросается**: у вызывающих разные правила. `mysql_migration_pending`
    на молчащей базе честно отвечает «сверки не было», а `mysql_schema_diff` обязан
    упасть — пустой снимок сравнился бы с полным и объявил, что «на проде нет ничего».
    Свести их к одному поведению значило бы соврать одному из двух.

    ⚠ Не ответившая по таймауту база — это `ok = False` с `said` про срок, а не
    исключение: для вызывающего это тот же отказ, что и любой другой.
    """
    try:
        got = subprocess.run(str(source), shell=True, input=str(sql or ''),
                             capture_output=True, text=True, check=False,
                             timeout=timeout)
    except subprocess.TimeoutExpired:
        return {'ok': False, 'out': '', 'said': f'база не ответила за {timeout} с'}

    said = '\n'.join(one for one in (got.stderr or '').split('\n')
                     if one.strip() and not any(bad in one
                                                for bad in MYSQL_SOURCE_NOISE))

    return {'ok': got.returncode == 0, 'out': got.stdout, 'said': said.strip()}


def mysql_source_rows(source, sql, what='', timeout=MYSQL_SOURCE_TIMEOUT) -> list:
    """Строки ответа таблицей: список кортежей по столбцам.

    Args:
        source: команда базы.
        sql: запрос.
        what: о чём спрашивали — попадёт в текст отказа.
        timeout: сколько ждать ответа.

    Returns:
        list: по кортежу на строку, поля разделены табуляцией. Пустой список — база
        ответила, но строк нет.

    Raises:
        RuntimeError: база не ответила.

    ⚠ Пустые строки вывода отбрасываются, но `NULL` приходит **словом** `NULL` и от
    строки «NULL» неотличим: таким выводом читают ключи и имена, а не данные. Данные —
    `mysql_source_json`.
    """
    got = mysql_source_ask(source, sql, timeout)
    if not got['ok']:
        raise RuntimeError(f"{what or 'запрос'}: {got['said'][:200] or 'база молчит'}")

    return [tuple(one.split('\t')) for one in got['out'].split('\n') if one.strip()]


def mysql_source_json(source, sql, what='', timeout=MYSQL_SOURCE_TIMEOUT):
    """Ответ JSON-ом, не побитым экранированием пакетного режима.

    Args:
        source: команда базы.
        sql: запрос, отдающий **одно** значение — готовый JSON (`JSON_ARRAYAGG(…)`,
            `JSON_OBJECT(…)`). Обёртку в base64 функция делает сама.
        what: о чём спрашивали — попадёт в текст отказа.
        timeout: сколько ждать ответа.

    Returns:
        Разобранный JSON: список или словарь. Пустой ответ базы — пустой список.

    Raises:
        RuntimeError: база не ответила либо ответ не разобрался.

    ⚠⚠ Именно так, а не чтением TSV: в TSV `NULL` приходит словом и неотличим от
    строки «NULL», а числа — от строк. Для переноса данных это разница между пустым
    полем и полем со словом внутри. Почему base64 — см. ⚠⚠ в докстринге модуля.
    """
    packed = ("SELECT REPLACE(TO_BASE64(CAST((" + str(sql).rstrip('; \n')
              + ") AS CHAR)), '\\n', '')")
    got = mysql_source_ask(source, packed, timeout)

    if not got['ok']:
        raise RuntimeError(f"{what or 'запрос'}: {got['said'][:200] or 'база молчит'}")

    ready = ''.join(got['out'].split())
    if not ready or ready.upper() == 'NULL':
        return []

    try:
        return json.loads(base64.b64decode(ready).decode('utf-8'))
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError) as bad:
        raise RuntimeError(f"{what or 'запрос'}: ответ не разобрался — {bad}") from bad
