"""Журнал перемен страницы: что скрипты поменяли и что от этого видно на экране.

`web_probe`/`web_drive_page` отдают состояние узла, и человек читает атрибут
`hidden` как «скрыто»… а атрибут прячет узел, только когда его `display` не
забит author-правилом: `.ds-thumbs { display: grid }` — забор для атрибута, и
«скрытая» сетка так и стоит на экране четвёртым блоком. Атрибут — намерение,
раскладка — факт. Этот инструмент отдаёт факт: видимость по `display`/
`visibility` вдоль всей цепочки предков с виновником в тексте, и что из
атрибутов узла сменил шаг (ловушка MutationObserver со старым значением).

Грабли, из-за которых это не однострочник:
* читать надо `getComputedStyle` по предкам, а не `.hidden`: правило с `display`
  перебивает атрибут, и проба, верящая атрибуту, «не видит» выделение и
  «видит» скрытое;
* шаг исполняется телом async-функции и видит `wait(fn, ms, base)` оболочки —
  переключить вкладку и дождаться перерисовки можно самим шагом;
* observer вешается на сам_watch-узел и ловит только его атрибуты: скрытие
  предком узел в записи не светится — потому запись всегда с фактом видимости.
"""
import argparse
import json

from web_.web_drive_page import web_drive_page


def web_watch(src, watch, steps=(), *, login=None, init=None, chrome='',
              size=()) -> dict:
    """Что скрипты страницы поменяли и что видно после каждого шага — факт раскладки.

    Args:
        src: URL живой страницы (оболочка `web_drive_page`: вход из `.env`,
            модуль страницы, `wait`).
        watch: списки CSS-селекторов, за которыми следят.
        steps: список шагов `{'step': подпись, 'js': код}`: код исполняется
            телом async-функции после предыдущего шага (видит `wait`, `q` — сам).
        login/init/chrome/size: как в `web_drive_page`.

    Returns:
        {'before': {селектор: {'visible', 'why'}}, 'steps': [{'step',
        'changes': [{'selector', 'attribute', 'was', 'now'}], 'visible':
        {селектор: {'visible', 'why'}}}]}. `visible` — факт раскладки
        (display/visibility по предкам, высота), `why` — виновник, когда факта
        нет у атрибута: правило, перебившее `hidden`, называется прямо там.

    ⚠ Ключи ответа — латиницей (правило `code_rules.md`, §1.2), в том числе у
    вложенного JS: до перевода они были русскими, и код, читавший `r['до']`,
    обязан перейти на `r['before']`.
    """
    blocks = []
    for step in steps:
        blocks.append('log.length = 0; %s;\nsteps.push({step: %s, changes: '
                      'log.slice(), visible: sum()});'
                      % (step['js'], json.dumps(step.get('step', ''),
                                                ensure_ascii=False)))
    body = """
const q = s => document.querySelector(s);
const fact = s => { const n = q(s); if (!n) return {visible: false, why: 'нет узла'};
    for (let e = n; e && e !== document.documentElement; e = e.parentElement) {
        const cs = getComputedStyle(e);
        if (cs.display === 'none' || cs.visibility === 'hidden')
            return {visible: false,
                    why: (e.id ? '#' + e.id : e.tagName.toLowerCase()) + ': ' + cs.display}; }
    const r = n.getBoundingClientRect();
    return {visible: r.height > 0, why: r.height > 0 && n.hasAttribute('hidden')
        ? 'атрибут hidden есть, но правило display:' + getComputedStyle(n).display
          + ' перебивает его' : ''}; };
const sel = %(watch)s;
const log = [];
sel.forEach(s => { const n = q(s); if (!n) return;
    new MutationObserver(rs => rs.forEach(r => log.push({selector: s,
        attribute: r.attributeName, was: r.oldValue, now: n.getAttribute(r.attributeName)})))
        .observe(n, {attributes: true, attributeOldValue: true}); });
const sum = () => Object.fromEntries(sel.map(s => [s, fact(s)]));
const before = sum();
const steps = [];
%(steps)s
return {before, steps};
""" % {'watch': json.dumps(list(watch), ensure_ascii=False),
       'steps': '\n'.join(blocks)}
    return web_drive_page(src, body, login=login, init=init, chrome=chrome,
                          size=size)['value']


if __name__ == '__main__':
    ap = argparse.ArgumentParser(
        description='Журнал перемен страницы: что скрипты поменяли и что видно '
                    'после каждого шага (факт раскладки, не атрибут).')
    ap.add_argument('url', help='адрес живой страницы')
    ap.add_argument('--watch', action='append', required=True, metavar='CSS',
                    help='селектор за ним следят, повторять')
    ap.add_argument('--step', action='append', default=[], metavar='JS',
                    help='шаг (тело async), повторять по порядку')
    ap.add_argument('--init', default='',
                    help='модуль инициализации "файл|имя|аргумент"; пусто — автопоиск')
    ns = ap.parse_args()
    and_init = {}
    if ns.init:
        parts = ns.init.split('|', 2)
        and_init = ({'file': parts[0], 'name': parts[1]} if len(parts) < 3 else
                    {'file': parts[0], 'name': parts[1], 'argument': parts[2]})
    out = web_watch(ns.url, ns.watch, [{'step': j, 'js': j} for j in ns.step],
                    init=and_init or None)
    print(json.dumps(out, ensure_ascii=False, indent=1))
