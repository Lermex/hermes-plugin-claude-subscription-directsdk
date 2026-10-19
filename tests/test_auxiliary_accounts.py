"""Offline regressions for the real Hermes auxiliary/compression routing seam."""
import os
import sys

import pytest

import accounts
from conftest import PLUGIN_NAME


def add_accounts(tmp_path):
    from agent.credential_pool import AUTH_TYPE_API_KEY, SOURCE_MANUAL, PooledCredential, load_pool
    pool = load_pool(PLUGIN_NAME)
    for name in ('limited', 'available'):
        pool.add_entry(PooledCredential(
            provider=PLUGIN_NAME, id=name, label=name, auth_type=AUTH_TYPE_API_KEY,
            priority=0, source=SOURCE_MANUAL,
            access_token=accounts.account_ref(tmp_path / name), base_url=accounts.BASE_URL))
    return pool


def test_auxiliary_resolver_selects_pooled_account(profile, tmp_path, monkeypatch):
    from agent.auxiliary_client import resolve_provider_client
    add_accounts(tmp_path)
    # A real executable satisfies core discovery; no child or network request is made.
    monkeypatch.setenv('CLAUDE_SUBSCRIPTION_DIRECTSDK_COMMAND', sys.executable)
    client, model = resolve_provider_client(PLUGIN_NAME, 'sonnet')
    try:
        assert client is not None
        assert client.api_key == accounts.account_ref(tmp_path / 'limited')
    finally:
        if client is not None:
            client.close()


def test_nonempty_exhausted_pool_never_uses_ambient_login(profile, tmp_path):
    pool = add_accounts(tmp_path)
    for entry in pool.entries():
        pool.mark_exhausted_and_rotate(status_code=429, api_key_hint=entry.runtime_api_key,
                                      error_context={'reason': 'usage_limit_reached', 'reset_at': 4102444800})
    with pytest.raises(RuntimeError, match='No available pooled Claude account'):
        profile.create_client(api_key=PLUGIN_NAME)


@pytest.mark.parametrize('provider_mode', ['explicit', 'auto'])
def test_compression_call_rotates_without_provider_fallback(profile, tmp_path, monkeypatch, provider_mode):
    from agent import auxiliary_client as aux
    from agent.credential_pool import load_pool
    from test_directsdk import FAKE
    import json

    add_accounts(tmp_path)
    capture = tmp_path / 'used-accounts.jsonl'
    script = tmp_path / 'native.py'
    probe = (
        "directory = os.environ.get('CLAUDE_CONFIG_DIR', 'ambient')\n"
        "open(os.environ['ACCOUNT_CAPTURE'], 'a').write(json.dumps(directory) + '\\n')\n"
        "if not directory.endswith('available'):\n"
        " os.environ['NATIVE_ERROR'] = \"rate_limit:You've hit your weekly limit · resets Jan 1, 3am (UTC)\"\n"
    )
    # Summary calls have no tools. Keep the existing fake native transport, remove only its tool fixture assertion.
    script.write_text(FAKE.replace('rows=[]', probe + 'rows=[]').replace(
        "assert wire['tools'][0]['description'].endswith('TAIL')", 'pass'), encoding='utf-8')
    factory = profile.create_client

    def offline_factory(**kwargs):
        kwargs.update(command=sys.executable, args=[str(script)], env={
            'PATH': os.defpath, 'HOME': str(tmp_path), 'ACCOUNT_CAPTURE': str(capture)})
        return factory(**kwargs)

    monkeypatch.setattr(profile, 'create_client', offline_factory)
    monkeypatch.setenv('CLAUDE_SUBSCRIPTION_DIRECTSDK_COMMAND', sys.executable)
    aux._evict_cached_clients(PLUGIN_NAME)
    aux._evict_cached_clients('auto')

    def no_fallback(*args, **kwargs):
        pytest.fail('Compression must recover within the same provider/account pool')

    monkeypatch.setattr(aux, '_try_configured_fallback_chain', no_fallback)
    runtime = dict(provider=PLUGIN_NAME, model='sonnet', api_mode='chat_completions',
                   base_url=accounts.BASE_URL, api_key=accounts.account_ref(tmp_path / 'limited'))
    response = aux.call_llm(task='compression', provider=PLUGIN_NAME if provider_mode == 'explicit' else 'auto',
                            model='sonnet', main_runtime=runtime,
                            messages=[{'role': 'user', 'content': 'Summarize this conversation.'}], timeout=10)
    assert response.choices[0].message.content == 'hello\n'
    used = [json.loads(line) for line in capture.read_text().splitlines()]
    assert used[0] == str(tmp_path / 'limited')
    assert used[-1] == str(tmp_path / 'available')
    assert set(used) == {str(tmp_path / 'limited'), str(tmp_path / 'available')}
    entries = {entry.id: entry for entry in load_pool(PLUGIN_NAME).entries()}
    assert entries['limited'].last_status == 'exhausted'
    assert entries['limited'].last_error_reason == 'usage_limit_reached'
    assert entries['limited'].last_error_reset_at > 0
    assert entries['available'].last_status != 'exhausted'


def test_explicit_main_account_is_not_reselected(profile, tmp_path):
    add_accounts(tmp_path)
    ref = accounts.account_ref(tmp_path / 'available')
    client = profile.create_client(api_key=ref)
    try:
        assert client.api_key == ref
    finally:
        client.close()


def test_empty_pool_preserves_single_login(profile):
    client = profile.create_client(api_key=PLUGIN_NAME)
    try:
        assert client.api_key == 'external-process'
    finally:
        client.close()


@pytest.mark.parametrize('async_mode', [False, True])
def test_cached_aux_client_reselects_and_preserves_quota_reset(profile, tmp_path, monkeypatch, async_mode):
    import asyncio
    from agent.credential_pool import load_pool

    add_accounts(tmp_path)
    client = profile.create_client(api_key=PLUGIN_NAME)
    error_type = sys.modules[type(client).__module__].ClaudeAPIError
    calls = []
    reset = 4102444800

    def attempt(key, **kwargs):
        calls.append(key)
        if key.endswith('limited'):
            raise error_type('quota exhausted', 429,
                             {'error': {'type': 'usage_limit_reached', 'resets_at': reset}})
        return 'summary'

    monkeypatch.setattr(client, '_create_once', attempt)
    try:
        for _ in range(2):
            result = asyncio.run(client._acreate(model='sonnet')) if async_mode else client.create(model='sonnet')
            assert result == 'summary'
        assert calls == [accounts.account_ref(tmp_path / name) for name in ('limited', 'available', 'available')]
        entry = next(e for e in load_pool(PLUGIN_NAME).entries() if e.id == 'limited')
        assert entry.last_error_reset_at == reset
        assert entry.last_error_reason == 'usage_limit_reached'
        # Account selection is request-local, not a shared mutable credential field.
        assert client.api_key == accounts.account_ref(tmp_path / 'limited')
    finally:
        client.close()


@pytest.mark.parametrize('status,error_kind', [(429, 'rate_limit_error'), (500, 'api_error'), (400, 'invalid_request_error')])
def test_non_quota_errors_do_not_rotate(profile, tmp_path, monkeypatch, status, error_kind):
    from agent.credential_pool import load_pool
    add_accounts(tmp_path)
    client = profile.create_client(api_key=PLUGIN_NAME)
    error_type = sys.modules[type(client).__module__].ClaudeAPIError
    error = error_type('not a subscription quota', status, {'error': {'type': error_kind}})
    calls = []

    def attempt(key, **kwargs):
        calls.append(key)
        raise error

    monkeypatch.setattr(client, '_create_once', attempt)
    try:
        with pytest.raises(error_type) as caught:
            client.create(model='sonnet')
        assert caught.value is error
        assert len(calls) == 1
        assert all(e.last_status != 'exhausted' for e in load_pool(PLUGIN_NAME).entries())
    finally:
        client.close()


def test_all_quota_limited_accounts_are_tried_once(profile, tmp_path, monkeypatch):
    from agent.credential_pool import load_pool
    add_accounts(tmp_path)
    client = profile.create_client(api_key=PLUGIN_NAME)
    error_type = sys.modules[type(client).__module__].ClaudeAPIError
    calls = []

    def attempt(key, **kwargs):
        calls.append(key)
        raise error_type('quota exhausted', 429,
                         {'error': {'type': 'usage_limit_reached', 'resets_at': 4102444800}})

    monkeypatch.setattr(client, '_create_once', attempt)
    try:
        with pytest.raises(error_type):
            client.create(model='sonnet')
        assert calls == [accounts.account_ref(tmp_path / name) for name in ('limited', 'available')]
        assert all(e.last_error_reset_at == 4102444800 for e in load_pool(PLUGIN_NAME).entries())
        # Reusing a cached client cannot escape to an unpooled/default login.
        with pytest.raises(RuntimeError, match='No available pooled Claude account'):
            client.create(model='sonnet')
        assert len(calls) == 2
    finally:
        client.close()
