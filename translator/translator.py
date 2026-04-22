from deep_translator import GoogleTranslator


def translate_text(text, target='ru'):
    translated = GoogleTranslator(source='auto', target=target).translate(text)

    return translated
