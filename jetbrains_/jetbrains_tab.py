"""Вкладка терминала JetBrains IDE по заголовку — вывести на экран из любого процесса.

Работает через плагин Claude Tab Switch (`claude-shell/jetbrains/claude-tab`): он вешает
`GET /api/claude-tab?title=…` на встроенный сервер IDE. У каждой запущенной IDE свой
порт — 63342, 63343, … по порядку запуска, поэтому опрашиваются все подряд.

```bash
PYTHONPATH=. python -m jetbrains_.jetbrains_tab select «Заголовок вкладки»
PYTHONPATH=. python -m jetbrains_.jetbrains_tab list
```

⚠ Без плагина IDE отвечает на этот адрес 404 — для вызывающего это «вкладки нет».
⚠ Заголовок сравнивается без значков в начале (крутилка Claude Code «✳», «⠂»).
"""
import sys
import urllib.error
import urllib.parse
import urllib.request

# Порты встроенного сервера IDE: первый и сколько подряд пробовать.
JETBRAINS_TAB_PORT = 63342
JETBRAINS_TAB_PORTS = 10


def jetbrains_tab_select(title: str) -> str:
    """Выбрать вкладку терминала с этим заголовком в той IDE, где она открыта.

    Returns:
        Имя проекта, где вкладка нашлась и выбрана; пусто — ни в одной IDE её нет.
    """
    query = urllib.parse.urlencode({'title': title})
    for port in _jetbrains_tab_ports():
        answer = _jetbrains_tab_get(port, f'/api/claude-tab?{query}')
        if answer.startswith('selected '):
            return answer[len('selected '):]
    return ''


def jetbrains_tab_list() -> list:
    """Все вкладки терминала всех IDE с плагином: `[{port, project, title}]`."""
    rows = []
    for port in _jetbrains_tab_ports():
        for line in _jetbrains_tab_get(port, '/api/claude-tab/list').splitlines():
            project, _, title = line.partition('\t')
            rows.append({'port': port, 'project': project, 'title': title})
    return rows


def _jetbrains_tab_ports() -> range:
    return range(JETBRAINS_TAB_PORT, JETBRAINS_TAB_PORT + JETBRAINS_TAB_PORTS)


def _jetbrains_tab_get(port: int, path: str) -> str:
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}{path}', timeout=3) as response:
            return response.read().decode('utf-8', 'replace')
    except (urllib.error.URLError, OSError, ValueError):
        return ''


if __name__ == '__main__':
    if len(sys.argv) > 2 and sys.argv[1] == 'select':
        found = jetbrains_tab_select(sys.argv[2])
        print(found or 'вкладки нет')
        sys.exit(0 if found else 1)
    if len(sys.argv) > 1 and sys.argv[1] == 'list':
        for item in jetbrains_tab_list():
            print(f"{item['port']}  {item['project']:<30} {item['title']}")
        sys.exit(0)
    print(__doc__)
    sys.exit(2)
