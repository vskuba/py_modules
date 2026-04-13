import os
from dotenv import find_dotenv, set_key, load_dotenv
from typing import Any

load_dotenv()

def config_get(key: str, default: str = '') -> Any:
    """
    Возращает конфиги по ключу из .env
    """
    return os.getenv(key.upper(), default)


def config_update_and_save(key: str, value: Any):
    """
    Сохраняет конфиги по ключу и значение в .env
    """
    set_key(find_dotenv(), key.upper(), value)
    os.environ[key.upper()] = value