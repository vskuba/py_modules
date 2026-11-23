"""Тесты сопоставления аккаунта в `dsh_saving` при дрейфе недельного сброса.

Снимки и транскрипты синтетические — той формы, которую статусная строка кладёт в
`~/.cache/claude-rate`: живая сессия Claude здесь не нужна, а живая сделала бы тест
нелокальным. Все файловые константы модуля подменены на `tmp_path`.
"""
import json
import os
import time
from datetime import datetime, timezone

import pytest

from dsh_ import dsh_saving
from dsh_.dsh_saving import dsh_saving_account_same, dsh_saving_calibrate, dsh_saving_refresh

NOW = time.time()
S5 = int(NOW) + 4 * 3600       # сброс 5-часового окна: через пару часов (старт окна — час назад)
S7 = int(NOW) + 6 * 86400      # сброс недельного окна
HOUR, DAY = 3600, 86400


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Файлы модуля и транскрипты — в tmp; `DSH_HOME` — чтобы не трогать `~/.dsh`."""
    monkeypatch.setattr(dsh_saving, 'DSH_SAVING_RATE_DIR', str(tmp_path / 'claude-rate'))
    monkeypatch.setattr(dsh_saving, 'DSH_SAVING_CALIBRATION_FILE', str(tmp_path / 'calibration.json'))
    monkeypatch.setattr(dsh_saving, 'DSH_SAVING_BASELINE_FILE', str(tmp_path / 'baseline.json'))
    monkeypatch.setattr(dsh_saving, 'DSH_SAVING_REPORT_FILE', str(tmp_path / 'report.json'))
    monkeypatch.setattr(dsh_saving, 'DSH_SAVING_TRANSCRIPTS', str(tmp_path / 'proj' / '**' / '*.jsonl'))
    monkeypatch.setenv('DSH_HOME', str(tmp_path / 'dsh-home'))
    return tmp_path


def test_account_same_matches_drift_not_gaps():
    """Сдвиг недельного сброса на час — тот же аккаунт; на дни — нет; пустое — ни с чем."""
    assert dsh_saving_account_same('1795777200', '1795780800')            # случай из жизни: 27.11 13:00 → 14:00
    assert dsh_saving_account_same(1795777200, '1795777200')              # число и строка
    assert not dsh_saving_account_same('1795777200', str(1795777200 + 86400))   # граница дрейфа — уже не вместе
    assert not dsh_saving_account_same('1795777200', str(1795777200 + 3 * 86400))
    assert not dsh_saving_account_same('', '')
    assert not dsh_saving_account_same('', '1795777200')
    assert not dsh_saving_account_same(None, '1795777200')
    assert not dsh_saving_account_same(0, 0)


def test_calibrate_reuses_previous_after_reset_drift(sandbox):
    """Сброс сдвинулся на час — калибровка берётся из прошлой записи того же аккаунта, stale."""
    _snapshot('sess-a', S7 + HOUR, S5, five_used=3, seven_used=3)   # свежее 5-часовое окно, used < MIN_PCT
    old = str(S7)
    _write(_calibration_path(), {old: {'account': old, 'at': time.time() - 1000,
                                       'five_hour': {'per_pct': 250000.0, 'used_pct': 55, 'units': 13_750_000},
                                       'seven_day': {'per_pct': 9_000_000.0, 'used_pct': 30, 'units': 270_000_000}}})
    result = dsh_saving_calibrate('sess-a')
    assert result['account'] == str(S7 + HOUR)                     # новая запись — под новым ключом
    five = result['five_hour']
    assert five['stale'] and five['per_pct'] == 250000.0 and five['used_pct'] == 3
    store = _read(_calibration_path())
    assert old in store and str(S7 + HOUR) in store                # прошлую не выбросила, свежая добавлена


def test_calibrate_days_apart_is_new_account(sandbox):
    """Сброс расходится на три дня — другой аккаунт: калибровка из старой записи не берётся."""
    _snapshot('sess-a', S7 + 3 * DAY, S5, five_used=3, seven_used=3)
    old = str(S7)
    _write(_calibration_path(), {old: {'account': old, 'at': time.time() - 1000,
                                       'five_hour': {'per_pct': 250000.0, 'used_pct': 55, 'units': 13_750_000}}})
    result = dsh_saving_calibrate('sess-a')
    assert result['five_hour']['per_pct'] is None
    assert not result['five_hour'].get('stale')


def test_baseline_survives_reset_drift(sandbox):
    """Точка отсчёта метки не сбрасывается при сдвиге недельного сброса на час."""
    _snapshot('sess-a', S7 + HOUR, S5, five_used=3, seven_used=3)
    _write(_baseline_path(), {'sess-a': {'window': f'{S7}/{S5}', 'saved': 500000, 'at': time.time() - 60}})
    row = dsh_saving_refresh('sess-a')
    assert row['window_saved'] == 0                                # нового прироста относительно точки нет
    mark = _read(_baseline_path())['sess-a']
    assert mark['saved'] == 500000 and mark['window'] == f'{S7 + HOUR}/{S5}'


def test_baseline_resets_on_real_account_change(sandbox):
    """Сброс сдвинулся на три дня — другой аккаунт: точка отсчёта обнуляется."""
    _snapshot('sess-a', S7 + 3 * DAY, S5, five_used=3, seven_used=3)
    _write(_baseline_path(), {'sess-a': {'window': f'{S7}/{S5}', 'saved': 500000, 'at': time.time() - 60}})
    row = dsh_saving_refresh('sess-a')
    mark = _read(_baseline_path())['sess-a']
    assert mark['window'] == f'{S7 + 3 * DAY}/{S5}' and mark['saved'] == row['saved'] == 0


def test_drifted_session_not_foreign(sandbox):
    """Сессия со сдвигом сброса на час — свой аккаунт: её транскрипты в калибровке; с разницей в дни — чужая."""
    _snapshot('sess-a', S7, S5, five_used=50, seven_used=50)
    _snapshot('sess-b', S7 + HOUR, S5, five_used=50, seven_used=50)
    _snapshot('sess-c', S7 + 3 * DAY, S5, five_used=50, seven_used=50)
    _transcript(sandbox, 'sess-b', 1000, 100)                     # 1500 взвешенных токенов — свои
    _transcript(sandbox, 'sess-c', 2000, 0)                       # чужой аккаунт — не считается
    result = dsh_saving_calibrate('sess-a')
    assert result['five_hour']['units'] == pytest.approx(1500)
    assert result['five_hour']['per_pct'] == pytest.approx(30)    # 1500 / 50%


def _snapshot(session, seven, five, five_used=40.0, seven_used=40.0):
    """Снимок `rate_limits` сессии — как его кладёт статусная строка."""
    _write(os.path.join(os.path.expanduser(dsh_saving.DSH_SAVING_RATE_DIR), f'{session}.json'),
           {'session_id': session, 'at': time.time(), 'rate_limits': {
               'seven_day': {'resets_at': seven, 'used_percentage': seven_used},
               'five_hour': {'resets_at': five, 'used_percentage': five_used}}})


def _transcript(tmp_path, session, input_tokens, output_tokens):
    """Транскрипт сессии с одним ответом в текущих окнах."""
    path = tmp_path / 'proj' / 'sub' / f'{session}.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        'type': 'assistant', 'timestamp': datetime.fromtimestamp(NOW - 60, timezone.utc).isoformat(),
        'message': {'id': f'{session}-m1', 'usage': {'input_tokens': input_tokens,
                                                     'output_tokens': output_tokens,
                                                     'cache_read_input_tokens': 0,
                                                     'cache_creation_input_tokens': 0}}}) + '\n',
        encoding='utf-8')


def _calibration_path():
    return os.path.expanduser(dsh_saving.DSH_SAVING_CALIBRATION_FILE)


def _baseline_path():
    return os.path.expanduser(dsh_saving.DSH_SAVING_BASELINE_FILE)


def _write(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as handle:
        json.dump(value, handle)


def _read(path):
    with open(path, encoding='utf-8') as handle:
        return json.load(handle)
