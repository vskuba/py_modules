"""Тесты создания страницы и вложений Confluence; HTTP подменён, сеть не нужна."""
from confluence_ import confluence_api
from jira_ import jira_api


def test_jira_api_multipart_has_xsrf_header_and_file_parts(tmp_path):
    """Обоим продуктам нужен `X-Atlassian-Token: no-check`; PDF уходит с application/pdf."""
    png, pdf = tmp_path / 'a.png', tmp_path / 'r.pdf'
    png.write_bytes(b'PNG')
    pdf.write_bytes(b'PDF')

    raw, headers = jira_api.jira_api_multipart([str(png), str(pdf)])

    assert headers['X-Atlassian-Token'] == 'no-check'
    assert b'filename="a.png"\r\nContent-Type: image/png' in raw
    assert b'filename="r.pdf"\r\nContent-Type: application/pdf' in raw


def test_confluence_api_page_create_posts_storage_under_parent(monkeypatch):
    """Страница — v2 `pages` с числовым id пространства, родителем и телом storage."""
    calls = []
    monkeypatch.setattr(confluence_api, 'confluence_api_space', lambda key, env=None: {'id': '42'})
    monkeypatch.setattr(confluence_api, 'jira_api_request',
                        lambda *a, **k: calls.append((a, k)) or {'id': '777'})

    page = confluence_api.confluence_api_page_create('CAS', 234422617, 'T', '<p>x</p>', env={'url': 'https://site.test'})

    (method, path), kwargs = calls[0]
    assert (method, path, page['id']) == ('POST', 'wiki/api/v2/pages', '777')
    assert kwargs['body']['spaceId'] == '42' and kwargs['body']['parentId'] == '234422617'
    assert kwargs['body']['body'] == {'representation': 'storage', 'value': '<p>x</p>'}
    assert kwargs['product'] == 'confluence'


def test_confluence_api_attach_uses_v1_multipart(monkeypatch, tmp_path):
    """Вложения — v1 `child/attachment` тем же multipart; наружу — список `results`."""
    shot = tmp_path / 's.png'
    shot.write_bytes(b'PNG')
    seen = {}
    monkeypatch.setattr(confluence_api, 'jira_api_request',
                        lambda *a, **k: seen.update(path=a[1], headers=k['headers'])
                        or {'results': [{'id': 'att1', 'title': 's.png'}]})

    out = confluence_api.confluence_api_attach('777', [str(shot)], env={'url': 'https://site.test'})

    assert seen['path'] == 'wiki/rest/api/content/777/child/attachment'
    assert seen['headers']['X-Atlassian-Token'] == 'no-check'
    assert out == [{'id': 'att1', 'title': 's.png'}]
