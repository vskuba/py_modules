"""
Маска чувствительных данных (PAN/CVV/токены) в дампах логов и HTTP-дампов.

«В тикетах и дампе логов видны PAN и CVV в открытом виде (`card_cvv2=145`,
`"cardExpireDate":"3024"`)» и «Bearer-токены уходят в LLM-провайдер» — перед
публикацией дампа (в тикет, в чат, в LLM) текст прогоняется через `data_mask_text`:
номера карт и CVV режутся по маске, сессионные токены (xoxc-/xoxd-/Bearer) —
тоже; наружу из модуля секреты не кладутся: в отпечаток уходит не более
`value_prefix_len` символов значения (по умочалению 10).

Правило маски: «маска первыми символами» — значение заменяется префиксом 4
символов + `*` × остаток + хвостом 4 символов; короткие значения (≤8 символов)
маскируются целиком. Имена ключей (`x-api-key:`, `Authorization:`) остаются
чистыми — режется только значение.
"""
import json
import re
from pathlib import Path

# ── константы ──

DATA_MASK_MAX_VALUE = 10  # «значение обрезать до <=10 символов»
# Расширения, которые `data_mask_sweep` читает как текст (PDF — отдельно, через
# текстовый слой); PNG и прочие растры не сканируются: слоя с символами нет.
DATA_MASK_SWEEP_SUFFIXES = ('.html', '.htm', '.json', '.md', '.txt', '.log',
                            '.csv', '.pdf', '.py', '.sh', '.yaml', '.yml')
DATA_MASK_KEEP_PREFIX = 4
DATA_MASK_KEEP_SUFFIX = 4
DATA_MASK_PAN_RE = re.compile(
    r'(?<![-\w])(?:5[1-5]|4[0-9]|6011|6759|3755|9792|1556|50\d{2}|56\d{2}|65\d{2}|62\d{2})'
    r'\d{12,15}(?![-\d])')  # PAN по BIN-префиксам Visa/MC/Discover/JCB/Amex/UMir
DATA_MASK_CVV_RE = re.compile(
    r'(?P<key>\b(?:card[_-]?cvv2?|cvv2?)\b[\'"]?\s*[=:]\s*[\'"]?)(?P<cvv>\d{3,4})',
    re.IGNORECASE)
DATA_MASK_TOKEN_RE = re.compile(
    r'(?P<key>\b(?:authorization|x-api-key-test|x-api-key|x-signature|x-idempotency-key'
    r'|x-app-signature|api[_ -]?key)\b\s*[=:]\s*[\'"]?)?'
    r'(?P<tok>\b(?:xox[a-z]-[A-Za-z0-9\-]{16,}|dc-[A-Za-z0-9.\-_]{16,}|hc-[A-Za-z0-9.\-_]{16,}'
    r'|ct-[A-Za-z0-9.\-_]{16,}|ssda-[A-Za-z0-9.\-_]{16,}|ss-[A-Za-z0-9.\-_]{16,}'
    r'|Bearer\s+[A-Za-z0-9._\-]{16,}|eyJ[A-Za-z0-9_\-]{9,}\.[A-Za-z0-9_\-]{9,}'
    r'\.[A-Za-z0-9._\-]{9,})|[Aa]uthorization[:=]\s*[\'"]?[Bb]earer\s+[A-Za-z0-9._\-]{16,})',
    re.IGNORECASE)
# ⚠ Сравнение с ключом дампа — без учёта регистра (`_DATA_MASK_SECRET_KEYS_LOWER`):
# `cardExpireDate` лежал здесь в camelCase и против `key.lower()` не совпадал никогда.
# OAuth-пары (`accessToken`/`refreshToken`) — ответ токен-эндпоинтов, их сеть
# браузера пишет телом ответа (поймано на GraphQL-запросе токена платёжной страницы).
DATA_MASK_SECRET_KEYS = ('card_cvv2', 'cvv2', 'cvv', 'cvc', 'cardExpireDate', 'card_expire_date',
                         'authorization', 'x-api-key', 'x-signature', 'x-api-key-test',
                         'apikey', 'api_key', 'x-app-signature', 'x-idempotency-key',
                         'accessToken', 'access_token', 'refreshToken', 'refresh_token',
                         'id_token', 'client_secret', 'password', 'secret',
                         # Ключ подписи callback-а: по нему подделывают уведомление о
                         # статусе, а в дампах бренда он лежит открыто в таблице `account`.
                         'signature_key',
                         # Голые имена из настроек stage-контуров: `token`, `api_token`,
                         # `encryption_key` и `signature` — то же, что составные ключи выше,
                         # только в конфиге бренда лежат под коротким именем.
                         'token', 'api_token', 'encryption_key', 'signature')
_DATA_MASK_SECRET_KEYS_LOWER = frozenset(key.lower() for key in DATA_MASK_SECRET_KEYS)

# ── публичный API модуля ──


def data_mask_find(text: 'str | bytes', value_prefix_len: int = DATA_MASK_MAX_VALUE) -> tuple:
    """Найти в дампе PAN/CVV/токены; значения вернуть обрезаными (≤10 символов).

    Args:
        text: текст дампа (str) либо сырые байты (bytes — для `curl -v` дампов).
        value_prefix_len: сколько символов значения оставлять в отпечатке.

    Returns:
        ([{'kind', 'position', 'value_prefix_len', 'value_prefix'}], None) —
        полные значения наружу не кладут; либо (None, 'текст ошибки').
    """
    if not isinstance(text, (str, bytes)):
        return None, f'текст должен быть str|bytes, а {type(text).__name__}'
    if not 0 < value_prefix_len <= DATA_MASK_MAX_VALUE:
        return None, f'value_prefix_len={value_prefix_len} вне 1..{DATA_MASK_MAX_VALUE}'
    src = text if isinstance(text, str) else text.decode('latin-1')
    found = []
    for kind, pattern, group in (('PAN', DATA_MASK_PAN_RE, None),
                                 ('CVV', DATA_MASK_CVV_RE, 'cvv'),
                                 ('token', DATA_MASK_TOKEN_RE, 'tok')):
        for match in pattern.finditer(src):
            secret = match.group(group) if group else match.group(0)
            if not secret:
                continue
            found.append({'kind': kind, 'position': match.start(),
                          'value_prefix_len': min(len(secret), value_prefix_len),
                          'value_prefix': secret[:value_prefix_len]})
    return found, None


def data_mask_text(text: 'str | bytes') -> tuple:
    """Заменить в дампе PAN/CVV/токены «первыми символами» (маска, не удаление).

    Значение → префикс 4 символа + `*` × остаток + хвост 4 символа; короче
    8 символов — маска целиком. Имена ключей (`x-api-key:`, `Authorization:`)
    остаются чистыми, режется только значение.

    Args:
        text: текст дампа (str) либо сырые байты (bytes).

    Returns:
        (masked, None) того же типа, что text; либо (None, 'текст ошибки').
    """
    if not isinstance(text, (str, bytes)):
        return None, f'текст должен быть str|bytes, а {type(text).__name__}'
    is_text = isinstance(text, str)
    src = text if is_text else text.decode('latin-1')
    masked = _data_mask_sub(DATA_MASK_PAN_RE, src)
    masked = _data_mask_sub(DATA_MASK_CVV_RE, masked, 'cvv')
    masked = _data_mask_sub(DATA_MASK_TOKEN_RE, masked, 'tok')
    return (masked if is_text else masked.encode('latin-1', 'ignore')), None


def data_mask_json(obj: 'dict | list | tuple') -> tuple:
    """Обойти дамп dict/list (JSON-разбор) и замаскать секретные поля и токены-строки.

    Для строк применяет data_mask_text; для ключей из DATA_MASK_SECRET_KEYS
    значение меняет на `***` (ключ «cvv2»/«x-api-key» — уже признак секрета).

    Args:
        obj: размеченный JSON дампа (dict/list/tuple/scalars).

    Returns:
        (masked, None) — копия с маской, исходный obj не трогается;
        либо (None, 'текст ошибки').
    """
    if not isinstance(obj, (dict, list, tuple)):
        return None, f'obj должен быть dict|list|tuple, а {type(obj).__name__}'

    def _walk(node):
        if isinstance(node, dict):
            out = {}
            for key, val in node.items():
                if str(key).lower() in _DATA_MASK_SECRET_KEYS_LOWER and isinstance(val, str) and val:
                    out[key] = '*' * min(len(val), 6)
                else:
                    out[key] = _walk(val)
            return out
        if isinstance(node, (list, tuple)):
            return type(node)(_walk(val) for val in node)
        if isinstance(node, str):
            masked, err = data_mask_text(node)
            return node if err else masked
        return node

    return _walk(obj), None


def data_mask_values(source) -> tuple:
    """Список запрещённых значений: из файла (JSON-массив или построчно) либо из готового списка.

    Args:
        source: путь к файлу значений или сам итерабл значений.

    Returns:
        ([str, …], None); либо (None, 'текст ошибки') — пустой список значений
        это ошибка, а не PASS: проверять нечем.
    """
    if isinstance(source, (str, Path)):
        text = Path(source).read_text(encoding='utf-8')
        try:
            data = json.loads(text)
            vals = [str(v) for v in data] if isinstance(data, list) else [str(data)]
        except json.JSONDecodeError:
            vals = [line.strip() for line in text.splitlines() if line.strip()]
    else:
        vals = [str(v) for v in source]
    vals = [v for v in vals if v]
    return (vals, None) if vals else (None, 'список значений пуст: сравнивать не с чем')


def data_mask_sweep_text(text: str, values) -> tuple:
    """Нет ли в тексте запрещённых значений; вернуть их обрезанными (≤10 символов).

    Страховка после маскиратора: маскиратор чинит значения по ИМЕНИ ключа, а
    сюда передают сами значения из настроек (ключи stage-контуров, подписи) —
    совпало ли хоть одно, независимо от того, как оно в текст попало.

    Args:
        text: текст артефакта (HTML, JSON-спека, склейка текста PDF).
        values: реальные значения, которым в тексте быть нельзя.

    Returns:
        ([{'value', 'count'}], None) — значение в отчёте обрезано до
        `DATA_MASK_MAX_VALUE`, чтобы сам отчёт проверки ничего не выдавал;
        либо (None, 'текст ошибки').
    """
    vals, err = data_mask_values(values)
    if err:
        return None, err
    if not isinstance(text, str):
        return None, f'текст должен быть str, а {type(text).__name__}'
    hits = [{'value': value[:DATA_MASK_MAX_VALUE], 'count': text.count(value)}
            for value in vals if value in text]
    return hits, None


def data_mask_sweep(paths, values) -> tuple:
    """Нет ли запрещённых значений в артефактах: файлы и деревья, PDF — по текстовому слою.

    Общая страховка сборщиков (карточка `http_.http_card`, отчёт
    `pdf_.pdf_report`) и прогон «вручную перед публикацией»: PNG не сканируется
    (текстового слоя нет), сканируются входы — JSON-спеки, HTML, Markdown, и
    PDF, из которого сначала вынимается текст. Деревья обходятся `file_walk` —
    без `.venv` и служебных веток.

    Args:
        paths: файлы и/или каталоги (элементы списка либо строка).
        values: реальные значения, которым в артефактах быть нельзя.

    Returns:
        ({'scanned': int, 'hits': [{'file', 'value', 'count'}]}, None) — PASS
        это пустой `hits`; либо (None, 'текст ошибки').
    """
    from file_.file_walk import file_walk  # лениво: data_ ядром не тянет file_

    vals, vals_err = data_mask_values(values)
    if vals_err:
        return None, vals_err
    if isinstance(paths, (str, bytes, Path)):
        paths = [paths]
    files = []
    for one in paths:
        path = Path(one)
        if path.is_dir():
            files.extend(file_walk(path, DATA_MASK_SWEEP_SUFFIXES))
        elif path.is_file():
            files.append(path)
        else:
            return None, f'пути нет: {path}'
    # один файл мог приехать дважды (явно и в составе каталога) — скан один раз
    files = list(dict.fromkeys(Path(f).resolve() for f in files))
    scanned, hits = 0, []
    for f in files:
        if str(f).lower().endswith('.pdf'):
            from pdf_.pdf_ import pdf_text  # лениво: колёса pdf_ не в ядре data_
            text = pdf_text(str(f))
        else:
            text = Path(f).read_bytes().decode('utf-8', 'replace')
        found, err = data_mask_sweep_text(text, vals)
        if err:
            return None, err
        scanned += 1
        hits.extend({**hit, 'file': str(f)} for hit in found)
    return {'scanned': scanned, 'hits': hits}, None



# ── детали реализации ──


def _data_mask_sub(pattern: 're.Pattern', text: str, group: str = None) -> str:
    """Заменить значения под группой `group` маскированным префикс+`*`+хвост.

    Args:
        pattern: собранный regex с группой секрета (либо без — секрет = весь матчер).
        text: текст дампа.
        group: имя группы со секретом; None — секрет = весь матчер (PAN, токены-ключи).

    Returns:
        текст с маской.
    """

    def repl(match):
        secret = match.group(group) if group else match.group(0)
        if not secret:
            return match.group(0)
        if len(secret) <= DATA_MASK_KEEP_PREFIX + DATA_MASK_KEEP_SUFFIX:
            masked = '*' * len(secret)
        else:
            masked = (secret[:DATA_MASK_KEEP_PREFIX]
                      + '*' * (len(secret) - DATA_MASK_KEEP_PREFIX - DATA_MASK_KEEP_SUFFIX)
                      + secret[-DATA_MASK_KEEP_SUFFIX:])
        if group is None:
            return masked
        head = match.group(0)[:match.start(group) - match.start(0)]
        tail = match.group(0)[match.end(group) - match.start(0):]
        return head + masked + tail

    return pattern.sub(repl, text)


if __name__ == '__main__':
    import argparse
    import json
    import sys

    _P = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    _P.add_argument('cmd', choices=('find', 'mask', 'sweep'), help='что показать')
    _P.add_argument('--path', default=['-'], nargs='+',
                    help='файл дампа ('' — stdin) либо, для sweep, файлы/каталоги')
    _P.add_argument('--values', default='', metavar='FILE',
                    help='sweep: список запрещённых значений (JSON-массив или построчно)')
    _C = _P.parse_args()
    if _C.cmd == 'sweep':
        _vals, _err = data_mask_values(_C.values)
        if _vals is None:
            print(f'ошибка: {_err}', file=sys.stderr)
            raise SystemExit(1)
        _got, _err = data_mask_sweep(_C.path, _vals)
        if _got is None:
            print(f'ошибка: {_err}', file=sys.stderr)
            raise SystemExit(1)
        for _h in _got['hits']:
            print(f"FAIL {_h['file']}: {_h['value']}… x{_h['count']}")
        print(f"{'FAIL' if _got['hits'] else 'PASS'}: проверено файлов {_got['scanned']}, "
              f"значений {len(_vals)}")
        raise SystemExit(1 if _got['hits'] else 0)
    if _C.cmd == 'find':
        _text = open(_C.path[0], 'rb').read() if _C.path[0] != '-' else sys.stdin.buffer.read()
        _rows, _err = data_mask_find(_text)
        if _rows is None:
            print(f'ошибка: {_err}', file=sys.stderr)
            raise SystemExit(1)
        for _r in _rows:
            print(f"offset={_r['position']:<10} {_r['kind']:<6} "
                  f"value={_r['value_prefix']!r}… ({_r['value_prefix_len']}б)")
    else:
        _text = open(_C.path[0], 'r', encoding='utf-8').read() if _C.path[0] != '-' \
            else sys.stdin.read()
        _masked, _err = data_mask_text(_text)
        if _masked is None:
            print(f'ошибка: {_err}', file=sys.stderr)
            raise SystemExit(1)
        sys.stdout.write(_masked)
    raise SystemExit(0)