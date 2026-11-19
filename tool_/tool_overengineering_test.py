"""Тесты примет overengineering на файлах-фикстурах (без сети)."""
import textwrap

from tool_.tool_overengineering import tool_overengineering, tool_overengineering_format


def test_passthrough_wrapper_found(tmp_path):
    """Старое имя, оставленное обёрткой после переименования, — второе имя одного действия."""
    found = _scan(tmp_path, '''
        from other import report_send

        def report_daily_send(chat_id, period):
            """Отправить отчёт."""
            return report_send(chat_id, period)
    ''')
    assert _kinds(found) == [('passthrough', 'report_daily_send')]


def test_wrapper_that_adds_value_or_is_a_door_is_not_passthrough(tmp_path):
    """Своё значение, протокольный dunder и публичная дверь к приватной реализации — не находки."""
    found = _scan(tmp_path, '''
        def a(x):
            return b(x, 1)

        def c(x):
            return _impl(x)

        class Ctx:
            async def __aexit__(self, *exc):
                return await self._ctx.__aexit__(*exc)

        def _impl(x):
            return x
    ''')
    assert [one for one in found if one['kind'] == 'passthrough'] == []


def test_fixed_param_never_overridden_and_always_same(tmp_path):
    """Опция, которой никто не пользуется, и параметр, в который всегда передают одно."""
    found = _scan(tmp_path, '''
        def run():
            _crop('a')
            _crop('b')
            _face(1, mode='fast')
            _face(2, mode='fast')

        def _crop(path, quality=55):
            return path, quality

        def _face(x, mode='slow'):
            return x, mode
    ''')
    names = [one['name'] for one in found if one['kind'] == 'fixed_param']
    assert names == ['_crop(quality)', '_face(mode)']


def test_fixed_param_skips_none_variables_and_public(tmp_path):
    """`None` — точка подмены для тестов, переменная — разные значения, публичную судить нельзя."""
    found = _scan(tmp_path, '''
        def run(size):
            _cli()
            _face(size)
            _face(size)
            public()

        def _cli(argv=None):
            return argv

        def _face(det=640):
            return det

        def public(x=1):
            return x
    ''')
    assert [one for one in found if one['kind'] == 'fixed_param'] == []


def test_single_impl_abstraction_counts_heirs_in_root(tmp_path):
    """ABC с одним наследником — находка; с двумя — нет; null-object из `pass` — не абстракция."""
    found = _scan(tmp_path, '''
        from abc import ABC, abstractmethod

        class Lonely(ABC):
            @abstractmethod
            def go(self): ...

        class Shared(ABC):
            @abstractmethod
            def go(self): ...

        class Quiet:
            def info(self, *a):
                pass
    ''', extra={'impl.py': '''
        from mod import Lonely, Shared

        class OnlyOne(Lonely):
            def go(self): return 1

        class First(Shared):
            def go(self): return 1

        class Second(Shared):
            def go(self): return 2
    '''})
    single = [one for one in found if one['kind'] == 'single_impl']
    assert [one['name'] for one in single] == ['Lonely']
    assert 'OnlyOne' in single[0]['hint']


def test_class_as_function(tmp_path):
    """`__init__` и один метод — состояние ради одного вызова."""
    found = _scan(tmp_path, '''
        class Sender:
            def __init__(self, chat):
                self.chat = chat

            def send(self, text):
                return self.chat, text
    ''')
    assert _kinds(found) == [('class_as_function', 'Sender')]


def test_broken_file_skipped_and_format(tmp_path):
    """Неразбираемый файл не валит прогон; пустой отчёт сказан словами."""
    assert _scan(tmp_path, 'def (:\n') == []
    assert tool_overengineering_format([]) == 'Примет не нашлось.'


def _scan(tmp_path, code, name='mod.py', extra=None):
    """Положить код в файл и прогнать инструмент по каталогу."""
    (tmp_path / name).write_text(textwrap.dedent(code), encoding='utf-8')

    for other, text in (extra or {}).items():
        (tmp_path / other).write_text(textwrap.dedent(text), encoding='utf-8')

    return tool_overengineering([tmp_path / name], root=tmp_path)


def _kinds(found):
    return sorted((one['kind'], one['name']) for one in found)
