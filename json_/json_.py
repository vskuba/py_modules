import json
import traceback

from logging_.logging_ import logger_info


def json_from_string(string: str, raise_on_error: bool = False) -> dict:
    # 1. Если уже пришел словарь, сразу возвращаем его
    if isinstance(string, dict):
        return string

    # 2. Если пришла не строка (например, None, int, list)
    if not isinstance(string, str):
        if raise_on_error:
            raise ValueError(f"Ожидалась строка или словарь, получен тип: {type(string).__name__}")
        return {}

    try:
        # Убираем лишние пробелы по краям перед парсингом
        string_clean = string.strip()
        if not string_clean:
            return {}

        parsed = json.loads(string_clean, strict=False)

        # Гарантируем, что результат парсинга — это именно словарь (dict)
        if isinstance(parsed, dict):
            return parsed

        # Если распарсился массив [ ... ] или примитив
        error_msg = f"Ожидался JSON-объект (dict), но получен {type(parsed).__name__}."
        if raise_on_error:
            raise TypeError(error_msg)

        logger_info(f"⚠️ {error_msg} Текст: {string}")
        return {}

    except Exception as e:
        backtrace = traceback.format_exc()
        logger_info(
            f"❌ Не удалось распарсить json: {e}, string: {string}\n"
            f"Полный стек вызовов:\n{backtrace}"
        )

        # Если включен флаг, пробрасываем ошибку дальше
        if raise_on_error:
            raise

        return {}