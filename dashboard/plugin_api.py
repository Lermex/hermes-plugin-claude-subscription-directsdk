"""Claude Accounts dashboard tab — backend, mounted at /api/plugins/claude-subscription-directsdk-experimental/.

Read-only view of the provider's pooled Claude logins (see ../accounts.py) plus the one write
the operator needs from a dashboard: clearing a bench early. Plan limits come from Anthropic's
``/api/oauth/usage`` with each account's own token, read from its ``CLAUDE_CONFIG_DIR`` and never
refreshed or written (Claude Code owns the grant; its refresh tokens are single-use). Login state
is likewise read from the credential file: this page polls, and a polled ``claude auth status``
can exit mid-refresh and sign the account out.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException

log = logging.getLogger(__name__)
router = APIRouter()

PLUGIN_ID = "claude-subscription-directsdk-experimental"
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
USAGE_TTL = 60.0
STATUS_TTL = 120.0

_usage_cache: Dict[str, tuple] = {}
_status_cache: Dict[str, tuple] = {}
_lock = threading.Lock()


_PKG = "hermes_dashboard_plugin_pkg_claude_subscription_directsdk"


def _module(name: str):
    """The plugin's own module, loaded as a submodule of the plugin root (so its relative imports
    work), under a package name separate from the provider loader's hashed one."""
    import importlib
    if _PKG not in sys.modules:
        spec = importlib.util.spec_from_file_location(_PKG, PLUGIN_ROOT / "__init__.py",
                                                      submodule_search_locations=[str(PLUGIN_ROOT)])
        pkg = importlib.util.module_from_spec(spec)
        sys.modules[_PKG] = pkg
        try:
            spec.loader.exec_module(pkg)
        except Exception:
            sys.modules.pop(_PKG, None)
            raise
    return sys.modules[_PKG] if name == "__init__" else importlib.import_module(f"{_PKG}.{name}")


def _usage_for(config_dir: str) -> Dict[str, Any]:
    """``{windows: [...], details: [...], unavailable: str|None}`` for one login; cached briefly."""
    now = time.time()
    with _lock:
        hit = _usage_cache.get(config_dir)
        if hit and now - hit[0] < USAGE_TTL:
            return hit[1]
    result = _fetch_usage(config_dir)
    with _lock:
        _usage_cache[config_dir] = (now, result)
    return result


def _fetch_usage(config_dir: str) -> Dict[str, Any]:
    accounts = _module("accounts")
    provider = _module("__init__")
    creds = accounts.read_oauth_credentials(config_dir)
    if not creds:
        return {"windows": [], "details": [], "unavailable": "no login in this directory"}
    expires = creds.get("expiresAt")
    if isinstance(expires, (int, float)) and expires / 1000 < time.time():
        return {"windows": [], "details": [], "unavailable": "token expired — next request through Claude Code refreshes it"}
    import httpx
    headers = {"Authorization": f"Bearer {creds['accessToken']}", "anthropic-beta": "oauth-2025-04-20",
               "Accept": "application/json", "User-Agent": "claude-code/2.1.0"}
    try:
        with httpx.Client(timeout=8.0) as client:
            response = client.get(provider.USAGE_URL, headers=headers)
    except httpx.HTTPError:
        return {"windows": [], "details": [], "unavailable": "could not reach the usage API"}
    if response.status_code in (401, 403):
        return {"windows": [], "details": [], "unavailable": "token rejected"}
    if not 200 <= response.status_code < 300:
        return {"windows": [], "details": [], "unavailable": f"usage API returned HTTP {response.status_code}"}
    try:
        payload = response.json()
    except ValueError:
        return {"windows": [], "details": [], "unavailable": "usage API returned non-JSON"}
    windows, details = provider.usage_windows(payload if isinstance(payload, dict) else {})
    return {
        "windows": [{"label": w.label, "used_percent": w.used_percent,
                     "reset_at": w.reset_at.timestamp() if w.reset_at else None} for w in windows],
        "details": list(details), "unavailable": None,
    }


def _login_status(config_dir: str) -> Dict[str, Any]:
    now = time.time()
    with _lock:
        hit = _status_cache.get(config_dir)
        if hit and now - hit[0] < STATUS_TTL:
            return hit[1]
    # File read only. Never `claude auth status` here: that short-lived process can rotate the
    # single-use refresh token and exit before saving it, logging the account out (CLI <= 2.1.300).
    status = _module("accounts").login_state(config_dir)
    with _lock:
        _status_cache[config_dir] = (now, status)
    return status


def _entry_row(entry, config_dir: str, current_id: Optional[str]) -> Dict[str, Any]:
    reset_at = entry.last_error_reset_at
    if isinstance(reset_at, str):
        try:
            reset_at = datetime.fromisoformat(reset_at.replace("Z", "+00:00")).timestamp()
        except ValueError:
            reset_at = None
    benched = bool(entry.last_status and entry.last_status != "ok" and (reset_at is None or reset_at > time.time()))
    login = _login_status(config_dir)
    return {
        "id": entry.id, "label": entry.label, "priority": entry.priority, "config_dir": config_dir,
        "current": entry.id == current_id, "request_count": entry.request_count,
        "status": entry.last_status or "ok", "benched": benched,
        "error_reason": entry.last_error_reason, "error_message": (entry.last_error_message or "")[:200],
        "error_reset_at": reset_at, "status_at": entry.last_status_at,
        "logged_in": login["logged_in"], "plan": login["plan"], "login_error": login.get("error") or "",
        "usage": _usage_for(config_dir) if login["logged_in"] else {"windows": [], "details": [], "unavailable": "logged out"},
    }


@router.get("/accounts")
def list_accounts() -> Dict[str, Any]:
    from agent.credential_pool import load_pool
    accounts = _module("accounts")
    pool = load_pool(PLUGIN_ID)
    current_id = getattr(pool, "_current_id", None)
    rows: List[Dict[str, Any]] = []
    for entry in pool.entries():
        config_dir = accounts.account_dir(entry.access_token)
        if config_dir:
            rows.append(_entry_row(entry, config_dir, current_id))
    default = None
    if not rows:
        from agent.anthropic_credentials import claude_code_credentials_path
        config_dir = str(Path(claude_code_credentials_path()).parent)
        login = _login_status(config_dir)
        default = {"config_dir": config_dir, "logged_in": login["logged_in"], "plan": login["plan"],
                   "usage": _usage_for(config_dir) if login["logged_in"] else {"windows": [], "details": [], "unavailable": "logged out"}}
    return {"provider": PLUGIN_ID, "accounts": rows, "default_login": default, "fetched_at": time.time(),
            "add_hint": f"hermes auth add {PLUGIN_ID} --label <name>"}


@router.post("/accounts/{credential_id}/clear-bench")
def clear_bench(credential_id: str) -> Dict[str, Any]:
    from agent.credential_pool import load_pool
    pool = load_pool(PLUGIN_ID)
    cleared = pool.reset_status(credential_id)
    if cleared is None:
        raise HTTPException(status_code=404, detail="no such pooled credential")
    return {"ok": True, "id": cleared.id, "label": cleared.label}


@router.post("/accounts/refresh")
def refresh() -> Dict[str, Any]:
    with _lock:
        _usage_cache.clear()
        _status_cache.clear()
    return {"ok": True}


@router.delete("/accounts/{credential_id}")
def remove_account(credential_id: str) -> Dict[str, Any]:
    removed = _module("accounts").remove_account(credential_id)
    if removed is None:
        raise HTTPException(status_code=404, detail="no such pooled credential")
    with _lock:
        _usage_cache.clear()
        _status_cache.clear()
    return {"ok": True, "id": removed.id, "label": removed.label}


# --- Add an account from the page: the official `claude auth login` on a pty -------------------
#
# Same shape as the vault form: the browser posts the pasted OAuth code straight to this process,
# which types it into the CLI's own prompt. The code never reaches chat, logs or the model; the
# OAuth grant lands in the new login's CLAUDE_CONFIG_DIR exactly as a terminal login would.

import os
import pty
import re
import select
import subprocess
import uuid

LOGIN_TTL = 600.0
_logins: Dict[str, "_Login"] = {}
_URL_RE = re.compile(r"https://claude\.com/\S+")
_ANSI_RE = re.compile(r"\x1b\][^\x07]*\x07|\x1b\[[0-9;]*[A-Za-z]")


class _Login:
    def __init__(self, label: str):
        accounts = _module("accounts")
        setup = _module("directsdk_setup")
        self.id = uuid.uuid4().hex[:12]
        self.label = label
        self.created = time.time()
        self.config_dir = accounts.new_account_dir()
        self.url: Optional[str] = None
        self.state = "starting"  # starting | awaiting_code | verifying | done | failed
        self.error = ""
        self.output = ""
        self.entry_id: Optional[str] = None
        env = setup._child_env(dict(os.environ))
        env["CLAUDE_CONFIG_DIR"] = str(self.config_dir)
        env["TERM"] = "dumb"
        env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        command = setup._resolve(None, env)
        if command is None:
            raise HTTPException(status_code=503, detail="Claude Code CLI is not installed on the dashboard host")
        self.master, slave = pty.openpty()
        self.proc = subprocess.Popen(command + ["auth", "login", "--claudeai"], stdin=slave, stdout=slave, stderr=slave,
                                     env=env, close_fds=True)
        os.close(slave)
        self.thread = threading.Thread(target=self._pump, daemon=True)
        self.thread.start()

    def _pump(self):
        accounts = _module("accounts")
        while True:
            r, _, _ = select.select([self.master], [], [], 0.5)
            if r:
                try:
                    chunk = os.read(self.master, 4096)
                except OSError:
                    chunk = b""
                if not chunk:
                    break
                text = _ANSI_RE.sub("", chunk.decode("utf-8", "replace"))
                self.output = (self.output + text)[-4000:]
                if self.url is None:
                    m = _URL_RE.search(self.output.replace("\r", "").replace("\n", ""))
                    if m:
                        self.url = m.group(0).rstrip(".,")
                if self.state == "starting" and "Paste code here" in self.output:
                    self.state = "awaiting_code"
                if self.state == "verifying" and "Invalid code" in self.output.split("Paste code here")[-1]:
                    self.state = "awaiting_code"
                    self.error = "Invalid code — copy the full code from the Claude page and try again."
                    self.output = ""
            if self.proc.poll() is not None:
                break
        rc = self.proc.returncode
        status = accounts.auth_status(self.config_dir)
        if status["logged_in"]:
            entry = accounts.register_dir(self.config_dir, self.label or f"claude-{status['plan'] or 'subscription'}")
            self.entry_id = entry.id
            self.state = "done"
            with _lock:
                _status_cache.clear()
        else:
            self.state = "failed"
            self.error = self.error or f"`claude auth login` exited with {rc} without a login"
            accounts._remove_dir(self.config_dir)
        try:
            os.close(self.master)
        except OSError:
            pass

    def submit(self, code: str):
        if self.state != "awaiting_code":
            raise HTTPException(status_code=409, detail=f"login is {self.state}")
        self.state = "verifying"
        self.error = ""
        os.write(self.master, (code.strip() + "\r").encode())

    def cancel(self):
        if self.proc.poll() is None:
            self.proc.kill()

    def view(self) -> Dict[str, Any]:
        return {"id": self.id, "label": self.label, "state": self.state, "url": self.url, "error": self.error,
                "entry_id": self.entry_id, "expires_in": max(0, int(self.created + LOGIN_TTL - time.time()))}


def _reap():
    now = time.time()
    for key, login in list(_logins.items()):
        if now - login.created > LOGIN_TTL or login.state in ("done", "failed") and now - login.created > 120:
            login.cancel()
            _logins.pop(key, None)


def _login(login_id: str) -> "_Login":
    _reap()
    login = _logins.get(login_id)
    if login is None:
        raise HTTPException(status_code=404, detail="login session expired or unknown")
    return login


@router.post("/logins")
def start_login(body: Dict[str, Any]) -> Dict[str, Any]:
    _reap()
    label = str(body.get("label") or "").strip()[:60]
    login = _Login(label)
    _logins[login.id] = login
    deadline = time.time() + 15
    while login.state == "starting" and login.proc.poll() is None and time.time() < deadline:
        time.sleep(0.2)
    return login.view()


@router.get("/logins/{login_id}")
def login_status(login_id: str) -> Dict[str, Any]:
    return _login(login_id).view()


@router.post("/logins/{login_id}/code")
def login_code(login_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
    code = str(body.get("code") or "").strip()
    if not code or len(code) > 600:
        raise HTTPException(status_code=400, detail="paste the code from the Claude page")
    login = _login(login_id)
    login.submit(code)
    deadline = time.time() + 25
    while login.state == "verifying" and time.time() < deadline:
        time.sleep(0.25)
    return login.view()


@router.delete("/logins/{login_id}")
def login_cancel(login_id: str) -> Dict[str, Any]:
    login = _logins.pop(login_id, None)
    if login:
        login.cancel()
    return {"ok": True}
