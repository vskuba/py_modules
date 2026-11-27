"""Тесты PII-скана и приватного кадра `browser_login_shots` — офлайн, без браузера:
страница подменена объектом, кадр рисуется PIL'ом в temp-каталоге."""
import asyncio

import pytest
from PIL import Image, ImageDraw

import browser_.browser_login_shots as bls
from browser_.browser_login_shots import (
    _by_file,
    browser_login_shots_pii,
    browser_login_shots_pii_report,
)


def test_pii_находит_почту_айпи_карту_и_маску():
    text = ('operator: admin@pay.example.com, client 10.11.12.13, '
            'card 4242424242424242, mask 424242******4242, cid 90210')
    hits = browser_login_shots_pii(text)
    assert {'email', 'ipv4', 'pan', 'masked_pan'} <= {h['kind'] for h in hits}
    assert all(len(h['value']) <= 10 for h in hits)  # полные значения — никогда


def test_pii_свой_паттерн_дополняет_стандартные():
    hits = browser_login_shots_pii('cid 90210', {'cid': r'\bcid\s+\d+'})
    assert hits == [{'kind': 'cid', 'value': 'cid 90210'}]  # 9 символов — целиком
    hits = browser_login_shots_pii('cid 90210111213', {'cid': r'\bcid\s+\d+'})
    assert hits[0]['value'] == 'cid 902101'                 # длиннее — срезано по 10


def test_pii_report_пишет_строки_без_значений_целиком(tmp_path):
    path = browser_login_shots_pii_report(
        str(tmp_path), {'a.png': [{'kind': 'email', 'value': 'admin@pay'}],
                        'b.png': []})
    body = open(path, encoding='utf-8').read()
    assert 'a.png: email «admin@pay»' in body and 'b.png' not in body


def test_pii_report_чисто_значит_нет_файла(tmp_path):
    assert browser_login_shots_pii_report(str(tmp_path), {'a.png': []}) == ''
    assert not (tmp_path / 'pii-report.md').exists()


def test_by_file_режет_аргументы_на_владельца_и_значение():
    assert _by_file(['a.png|sel', 'body .email']) == {'a.png': ['sel'], '*': ['body .email']}


class _FakePage:
    """Страница без Playwright: `boxes` — селектор → список боксов, кадр — белый PNG
    с чёрными прямоугольниками в этих боксах (blur обязан размыть каждый)."""

    def __init__(self, boxes=None, text='', size=(800, 600)):
        self.boxes, self.text, self.size = boxes or {}, text, size
        self.viewport_size = {'width': size[0], 'height': size[1]}

    async def goto(self, url, **kw):
        pass

    async def wait_for_timeout(self, ms):
        pass

    def locator(self, sel):
        return _FakeLocator(self, sel)

    async def evaluate(self, js):
        return self.text if 'innerText' in js else 0

    async def screenshot(self, path=None, full_page=False):
        im = Image.new('RGB', self.size, 'white')
        d = ImageDraw.Draw(im)
        for boxes in self.boxes.values():
            for b in boxes:
                d.rectangle([b['x'], b['y'], b['x'] + b['width'] - 1,
                             b['y'] + b['height'] - 1], fill='black')
        im.save(path)


class _FakeLocator:
    def __init__(self, page, sel):
        self._page, self._sel, self._idx = page, sel, 0

    @property
    def first(self):
        return self

    def nth(self, j):
        self._idx = j
        return self

    async def count(self):
        return len(self._page.boxes.get(self._sel, []))

    async def bounding_box(self):
        boxes = self._page.boxes.get(self._sel, [])
        return boxes[self._idx] if self._idx < len(boxes) else None

    async def evaluate(self, js):
        return None

    async def click(self):
        pass


def _patch(monkeypatch, page):
    async def _start():
        pass

    async def _open(*a, **k):
        return {'session_id': 's1'}

    async def _close(sid):
        return True

    async def _stop():
        pass

    async def _run(*a, **k):
        return {'status': 'ok'}

    monkeypatch.setattr(bls, 'browser_pool_start', _start)
    monkeypatch.setattr(bls, 'browser_pool_session_open', _open)
    monkeypatch.setattr(bls, 'browser_pool_session_get', lambda sid: {'page': page})
    monkeypatch.setattr(bls, 'browser_pool_session_close', _close)
    monkeypatch.setattr(bls, 'browser_pool_stop', _stop)
    monkeypatch.setattr(bls, 'browser_scenario_run', _run)


def _shots(page, out, **kw):
    return asyncio.run(bls.browser_login_shots(
        'http://site', {'#email': 'K'}, {'K': 'v'}, [('/p', 'a.png')], out,
        login_path='/login', wait_ms=0, **kw))


def test_кадр_не_пишется_когда_blur_селектор_ничего_не_нашёл(tmp_path, monkeypatch):
    page = _FakePage(boxes={}, text='admin@pay.example.com')
    _patch(monkeypatch, page)
    with pytest.raises(RuntimeError, match='ничего не нашёл'):
        _shots(page, str(tmp_path), blur={'*': ['td.pii']})
    assert list(tmp_path.iterdir()) == []  # ни PNG, ни .raw, ни отчёта-подсказки


def test_blur_размывает_каждый_матч_селектора_а_не_первый(tmp_path, monkeypatch):
    rows = [{'x': 100, 'y': y, 'width': 80, 'height': 18} for y in (100, 300)]
    page = _FakePage(boxes={'td.pii': rows})
    _patch(monkeypatch, page)
    got = _shots(page, str(tmp_path), blur={'*': ['td.pii']})
    im = Image.open(got[0])
    for r in rows:  # центр каждого ряда: гауссиана смешала чёрный с белым
        px = im.convert('L').getpixel((r['x'] + 40, r['y'] + 9))
        assert 30 < px < 250, f'строка y={r["y"]} не размыта (px={px})'
    assert im.convert('L').getpixel((500, 500)) == 255  # фон кадра не тронут


def test_blur_окно_вне_кропа_обрушает_прогон_без_файла(tmp_path, monkeypatch):
    page = _FakePage(boxes={'div.card': [{'x': 0, 'y': 200, 'width': 400, 'height': 300}],
                            'td.pii': [{'x': 100, 'y': 20, 'width': 80, 'height': 18}]})
    _patch(monkeypatch, page)
    with pytest.raises(RuntimeError, match='из 1'):
        _shots(page, str(tmp_path), blur={'*': ['td.pii']}, crop={'*': 'div.card'})
    assert not (tmp_path / 'a.png').exists() and not (tmp_path / '.a.png.tmp.png').exists()


def test_сбой_blur_не_оставляет_немазанный_кадр(tmp_path, monkeypatch):
    page = _FakePage(boxes={'td.pii': [{'x': 100, 'y': 100, 'width': 80, 'height': 18}]})
    _patch(monkeypatch, page)

    def _boom(*a, **k):
        raise OSError('PIL упала')

    monkeypatch.setattr(bls, 'image_fix_blur', _boom)
    with pytest.raises(OSError):
        _shots(page, str(tmp_path), blur={'*': ['td.pii']})
    assert list(tmp_path.iterdir()) == []  # ни raw-кадра, ни итога


def test_отчёт_догоняет_оборванный_прогон(tmp_path, monkeypatch):
    page = _FakePage(boxes={}, text='operator admin@pay.example.com')
    _patch(monkeypatch, page)
    with pytest.raises(RuntimeError):
        asyncio.run(bls.browser_login_shots(
            'http://site', {'#email': 'K'}, {'K': 'v'},
            [('/a', 'a.png'), ('/b', 'b.png')], str(tmp_path), wait_ms=0,
            blur={'b.png': ['нет-такого']}))
    assert (tmp_path / 'a.png').exists() and not (tmp_path / 'b.png').exists()
    body = (tmp_path / 'pii-report.md').read_text(encoding='utf-8')
    assert 'email «admin@pay.»' in body  # только префикс ≤10 символов
    assert 'admin@pay.example.com' not in body  # полного значения в отчёте нет
