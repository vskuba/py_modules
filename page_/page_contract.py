"""
Ключ ответа ручки против ключей, которые читает JS: мёртвые и невзяные.

Между роутером и страницей живёт контракт из строк-ключей: сервер что-то кладёт
в `result`, страница что-то из `result` читает. Расхождение гниёт тихо: поле
осталось в ответе и никто его не читает (мёртвый контракт), или JS читает ключ,
которого в ответе нет (молча `undefined` в разметке). Третье — ключ переехал, а
правку заметили только с одной стороны.

Инструмент сверяет литералы: какие ключи роутер кладёт в словари ответа и какие
ключи читает его JS. Сверка текстом, не типами: это про литералы, которыми они
и написаны в обоих слоях.
"""
import argparse
import re
from pathlib import Path

# ключ словаря: `'ключ':` или `['ключ'] =` — и то и другое кладёт значение в ответ.
_PAGE_CONTRACT_EMIT = re.compile(r'''['"]([\wа-яёА-ЯЁ]+)['"]\s*(?::|\]\s*=)''')
# доступ в JS: r['ключ'], got["ключ"] — что страница реально читает.
_PAGE_CONTRACT_READ = re.compile(r'''\[\s*['"]([\wа-яёА-ЯЁ]+)['"]\s*\]''')


def page_contract(router, js) -> dict:
    """Что роутер отдаёт и что его JS читает: мёртвые поля и неотданные ключи.

    Args:
        router: файл (или список) роутера — ключи кладутся в словари ответа.
        js: файл (или список) страницы — читает `r['...']`.

    Returns:
        {'only_server': {ключ: [файл:строка]}, 'only_js': {...}, 'both': N}:
        `only_server` — отдаётся и никем не читается (мёртвый контракт, править
        или удалить), `only_js` — читается, но в ответе этого нет (или ключ
        переехал, или JS смотрит не в этот роутер).
    """
    def keys(pattern, sources):
        out = {}
        files = sources if isinstance(sources, (list, tuple)) else [sources]
        for f in files:
            for number, line in enumerate(Path(f).read_text(encoding='utf-8')
                                          .splitlines(), 1):
                for key in pattern.findall(line):
                    out.setdefault(key, []).append(f'{Path(f).name}:{number}')
        return out

    emitted = keys(_PAGE_CONTRACT_EMIT, router if isinstance(router, (list, tuple))
                  else [router])
    read = keys(_PAGE_CONTRACT_READ, js if isinstance(js, (list, tuple)) else [js])
    both = {k for k in emitted} & {k for k in read}
    return {'only_server': {k: v for k, v in emitted.items() if k not in both},
            'only_js': {k: v for k, v in read.items() if k not in both},
            'both': len(both)}


if __name__ == '__main__':
    ap = argparse.ArgumentParser(
        description='Ключи ответа ручки против ключей JS: мёртвые поля, '
                    'невзяные ключи.')
    ap.add_argument('--router', nargs='+', required=True, help='файлы роутера')
    ap.add_argument('--js', nargs='+', required=True, help='файлы страницы')
    ap.add_argument('--both', action='store_true', help='показать и совпавшие')
    ns = ap.parse_args()
    r = page_contract(ns.router, ns.js)
    for name, title in (('only_server', 'отдаётся и не читается (мёртвый контракт)'),
                        ('only_js', 'читается, но не отдаётся')):
        for key, where in sorted(r[name].items()):
            print(f'  {key!r} — {title}: {", ".join(where[:3])}')
    gap = len(r['only_server']) + len(r['only_js'])
    print(f'совпало ключей: {r["both"]}' + (f', расшлось: {gap}'))
    raise SystemExit(min(gap, 125))   # код возврата живёт в байте, см. file_check
