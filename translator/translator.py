from deep_translator import GoogleTranslator

from logging_.logging_ import logger_info


def translate_text(text, target='ru'):
    """
        Переводит текст, учитывая ограничения Google (до 5000 символов)
        и возможные ошибки сети.
        """
    if not text or text.isspace():
        return text

    try:
        # GoogleTranslator имеет лимит около 5000 символов на один запрос.
        # Если текст большой, deep_translator может выдать ошибку или обрезать его.
        translator = GoogleTranslator(source='auto', target=target)

        # Если текст ОЧЕНЬ большой, используем встроенный метод translate_batch или разбиваем сами
        if len(text) > 4500:
            # Разрезаем текст по строкам, чтобы не сломать предложения
            lines = text.split('\n')
            return translator.translate_batch(lines)

        return translator.translate(text)

    except Exception as e:
        logger_info(f"Ошибка перевода: {e}")
        return f"[Error Translate]: {text}"
