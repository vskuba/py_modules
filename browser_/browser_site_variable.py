"""Переменные сайта для прогонов: `{{login}}`, `{{password}}`, адрес кабинета.

Сценарии ссылаются на переменные по имени, а значение нужно один раз на сайт, а не
в каждом сценарии. Отсюда два слова о форме хранения: секретные значения в базе
зашифрованы (`browser_secret`), ключ — в окружении проекта. В списке секрет не
отдаётся никогда: сырое значение — отдельное действие проекта, и доступ к нему
виден в журнале запросов как событие, а не растворяется в обычном показе страницы.

Модуль ничего не знает про права и страницы: список, сохранение, значения для
прогона и удаление — точка входа проекта оборачивает их своими правами.
"""

import re

from browser_.browser_secret import browser_secret_decrypt, browser_secret_encrypt
from mysql_.mysql_ import mysql_get_db_async

# Чем закрыто секретное значение в списке. Не пустая строка: пустое поле читается
# как «переменная не заполнена», и человек завёл бы её заново поверх целой.
BROWSER_SITE_VARIABLE_MASK = '••••••••'

# Имя переменной — то, что стоит внутри `{{…}}`: ровно то, что разбирает подстановка
# сценария. Пробел или скобка в имени дали бы переменную, на которую невозможно
# сослаться.
BROWSER_SITE_VARIABLE_NAME_RE = re.compile(r'^[A-Za-z0-9_-]+$')


async def browser_site_variable_list(site_id: int) -> list:
    """Переменные сайта в порядке показа; секретные значения — под маской, всегда.

    Сырое значение здесь не показывает никто: список рисуется на каждой смене
    сайта, и отдавать пароли пачкой в ответ, который осядет в журнале прокси и в
    истории браузера, незачем. Готов ли ключ к секретам — скажет
    `browser_secret_ready()`: без него завести секрет нельзя, и человек должен
    видеть причину, а не отказ без объяснения.
    """
    async with mysql_get_db_async() as db:
        await db.execute(
            'SELECT id, site_id, name, value, is_secret, description, position, updated_at '
            '  FROM browser_site_variable WHERE site_id = %s ORDER BY position, id',
            (int(site_id),))
        rows = await db.fetchall() or []

    items = []
    for row in rows:
        row = dict(row)
        row['is_secret'] = bool(row.get('is_secret'))
        # Открытое значение проходит через тот же разбор: без метки `enc:v1:`
        # расшифровка отдаёт строку как есть, а с чужой меткой честно скажет,
        # что не читает.
        row['value'] = (BROWSER_SITE_VARIABLE_MASK if row['is_secret']
                        else browser_secret_decrypt(row.get('value')))
        items.append(row)

    return items


async def browser_site_variable_values(site_id: int) -> tuple:
    """Переменные сайта для прогона: `(обычные, секретные)` — обе расшифрованы.

    Две сумки, а не одна с флажком: потребителю нельзя дать возможность залогировать
    всё скопом. Обычные попадают в журнал прогона как есть, секретные замазываются
    в трассе и в тексте ошибок (`browser_scenario_mask`).
    """
    async with mysql_get_db_async() as db:
        await db.execute(
            'SELECT name, value, is_secret FROM browser_site_variable '
            ' WHERE site_id = %s ORDER BY position, id', (int(site_id),))
        rows = await db.fetchall() or []

    plain, secret = {}, {}
    for row in rows:
        target = secret if row.get('is_secret') else plain
        target[row['name']] = browser_secret_decrypt(row.get('value'))

    return plain, secret


async def browser_site_variable_save(variable_id: int, site_id: int, name: str,
                                    value: str = '', is_secret: bool = False,
                                    description: str = '') -> int:
    """Завести или переписать переменную сайта; возвращает номер строки.

    Пустой `value` при правке означает «значение не трогать»: поле секрета на
    странице показано маской, и отправь мы его как есть, паролем стала бы строка
    из точек. Очистить значение по-настоящему — удалить переменную.

    Смена `is_secret` переписывает значение целиком (открытое, ставшее секретным,
    шифруется заново), иначе в базе осталась бы строка, чей вид не соответствует
    пометке, и решать «шифровать ли» пришлось бы гаданием по содержимому.

    Raises:
        ValueError: имя не похоже на подстановку сценария, ключ секретов не задан
            или такое имя у сайта уже есть.
        KeyError: правка переменной, которой нет, — иначе она молча ничего бы не делала.
    """
    name = str(name or '').strip().strip('{}').strip()
    if not BROWSER_SITE_VARIABLE_NAME_RE.match(name):
        raise ValueError('имя переменной — только буквы, цифры, `_` и `-` '
                         '(без фигурных скобок)')

    row = None
    if variable_id:
        async with mysql_get_db_async() as db:
            await db.execute('SELECT * FROM browser_site_variable WHERE id = %s',
                             (int(variable_id),))
            row = await db.fetchone()
            if not row:
                raise KeyError('переменная не найдена')
            await _name_free(db, site_id, name, skip_id=int(variable_id))
            if value:
                stored = _value_store(value, is_secret)
            elif bool(is_secret) == bool(row.get('is_secret')):
                # Пометка не сменилась и нового значения не прислали — оставляем прежнее.
                stored = row.get('value') or ''
            else:
                # Пометка сменилась — перекладываем прежнее: расшифровываем и,
                # если надо, шифруем обратно.
                stored = _value_store(browser_secret_decrypt(row.get('value')), is_secret)
            await db.execute(
                'UPDATE browser_site_variable'
                '   SET site_id = %s, name = %s, value = %s, is_secret = %s, description = %s '
                ' WHERE id = %s',
                (site_id, name, stored, int(is_secret), description, int(variable_id)))

            return int(variable_id)

    async with mysql_get_db_async() as db:
        await _name_free(db, site_id, name)
        await db.execute(
            'SELECT COALESCE(MAX(position), -1) + 1 AS next FROM browser_site_variable '
            ' WHERE site_id = %s', (site_id,))
        position = int((await db.fetchone() or {}).get('next') or 0)
        await db.execute(
            'INSERT INTO browser_site_variable '
            '(site_id, name, value, is_secret, description, position) '
            'VALUES (%s, %s, %s, %s, %s, %s)',
            (site_id, name, _value_store(value, is_secret), int(is_secret),
             description, position))

        return int(db.lastrowid)


async def browser_site_variable_remove(variable_id: int) -> None:
    """Убрать переменную. Несуществующая — тоже успех: результат тот же."""
    async with mysql_get_db_async() as db:
        await db.execute('DELETE FROM browser_site_variable WHERE id = %s', (int(variable_id),))


async def _name_free(db, site_id: int, name: str, skip_id: int = 0) -> None:
    """Имя уникально внутри сайта — двойник дал бы вторую сумку под то же `{{имя}}`."""
    await db.execute(
        'SELECT 1 FROM browser_site_variable '
        ' WHERE site_id = %s AND name = %s AND id <> %s LIMIT 1',
        (site_id, name, int(skip_id)))
    if await db.fetchone():
        raise ValueError(f'переменная «{name}» у сайта уже есть')


def _value_store(value: str, is_secret: bool) -> str:
    """Значение в том виде, в каком оно ложится в базу: секрет — шифротекст с меткой.

    Отказ ключа — ValueError, не поломка сервера: это незаконченная настройка
    установки, и человеку нужно сказать, чего именно не хватает.
    """
    if not is_secret:
        return str(value or '')
    try:
        return browser_secret_encrypt(value)
    except RuntimeError as error:
        raise ValueError(str(error))
