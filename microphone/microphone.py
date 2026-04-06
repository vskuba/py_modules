import speech_recognition as sr

from speech_recognition import Recognizer


def microphone_create_microphone() -> Recognizer:
    """
    Создает и возращает обьект микрофона для прослушивания звуков
    """
    recognizer = sr.Recognizer()  # Настраиваем микрофон
    recognizer.energy_threshold = 1000  # Порог чувствительности (можно подстроить)
    recognizer.dynamic_energy_threshold = True
    return recognizer
