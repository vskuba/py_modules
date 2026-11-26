"""
Имя модели в теле запроса: `raw_model` и его места применения.

vLLM принимает только имя, под которым поднял модель, и на реестровый хвост
(`qwen-large`) отвечает 404; llama.cpp принимал любое. Тест проверяет правило
выбора имени (`GX10_<РАЗМЕР>_MODEL` > `GX10_MODEL` > имя как есть) и то, что
сырые сборщики тела (`translate_review._ask`, `ai_vision`) берут имя у
стратегии, а не подставляют реестровое или своё ad-hoc.

Сервера здесь нет: HTTP подменён, картинки — синтетические. Живой проход —
отдельной проверкой на стенде.
"""
import io

import pytest
from PIL import Image

import httpx

from ai.vision import ai_vision
from ai.provider.ai_provider_registry import ai_provider_registry_get
import translate_.translate_review as tr


def test_raw_model_other_provider_passthrough():
    """Чужие сервисы кладут в запрос реестровое имя без изменений."""
    provider = ai_provider_registry_get('openrouter/deepseek/deepseek-v4-flash-0731')
    assert provider.raw_model('deepseek/deepseek-v4-flash-0731') == 'deepseek/deepseek-v4-flash-0731'


def test_raw_model_gx10_served_name(monkeypatch):
    """`GX10_MODEL` важнее реестрового хвоста: vLLM другого не знает."""
    monkeypatch.delenv('GX10_LARGE_MODEL', raising=False)
    monkeypatch.setenv('GX10_MODEL', 'qwen3.8-flash-next')
    provider = ai_provider_registry_get('gx10/qwen-large')
    assert provider.raw_model('qwen-large') == 'qwen3.8-flash-next'


def test_raw_model_gx10_per_size_wins(monkeypatch):
    """Переопределение по размеру бьёт общий `GX10_MODEL`."""
    monkeypatch.setenv('GX10_MODEL', 'qwen3.8-flash-next')
    monkeypatch.setenv('GX10_LARGE_MODEL', 'served-large')
    provider = ai_provider_registry_get('gx10/qwen-large')
    assert provider.raw_model('qwen-large') == 'served-large'


def test_raw_model_gx10_fallback(monkeypatch):
    """Ни одной переменной нет — имя из реестра, как было при llama.cpp."""
    monkeypatch.delenv('GX10_MODEL', raising=False)
    monkeypatch.delenv('GX10_LARGE_MODEL', raising=False)
    provider = ai_provider_registry_get('gx10/qwen-large')
    assert provider.raw_model('qwen-large') == 'qwen-large'


class _Resp:
    status_code = 200

    @staticmethod
    def json():
        return {'choices': [{'message': {'content': 'ok'}}]}

    @staticmethod
    def raise_for_status():
        pass


def test_ask_uses_raw_model_and_no_retry(monkeypatch):
    """`_ask`: имя из стратегии в теле, одного запроса хватает — `/models` не зовём."""
    monkeypatch.delenv('GX10_LARGE_MODEL', raising=False)
    monkeypatch.setenv('GX10_MODEL', 'served-x')
    monkeypatch.setenv('GX10_LARGE_API_URL', 'http://gx10.test/v1')
    monkeypatch.setenv('GX10_API_KEY', 'k')
    posts = []

    def fake_post(url, json=None, **_kw):
        posts.append((url, json))
        return _Resp()

    def fake_get(*_a, **_kw):
        raise AssertionError('retry на /models больше не нужен')

    monkeypatch.setattr(tr.httpx, 'post', fake_post)
    monkeypatch.setattr(tr.httpx, 'get', fake_get)

    assert tr._ask('gx10/qwen-large', 'вопрос', 5) == 'ok'
    assert len(posts) == 1
    assert posts[0][1]['model'] == 'served-x'


class _FakeAsyncClient:
    """Подмена `httpx.AsyncClient` для `ai_vision`: тот же контракт, без сети."""

    def __init__(self, **_kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def post(self, url, json=None, **_kw):
        _FakeAsyncClient.sent.append(json)
        return _Resp()


def test_vision_body_uses_raw_model(monkeypatch):
    """`ai_vision` кладёт в `body['model']` имя от стратегии, а не реестровый хвост."""
    monkeypatch.delenv('GX10_LARGE_MODEL', raising=False)
    monkeypatch.setenv('GX10_MODEL', 'served-x')
    monkeypatch.setenv('GX10_LARGE_API_URL', 'http://gx10.test/v1')
    monkeypatch.setenv('GX10_API_KEY', 'k')
    monkeypatch.setenv('AI_VISION_MODEL_NAME', 'gx10/qwen-large')

    _FakeAsyncClient.sent = []
    monkeypatch.setattr(ai_vision, 'httpx', type('_m', (), {'AsyncClient': _FakeAsyncClient}))

    img = io.BytesIO()
    Image.new('RGB', (8, 8)).save(img, 'PNG')
    assert ai_vision.ai_vision_describe_wait(img.getvalue()) == 'ok'
    assert len(_FakeAsyncClient.sent) == 1
    assert _FakeAsyncClient.sent[0]['model'] == 'served-x'
