"""Several Claude subscriptions: pooled logins rotate on a plan limit and bench until the reset."""
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import accounts
import directsdk
from conftest import PLUGIN_NAME
from test_directsdk import FAKE

REQUEST = dict(model='sonnet', messages=[{'role': 'user', 'content': 'go'}],
               tools=[{'type': 'function', 'function': {'name': 'probe', 'description': 'TAIL',
                                                        'parameters': {'type': 'object', 'properties': {}}}}])


@pytest.mark.parametrize('message,expected', [
    ("You've hit your weekly limit · resets Oct 20, 3am (UTC)", datetime(2026, 10, 20, 3, tzinfo=timezone.utc)),
    ("You've hit your session limit · resets 3:45pm (UTC)", datetime(2026, 10, 17, 15, 45, tzinfo=timezone.utc)),
    ("You've hit your session limit · resets 9am (UTC)", datetime(2026, 10, 18, 9, tzinfo=timezone.utc)),  # already past -> tomorrow
    ("You've hit your limit · resets 7pm (America/Los_Angeles)", datetime(2026, 10, 18, 2, tzinfo=timezone.utc)),
])
def test_reset_time_parses_native_grammar(message, expected):
    now = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
    assert accounts.reset_epoch_from_message(message, now=now) == expected.timestamp()


def test_non_limit_text_has_no_reset_and_no_body():
    assert accounts.reset_epoch_from_message('API Error: Overloaded') is None
    assert directsdk.native_limit_body('API Error: Overloaded') is None
    body = directsdk.native_limit_body("You've hit your weekly limit · resets Oct 20, 3am (UTC)")
    assert body['error']['type'] == 'usage_limit_reached' and body['error']['resets_at'] > time.time()


def _native(tmp_path, native_error, create_client=directsdk.Client, **kw):
    native = tmp_path / 'native.py'
    native.write_text(FAKE)
    return create_client(command=[sys.executable, str(native)],
                         env={'PATH': os.defpath, 'HOME': str(tmp_path), 'NATIVE_ERROR': native_error}, **kw)


def test_plan_limit_rotates_the_credential_and_benches_until_reset(profile, tmp_path):
    """Core's classifier sees a 429 usage-limit with a reset: rotate, fall back, bench until then."""
    from agent.error_classifier import FailoverReason, classify_api_error
    from agent.agent_runtime_helpers import extract_api_error_context
    client = _native(tmp_path, "rate_limit:You've hit your weekly limit · resets Oct 20, 3am (UTC)", profile.create_client)
    try:
        with pytest.raises(RuntimeError) as raised:  # the loader's own ClaudeAPIError class
            client.create(**REQUEST)
    finally:
        client.close()
    assert raised.value.status_code == 429 and raised.value.body['error']['type'] == 'usage_limit_reached'
    verdict = classify_api_error(raised.value, provider=profile.name, model='sonnet')
    assert verdict.status_code == 429 and verdict.reason == FailoverReason.rate_limit
    assert verdict.should_rotate_credential and verdict.should_fallback
    context = extract_api_error_context(raised.value)
    assert context['reason'] == 'usage_limit_reached'
    assert context['reset_at'] > time.time()


def test_pooled_reference_selects_the_login_directory(tmp_path):
    capture = tmp_path / 'dirs.jsonl'
    probe = ("open(os.environ['DIR_CAPTURE'], 'a').write(json.dumps({k: os.environ[k] "
             "for k in ('CLAUDE_CONFIG_DIR',) if k in os.environ}) + '\\n')\n")
    script = tmp_path / 'native.py'
    script.write_text(FAKE.replace('rows=[]', probe + 'rows=[]'), encoding='utf-8')
    login = tmp_path / 'login-a'
    env = {'PATH': os.defpath, 'HOME': str(tmp_path), 'DIR_CAPTURE': str(capture), 'CLAUDE_CONFIG_DIR': str(tmp_path / 'inherited')}
    client = directsdk.Client(command=[sys.executable, str(script)], env=env, api_key=accounts.account_ref(login))
    assert client.api_key == accounts.account_ref(login)  # core attributes failures to this pool row
    try:
        assert client.create(**REQUEST).choices[0].message.content == 'hello\n'
    finally:
        client.close()
    assert json.loads(capture.read_text().splitlines()[0]) == {'CLAUDE_CONFIG_DIR': str(login)}
    plain = directsdk.Client(command=[sys.executable, str(script)], env=env, api_key='external-process')
    assert plain.api_key == 'external-process'


def test_runtime_resolves_from_the_pool_and_rotates(profile, tmp_path, monkeypatch):
    """The pool rung of core's runtime ladder hands the agent the first login; a 429 moves to the next."""
    from agent.credential_pool import AUTH_TYPE_API_KEY, SOURCE_MANUAL, PooledCredential, load_pool
    from hermes_cli.runtime_provider import resolve_runtime_provider
    pool = load_pool(PLUGIN_NAME)
    for name in ('work', 'home'):
        pool.add_entry(PooledCredential(provider=PLUGIN_NAME, id=name, label=name, auth_type=AUTH_TYPE_API_KEY, priority=0,
                                        source=SOURCE_MANUAL, access_token=accounts.account_ref(tmp_path / name), base_url=accounts.BASE_URL))
    runtime = resolve_runtime_provider(requested=PLUGIN_NAME, target_model='sonnet')
    assert runtime['provider'] == PLUGIN_NAME and runtime['api_key'] == accounts.account_ref(tmp_path / 'work')
    assert runtime['credential_pool'] is not None and runtime['base_url'] == accounts.BASE_URL
    pool = runtime['credential_pool']
    nxt = pool.mark_exhausted_and_rotate(status_code=429, api_key_hint=runtime['api_key'],
                                         error_context={'reason': 'usage_limit_reached', 'reset_at': time.time() + 3600})
    assert nxt.id == 'home'
    benched = next(e for e in pool.entries() if e.id == 'work')
    assert benched.last_status == 'exhausted' and benched.last_error_reset_at > time.time() + 3000


def test_auth_add_status_logout_round_trip(profile, tmp_path, monkeypatch):
    from agent.credential_pool import load_pool
    calls = []

    def fake_login(config_dir, command=None, env=None):
        calls.append(('login', str(config_dir)))
        Path(config_dir, '.credentials.json').write_text(json.dumps({'claudeAiOauth': {'accessToken': 'x'}}))
        return 0

    def fake_status(config_dir, command=None, env=None, timeout=20):
        logged = Path(config_dir, '.credentials.json').exists()
        return {'logged_in': logged, 'plan': 'max' if logged else '', 'method': 'claude.ai', 'error': ''}

    monkeypatch.setattr(accounts, 'login', fake_login)
    monkeypatch.setattr(accounts, 'auth_status', fake_status)
    monkeypatch.setattr(accounts, 'logout', lambda *a, **k: calls.append(('logout', str(a[0]))))
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: False) if hasattr(sys.stdin, 'isatty') else None
    assert accounts.auth_handler('add', SimpleNamespace(label='work')) is True
    assert accounts.auth_handler('add', SimpleNamespace(label=None)) is True
    entries = load_pool(PLUGIN_NAME).entries()
    assert [e.label for e in entries] == ['work', 'claude-max-2']
    assert all(accounts.account_dir(e.access_token) and Path(accounts.account_dir(e.access_token)).is_dir() for e in entries)
    assert all(accounts.account_dir(e.access_token).startswith(str(accounts.accounts_root())) for e in entries)
    assert 'accessToken' not in json.dumps([e.to_dict() for e in entries])  # the pool row is a reference, never a token
    assert accounts.auth_handler('status', SimpleNamespace()) is True
    assert profile.setup_status(command=[str(tmp_path / 'no-such-claude')])['logged_in'] is False  # CLI missing wins
    assert accounts.auth_handler('logout', SimpleNamespace()) is True
    assert load_pool(PLUGIN_NAME).entries() == []
    assert [c[0] for c in calls] == ['login', 'login', 'logout', 'logout']
    assert accounts.auth_handler('refresh', SimpleNamespace()) is False  # core's own path


def test_adopting_an_existing_login_directory(profile, tmp_path, monkeypatch):
    from agent.credential_pool import load_pool
    existing = tmp_path / 'existing-login'
    existing.mkdir()
    (existing / '.credentials.json').write_text(json.dumps({'claudeAiOauth': {'accessToken': 'x'}}))
    monkeypatch.setattr(accounts, 'auth_status', lambda d, **k: {'logged_in': True, 'plan': 'pro', 'method': 'claude.ai', 'error': ''})
    monkeypatch.setenv(accounts.ADOPT_ENV, str(existing))
    assert accounts.auth_handler('add', SimpleNamespace(label='laptop')) is True
    entry = load_pool(PLUGIN_NAME).entries()[0]
    assert accounts.account_dir(entry.access_token) == str(existing)
    with pytest.raises(SystemExit):
        accounts.auth_handler('add', SimpleNamespace(label='dup'))
    accounts.auth_handler('logout', SimpleNamespace())
    assert existing.is_dir()  # adopted directories are never deleted


def test_manifest_and_core_dispatch_route_auth_to_the_plugin(profile):
    from hermes_cli.auth_plugin_providers import plugin_auth_handler, plugin_missing_auth_handler_error
    handler = plugin_auth_handler(PLUGIN_NAME)  # the loader's copy of accounts.auth_handler
    assert handler is not None and handler.__module__.endswith('.accounts')
    assert handler('refresh', SimpleNamespace()) is False
    assert plugin_missing_auth_handler_error(PLUGIN_NAME, 'add') is None
