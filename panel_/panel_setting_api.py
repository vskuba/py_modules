"""Настройки панели: ключ → значение, правятся из браузера.

Второй способ настроить панель, помимо окружения. Разделение простое: **в `.env`
живёт то, без чего процесс не поднимется** (адрес базы, ключ подписи сессии),
**здесь — то, что подкручивают на ходу**. Первое меняют раз в жизни и
перезапуском, второе — по ходу работы, и лезть за этим на сервер не должно быть
нужно.

Значения читает и пишет `setting_/setting_.py` — он же и задаёт форму таблицы.

⚠ Право проверяется зависимостью в объявлении каждого роута, а не в теле:
забытую проверку тогда находит чтение файла, а не разбор происшествия.

## Что страница не показывает

Два вида строк, и оба намеренно:

* **личные** (`user_id` не пуст) — настройки одного человека, а страница про общие;
* **служебные** (`dynamic = 1`) — поставленные кодом. Это состояние, а не
  настройка: править его руками нечего, а увидеть в общем списке — повод
  попробовать.
"""

from datetime_.datetime_ import datetime_offset_hours
from fastapi import APIRouter, Depends, HTTPException, status
from logging_.logging_ import logger_info
from mysql_.mysql_ import mysql_get_db_async
from pydantic import BaseModel, Field
from setting_.setting_ import setting_delete, setting_get, setting_set

from panel_.panel_auth import panel_auth_admin_required, panel_auth_need

# Под каким путём живут точки. Константой, потому что на него ссылается страница
# настроек: разойдись они — страница тихо показывает пустой список.
PANEL_SETTING_API_PREFIX = '/api/setting'

# Длина ключа — та же, что в схеме (`CHAR(50)`). Проверяем здесь, чтобы вернуть
# 422 с внятным текстом, а не 500 от обрезки на стороне MySQL.
PANEL_SETTING_KEY_MAX = 50

# Ключ часового пояса. ⚠ Имя одно на все панели: настройка одна по смыслу, и две
# панели на одном домене не должны звать её по-разному.
PANEL_SETTING_TIMEZONE_KEY = 'timezone'

# Умолчание — UTC, то есть «как контейнер и живёт». Не «+3» и не пояс машины:
# сервер стоит в UTC, времена в базе лежат в UTC, и молча сдвигать показанное
# значит врать про то, чего человек не просил.
PANEL_SETTING_TIMEZONE_DEFAULT = '+0'


class PanelSettingSave(BaseModel):
    """Что сохраняем. Пустое значение — удалить строку, а не записать пустоту."""

    key: str = Field(..., min_length=1, max_length=PANEL_SETTING_KEY_MAX)
    value: str = ''
    # ⚠ Область узла. Настройка живёт в паре `(user_id, node_id)`, и это свойство
    # самого хранилища (`setting_/setting_.py`), а не одного проекта: там, где
    # узлов нет, поле просто всегда пустое.
    node_id: int | None = None


async def panel_setting_timezone_hours() -> int:
    """Часовой пояс панели в часах от UTC. Не задан или мусор — ноль.

    Зовётся на каждую отрисовку страницы, поэтому **не бросает**: сбой чтения
    настройки не повод не показать панель. Ноль означает «показываем как есть, в
    UTC» — то же, что было до появления настройки, и человек увидит не то время,
    но и не пустую страницу.

    ⚠ Живёт здесь, а не у страницы: страница про пояс ничего не знает, она
    только передаёт число в разметку. Знание о том, каким ключом он зовётся и
    чему равен по умолчанию, принадлежит настройкам.
    """
    try:
        raw = await setting_get(PANEL_SETTING_TIMEZONE_KEY,
                                PANEL_SETTING_TIMEZONE_DEFAULT)
    except Exception as e:
        logger_info(f'[setting] часовой пояс не прочитался: {e}')

        return 0

    return datetime_offset_hours(raw)


def panel_setting_api_router(view=None, manage=None,
                             prefix: str = PANEL_SETTING_API_PREFIX) -> APIRouter:
    """Роутер настроек: `app.include_router(panel_setting_api_router())`.

    Args:
        view: право на чтение списка. `None` — полноправному.
        manage: право на правку и удаление. `None` — то же.
        prefix: под каким путём жить.

    ⚠ Роутер отдаётся функцией, а не переменной модуля: будь он переменной, два
    приложения в одном процессе (а так живут тесты) получили бы один объект с
    уже навешенными роутами.
    """
    guard_view = panel_auth_need(view) if view else panel_auth_admin_required
    guard_manage = panel_auth_need(manage) if manage else panel_auth_admin_required

    router = APIRouter(prefix=prefix, tags=['setting'])

    @router.get('')
    async def setting_list(user=Depends(guard_view)):
        """Общие настройки: ключ, значение, номер строки.

        Личные и служебные не показываются — почему, написано в шапке модуля.
        Сортировка по ключу: список читают глазами, и порядок вставки в нём не
        значит ничего.
        """
        async with mysql_get_db_async() as db:
            await db.execute('SELECT id, `key`, node_id, value FROM setting'
                             ' WHERE user_id IS NULL AND dynamic = 0'
                             ' ORDER BY `key` ASC')
            rows = await db.fetchall() or []

        return {'result': {'settings': [dict(row) for row in rows]}}

    @router.post('')
    async def setting_save(payload: PanelSettingSave, user=Depends(guard_manage)):
        """Заводит настройку или переписывает её значение.

        ⚠⚠ **Пустое значение удаляет строку.** Не записывает пустоту: настройки
        нет и настройка со значением «ничего» — разные вещи. Первое означает
        «действует умолчание кода», второе — «действует пустая строка», и путать
        их значит получать необъяснимое поведение там, где умолчание было
        осмысленным.

        Отдельной ручки «удалить значение» поэтому и нет: очистка поля на
        странице — то же действие, и заводить под него второй путь незачем.
        """
        key = payload.key.strip()
        if not key:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                                detail='ключ не может быть пустым')

        if payload.value == '':
            await setting_delete(key, node_id=payload.node_id)
        else:
            await setting_set(key, payload.value, node_id=payload.node_id)

        return {'result': 'ok'}

    @router.get('/{key}/user/{user_id}')
    async def setting_user_get(key: str, user_id: int, user=Depends(guard_view)):
        """Личная настройка человека. Нет такой — `None`, решает страница."""
        return {'result': await setting_get(str(key).strip(), None, int(user_id))}

    @router.post('/{key}/user/{user_id}')
    async def setting_user_set(key: str, user_id: int, data: dict,
                               user=Depends(guard_manage)):
        """Ставит личную настройку человека.

        ⚠ Пустое значение здесь **отвергается**, а не удаляет строку — в
        отличие от общей настройки. Личную ставят точечно и по одной, и пустота
        в теле запроса тут вернее всего означает ошибку вызывающего.
        """
        value = data.get('value')
        if not value:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                                detail='нет значения')

        return {'result': await setting_set(str(key).strip(), value, int(user_id))}

    @router.delete('/{key}')
    async def setting_drop(key: str, user=Depends(guard_manage)):
        """Убирает настройку целиком: общее значение **и все узловые**.

        ⚠⚠ Именно целиком, а не `setting_delete(key)`. Тот сносит область с
        пустым `node_id` и оставляет переопределения узлов жить — а страница под
        крестиком понимает «настройки больше нет». Там, где узлов не заводят,
        разницы не видно вовсе; там, где заводят, остался бы призрак.

        ⚠ Личные настройки не трогаются: они принадлежат человеку, а не
        установке.

        ⚠ Повторный вызов не ошибка: результат тот же — настройки нет. Отвечать
        404 на удаление уже удалённого значило бы пугать человека тем, чего он и
        добивался.
        """
        async with mysql_get_db_async() as db:
            await db.execute('DELETE FROM setting'
                             ' WHERE `key` = %s AND user_id IS NULL',
                             (str(key).strip(),))
            await db.connection.commit()

        return {'result': 'ok'}

    return router
