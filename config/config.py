import os
from dotenv import find_dotenv, set_key, load_dotenv
from typing import Any

# Два слоя: `.env` — базовый (на деплое он пересобирается из секрета, поэтому
# правки в нём недолговечны), `.env.local` — необязательная накладка машины
# разработчика. Накладка идёт с `override=True`: она обязана побеждать и значение
# из `.env`, и переменную, которую контейнеру подсунул `env_file` compose-а, —
# иначе локальные порты и учётки в ней бессмысленны. Файла нет (прод) — остаётся
# один слой, поведение прежнее.
ENV_PATH = find_dotenv(usecwd=True)
ENV_LOCAL_PATH = os.path.join(os.path.dirname(ENV_PATH), '.env.local') if ENV_PATH else ''

load_dotenv(ENV_PATH)
if ENV_LOCAL_PATH and os.path.isfile(ENV_LOCAL_PATH):
    load_dotenv(ENV_LOCAL_PATH, override=True)


def config_get(key: str, default: str = '') -> Any:
    """
    Возращает конфиги по ключу из .env (значение из .env.local, если оно там есть)
    """
    return os.getenv(key.upper(), default)


def config_update_and_save(key: str, value: Any):
    """
    Сохраняет конфиги по ключу и значение в .env

    Пишем в базовый слой. Если этот же ключ переопределён в `.env.local`, после
    перезапуска процесса вернётся значение накладки, а не сохранённое, — правку
    надо делать в самой накладке.
    """
    set_key(ENV_PATH or find_dotenv(), key.upper(), value)
    os.environ[key.upper()] = value