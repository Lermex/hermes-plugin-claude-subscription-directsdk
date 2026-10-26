"""Multiple Claude subscriptions as Hermes credential-pool entries.

Each pooled entry is one Claude Code login kept in its own ``CLAUDE_CONFIG_DIR`` under
``<hermes home>/plugin-data/<plugin>/accounts/<id>/``. The pool row stores only a reference to
that directory (``claude-config-dir:/path``), never a token: Claude Code owns, refreshes and
stores the OAuth grant exactly as it does for a single login. Hermes core's existing pool
machinery does the rest — the reference travels as the row's ``access_token`` / the agent's
``api_key``, so ``_swap_credential`` rebuilds the client on the next account and
``mark_exhausted_and_rotate`` benches the exhausted one until its reset time.
"""
import json
import os
import re
import subprocess
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

try:
    from .directsdk_setup import PLUGIN_ID, _child_env, _resolve
except ImportError:
    from directsdk_setup import PLUGIN_ID, _child_env, _resolve

REF_PREFIX = "claude-config-dir:"
BASE_URL = "process://" + PLUGIN_ID
ADOPT_ENV = "CLAUDE_SUBSCRIPTION_DIRECTSDK_ADOPT_CONFIG_DIR"


def account_ref(path):
    return REF_PREFIX + os.path.abspath(os.path.expanduser(str(path)))


def account_dir(api_key):
    """The config directory a pool reference names, or ``None`` for anything else (the
    ``external-process`` placeholder core hands single-login runtimes, an empty key, ...)."""
    if not isinstance(api_key, str) or not api_key.startswith(REF_PREFIX):
        return None
    path = api_key[len(REF_PREFIX):].strip()
    return path or None


def accounts_root():
    from plugins.plugin_storage import plugin_data_dir
    root = plugin_data_dir(PLUGIN_ID) / "accounts"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root


def new_account_dir():
    path = accounts_root() / uuid.uuid4().hex[:8]
    path.mkdir(mode=0o700)
    return path


def credentials_file(config_dir):
    return Path(config_dir) / ".credentials.json"


def read_oauth_credentials(config_dir):
    """``claudeAiOauth`` of the directory's credential file, read-only; ``None`` when absent."""
    try:
        payload = json.loads(credentials_file(config_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    creds = payload.get("claudeAiOauth") if isinstance(payload, dict) else None
    return creds if isinstance(creds, dict) and creds.get("accessToken") else None


def login_state(config_dir):
    """``{logged_in, plan, method, error}`` from the directory's credential file alone — no Claude
    Code process. ``claude auth status`` is a short-lived process that can start an OAuth refresh
    and exit before saving the rotated (single-use) refresh token; the next real request then gets
    ``invalid_grant`` and Claude Code blanks the login (CLI 2.1.288–2.1.300). Anything that polls
    login state — the dashboard above all — must use this, never ``auth_status``."""
    try:
        payload = json.loads(credentials_file(config_dir).read_text(encoding="utf-8"))
    except OSError:
        return {"logged_in": False, "plan": "", "method": "", "error": "no credential file"}
    except ValueError:
        return {"logged_in": False, "plan": "", "method": "", "error": "unreadable credential file"}
    creds = payload.get("claudeAiOauth") if isinstance(payload, dict) else None
    if not isinstance(creds, dict):
        return {"logged_in": False, "plan": "", "method": "", "error": "no claude.ai login"}
    # A blanked login keeps every non-secret field and empties both tokens (expiresAt 0).
    if not (creds.get("accessToken") or creds.get("refreshToken")):
        return {"logged_in": False, "plan": str(creds.get("subscriptionType") or ""), "method": "",
                "error": "login cleared — sign in again"}
    return {"logged_in": True, "plan": str(creds.get("subscriptionType") or ""), "method": "claude.ai", "error": ""}


def auth_status(config_dir, command=None, env=None, timeout=20):
    """``claude auth status`` for one directory: ``{logged_in, plan, method}`` (never raises).

    Spawns Claude Code: for one-off CLI commands only. Pollers must use :func:`login_state`."""
    env = dict(env if env is not None else os.environ)
    resolved = _resolve(command, env)
    if resolved is None:
        return {"logged_in": False, "plan": "", "method": "", "error": "claude not installed"}
    child = _child_env(env)
    child["CLAUDE_CONFIG_DIR"] = str(config_dir)
    try:
        run = subprocess.run(resolved + ["auth", "status"], env=child, stdin=subprocess.DEVNULL, capture_output=True,
                             text=True, encoding="utf-8", errors="replace", timeout=timeout)
        auth = json.loads(run.stdout) if run.stdout.strip().startswith("{") else {}
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return {"logged_in": False, "plan": "", "method": "", "error": type(exc).__name__}
    return {"logged_in": auth.get("loggedIn") is True, "plan": str(auth.get("subscriptionType") or ""),
            "method": str(auth.get("authMethod") or ""), "error": ""}


def login(config_dir, command=None, env=None):
    """Run the official ``claude auth login`` into ``config_dir`` on the caller's terminal.

    The CLI prints the sign-in URL and reads the pasted code itself; nothing credential-shaped
    passes through Hermes. Returns the process exit code."""
    env = dict(env if env is not None else os.environ)
    resolved = _resolve(command, env)
    if resolved is None:
        return 127
    child = _child_env(env)
    child["CLAUDE_CONFIG_DIR"] = str(config_dir)
    return subprocess.call(resolved + ["auth", "login", "--claudeai"], env=child)


def logout(config_dir, command=None, env=None, timeout=20):
    env = dict(env if env is not None else os.environ)
    resolved = _resolve(command, env)
    if resolved is None:
        return
    child = _child_env(env)
    child["CLAUDE_CONFIG_DIR"] = str(config_dir)
    try:
        subprocess.run(resolved + ["auth", "logout"], env=child, stdin=subprocess.DEVNULL, capture_output=True,
                       timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        pass


def pool_accounts():
    """``[(entry, config_dir)]`` for every pooled row that names a directory."""
    from agent.credential_pool import load_pool
    rows = []
    for entry in load_pool(PLUGIN_ID).entries():
        path = account_dir(entry.access_token)
        if path:
            rows.append((entry, path))
    return rows


def any_pooled_login(command=None, env=None):
    """``{logged_in, plan}`` of the first pooled account that is signed in, else ``None``.

    Reads the credential files; status probes run often enough that a spawned ``auth status`` per
    account would risk the mid-refresh sign-out described at :func:`login_state`."""
    try:
        rows = pool_accounts()
    except Exception:
        return None
    for _entry, path in rows:
        status = login_state(path)
        if status["logged_in"]:
            return status
    return None


# --- `hermes auth <action> claude-subscription-directsdk-experimental` ----------------------------

def _print(*lines):
    for line in lines:
        print(line)


def _add(args):
    from agent.credential_pool import AUTH_TYPE_API_KEY, SOURCE_MANUAL, PooledCredential, load_pool
    pool = load_pool(PLUGIN_ID)
    adopt = (os.environ.get(ADOPT_ENV) or "").strip()
    if adopt:
        config_dir = Path(os.path.abspath(os.path.expanduser(adopt)))
        if not config_dir.is_dir():
            raise SystemExit(f"{ADOPT_ENV}={adopt!r} is not a directory.")
        if any(os.path.samefile(path, config_dir) for _e, path in pool_accounts() if os.path.isdir(path)):
            raise SystemExit(f"{config_dir} is already in the pool (`hermes auth list {PLUGIN_ID}`).")
    else:
        config_dir = new_account_dir()
        _print(f"Signing in a Claude subscription into {config_dir}",
               "(the official `claude auth login` runs on this terminal; follow its URL / paste prompt)")
        code = login(config_dir)
        if code != 0:
            _remove_dir(config_dir)
            raise SystemExit(f"`claude auth login` exited with {code}; nothing was added.")
    status = auth_status(config_dir)
    if not status["logged_in"]:
        if not adopt:
            _remove_dir(config_dir)
        raise SystemExit(f"Claude Code reports no login in {config_dir} ({status.get('error') or 'loggedIn: false'}); nothing was added.")
    default_label = f"claude-{status['plan'] or 'subscription'}-{len(pool.entries()) + 1}"
    label = (getattr(args, "label", None) or "").strip()
    if not label and sys.stdin.isatty():
        label = input(f"Label (optional, default: {default_label}): ").strip()
    entry = register_dir(config_dir, label or default_label, priority=getattr(args, "priority", None))
    _print(f'Added {PLUGIN_ID} credential #{entry.priority + 1}: "{entry.label}" ({status["plan"] or "subscription"}, {config_dir})',
           f"Hermes rotates to the next pooled login when one hits its session or weekly limit; "
           f"`hermes auth list {PLUGIN_ID}` shows the order.")
    return True


def register_dir(config_dir, label, priority=None):
    """Append ``config_dir`` to the pool as one login (the row stores only the path)."""
    from agent.credential_pool import AUTH_TYPE_API_KEY, SOURCE_MANUAL, PooledCredential, load_pool
    pool = load_pool(PLUGIN_ID)
    entry = PooledCredential(provider=PLUGIN_ID, id=uuid.uuid4().hex[:6], label=label, auth_type=AUTH_TYPE_API_KEY,
                             priority=0, source=SOURCE_MANUAL, access_token=account_ref(config_dir), base_url=BASE_URL)
    entry = pool.add_entry(entry)
    if priority is not None:
        moved = load_pool(PLUGIN_ID).move_entry(entry.id, int(priority))
        if moved is not None:
            entry = moved
    return entry


def remove_account(credential_id):
    """Sign one pooled login out, drop its row and (if the plugin created it) its directory."""
    from agent.credential_pool import load_pool
    pool = load_pool(PLUGIN_ID)
    for index, entry in enumerate(pool.entries(), start=1):
        if entry.id != credential_id:
            continue
        path = account_dir(entry.access_token)
        if path:
            logout(path)
        pool.remove_index(index)
        if path:
            _remove_dir(path)
        return entry
    return None


def _status(_args):
    rows = pool_accounts()
    if not rows:
        try:
            from .directsdk_setup import setup_status
        except ImportError:
            from directsdk_setup import setup_status
        probe = setup_status()
        state = ("logged in" + (f" ({probe['plan']})" if probe["plan"] else "")) if probe["logged_in"] else "logged out"
        _print(f"{PLUGIN_ID}: {state} (single Claude Code login; `hermes auth add {PLUGIN_ID}` pools more subscriptions)")
        return True
    _print(f"{PLUGIN_ID}: {len(rows)} pooled Claude logins")
    for entry, path in rows:
        status = auth_status(path)
        state = ("logged in" + (f", {status['plan']}" if status["plan"] else "")) if status["logged_in"] else (
            "LOGGED OUT" + (f" ({status['error']})" if status.get("error") else ""))
        bench = f"  [{entry.last_status}]" if entry.last_status and entry.last_status != "ok" else ""
        _print(f"  #{entry.priority + 1} {entry.label:<20} {state}{bench}  {path}")
    return True


def _logout(_args):
    from agent.credential_pool import load_pool
    rows = pool_accounts()
    if not rows:
        return False  # single-login mode: core's own logout path
    for entry, path in rows:
        remove_account(entry.id)
        _print(f'Logged out and removed "{entry.label}" ({path})')
    return True


def _remove_dir(path):
    import shutil
    try:
        if Path(path).resolve().is_relative_to(accounts_root().resolve()):
            shutil.rmtree(path, ignore_errors=True)
    except (OSError, ValueError):
        pass


def auth_handler(action, args):
    handler = {"add": _add, "status": _status, "logout": _logout}.get(action)
    return bool(handler(args)) if handler else False


# --- Reset-time parsing for native's limit messages ------------------------------------------------

# "You've hit your weekly limit · resets Oct 20, 3am (UTC)" / "... session limit · resets 3:45pm (America/Los_Angeles)"
_RESETS_RE = re.compile(
    r"resets?\s+(?:(?P<mon>[A-Za-z]{3})\s+(?P<day>\d{1,2}),?\s+)?(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>am|pm)?"
    r"\s*(?:\((?P<tz>[^)]+)\))?", re.IGNORECASE)
_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}


def reset_epoch_from_message(message, now=None):
    """Epoch seconds the limit reopens at, parsed from native's message; ``None`` when it names none."""
    match = _RESETS_RE.search(message or "")
    if not match:
        return None
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(match.group("tz").strip()) if match.group("tz") else None
    except Exception:
        tz = None
    if tz is None:
        from datetime import timezone
        tz = timezone.utc
    now = (now or datetime.now(tz)).astimezone(tz)
    hour = int(match.group("hour"))
    minute = int(match.group("minute") or 0)
    ampm = (match.group("ampm") or "").lower()
    if ampm == "pm" and hour < 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    when = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    month = _MONTHS.get((match.group("mon") or "").lower())
    if month:
        when = when.replace(month=month, day=int(match.group("day")))
        if when < now - timedelta(days=1):
            when = when.replace(year=when.year + 1)
    elif when <= now:
        when += timedelta(days=1)
    return when.timestamp()
