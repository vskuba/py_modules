"""Адрес почты из имени человека — такой, каким его заводят люди.

Задача выглядит как «склеить имя с доменом», и в этом виде решается одной
строкой. Беда в том, что одна строка даёт **однообразие**, а однообразие видно
сразу: пятьсот адресов вида `ivan.petrov7@gmail.com` отличаются только цифрой и
читаются как список, сделанный скриптом.

Люди заводят почту по-разному: кто-то одним именем, кто-то через точку, кто-то
инициалом, кто-то добавляет год, кто-то нет. Этот модуль перебирает ровно те
формы, которые встречаются живьём, и смешивает их случайно.

## ⚠⚠ Хвостовое подчёркивание в имени пакета обязательно

`email` — **модуль стандартной библиотеки**. Пакет без подчёркивания перекрыл бы
его для всего процесса: `import email.mime` после такого соседства приводит не
туда, и ломается это не здесь, а у того, кто отправляет письма. То же правило,
что у `queue_` и `mysql_` (`code_rules.md`, §1).

## ⚠ Почему `rand` — довод, а не глобальный `random`

Разнообразие проверяют счётом: «на ста именах вышло столько-то разных форм».
Такой проверке нужен повторяемый источник случайности, иначе тест то проходит, то нет.
Передал `random.Random(42)` — получил тот же набор; не передал — обычный
случай.

## Чего здесь нет

Проверки, что адрес **существует**. Модуль про то, как адрес выглядит, а не про
то, доходит ли до него письмо: доставка — дело `mail_box.py` и почтового
сервера.
"""
import argparse
import random
import re

from i18n_.i18n_ import i18n_translit_cyr

# Формы локальной части — то, что стоит слева от собачки. Доли подобраны по
# тому, как выглядят живые списки: имя с фамилией через точку встречается чаще
# всего, «фамилия вперёд» — заметно реже.
#
# ⚠ Форма с одним именем (`vasyl`) нужна, но её мало: короткие адреса давно
# разобраны, и человек, заводящий почту сегодня, почти всегда добавляет к имени
# что-то ещё. Поставь её наравне с прочими — список начнёт выглядеть старым.
EMAIL_SHAPES = (
    ('first.last', 22), ('firstlast', 16), ('first_last', 10),
    ('flast', 10), ('f.last', 9), ('first', 8), ('last.first', 6),
    ('lastfirst', 5), ('first-last', 4), ('first.l', 4), ('lastf', 3),
)

# Как к имени добавляют цифры. Пусто — не добавляют вовсе; это самый частый
# случай у живых адресов с фамилией, и именно он чаще всего забывается в
# генераторах.
EMAIL_TAILS = (
    ('', 34), ('digits', 26), ('year', 22), ('year2', 10), ('number', 8),
)

# Разделитель перед цифрами: чаще всего его нет.
EMAIL_TAIL_SEPARATORS = (('', 74), ('.', 13), ('_', 13))

# Годы рождения, которые встречаются у взрослого покупателя. Не «любой год»:
# `ivan1932` выглядит ошибкой, а не почтой.
EMAIL_YEAR_FROM = 1972
EMAIL_YEAR_TO = 2006

# Потолок локальной части. Формально RFC разрешает 64, но адреса длиннее
# тридцати знаков у людей не встречаются — только у генераторов.
EMAIL_LOCAL_MAX = 30


def email_make(first: str = '', last: str = '', domains=(), taken=(),
               rand: random.Random = None) -> str:
    """Собрать адрес почты из имени и фамилии.

    Args:
        first: имя — кириллицей или латиницей, регистр любой.
        last: фамилия; пусто — адрес соберётся из одного имени.
        domains: домены. Отображение `{домен: вес}` — вес задаёт долю в выдаче;
            простой список — все поровну. Пусто — вернётся пустая строка:
            выдумывать домен за вызывающего модуль не станет.
        taken: адреса, которые уже заняты. Совпавший собирается заново с
            другими цифрами — до десяти попыток, потом отдаётся как есть.
        rand: источник случайности; свой нужен тестам (см. шапку).

    Returns:
        Адрес строкой, либо **пустая строка**, если из имени не вышло ни одной
        латинской буквы (эмодзи, одни знаки) или не дали доменов.

    ⚠ Пустая строка — не отказ, а ответ: имена в собранных базах бывают
    любыми, и ронять на них весь прогон нельзя. Вызывающий считает пропуски и
    показывает их числом.
    """
    rand = rand or random
    weighted = _domains_weighted(domains)
    if not weighted:
        return ''

    name, surname = email_latin(first), email_latin(last)
    if not name and not surname:
        return ''

    busy = set(taken or ())
    for _ in range(10):
        local = email_local(name, surname, rand)
        if not local:
            return ''
        domain = _pick(weighted, rand)
        address = f'{local}@{domain}'
        if address not in busy:
            return address

    return address


def email_local(first: str, last: str = '', rand: random.Random = None) -> str:
    """Левая часть адреса: форма имени плюс, иногда, цифры.

    Имя и фамилия ожидаются **уже латиницей** (`email_latin`): отдельная
    функция затем, чтобы вызывающий мог взять только форму, не трогая домен.

    ⚠ Фамилии нет — формы с ней выпадают сами: `flast` без фамилии дал бы одну
    букву, а `last.first` — имя с лишней точкой впереди.
    """
    rand = rand or random
    shapes = EMAIL_SHAPES if last else (('first', 1),)
    local = _shape_apply(_pick(shapes, rand), first, last)
    if not local:
        return ''

    tail = _tail_make(_pick(EMAIL_TAILS, rand), rand)
    if tail:
        local += _pick(EMAIL_TAIL_SEPARATORS, rand) + tail

    # Два разделителя подряд и разделитель по краям адреса не встречаются у
    # живых ящиков, а собраться могут: `v..lapko` из пустой фамилии и точки.
    local = re.sub(r'[._-]{2,}', lambda m: m.group(0)[0], local).strip('._-')

    return local[:EMAIL_LOCAL_MAX].strip('._-')


def email_latin(text: str) -> str:
    """Имя в вид, пригодный для адреса: латиница в нижнем регистре, без прочего.

    Кириллица уходит в транслит общего слоя (`i18n_translit_cyr` — буква в
    букву, ровно то, что нужно для идентификатора). Всё, что не латинская
    буква, выбрасывается: в собранных базах имена бывают с эмодзи, точками и
    скобками, и тащить это в адрес нельзя.

    ⚠ Цифры тоже выбрасываются, хотя в адресе они законны: `anna95` в поле
    «имя» — это не часть имени, а хвост чужого ника, и повторять его в адресе
    значит выдавать источник.
    """
    latin = i18n_translit_cyr(str(text or '')).lower()

    return re.sub(r'[^a-z]', '', latin)


# ── детали реализации ──────────────────────────────────────────────────────

def _domains_weighted(domains) -> tuple:
    """Домены к общему виду `((домен, вес), …)`; пусто — пустой кортеж."""
    if not domains:
        return ()
    if isinstance(domains, dict):
        return tuple((str(name), max(1, int(weight)))
                     for name, weight in domains.items() if name)

    return tuple((str(name), 1) for name in domains if name)


def _pick(weighted, rand: random.Random):
    """Случайный элемент из `((значение, вес), …)` с учётом весов."""
    values = [value for value, _ in weighted]
    weights = [weight for _, weight in weighted]

    return rand.choices(values, weights=weights, k=1)[0]


def _shape_apply(shape: str, first: str, last: str) -> str:
    """Форма локальной части по её имени — без цифр и без домена."""
    parts = {
        'first': first,
        'first.last': f'{first}.{last}',
        'firstlast': f'{first}{last}',
        'first_last': f'{first}_{last}',
        'first-last': f'{first}-{last}',
        'flast': f'{first[:1]}{last}',
        'f.last': f'{first[:1]}.{last}',
        'first.l': f'{first}.{last[:1]}',
        'lastf': f'{last}{first[:1]}',
        'last.first': f'{last}.{first}',
        'lastfirst': f'{last}{first}',
    }

    return parts.get(shape, first)


def _tail_make(kind: str, rand: random.Random) -> str:
    """Цифровой хвост выбранного вида; `''` — хвоста нет."""
    if kind == 'digits':
        return str(rand.randint(1, 999))
    if kind == 'year':
        return str(rand.randint(EMAIL_YEAR_FROM, EMAIL_YEAR_TO))
    if kind == 'year2':
        return f'{rand.randint(EMAIL_YEAR_FROM, EMAIL_YEAR_TO) % 100:02d}'
    if kind == 'number':
        return str(rand.randint(1, 9))

    return ''


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        prog='python3 -m email_.email_',
        description='Адрес почты из имени: формы и домены вперемешку.')
    parser.add_argument('first', help='имя (кириллица или латиница)')
    parser.add_argument('last', nargs='?', default='', help='фамилия')
    parser.add_argument('--domains', default='gmail.com,ukr.net,i.ua',
                        help='домены через запятую; «домен:вес» задаёт долю')
    parser.add_argument('-n', '--count', type=int, default=1,
                        help='сколько адресов показать')
    parser.add_argument('--seed', type=int, default=0,
                        help='источник случайности для повторяемого ответа')
    args = parser.parse_args()

    shelf = {}
    for item in args.domains.split(','):
        name, _, weight = item.strip().partition(':')
        if name:
            shelf[name] = int(weight) if weight.isdigit() else 1

    dice = random.Random(args.seed) if args.seed else random
    made = []
    for _ in range(max(1, args.count)):
        made.append(email_make(args.first, args.last, shelf, made, dice))
    print('\n'.join(made))
