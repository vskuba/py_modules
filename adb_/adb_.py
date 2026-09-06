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

Здесь же общий низ пакета — `adb_run` и `adb_run_bytes`: соседние модули
(`adb_ui`, `adb_input`, `adb_app`, `adb_log`) отличаются командой, а не
способом её звать, и свой `subprocess.run` каждому не нужен.
"""
import argparse
import re
import subprocess

# Потолок на одну команду adb. Секунды: `screencap` на большом экране идёт
# заметно дольше `devices`, но всё это единицы секунд — команда, ушедшая за
# полминуты, уже не задумалась, а повисла.
ADB_TIMEOUT = 30.0

# `ai_vision` подключается лениво, внутри функций, — не шапкой модуля:
# снимок экрана нужен и в тощих проектах без LLM-стека, а `ai_vision`
# через реестр провайдеров тянет `pydantic_ai` (проверено на Reserve: без
# него падал даже `adb_capture`, хотя модели там и не нюхом бывало).


def adb_devices() -> list[str]:
    """
    Сериалы подключённых устройств, без offline-строк.

    Пустой список — adb жив, но телефонов нет (или не включён USB-debug).

    Raises:
        RuntimeError: `adb` не установлен или сервер не отвечает.
    """
    out = adb_run('devices')
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
    from ai.ai_vision import ai_vision_normalize
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
    from ai.ai_vision import ai_vision_describe_wait
    return ai_vision_describe_wait(adb_capture(serial=serial), prompt=prompt,
                                   model_name=model_name)


def adb_screen_size(serial: str = '') -> tuple[int, int]:
    """
    Размер экрана в пикселях — `(ширина, высота)`.

    Нужен всем, кто считает координаты долями экрана: у телефонов они разные,
    а зашитые числа означают жест не туда на первом же другом устройстве.

    Args:
        serial: устройство; пусто — единственное подключённое.

    Raises:
        RuntimeError: adb подвёл или ответ `wm size` не разобран.
    """
    out = adb_run('shell', 'wm', 'size', serial=serial)
    # «Physical size: 1080x2400», а при включённом масштабировании ниже ещё и
    # «Override size: …» — берём последнюю названную, она и действует.
    sizes = re.findall(r'size:\s*(\d+)x(\d+)', out)
    if not sizes:
        raise RuntimeError(f'adb wm size: не разобран ответ: {out.strip()[:200]}')
    return int(sizes[-1][0]), int(sizes[-1][1])


def adb_run(*args: str, serial: str = '', timeout: float = ADB_TIMEOUT) -> str:
    """
    Вызвать adb и вернуть stdout текстом — общий вход для всего пакета.

    Args:
        *args: аргументы после `adb` — `('shell', 'input', 'tap', '10', '20')`.
        serial: устройство; пусто — единственное подключённое.
        timeout: секунды на команду.

    Returns:
        stdout без изменений.

    Raises:
        RuntimeError: `adb` не установлен, не ответил за `timeout` или вернул
            ненулевой код.
    """
    proc = _adb_exec(args, serial, timeout, text=True)
    if proc.returncode != 0:
        # adb пишет причину то в stderr, то в stdout («error: device offline»),
        # поэтому берём то, что непусто, — иначе получаем ошибку без текста.
        why = (proc.stderr or proc.stdout or '').strip()
        raise RuntimeError(f"adb {' '.join(args)}: {why[:200]}")
    return proc.stdout


def adb_run_bytes(*args: str, serial: str = '', timeout: float = ADB_TIMEOUT) -> bytes:
    """
    То же, что `adb_run`, но stdout возвращается байтами.

    Для двоичного вывода (`exec-out screencap`, `exec-out tar`): в текстовом
    режиме перекодировка портит байты необратимо.
    """
    proc = _adb_exec(args, serial, timeout, text=False)
    if proc.returncode != 0:
        raise RuntimeError(f"adb {' '.join(args)}: "
                           f"{proc.stderr.decode(errors='replace').strip()[:200]}")
    return proc.stdout


def _screencap_png(serial: str = '') -> bytes:
    """Голый PNG из `adb exec-out screencap -p`; exec-out — чтобы перевод строк не испортил бинарь."""
    png = adb_run_bytes('exec-out', 'screencap', '-p', serial=serial)
    if not png.startswith(b'\x89PNG'):
        raise RuntimeError('adb screencap: вывод не похож на PNG (устройство спит? снимок экранирован?)')
    return png


def _adb_exec(args: tuple, serial: str, timeout: float, text: bool):
    """Собственно запуск: сборка команды и перевод отказов среды в понятную ошибку."""
    try:
        return subprocess.run(_adb_cmd(serial) + list(args),
                              capture_output=True, timeout=timeout, text=text)
    except FileNotFoundError:
        raise RuntimeError('adb не найден в PATH — поставь android-tools (пакет с platform-tools)')
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"adb {' '.join(args)}: не ответил за {timeout:g} с — "
                           f"устройство занято или команда не завершается сама (logcat без -d)")


def _adb_cmd(serial: str) -> list[str]:
    """Команда adb с `-s`, когда сериал назван; проверять единственность устройства нечего — adb сам разберётся и сам же ошибётся громко."""
    return ['adb'] + (['-s', serial] if serial else [])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Снимки андроид-устройства и их описание.',
        epilog='вопрос идёт последним, ключи — до него: describe --model gx10/qwen-large что на экране')
    parser.add_argument('command', choices=['devices', 'size', 'capture', 'describe'])
    parser.add_argument('--serial', default='', help='устройство; по умолчанию единственное')
    parser.add_argument('--out', default='/tmp/adb_screen.jpg', help='куда сохранить снимок (capture)')
    parser.add_argument('--model', default='', help='vision-модель, например gx10/qwen-large')
    parser.add_argument('prompt', nargs='*', help='вопрос об экране (describe)')
    ns = parser.parse_args()

    try:
        if ns.command == 'devices':
            print('\n'.join(adb_devices()) or 'устройств нет')
        elif ns.command == 'size':
            print('{}x{}'.format(*adb_screen_size(serial=ns.serial)))
        elif ns.command == 'capture':
            print(adb_capture_save(ns.out, serial=ns.serial))
        else:
            print(adb_screen_describe(' '.join(ns.prompt), serial=ns.serial, model_name=ns.model))
    except (RuntimeError, FileNotFoundError) as err:
        raise SystemExit(f"ошибка: {err}")
