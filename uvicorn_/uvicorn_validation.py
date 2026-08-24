"""Ошибки проверки полей — человеческим языком.

FastAPI на неверный ввод отвечает списком объектов: `[{"type": "string_too_short",
"loc": ["body", "password"], "msg": "String should have at least 1 character", …}]`.
Для клиента это исчерпывающе, для человека — нет: страница честно печатала эту
простыню прямо в форму входа.

Здесь она превращается в одну фразу: «Заполните пароль». Тот же формат, что у
остальных отказов (`{"detail": "…"}`), поэтому страница показывает её как есть,
ничего не разбирая.

**Перевод по смыслу, а не по словарю сообщений.** Мы смотрим на тип ошибки и имя
поля, а не на английский текст pydantic: текст меняется от версии к версии, а тип
и путь — нет.
"""

from fastapi import Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

# Как называется поле для человека. Чего нет в списке — показывается как есть:
# «site_id» в сообщении лучше, чем молчание о том, где именно ошибка.
UVICORN_VALIDATION_FIELDS = {
    'username': 'логин',
    'password': 'пароль',
    'login': 'логин',
    'site_id': 'сайт',
    'note': 'пометка',
    'text': 'текст сообщения',
    'to': 'номер собеседника',
    'seconds': 'время прослушивания',
    'scenario_id': 'сценарий',
    'session_id': 'сессия браузера',
    'auto_answer': 'автоответ',
    'auto_answer_only': 'список собеседников',
    'limit': 'количество',
}

# Что сказать про каждый род ошибки. `missing` и `string_too_short` с нижней
# границей в один символ — это одно и то же для человека: поле не заполнено.
UVICORN_VALIDATION_TEXTS = {
    'missing': 'заполните {field}',
    'string_too_short': 'заполните {field}',
    'string_too_long': '{field}: слишком длинное значение (не больше {limit} знаков)',
    'value_error': '{field}: неверное значение',
    'int_parsing': '{field}: нужно число',
    'float_parsing': '{field}: нужно число',
    'bool_parsing': '{field}: нужно «да» или «нет»',
    'greater_than': '{field}: значение должно быть больше {limit}',
    'greater_than_equal': '{field}: значение не меньше {limit}',
    'less_than_equal': '{field}: значение не больше {limit}',
    'json_invalid': 'тело запроса не разобралось как JSON',
}

# Чем отвечаем, когда род ошибки незнаком. Лучше общая фраза с именем поля, чем
# английская простыня из pydantic.
UVICORN_VALIDATION_FALLBACK = '{field}: неверное значение'

# Сколько ошибок показываем. Больше трёх человек всё равно не читает, а форма
# из двух полей столько и не даст.
UVICORN_VALIDATION_MAX = 3


def uvicorn_validation_message(errors: list) -> str:
    """Список ошибок pydantic → одна фраза для человека."""
    parts = []
    for error in (errors or [])[:UVICORN_VALIDATION_MAX]:
        text = _error_text(error)
        if text and text not in parts:
            parts.append(text)

    if not parts:
        return 'проверьте заполнение полей'

    # Первая буква заглавная — фраза попадает прямо в форму, а не в середину
    # чужого предложения.
    message = '; '.join(parts)

    return message[0].upper() + message[1:]


async def uvicorn_validation_handler(request: Request, exc: RequestValidationError):
    """Ответ на неверный ввод: тот же `detail`, что у остальных отказов."""
    return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        content={'detail': uvicorn_validation_message(exc.errors())})


def _error_text(error: dict) -> str:
    """Одна ошибка pydantic → фраза. Не разобрались — общая, но с именем поля."""
    field = _field_name(error.get('loc') or ())
    kind = str(error.get('type') or '')
    template = UVICORN_VALIDATION_TEXTS.get(kind, UVICORN_VALIDATION_FALLBACK)

    context = error.get('ctx') or {}
    limit = context.get('max_length', context.get('min_length', context.get('ge', context.get('gt', ''))))

    return template.format(field=field, limit=limit)


def _field_name(loc) -> str:
    """Человеческое имя поля из пути ошибки.

    Путь приходит целиком (`['body', 'password']`), и первый его кусок —
    «где именно» (`body`, `query`, `path`) — человеку не нужен: он и так видит,
    какую форму заполняет.
    """
    parts = [str(part) for part in loc if str(part) not in ('body', 'query', 'path', 'header')]
    name = parts[-1] if parts else ''

    return UVICORN_VALIDATION_FIELDS.get(name, name or 'поле')
