"""
Андроид-устройство через adb: снимки экрана и их чтение.

Модуль отдельный от `ai/ai_vision` сознательно: adb — про устройство, а не
про модели. Связь одна и она же причина, почему снимок нельзя отдавать «как
есть»: `screencap` вернёт RGBA-PNG, который локальные vision-серверы
отвергают (шапка `ai_vision`), поэтому `adb_capture` нормализует кадр в JPEG
сразу, на выходе из adb.

Всё через `adb` из PATH. Устройство — либо по сериалу, либо единственное
подключённое; при нескольких adb не угадывает, а требует `serial` — снимок не
с того телефона дороже ошибки молчанием.
"""
import argparse
import subprocess

from ai.ai_vision import ai_vision_describe_wait, ai_vision_normalize


def adb_devices() -> list[str]:
    """
    Сериалы подключённых устройств, без offline-строк.

    Пустой список — adb жив, но телефонов нет (или не включён USB-debug).

    Raises:
        RuntimeError: `adb` не установлен или сервер не отвечает.
    """
    out = _adb_run('devices')
    serials = []
    for line in out.splitlines()[1:]:  # первая строка — заголовок «List of devices attached»
        serial, _, state = line.partition('\t')
        if state.strip() == 'device':
            serials.append(serial.strip())
    return serials


def adb_capture(serial: str = '', max_side: int = 0) -> bytes:
    """
    Снять текущий экран устройства — готовым для vision-моделей RGB-JPEG.

    Байты уже без альфы и, при желании, уменьшены: можно слать в `ai_vision`
    или сохранять как есть.

    Args:
        serial: устройство; пусто — единственное подключённое.
        max_side: предел длинной стороны, 0 — оставить разрешение снимка
            (впрочем, нормализация без `max_side` всё равно снимает альфу,
            иначе кадр из adb нечитаем vision-серверами).

    Returns:
        Байты JPEG.

    Raises:
        RuntimeError: adb/устройство подвело или вывод не похож на PNG.
    """
    png = _screencap_png(serial)
    return ai_vision_normalize(png, max_side=max_side)


def adb_capture_save(path: str, serial: str = '') -> str:
    """
    Снять экран устройства и сохранить в файл; вернуть путь файла.

    Args:
        path: куда писать (`.jpg` по смыслу; расширение не проверяем).
        serial: устройство; пусто — единственное подключённое.
    """
    with open(path, 'wb') as fh:
        fh.write(adb_capture(serial=serial))
    return path


def adb_screen_describe(prompt: str = '', serial: str = '', model_name: str = '') -> str:
    """
    Увидеть текущий экран устройства: снять и описать vision-моделью.

    Самый короткий путь «какие у телефона глаза»: снимок -> JPEG -> модель.

    Args:
        prompt: вопрос об экране; пусто — подробное описание с транскрипцией.
        serial: устройство; пусто — единственное подключённое.
        model_name: vision-модель с префиксом сервиса; пусто — по умолчанию
            из `AI_VISION_MODEL_NAME` / gx10.

    Returns:
        Текст описания экрана.
    """
    return ai_vision_describe_wait(adb_capture(serial=serial), prompt=prompt,
                                   model_name=model_name)


def _screencap_png(serial: str = '') -> bytes:
    """Голый PNG из `adb exec-out screencap -p`; exec-out — чтобы перевод строк не испортил бинарь."""
    proc = subprocess.run(_adb_cmd(serial) + ['exec-out', 'screencap', '-p'],
                          capture_output=True, timeout=30)
    if proc.returncode != 0:
        raise RuntimeError(f"adb screencap: {proc.stderr.decode(errors='replace')[:200]}")
    if not proc.stdout.startswith(b'\x89PNG'):
        raise RuntimeError('adb screencap: вывод не похож на PNG (устройство спит? снимок экранирован?)')
    return proc.stdout


def _adb_run(*args: str) -> str:
    """Вызвать adb без устройства и вернуть stdout текстом."""
    proc = subprocess.run(['adb'] + list(args), capture_output=True, timeout=30, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"adb {' '.join(args)}: {proc.stderr.strip()[:200]}")
    return proc.stdout


def _adb_cmd(serial: str) -> list[str]:
    """Команда adb с `-s`, когда сериал назван; проверять единственность устройства нечего — adb сам разберётся и сам же ошибётся громко."""
    return ['adb'] + (['-s', serial] if serial else [])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Снимки андроид-устройства и их описание.')
    parser.add_argument('command', choices=['devices', 'capture', 'describe'])
    parser.add_argument('--serial', default='', help='устройство; по умолчанию единственное')
    parser.add_argument('--out', default='/tmp/adb_screen.jpg', help='куда сохранить снимок (capture)')
    parser.add_argument('--model', default='', help='vision-модель, например gx10/qwen-large')
    parser.add_argument('prompt', nargs='*', help='вопрос об экране (describe)')
    ns = parser.parse_args()

    try:
        if ns.command == 'devices':
            print('\n'.join(adb_devices()) or 'устройств нет')
        elif ns.command == 'capture':
            print(adb_capture_save(ns.out, serial=ns.serial))
        else:
            print(adb_screen_describe(' '.join(ns.prompt), serial=ns.serial, model_name=ns.model))
    except (RuntimeError, FileNotFoundError) as err:
        raise SystemExit(f"ошибка: {err}")
