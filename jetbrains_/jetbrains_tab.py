"""Вкладка терминала JetBrains IDE по заголовку — вывести на экран из любого процесса.

Работает через плагин Claude Tab Switch (`claude-shell/jetbrains/claude-tab`): он вешает
`GET /api/claude-tab?title=…` на встроенный сервер IDE. У каждой запущенной IDE свой
порт — 63342, 63343, … по порядку запуска, поэтому опрашиваются все подряд.

```bash
PYTHONPATH=. python -m jetbrains_.jetbrains_tab select «Заголовок вкладки»
PYTHONPATH=. python -m jetbrains_.jetbrains_tab list
PYTHONPATH=. python -m jetbrains_.jetbrains_tab new ПАПКА       # вкладка с Claude Code
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


def jetbrains_tab_new(cwd: str) -> str:
    """Открыть новую вкладку терминала с Claude Code в IDE, где открыт проект этой папки.

    Сначала все IDE спрашиваются строго — «открыт ли у тебя проект этой папки»; ни одна
    не узнала папку — вкладка открывается в первой живой IDE, в проекте последнем в фокусе.

    Returns:
        Имя проекта, где открыта вкладка; пусто — ни одной IDE с плагином.
    """
    for strict in ('1', '0'):
        query = urllib.parse.urlencode({'cwd': cwd, 'strict': strict})
        for port in _jetbrains_tab_ports():
            answer = _jetbrains_tab_get(port, f'/api/claude-tab/new?{query}')
            if answer.startswith('opened '):
                return answer[len('opened '):]
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
    if len(sys.argv) > 2 and sys.argv[1] == 'new':
        found = jetbrains_tab_new(sys.argv[2])
        print(found or 'ни одной IDE с плагином')
        sys.exit(0 if found else 1)
    if len(sys.argv) > 1 and sys.argv[1] == 'list':
        for item in jetbrains_tab_list():
            print(f"{item['port']}  {item['project']:<30} {item['title']}")
        sys.exit(0)
    print(__doc__)
    sys.exit(2)
