# Atlassian Cloud — Jira и Confluence из терминала

> Задача, JQL-поиск, комментарий (`jira_/jira_api.py`); страница, дерево,
> выгрузка пространства, CQL (`confluence_/confluence_api.py`). Один сайт,
> один токен, один запросчик — `jira_api_request(..., product=…)`.

## Ключи

`.env.local` проекта (или окружение процесса):

| Ключ | Что |
|---|---|
| `JIRA_URL` | `https://<сайт>.atlassian.net` — общий для Jira и Confluence |
| `JIRA_EMAIL`, `JIRA_API_TOKEN` | basic auth; токен — id.atlassian.com → Security → API tokens |
| `JIRA_KEY_ALIASES` | `TIER:TIER3,…` — префикс ключа в ветках → в Jira (знание проекта, в слое его нет) |

## Команды

```bash
python -m jira_.jira_api issue TIER-1637
python -m jira_.jira_api search 'assignee = currentUser() AND statusCategory != Done'
python -m jira_.jira_api comment KEY-1 'текст'            # публикация — только по просьбе
python -m confluence_.confluence_api page <ссылка или id>
python -m confluence_.confluence_api tree CAS
python -m confluence_.confluence_api export CAS /tmp/cas  # <id>.md + index.tsv
python -m confluence_.confluence_api search 'space = CAS AND text ~ "payout"'
```

## Грабли

- ⚠⚠ **Scoped-токен на прямом адресе — `401 Client must be authenticated`**,
  будто неверен пароль. Работает только шлюз `api.atlassian.com/ex/<product>/<cloudId>`;
  `jira_api_request` уходит туда сам на 401. `scope does not match` — токен
  верен, не выданы scopes продукта.
- ⚠ Нет задачи и нет прав — **один и тот же 404**. Сначала ключ (`JIRA_KEY_ALIASES`).
- Jira-поиск — `POST rest/api/3/search/jql` (старый `/search` — 410), курсор `nextPageToken`.
- Тексты Jira — ADF (JSON-дерево), Confluence — storage XHTML с макросами `ac:`;
  оба модуля переводят в текст сами.
- Confluence: пространство в API — числовой id, не ключ; CQL только в v1 `rest/api/search`.
