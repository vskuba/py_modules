"""
APK на машине: что внутри, как устроен чужой интерфейс, как вытащить ресурс.

`unzip -l` из APK делает список, и на этом его полезная работа кончается:
манифест, темы и drawable лежат в бинарном AXML — текстовый `grep` по ним не
находит ничего и молча возвращает пустоту, что выглядит как «атрибута нет».
Настоящий читатель — `aapt2 dump` из Android SDK: `apk_dump` показывает
таблицу ресурсов (темы, значения атрибутов), `apk_xml` разворачивает один
компилированный XML в читаемое дерево, `apk_pathdata` вынимает из vector-иконки
`pathData` и цвета заливки — их потом рисует `image_.image_svg`.

Приложение сначала снимают с устройства (`adb_.adb_app.adb_app_pull_apk`) или
берут скачанный файл: смотреть чужой APK можно целиком без телефона.
"""
import argparse
import fnmatch
import glob
import json
import os
import shutil
import subprocess
import zipfile

# Зонтик build-tools: конкретная версия в SDK меняется, искать всегда свежайшую.
APK_BUILD_TOOLS_GLOB = 'build-tools/*/aapt2'

# Строки aapt2, которыми начинается запись ресурсной таблицы; они тянутся за
# совпадением в apk_dump, чтобы значение не приезжало без имени ресурса.
APK_ENTRY_MARKS = ('resource ', 'package ', 'type ')


def apk_entries(apk: str, pattern: str = '', base_only: bool = False) -> list:
    """
    Имена записей APK (zip-путей).

    Args:
        apk: путь к APK на машине.
        pattern: fnmatch-фильтр (`res/drawable*/ic_launcher*`, `assets/*`);
            пусто — все записи.
        base_only: только `res/` и `assets/` без корневого мусора APK
            (META-INF, classes*.dex, resources.arsc).

    Returns:
        Отсортированный список путей записей.

    Raises:
        FileNotFoundError: файла нет или это не zip.
        ValueError: файл есть, но zip-ом не открывается.
    """
    with _zip(apk) as zf:
        names = zf.namelist()
    if pattern:
        names = [n for n in names if fnmatch.fnmatch(n, pattern)]
    if base_only:
        names = [n for n in names if n.startswith(('res/', 'assets/'))]
    return sorted(names)


def apk_dump(apk: str, grep: str = '', config: str = 'default') -> list:
    """
    Таблица ресурсов APK строкой в строку: имена, id, значения по конфигурациям.

    Здесь видны темы и атрибуты уровня системы (`windowSplashScreenAnimatedIcon`,
    `windowLightStatusBar`): какой ресурс назначен атрибутом, из чего собран
    стиль. Совпадения возвращаются вместе со строками `resource/type/package`,
    под которыми живут, — значение без имени ресурса бесполезно.

    Args:
        apk: путь к APK.
        grep: подстрока (регистронезависимая); пусто — все строки дампа.
        config: какую конфигурацию строки значения оставлять; 'default' — без
            суффиксов языка/плотности, '' — все (для фильтров вроде
            'xxhdpi' или 'ua').

    Returns:
        Список строк дампа.

    Raises:
        RuntimeError: aapt2 не установлен или не открыл APK.
    """
    lines = _aapt2_dump(apk)
    if not grep:
        return lines
    low = grep.lower()
    out, header = [], ''
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith(APK_ENTRY_MARKS):
            header = line
            if low in line.lower() and _config_ok(line, config):
                out.append(line)
                header = ''          # совпало само имя — заголовок уже сказан
        elif low in line.lower() and _config_ok(line, config):
            if header:
                out.append(header)
                header = ''
            out.append(line)
    return out


def apk_xml(apk: str, entry: str) -> str:
    """
    Один компилированный XML APK (манифест, layout, drawable) в читаемом виде.

    Это ответ на «grep по APK ничего не нашёл»: AXML бинарный, и отсутствие
    строки в zip-выводе не значит, что атрибута нет. Дерево выдаётся как его
    печатает aapt2: элементы `E:`, атрибуты `A:` с полными urn-именами.

    Args:
        apk: путь к APK.
        entry: путь записи внутри APK (`AndroidManifest.xml`,
            `res/drawable/ic_logo.xml`); можно указать без `res/` — угадается,
            если кандидат ровно один.

    Returns:
        Текст дерева.

    Raises:
        RuntimeError: aapt2 не установлен или не разобрал файл.
        ValueError: записи нет в APK — в сообщении похожие имена.
    """
    with _zip(apk) as zf:
        names = zf.namelist()
    if entry not in names:
        cands = [n for n in names
                 if fnmatch.fnmatch(n, f'res/*/{os.path.basename(entry)}')
                 or fnmatch.fnmatch(n, entry)]
        if len(cands) != 1:
            near = ', '.join(cands[:5]) if cands else 'похожих нет'
            raise ValueError(f'записи «{entry}» в {os.path.basename(apk)} нет; {near}')
        entry = cands[0]
    return _aapt2_run(['dump', 'xmltree', '--file', entry, apk])


def apk_pathdata(apk: str, entry: str) -> list:
    """
    Векторные path'ы drawable-иконки: `pathData` и заливки каждого `<path>`.

    Так иконка оригинала переезжает в SVG без ручного переписывания кривых:
    pathData — те же координаты viewBox, а fillColor даёт тон. Порядок —
    документный, первый элемент списка рисуется под остальными.

    Args:
        apk: путь к APK.
        entry: путь vector-XML (`res/drawable/ic_logo.xml`).

    Returns:
        `[{'tag', 'attrs': {имя без urn, ...}}]` только для элементов с
        pathData; имя атрибута укорочено (`android:pathData` → `pathData`).

    Raises:
        RuntimeError / ValueError: как в `apk_xml`.
    """
    blocks = _elements(apk_xml(apk, entry))
    paths = []
    for block in blocks:
        attrs = _attrs(block)
        if 'pathData' in attrs:
            paths.append({'tag': _tag(block), 'attrs': attrs})
    return paths


def apk_extract(apk: str, entry: str, out_dir: str = '/tmp/apk_extract',
                png: bool = False) -> str:
    """
    Вытащить запись APK на диск; webp по желанию становится png.

    webp нужен именно png-ом: дальше его меряют `image_.image_scan` и клеят
    `adb_rec_compare`, а читают webp не все инструменты подряд.

    Args:
        apk: путь к APK.
        entry: путь записи (`res/mipmap-xxxhdpi-v4/ic_launcher.webp`).
        out_dir: каталог на машине, создаётся.
        png: сконвертировать webp в png рядом (суффикс выхода меняется).

    Returns:
        Путь извлечённого файла.

    Raises:
        RuntimeError: записи нет в APK.
    """
    with _zip(apk) as zf:
        if entry not in zf.namelist():
            raise RuntimeError(f'записи «{entry}» в {os.path.basename(apk)} нет')
        os.makedirs(out_dir, exist_ok=True)
        out = os.path.join(out_dir, os.path.basename(entry))
        with open(out, 'wb') as f:
            f.write(zf.read(entry))
    if png and entry.endswith('.webp'):
        from PIL import Image      # тяжёлый стек — лениво, как в остальных модулях
        png_out = out[:-len('.webp')] + '.png'
        Image.open(out).convert('RGBA').save(png_out)
        return png_out
    return out


def _aapt2() -> str:
    """
    aapt2 из PATH или свежайшего build-tools в SDK; отсутствие — отдельная ошибка.

    В PATH его обычно нет: это часть Android SDK, а не система. SDK ищется в
    `ANDROID_HOME` (он не экспортируется по умолчанию), затем в `~/Android/Sdk`,
    как в проектах с Cordova; версий build-tools в SDK бывает несколько —
    берётся последняя по числу, не по алфавиту (36.0.0 для str.sort младше 9.x).
    """
    found = shutil.which('aapt2')
    if found:
        return found
    roots = [os.environ.get('ANDROID_HOME') or '',
             os.path.expanduser('~/Android/Sdk')]
    candidates = []
    for root in roots:
        if not root:
            continue
        for path in glob.glob(os.path.join(root, APK_BUILD_TOOLS_GLOB)):
            ver = path.split(os.sep)[-2]
            key = tuple(int(p) for p in ver.split('.') if p.isdigit())
            candidates.append((key, path))
    if not candidates:
        raise FileNotFoundError(
            'aapt2 не найден: он лежит в Android SDK build-tools '
            '(ANDROID_HOME или ~/Android/Sdk), без него APK не прочитать')
    return max(candidates)[1]


def _aapt2_run(args: list) -> str:
    """aapt2 с выводом; ненулевой выход — RuntimeError с хвостом stderr."""
    proc = subprocess.run([_aapt2()] + args, capture_output=True, text=True,
                          timeout=120)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout).strip()[-300:]
        raise RuntimeError(f'aapt2 {" ".join(args[:3])}: {tail}')
    return proc.stdout


def _aapt2_dump(apk: str) -> list:
    return _aapt2_run(['dump', 'resources', apk]).splitlines()


def _config_ok(line: str, config: str) -> bool:
    """Строка дампа принадлежит нужной конфигурации (`config=''` — любая)."""
    if not config:
        return True
    low = line.lower()
    i = low.find('config=')
    if i < 0:
        return True     # строка без конфигурации — имя ресурса, не значение
    return config.lower() in low[i:]


def _zip(apk: str) -> zipfile.ZipFile:
    """Открыть APK как zip; «файла нет» и «не zip» — разные ошибки."""
    if not os.path.isfile(apk):
        raise FileNotFoundError(f'APK не найден: {apk}')
    try:
        return zipfile.ZipFile(apk)
    except zipfile.BadZipFile:
        raise ValueError(f'{apk} не является APK (zip не открылся)') from None


def _elements(xml: str) -> list:
    """Разбить вывод xmltree на блоки элементов, начиная с их строки `E:`."""
    blocks, cur = [], None
    for line in xml.splitlines():
        if line.lstrip().startswith('E: '):
            if cur is not None:
                blocks.append(cur)
            cur = [line]
        elif cur is not None:
            cur.append(line)
    if cur is not None:
        blocks.append(cur)
    return blocks


def _tag(block: list) -> str:
    """Имя элемента из строки `E: path (line=12)`."""
    head = block[0].lstrip()[3:]
    return head.split(' ', 1)[0].split('(')[0]


def _attrs(block: list) -> dict:
    """
    Атрибуты элемента из блока xmltree: имя без urn, значение целиком.

    aapt2 печатает строковые значения дважды: `"…" (Raw: "…")` — копия после
    `(Raw:` отсечена, иначе она приклеена к pathData и та разрывается пополам
    уже на коротких значениях. У длинных строк aapt2 переносит значение на
    следующую строку: пока кавычка не закрыта, строки доклеиваются.
    """
    attrs, key = {}, None
    for line in block[1:]:
        s = line.strip()
        if s.startswith('A: '):
            name, _, value = s[3:].partition('=')
            name = name.split('(')[0].split(':')[-1]
            attrs[name] = _cut_raw(value)
            key = name if _open_quote(attrs[name]) else None
        elif key is not None and s.startswith('(Raw:'):
            key = None                       # цитата закрыта, копия не нужна
        elif key is not None and s:
            attrs[key] = attrs[key] + ' ' + _cut_raw(s)
            key = key if _open_quote(attrs[key]) else None
        else:
            key = None
    return {k: _attr_value(v) for k, v in attrs.items()}


def _cut_raw(value: str) -> str:
    """Отсечь копию aapt2 ` (Raw: …)`, идущую за значением."""
    cut = value.find(' (Raw: ')
    return (value if cut < 0 else value[:cut]).rstrip()


def _open_quote(value: str) -> bool:
    """Кавычка открыта и не закрыта — длинное значение продолжится строкой ниже."""
    return value.count('"') % 2 == 1


def _attr_value(value: str) -> str:
    """Значение атрибута: кавычки по краям долой."""
    value = value.strip()
    if value.startswith('"') and value.endswith('"') and len(value) > 1:
        value = value[1:-1]
    return value


if __name__ == '__main__':
    ap = argparse.ArgumentParser(
        description='APK без телефона: список записей, таблица ресурсов, '
                    'бинарный XML, pathData векторов, извлечение файлов.')
    ap.add_argument('command', choices=['entries', 'dump', 'xml', 'pathdata', 'extract'])
    ap.add_argument('apk', help='путь к APK на машине')
    ap.add_argument('arg', nargs='?', default='',
                    help='pattern для entries, подстрока для dump, '
                         'путь записи для xml/pathdata/extract')
    ap.add_argument('--config', default='default',
                    help="конфигурация строк в dump: 'default', '' (все), 'xxhdpi', 'ua'")
    ap.add_argument('--out-dir', default='/tmp/apk_extract', help='куда извлекать')
    ap.add_argument('--png', action='store_true', help='webp при извлечении сделать png')
    ns = ap.parse_args()
    try:
        if ns.command == 'entries':
            print('\n'.join(apk_entries(ns.apk, ns.arg)))
        elif ns.command == 'dump':
            print('\n'.join(apk_dump(ns.apk, ns.arg, config=ns.config)))
        elif ns.command == 'xml':
            print(apk_xml(ns.apk, ns.arg))
        elif ns.command == 'pathdata':
            print(json.dumps(apk_pathdata(ns.apk, ns.arg), ensure_ascii=False, indent=1))
        else:
            print(apk_extract(ns.apk, ns.arg, out_dir=ns.out_dir, png=ns.png))
    except (RuntimeError, FileNotFoundError, ValueError) as err:
        raise SystemExit(f'ошибка: {err}')
