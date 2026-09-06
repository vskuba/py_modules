"""
Кнопка «Зберегти» в системном диалоге сохранения — documentsui.

Печать PDF из WebView (`createPrintDocumentAdapter`) финиширует не в нашем
окне, а в системном выборе «Сохранить как PDF»: CDP туда не достаёт (это не
наша страница), зрение даёт приблизительные координаты, а тап в фон диалога
его отменяет — остаются uiautomator-узлы и клики строго по кликабельным.
Файл на выходе ищется не «подождём секунду и поверим», а сдвигом снимка
каталога: диалог мог отработать, а мог и не открыться вовсе.
"""
import argparse
import time

from adb_.adb_ import adb_run
from adb_.adb_app import adb_app_current
from adb_.adb_input import adb_input_tap
from adb_.adb_ui import adb_ui_nodes

# Диалог сохранения — DocumentsUI. В GApps и голом AOSP один и тот же интерфейс
# называется то с префиксом `com.google.`, то без, поэтому фокус узнаём по
# подстроке `documentsui`, а константа — каноническое имя для сообщений.
ADB_DOC_PACKAGE = 'com.google.android.documentsui'

# Полный ожидательный бюджет, секунды: оффскрин-печать PDF на эмуляторе
# занимает несколько секунд, ещё пара на отрисовку диалога и запись файла.
ADB_DOC_TIMEOUT = 40.0

# Пауза между опросами диалога, секунды; uiautomator-дамп сам стоит ~полсекунды.
ADB_DOC_STEP = 0.5

# Кнопка сохранения, нижний регистр: в UA-локале «Зберегти», в EN — «Save».
_ADB_DOC_SAVE_TEXTS = ('зберегти', 'save')

# Подтверждение перезаписи: «Замінити» / «Replace». Имя eVOD несёт метку
# времени, так что штатно диалога нет; обрабатываем, чтобы повторный прогон
# в ту же секунду не вешался.
_ADB_DOC_REPLACE_TEXTS = ('замінити', 'заменить', 'replace')


def adb_doc_save(save_dir: str = 'Download', serial: str = '',
                 timeout: float = ADB_DOC_TIMEOUT) -> str:
    """
    Дождаться диалога сохранения, нажать «Зберегти» и дождаться файла.

    Порядок, в котором сам себя проверяет каждый шаг: фокус документов
    подхватил documentsui (не наш ли экран вообще?), кнопка «Зберегти»
    нашлась среди узлов, файл появился или обновился в каталоге. Промптов
    «точно сохранить?» нет намеренно: вызывающий уже нажал «Завантажити
    PDF», и молчаливый отказ здесь дороже лишнего подтверждения.

    Args:
        save_dir: каталог на устройстве; без `/` — под `/sdcard/`.
        serial: устройство; пусто — единственное подключённое.
        timeout: секунды на весь цикл (диалог + кнопка + файл).

    Returns:
        Путь сохранённого файла на устройстве (`/sdcard/Download/eVOD_….pdf`).

    Raises:
        RuntimeError: диалог не появился или кнопки «Зберегти» на нём нет —
            в сообщении перечислены кликабельные надписи экрана.
        TimeoutError: файл так и не появился; вероятные причины — другое
            место сохранения в диалоге или печать упала в самом приложении.
    """
    path = save_dir if save_dir.startswith('/') else f'/sdcard/{save_dir}'
    deadline = time.monotonic() + timeout
    before = _stat_snapshot(path, serial)
    _wait_documentsui(deadline, serial)
    button = _wait_button(deadline, serial, _ADB_DOC_SAVE_TEXTS, '«Зберегти» не дождались')
    adb_input_tap(*button['tap'], serial=serial)
    name = _wait_file(before, path, deadline, serial)
    return f'{path}/{name}'


def _wait_documentsui(deadline: float, serial: str) -> None:
    """Ждать, пока фокус перехватит documentsui — знак, что диалог открылся."""
    while True:
        package = adb_app_current(serial=serial).get('package', '')
        if 'documentsui' in package:
            return
        if time.monotonic() > deadline:
            raise RuntimeError(
                f'{ADB_DOC_PACKAGE} не перехватил фокус (сейчас: {package or "никто"}) — '
                f'открыт ли диалог сохранения?')
        time.sleep(ADB_DOC_STEP)


def _wait_button(deadline: float, serial: str, texts: tuple, what: str) -> dict:
    """Ждать кликабельную кнопку с нужной надписью; сорваться — с диагностикой."""
    while True:
        nodes = adb_ui_nodes(serial=serial)
        button = _pick_button(nodes, texts)
        if button:
            return button
        if time.monotonic() > deadline:
            seen = ', '.join(n['text'] for n in nodes if n['clickable'] and n['text'][:1].isalpha())
            raise RuntimeError(f'{what}: на экране кликабельно только «{seen or "ничего"}»')
        time.sleep(ADB_DOC_STEP)


def _wait_file(before: dict, path: str, deadline: float, serial: str) -> str:
    """
    Ждать нового или обновлённого файла в каталоге, попутно подтверждая перезапись.

    Промпт «Замінити?» приезжает с задержкой в секунды и только при коллизии
    имени, поэтому проверяется в том же цикле, что и файл: отдельная проверка
    сразу после тапа либо не застанет промпт, либо зависнет вместо ожидания.

    Файл отдаётся только когда его штамп совпадает два опроса подряд: запись
    через MediaStore начинается с дырки нулевого размера, и взятый сразу
    файл оказывается оборванным — поймано живым прогоном на проверке размера.
    """
    name, stamp = '', ()
    while True:
        snapshot = _stat_snapshot(path, serial)
        if not name:
            name = _file_diff(before, snapshot)
        entry = snapshot.get(name, ())
        if not entry:
            name, stamp = '', ()  # запись исчезла — ждать заново, с чистым штампом
        elif entry == stamp and entry[0] > 0:
            return name
        stamp = entry
        if not name:
            replace = _pick_button(adb_ui_nodes(serial=serial), _ADB_DOC_REPLACE_TEXTS)
            if replace:
                adb_input_tap(*replace['tap'], serial=serial)
        if time.monotonic() > deadline:
            raise TimeoutError(
                f'файл в {path} не появился или не дописался ({name or "не появлялся"}) — '
                f'возможно, диалог выбрал другое место или печать PDF упала в приложении')
        time.sleep(ADB_DOC_STEP)


def _pick_button(nodes: list, texts: tuple) -> dict:
    """Первая кликабельная кнопка с надписью из `texts` (без регистра); нет — {}."""
    for node in nodes:
        if node['clickable'] and any(t in node['text'].lower() for t in texts):
            return node
    return {}


def _file_diff(before: dict, after: dict) -> str:
    """Имя файла, которого не было или который обновился; пусто — каталог тот же."""
    for name, stamp in after.items():
        if before.get(name) != stamp:
            return name
    return ''


def _stat_snapshot(path: str, serial: str) -> dict:
    """
    Снимок каталога `{имя: (размер, mtime)}` через `stat` на устройстве.

    `ls -l` тут не годится: дата в нём точна до минуты, и сохранение за
    те же секунды, что и старый снимок, осталось бы «неизменённым».
    Нераскрытый glob (каталог пуст) device-Shell отдаёт как ошибку — это
    пустой снимок, а не падение. Каталог без пробелов: кавычек glob не переносит.
    """
    try:
        out = adb_run('shell', f'stat -c "%n|%Y|%s" {path}/* 2>&1', serial=serial)
    except RuntimeError:
        return {}
    snapshot = {}
    for line in out.splitlines():
        parts = line.strip().split('|')  # %n|%Y|%s: имя, mtime, размер
        if len(parts) != 3 or not (parts[1].isdigit() and parts[2].isdigit()):
            continue  # шум «stat: … No such file or directory»
        snapshot[parts[0].rsplit('/', 1)[-1]] = (int(parts[2]), int(parts[1]))
    return snapshot


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Системный диалог сохранения: нажать «Зберегти» и дождаться файла.')
    parser.add_argument('command', choices=['save'])
    parser.add_argument('--dir', default='Download', help='каталог на устройстве (без / — под /sdcard/)')
    parser.add_argument('--serial', default='', help='устройство')
    parser.add_argument('--timeout', type=float, default=ADB_DOC_TIMEOUT, help='секунды на весь цикл')
    ns = parser.parse_args()

    try:
        print(adb_doc_save(save_dir=ns.dir, serial=ns.serial, timeout=ns.timeout))
    except (RuntimeError, TimeoutError) as err:
        raise SystemExit(f'ошибка: {err}')
