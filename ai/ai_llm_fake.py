"""Подменный сервис LLM: отвечает то, что заранее велел тест.

⚠ Лежит **рядом с `ai_think`, а не в `ai/provider/`**: провайдер — это стратегия
сборки модели для нашего кода, а здесь обратная сторона — сервис, к которому наш
код обращается. В `provider/` ищут, чем говорить с моделью, а не саму модель.

Запуск:

    python -m ai.ai_llm_fake --port 8099

Адрес кладут в `LLM_FAKE_API_URL`, и модель `fake/...` идёт сюда вместо живой.

## ⚠⚠ Ответы задаёт тест, а не сервис

Первая версия пробовала **угадывать** ответ по промпту: разбирала просьбу «верни
JSON с такими ключами», подставляла заготовки по именам и типам. Это тупик, и он
проявился сразу: подпрогон фактов ждал `0`, ветка чата — текст, судья — свой
конверт, и каждый новый шаг workflow требовал править сервис. Подмена, которая
угадывает, всегда отстаёт от того, что у неё спрашивают.

Теперь сценарий приходит **от теста** — по ответу на каждый шаг, который зовёт
модель (`llm_to_json`, `return_response_llm`). Прочие шаги сюда не обращаются
вовсе: инструменты, переходы и подстановка работают по-настоящему, и подменять
их нечем — они и есть предмет проверки.

    POST /fixtures  {"rules": [{"when": "кусок промпта", "answer": {...}}],
                     "default": {...}}

На каждый запрос сервис берёт **первое правило**, чья примета встретилась в
промпте, и отдаёт его ответ. Не совпало ни одно — `default`.

⚠ Примета — кусок **промпта**, а не имя шага: имени шага в запросе нет вовсе,
движок его не шлёт. Зато промпт шага уникален, и куска из него довольно, чтобы
шаг назвать.

## ⚠ Журнал вызовов

`GET /calls` отдаёт, сколько раз и с какими промптами звали. По нему тест
проверяет, что шаг вообще спрашивал модель, — и видит настоящий промпт, если
правило не совпало.

## ⚠ Зависимостей нет

Стандартный `http.server`: сервис поднимается рядом с тестами и в CI, и тянуть
ради него фреймворк значило бы чинить его установку там, где всё уже работает.
"""
import argparse
import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Что отвечаем, когда тест не задал ни правил, ни умолчания.
#
# ⚠ Пустая строка, а не выдуманный текст: сценария нет — значит тест про эти
# ответы ничего не утверждает, и подсовывать ему правдоподобные слова незачем.
FAKE_TEXT = ''

# Сколько последних вызовов помним. ⚠ Не «все»: сервис живёт между прогонами, и
# неограниченный список рос бы всю неделю.
FAKE_CALLS_MAX = 200


class FakeState:
    """Сценарий и журнал вызовов. Общие на процесс, под замком.

    ⚠ Замок обязателен: `ThreadingHTTPServer` обслуживает запросы в разных
    потоках, а движок workflow шлёт их подряд, да ещё и из подпрогонов.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.rules: list = []
        self.default = None
        self.calls: list = []

    def fixtures_set(self, rules: list, default=None, forget: bool = True) -> None:
        """Заменить сценарий целиком.

        Args:
            rules: правила «примета промпта → ответ».
            default: чем отвечать, когда не совпало ни одно.
            forget: забыть ли прежние вызовы.

        ⚠⚠ **Снятие сценария журнал не чистит**, и это не мелочь: правило,
        переставшее совпадать, разбирают **после** теста — а фикстура снимает
        сценарий на выходе. Чисти она заодно и журнал, диагностика пропадала бы
        ровно в тот момент, когда за ней приходят.
        """
        with self.lock:
            self.rules = list(rules or [])
            self.default = default

            if forget:
                self.calls = []

    def answer_for(self, prompt: str) -> str:
        """Ответ по первому совпавшему правилу; иначе умолчание."""
        with self.lock:
            self.calls.append({'prompt': str(prompt)[:4000], 'at': time.time()})
            del self.calls[:-FAKE_CALLS_MAX]

            for rule in self.rules:
                mark = str((rule or {}).get('when') or '')

                if mark and mark in prompt:
                    return _as_text((rule or {}).get('answer'))

            return _as_text(self.default) if self.default is not None else FAKE_TEXT

    def calls_get(self) -> list:
        with self.lock:
            return list(self.calls)


STATE = FakeState()


def _as_text(answer) -> str:
    """Ответ в том виде, в каком его увидит движок.

    ⚠ Словарь и список отдаём **JSON-строкой**: шаг `llm_to_json` просит у модели
    JSON текстом и разбирает его сам. Тест при этом пишет обычный словарь — так
    сценарий читается, а не собирается из кавычек.
    """
    if answer is None:
        return FAKE_TEXT

    if isinstance(answer, str):
        return answer

    return json.dumps(answer, ensure_ascii=False)


class _Handler(BaseHTTPRequestHandler):
    """Ручки сервиса: ответ модели, загрузка сценария, журнал вызовов."""

    # ⚠ Тишина в консоли: сервис поднимается рядом с тестами, и строка на каждый
    # запрос заслонила бы их вывод.
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        path = self.path.rstrip('/')

        if path.endswith('/calls'):
            self._send({'calls': STATE.calls_get()})

            return

        # ⚠ `/models` спрашивают проверки живости — отвечаем, чтобы подмена
        # выглядела обычным сервисом и там.
        if path.endswith('/models'):
            self._send({'object': 'list', 'data': [{'id': 'fake', 'object': 'model'}]})

            return

        self._send({'status': 'ok'})

    def do_DELETE(self):
        # ⚠ Журнал оставляем: за ним приходят уже после теста.
        STATE.fixtures_set([], None, forget=False)
        self._send({'status': 'ok'})

    def do_POST(self):
        payload = self._payload()

        if self.path.rstrip('/').endswith('/fixtures'):
            STATE.fixtures_set(payload.get('rules') or [], payload.get('default'))
            self._send({'status': 'ok', 'rules': len(payload.get('rules') or [])})

            return

        self._completion(payload)

    def _completion(self, payload: dict) -> None:
        """Ответ модели на запрос `/chat/completions`."""
        messages = payload.get('messages') or []
        prompt = '\n'.join(str((one or {}).get('content') or '') for one in messages)
        content = STATE.answer_for(prompt)

        self._send({
            'id': f'chatcmpl-{uuid.uuid4().hex[:24]}',
            'object': 'chat.completion',
            'created': int(time.time()),
            'model': str(payload.get('model') or 'fake'),
            'choices': [{
                'index': 0,
                'message': {'role': 'assistant', 'content': content},
                'finish_reason': 'stop',
            }],
            # ⚠ Расход нужен: вызывающий пишет его в журнал и в трассу, а `None`
            # там читался бы как «шаг не ходил к модели».
            'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2},
        })

    def _payload(self) -> dict:
        size = int(self.headers.get('Content-Length') or 0)

        try:
            return json.loads(self.rfile.read(size) or b'{}')
        except ValueError:
            return {}

    def _send(self, body: dict) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode('utf-8')

        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Подменный OpenAI-совместимый сервис: отвечает по сценарию теста.')
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=8099)
    args = parser.parse_args()

    print(f'подменная LLM слушает {args.host}:{args.port}', flush=True)
    ThreadingHTTPServer((args.host, args.port), _Handler).serve_forever()
