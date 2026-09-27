# systemd — состояние юнита и журнал без глаз человека

> `systemctl status` написан для терминала: человек пробежит глазами, агент
> утонет в ANSI, переносах и «(+ )»-хвостах. А спрашивается всегда одно и то
> же: жив ли, включён ли, сколько весит, сколько раз перерождался — и что в
> журнале по делу. Ответ лежит в `systemctl show` и `journalctl`, и
> разворачивается в плоский словарь без единой регулярки по тексту статуса.

## 1. Состояние юнита — словами, не глазами

```python
from systemd_.systemd_status import systemd_status_get

s = systemd_status_get('app.service')
# {'code': 0, 'state': 'active', 'sub': 'running', 'enabled': 'enabled',
#  'pid': 314, 'memory': 9338880, 'restarts': 0, 'description': '...', 'error': ''}
```

Снимаемый набор — `SYSTEMD_STATUS_PROPS` (`ActiveState`, `SubState`,
`UnitFileState`, `MainPID`, `MemoryCurrent`, `NRestarts`, `Description`);
`memory` — байты, `restarts` — `NRestarts`: сколько перезапусков этого юнита
запомнил systemd. `enabled` отвечает на «включён ли автозапуск»,
отдельно от «жив ли сейчас» — живой-но-выключенный после перезагрузки
умрёт, и это два разных вопроса.

Удалённый узел — `host=` в формате `ssh_x`, `user=True` — `--user`-юниты.

```bash
PYTHONPATH=. python -m systemd_.systemd_status app.service            # JSON
PYTHONPATH=. python -m systemd_.systemd_status app.service --host узел
```

## 2. Журнал: хвост с фильтром, без `grep` по выводам

```python
from systemd_.systemd_journal import systemd_journal_tail

j = systemd_journal_tail('app.service', lines=50, since='-10 min', grep='error')
# {'code': 0, 'lines': [...], 'error': ''}
```

`grep` фильтрует **локально**, регистронезависимо, а выгружается при этом
`SYSTEMD_JOURNAL_SCAN` (5000) строк — иначе фильтр по 50 строкам найдёт
то, что попало в окно, и враньё «в журнале чисто» хуже отсутствия ответа.
`-f` нет сознательно: висящий хвост в агентском инструменте — висящий
процесс.

```bash
PYTHONPATH=. python -m systemd_.systemd_journal app.service -n 20 --since '1 hour ago' --grep timeout
```

## Грабли

1. **Замороженный юнит выглядит живым**: `SIGSTOP` (или `docker pause`
   сверху) оставляет `active (running)`, порт открытым (accept-queue ядра)
   и память в `MemoryCurrent`. Признак заморозки — `T` в
   `/proc/<pid>/stat`, и спрашивать её надо отдельно, этот модуль её не
   выдаёт.
2. **`MemoryCurrent` без контрольной группы — огромное число**, а не ноль:
   выключенный аккаунтинг отдаёт `18446744073709551615`; модуль сворачивает
   его в `None`, а наивный вызов `systemctl show` суммирует это в отчёт о
   «петабайтах».
3. **`--since` понимает относительные времена** (`'-10 min'`, `'1 hour
   ago'`) — извращаться с `date` не надо, но часовой пояс он берёт
   локальный: журнал с UTC-машиной и локальное время могут разойтись.
4. **Журнал без `-u` юнита, живущего под другим пользователем, читается не
   всяким членом `adm`**: `code 1` с правом «no permissions» — это не
   «строк нет», и отличать их должен вызывающий, не этот модуль.
5. **Фильтр `grep` не знает про смысл**: `error` не найдёт `Error:` внутри
   JSON-строки? — найдёт, подстроку; а вот «уровень error» не найдёт.
   Тонкая выборка — за пределами подстройки.
