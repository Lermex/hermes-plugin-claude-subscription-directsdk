"""Login state for pollers must come from the credential file, never from `claude auth status`.

A spawned `auth status` is a short-lived Claude Code process: on an expired access token it starts
an OAuth refresh and can exit before saving the rotated single-use refresh token, so the next real
request gets invalid_grant and Claude Code blanks the login (CLI 2.1.288–2.1.300). The dashboard
polls every minute, which is exactly the pattern that signed five pooled accounts out.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import accounts


def _write(config_dir, oauth):
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / '.credentials.json').write_text(json.dumps({'claudeAiOauth': oauth}), encoding='utf-8')


LIVE = {'accessToken': 'sk-ant-oat01-x', 'refreshToken': 'sk-ant-ort01-x', 'expiresAt': 1, 'subscriptionType': 'max'}
BLANKED = {'accessToken': '', 'refreshToken': '', 'expiresAt': 0, 'refreshTokenExpiresAt': 1_800_000_000_000, 'subscriptionType': 'max'}


def test_login_state_reads_live_login(tmp_path):
    _write(tmp_path, LIVE)
    assert accounts.login_state(tmp_path) == {'logged_in': True, 'plan': 'max', 'method': 'claude.ai', 'error': ''}


def test_login_state_names_a_blanked_login(tmp_path):
    _write(tmp_path, BLANKED)
    state = accounts.login_state(tmp_path)
    assert state['logged_in'] is False and state['plan'] == 'max' and 'sign in again' in state['error']


@pytest.mark.parametrize('payload,error', [(None, 'no credential file'), ('{not json', 'unreadable credential file'), ('{}', 'no claude.ai login')])
def test_login_state_without_a_login(tmp_path, payload, error):
    if payload is not None:
        (tmp_path / '.credentials.json').write_text(payload, encoding='utf-8')
    state = accounts.login_state(tmp_path)
    assert state == {'logged_in': False, 'plan': '', 'method': '', 'error': error}


def test_login_state_never_spawns_claude(tmp_path, monkeypatch):
    _write(tmp_path, LIVE)

    def boom(*_a, **_k):
        raise AssertionError('login_state must not run a Claude Code process')
    monkeypatch.setattr(subprocess, 'run', boom)
    monkeypatch.setattr(subprocess, 'Popen', boom)
    assert accounts.login_state(tmp_path)['logged_in'] is True


def test_any_pooled_login_reads_files_not_processes(tmp_path, monkeypatch):
    live, dead = tmp_path / 'live', tmp_path / 'dead'
    _write(dead, BLANKED)
    _write(live, dict(LIVE, subscriptionType='team'))
    monkeypatch.setattr(accounts, 'pool_accounts', lambda: [(object(), str(dead)), (object(), str(live))])
    monkeypatch.setattr(accounts, 'auth_status', lambda *_a, **_k: pytest.fail('any_pooled_login spawned claude auth status'))
    assert accounts.any_pooled_login()['plan'] == 'team'


def test_dashboard_login_status_reads_files_not_processes(tmp_path, monkeypatch):
    pytest.importorskip('fastapi')
    import importlib.util
    spec = importlib.util.spec_from_file_location('plugin_api_under_test', Path(__file__).resolve().parents[1] / 'dashboard' / 'plugin_api.py')
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    _write(tmp_path, BLANKED)
    monkeypatch.setattr(api, '_module', lambda name: accounts if name == 'accounts' else pytest.fail(name))
    monkeypatch.setattr(accounts, 'auth_status', lambda *_a, **_k: pytest.fail('dashboard spawned claude auth status'))
    api._status_cache.clear()
    state = api._login_status(str(tmp_path))
    assert state['logged_in'] is False and 'sign in again' in state['error']
