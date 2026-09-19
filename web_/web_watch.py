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
        steps: шагов список {'шаг': подпись, 'js': код}: код исполняется телом
            async-функции после предыдущего шага (видит `wait`, `q` — сам).
        login/init/chrome/size: как в `web_drive_page`.

    Returns:
        {'до': {селектор: {видно, почему}}, 'шаги': [{'шаг', 'изменения':
        [{селектор, атрибут, было, стало}], 'видно': {селектор: {видно,
        почему}}}]}. `видно` — факт раскладки (display/visibility по предкам,
        высота), `почему` — виновник, когда факта нет у атрибута: правило,
        перебившее `hidden`, называется прямо там.
    """
    блоки = []
    for ш in steps:
        блоки.append('log.length = 0; %s;\nшаги.push({шаг: %s, изменения: '
                     'log.slice(), видно: свод()});'
                     % (ш['js'], json.dumps(ш.get('шаг', ''), ensure_ascii=False)))
    body = """
const q = s => document.querySelector(s);
const факт = s => { const n = q(s); if (!n) return {видно: false, почему: 'нет узла'};
    for (let e = n; e && e !== document.documentElement; e = e.parentElement) {
        const cs = getComputedStyle(e);
        if (cs.display === 'none' || cs.visibility === 'hidden')
            return {видно: false,
                    почему: (e.id ? '#' + e.id : e.tagName.toLowerCase()) + ': ' + cs.display}; }
    const r = n.getBoundingClientRect();
    return {видно: r.height > 0, почему: r.height > 0 && n.hasAttribute('hidden')
        ? 'атрибут hidden есть, но правило display:' + getComputedStyle(n).display
          + ' перебивает его' : ''}; };
const св = %(watch)s;
const log = [];
св.forEach(s => { const n = q(s); if (!n) return;
    new MutationObserver(rs => rs.forEach(r => log.push({селектор: s,
        атрибут: r.attributeName, было: r.oldValue, стало: n.getAttribute(r.attributeName)})))
        .observe(n, {attributes: true, attributeOldValue: true}); });
const свод = () => Object.fromEntries(св.map(s => [s, факт(s)]));
const до = свод();
const шаги = [];
%(шаги)s
return {до, шаги};
""" % {'watch': json.dumps(list(watch), ensure_ascii=False),
       'шаги': '\n'.join(блоки)}
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
        ч = ns.init.split('|', 2)
        and_init = {'файл': ч[0], 'имя': ч[1]} if len(ч) < 3 else {'файл': ч[0], 'имя': ч[1], 'аргумент': ч[2]}
    out = web_watch(ns.url, ns.watch, [{'шаг': j, 'js': j} for j in ns.step],
                    init=and_init or None)
    print(json.dumps(out, ensure_ascii=False, indent=1))
