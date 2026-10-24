"""
Орфография русской прозы репозитория — то, чего `tool_typo` не ловит и не может.

`tool_/tool_typo.py` ищет **механические следы правки**: буквы из двух алфавитов в одном
слове, слипшиеся при вставке слова, повтор слова подряд. Орфографии у него нет
сознательно — попытка обойтись частотностью корпуса провалилась наглядно: у флективного
языка законные формы похожи друг на друга сильнее («сериалу»/«сериал» — 0.92), чем
опечатка на верное слово («пояснем»/«пояснение» — 0.75), и порога между этими случаями
не существует.

Словарь снимает именно эту беду. Но одного его мало, и это замерено: на живом
репозитории он объявил незнакомыми **1758 слов** — весь профессиональный словарь проекта
(«докстринг», «воркфлоу», «инвайт», «кронджоб») вперемешку с настоящими опечатками.
Список, который выключат на второй день.

## ⚠⚠ Опечатку от термина отличает не словарь, а его подсказка

Словарь на незнакомое слово предлагает замены, и у двух случаев они выглядят
по-разному — живой замер этого репозитория:

    кавышки    → кавычки ×74      одна буква, и правильное слово всюду в корпусе
    заголошки  → заголовки ×73    то же
    докстринг  → декстрин          три буквы, и такого слова в корпусе нет вовсе
    кронджоб   → корундом           пять букв, мимо

Отсюда подпись опечатки, из трёх условий сразу: словарь слова **не знает**, его
подсказка отстоит на **одну букву**, и эта подсказка **живёт в нашем корпусе**. Термину
все три не пройти: ему неоткуда быть похожим на частое русское слово.

## ⚠⚠ И ещё два отсева, оба замерены, а не придуманы

**Короткое слово.** На слове из трёх букв одна правка — это треть слова, и «дет» → «нет»
(×4195), «кам» → «как», «нод» → «под» лезут пачками. Порог в шесть букв убирает их все,
не теряя ни одной настоящей находки: 321 → 193.

**Различие в хвосте — это словоформа, а не опечатка.** «габарити» → «габарит»,
«сервисам» → «сервиса», «рестарты» → «рестарт»: словарь не знает падежной формы термина,
и подсказывает его же в другом падеже. Опечатка выглядит иначе — буква ломается
**внутри** слова: «кавышки», «заголошки», «оригнал», «обьект». Правило срезало ещё
половину: 193 → 98.

## ⚠ Чего здесь нет: согласования и управления

«по непустоте вопроса» вместо «по непустому вопросу» — не опечатка: слова написаны
верно, неверен падеж. Словарь такое пропускает по устройству, и ловит это только
вычитка — человеком или моделью (`tool_typo --prose`).

## ⚠ Почему namespace `dictionary_`, а не `tool_`

Здесь не «инструмент про репозиторий», а **работа со словарём языка**: движок, его
наличие, подсказки. Имя файла несёт язык (`_ru`) потому, что от языка зависит всё —
пакет словаря, проба на живость и само понятие «похоже»; английскому понадобится
соседний файл, а не флаг.
"""
import re
import subprocess
import sys

from collections import Counter
from pathlib import Path

# Файл запускают и путём. Тогда первым в путях лежит каталог файла, и соседний
# namespace не находится вовсе — та же починка, что в `tool_/tool_typo.py`.
if __package__ in (None, ''):
    _here = str(Path(__file__).resolve().parent)
    sys.path[:] = [item for item in sys.path if item not in ('', '.', _here)]
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tool_.tool_typo import tool_typo_prose

# Слово — **только кириллица**. Латиницу русскому словарю не показывают вовсе:
# `workflow`, `INSERT`, `uid` он не знает все до одного, и выдача утонула бы в них.
# Смешанные слова — дело `tool_typo --kind mixed`.
DICTIONARY_SPELL_CHECK_RU_WORD = re.compile(r'[а-яёА-ЯЁ]+')

# Короче этого слово не судим: одна правка на трёх буквах — треть слова, и такие
# подсказки находятся у чего угодно. Замер в шапке модуля: 321 находка против 193.
DICTIONARY_SPELL_CHECK_RU_SHORT = 6

# Сколько раз слово может встретиться в корпусе и всё ещё считаться подозрительным.
#
# ⚠ Двойка, а не единица: опечатку копируют вместе со строкой, когда правят соседний
# файл по образцу. Выше поднимать смысла нет — тремя повторами слово заявляет себя
# термином.
DICTIONARY_SPELL_CHECK_RU_RARE = 2

# На сколько букв подсказка словаря может отстоять от слова, чтобы считаться «тем
# самым словом, которое хотели написать».
#
# ⚠⚠ Единица — **замеренная граница, а не осторожность**. На расстоянии 1 выдача
# состоит из опечаток, на 2 в неё входят словоформы терминов, на 3 — уже только шум
# («докстринг» → «декстрин»). Поднимают разовым `--distance 2`, когда ищут прицельно.
DICTIONARY_SPELL_CHECK_RU_NEAR = 1

# Сколько общих букв с начала делают пару «слово и его же словоформой».
#
# ⚠ Пять, а не четыре: «времям» → «время» имеет общими четыре и опечаткой быть не
# перестаёт, а «ширинам» → «ширина» с шестью общими — честная падежная форма.
DICTIONARY_SPELL_CHECK_RU_STEM = 5

# Свои слова проекта, которых нет в словаре: по слову в строке, `#` — комментарий.
# Лежит в корне проверяемого репозитория, а не здесь: словарь терминов у каждого свой.
DICTIONARY_SPELL_CHECK_RU_ALLOW = '.spell-allow'

# Чем спрашивать словарь. Порядок — порядок предпочтения, берётся первый живой.
#
# ⚠⚠ Движка два не ради щедрости: `aspell` стоит в системе почти всегда, но русского
# словаря к нему может не быть, а `hunspell-ru` ставится как **данные без бинарника** —
# пакет кладёт `.dic`/`.aff` и не кладёт `hunspell`. Связка «есть пакет → есть проверка»
# не работает ни в одну сторону, поэтому спрашиваем обе.
#
# ⚠ Режим `-a` (pipe), а не `-l`/`list`: нам нужен не факт незнания, а **подсказки** —
# на них держится весь отбор (см. шапку).
DICTIONARY_SPELL_CHECK_RU_ENGINES = (
    ('aspell', ('aspell', '--encoding=utf-8', '-d', 'ru', '-a')),
    ('hunspell', ('hunspell', '-d', 'ru_RU', '-a')))

# Сколько мест показывать у одного слова. Опечатка живёт в одном месте; больше трёх
# адресов — это уже не находка, а термин, и смотреть надо на него, а не на список.
DICTIONARY_SPELL_CHECK_RU_PLACES = 3

# Проба словаря: пара «настоящее слово — испорченное». Живым словарь считается, только
# если вернул второе и не вернул первое.
#
# ⚠⚠ **Пары, а не одного слова, и это поймано дымовой проверкой.** Чужой словарь
# кириллицу не объявляет незнакомой — он её **пропускает**: `aspell -d en` на «проверка»
# отвечает пустотой ровно как русский. Одним словом «словарь есть, но не тот» не
# отличить от «словарь на месте»; испорченное слово в паре отличает их обоих.
DICTIONARY_SPELL_CHECK_RU_PROBE_GOOD = 'проверка'
DICTIONARY_SPELL_CHECK_RU_PROBE_BAD = 'провреказьх'


def dictionary_spell_check_ru(root=None, rare: int = DICTIONARY_SPELL_CHECK_RU_RARE,
                              near: int = DICTIONARY_SPELL_CHECK_RU_NEAR) -> list[dict]:
    """Найти опечатки в русской прозе репозитория по системному словарю.

    Смотрит ту же прозу, что `tool_typo`: докстринги и комментарии `*.py`,
    комментарии js-семейства и `*.md`; код в обратных кавычках, заборы и адреса
    из неё уже вырезаны.

    Args:
        root: корень репозитория; пусто — **текущий каталог**. ⚠ Не как у
            `tool_typo`, который по умолчанию берёт корень общего слоя: орфографию
            спрашивают про тот репозиторий, в котором стоят.
        rare: сколько раз слово может встретиться и остаться подозрительным.
        near: на сколько букв подсказка словаря может отстоять от слова.

    Returns:
        list[dict]: находки полями `word`, `count`, `like` (чем словарь предлагает
        заменить), `like_count` (сколько раз замена встречается в корпусе),
        `places`. Порядок — по частоте замены убывающе: бесспорное сверху. Пустой
        список — либо чисто, либо словаря нет; что именно, отвечает
        `dictionary_spell_check_ru_ready`.

    ⚠⚠ **Находка — это четыре условия сразу**, а не «словарь не знает»: слово от
    шести букв, встречается редко, подсказка в одну букву, и подсказка живёт в
    нашем корпусе **не хвостом слова, а серединой**. Поодиночке ни одно не работает
    — замеры и причина каждого в шапке модуля.

    ⚠ **Словаря нет — пустой список, а не исключение.** Проверка стоит в заставе
    перед коммитом, и падать из-за ненастроенной машины ей нельзя: застава при
    своей поломке пропускает, а не держит.
    """
    base = Path(root) if root else Path.cwd()
    if not dictionary_spell_check_ru_ready():
        return []

    counts = Counter()
    places = {}
    for where, line, text in _lines(base):
        for word in DICTIONARY_SPELL_CHECK_RU_WORD.findall(text):
            low = word.lower()
            counts[low] += 1
            places.setdefault(low, []).append(f'{where}:{line}')

    allow = _allow(base)
    asking = {low for low, n in counts.items()
              if len(low) >= DICTIONARY_SPELL_CHECK_RU_SHORT
              and n <= max(1, int(rare)) and low not in allow}
    if not asking:
        return []

    out = []
    for word, hints in _unknown(asking).items():
        like = _like(word, hints, counts, max(1, int(near)))
        if not like:
            continue

        out.append({'word': word, 'count': counts[word], 'like': like,
                    'like_count': counts[like],
                    'places': places[word][:DICTIONARY_SPELL_CHECK_RU_PLACES]})

    return sorted(out, key=lambda row: (-row['like_count'], row['word']))


def dictionary_spell_check_ru_ready() -> str:
    """Каким словарём проверяем. Пустая строка — русского словаря в системе нет.

    Returns:
        str: `aspell` или `hunspell`; пусто — ни одного рабочего.

    ⚠ Проверяется **вызовом**, а не наличием файла: `hunspell-ru` ставит данные
    без бинарника, и «пакет стоит» ещё не значит «спросить есть чем».

    ⚠⚠ Проба — **пара слов**, настоящее и испорченное: чужой словарь кириллицу не
    объявляет незнакомой, а пропускает, и на одном слове «не тот язык» неотличимо
    от «всё в порядке» (см. `DICTIONARY_SPELL_CHECK_RU_PROBE_GOOD`).
    """
    for name, command in DICTIONARY_SPELL_CHECK_RU_ENGINES:
        said = _ask(command, (DICTIONARY_SPELL_CHECK_RU_PROBE_GOOD,
                              DICTIONARY_SPELL_CHECK_RU_PROBE_BAD))
        if said is not None and DICTIONARY_SPELL_CHECK_RU_PROBE_BAD in said \
                and DICTIONARY_SPELL_CHECK_RU_PROBE_GOOD not in said:
            return name

    return ''


def main() -> int:
    """CLI: `python -m dictionary_.dictionary_spell_check_ru [--root .] [--rare N]`.

    Код возврата — число находок (0 — чисто, потолок 125), как у `tool_typo`:
    проверку вешают хуком, а хук читает код, а не глазами вывод.
    """
    import argparse

    ap = argparse.ArgumentParser(
        description='Опечатки в русской прозе по системному словарю: слово, которого '
                    'словарь не знает, а его замена — частое слово нашего корпуса в '
                    'одну букву отсюда.')
    ap.add_argument('--root', default='', help='корень; пусто — текущий каталог')
    ap.add_argument('--rare', type=int, default=DICTIONARY_SPELL_CHECK_RU_RARE,
                    help='сколько раз слово может встретиться (умолчание '
                         f'{DICTIONARY_SPELL_CHECK_RU_RARE})')
    ap.add_argument('--distance', type=int, default=DICTIONARY_SPELL_CHECK_RU_NEAR,
                    help=f'букв до замены (умолчание {DICTIONARY_SPELL_CHECK_RU_NEAR}; '
                         '2 добавит словоформы терминов)')
    ns = ap.parse_args()

    engine = dictionary_spell_check_ru_ready()
    if not engine:
        print('русского словаря в системе нет — проверка пропущена.\n'
              'поставить: sudo apt install -y aspell-ru')
        return 0

    rows = dictionary_spell_check_ru(ns.root or None, rare=ns.rare, near=ns.distance)
    if not rows:
        print(f'орфографических находок нет (словарь: {engine}; '
              'согласование и управление это не проверяет)')
        return 0

    print(f'📖 ПОХОЖЕ НА ОПЕЧАТКУ — {len(rows)} (словарь: {engine})')
    for row in rows:
        print(f"  «{row['word']}» ×{row['count']}  →  похоже на «{row['like']}» "
              f"×{row['like_count']}")
        for place in row['places']:
            print(f'      {place}')

    return min(len(rows), 125)


def _like(word: str, hints: list, counts, near: int) -> str:
    """Чем словарь предлагает заменить слово, если это похоже на опечатку.

    Пустая строка — не похоже: замена далеко, её нет в корпусе либо расходится со
    словом одним хвостом (то есть это падежная форма того же термина).

    ⚠ Подсказки с пробелом и дефисом выброшены: `непустоте` → `не пустоте` это не
    замена слова, а разбиение, и расстояние у него всегда мало — такие подсказки
    есть почти у каждого слитного термина.
    """
    near_ones = [(_distance(word, one), one) for one in hints
                 if ' ' not in one and '-' not in one and counts.get(one)]
    fits = [pair for pair in near_ones
            if pair[0] <= near and not _same_stem(word, pair[1])]
    if not fits:
        return ''

    return min(fits, key=lambda pair: (pair[0], -counts[pair[1]]))[1]


def _same_stem(word: str, like: str) -> bool:
    """Слова расходятся только хвостом — значит это одна основа в двух формах.

    ⚠⚠ Главный отсев после словаря, и он не косметический: словарь не знает
    падежных форм терминов и подсказывает тот же термин в другом падеже —
    «габарити» → «габарит», «сервисам» → «сервиса», «рестарты» → «рестарт».
    Опечатка же ломает букву **внутри** слова: «кавышки», «заголошки», «оригнал».
    Замер: 193 находки против 98.
    """
    common = 0
    for one, two in zip(word, like):
        if one != two:
            break
        common += 1

    return (common >= DICTIONARY_SPELL_CHECK_RU_STEM
            and common >= min(len(word), len(like)) - 2)


def _lines(base: Path) -> list[tuple]:
    """Проза репозитория тройками (файл, строка, текст).

    ⚠ Берётся у `tool_typo_prose` и разбирается обратно, а не собирается своим
    обходом: вырезание кода в обратных кавычках, заборов и адресов — его работа, и
    вторая копия этих правил разошлась бы с первой молча.
    """
    out = []
    for row in tool_typo_prose(str(base)).split('\n'):
        where, _, text = row.partition('\t')
        path, _, line = where.rpartition(':')
        if path and line.isdigit():
            out.append((path, int(line), text))

    return out


def _unknown(words) -> dict:
    """Незнакомые словарю слова и его замены к ним: `{слово: [замены]}`.

    Спрашивается **одним вызовом** на все слова: тринадцать тысяч запусков `aspell`
    стоили бы минут, один поток — секунды.

    ⚠ Ответ `-a` читается построчно, и нас занимают только строки `&` — «не знаю,
    вот замены». Строка `#` («не знаю, замен нет») находкой стать не может по
    устройству отбора: без замены сравнивать не с чем.
    """
    command = next((c for n, c in DICTIONARY_SPELL_CHECK_RU_ENGINES
                    if n == dictionary_spell_check_ru_ready()), None)
    if not command:
        return {}

    said = _ask(command, sorted(words), timeout=600)
    if said is None:
        return {}

    out = {}
    for line in said.split('\n'):
        if not line.startswith('&'):
            continue

        head, _, tail = line.partition(':')
        parts = head.split()
        if len(parts) > 1:
            out[parts[1].lower()] = [one.strip().lower() for one in tail.split(',')
                                     if one.strip()]

    return out


def _ask(command, words, timeout: int = 60):
    """Спросить движок словаря про слова. `None` — движка нет или он не ответил.

    ⚠ Первая строка ввода — `!`: у обоих движков это «режим терпимости», без него
    длинный список слов они разбирают как команды при первом же символе `*` или `+`
    в начале строки.
    """
    try:
        done = subprocess.run(command, input='!\n' + '\n'.join(words) + '\n',
                              text=True, encoding='utf-8', capture_output=True,
                              timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None

    return done.stdout if done.returncode == 0 else None


def _distance(first: str, second: str) -> int:
    """Сколько правок (вставка, удаление, замена) между словами — Левенштейн.

    ⚠ Разница длин больше трёх отвечает сразу: нас занимает только близкое, и
    считать полную матрицу ради заведомо далёкой пары незачем — слов в прогоне
    тринадцать тысяч, у каждого до сорока замен.
    """
    if abs(len(first) - len(second)) > 3:
        return 9

    prev = list(range(len(second) + 1))
    for i, one in enumerate(first, 1):
        cur = [i]
        for j, two in enumerate(second, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (one != two)))
        prev = cur

    return prev[-1]


def _allow(base: Path) -> set:
    """Свои слова проекта из `.spell-allow`. Нет файла — пустое множество."""
    path = base / DICTIONARY_SPELL_CHECK_RU_ALLOW
    try:
        rows = path.read_text(encoding='utf-8').split('\n')
    except OSError:
        return set()

    return {one.strip().lower() for one in rows
            if one.strip() and not one.lstrip().startswith('#')}


if __name__ == '__main__':
    raise SystemExit(main())
