# Changelog

## [0.3.6] - Unreleased

### Fixed

- The Claude Accounts dashboard tab and the provider's setup probe no longer run `claude auth status` to learn whether a pooled login is signed in; they read the login's credential file. A short-lived `auth status` on an expired access token starts an OAuth refresh and can exit before the rotated, single-use refresh token is saved, so the next real request gets `invalid_grant` and Claude Code blanks the login (CLI 2.1.288-2.1.300; fixed upstream in 2.1.301). The dashboard polled that command every minute per account, which signed pooled accounts out one by one. `auth_status` remains for the one-off `hermes auth` commands and the post-login check. A blanked login is now reported as "login cleared - sign in again" instead of "logged in".

## [0.3.5] - Unreleased

### Fixed

- Compression/auxiliary external-process clients now resolve placeholder credentials through the same account pool as main-agent requests, instead of silently using the ambient Claude login. Non-streaming auxiliary requests retry recognized subscription quota failures on the next pooled login, including `provider: auto`, preserving the quota reset timestamp. Account references are request-local for cached/shared clients; an exhausted configured pool never falls back to the ambient login. Explicit main-agent references and empty-pool single-login behavior are unchanged.
- No compression cooldown is disabled or cleared: the fix prevents this account-routing failure before it reaches the summary-failure breaker. Other summary failures still surface normally; streaming replay and provider/model fallback behavior are not changed.

## [0.3.4] - Unreleased

### Added

- Several Claude subscriptions as pooled credentials: `hermes auth add|status|logout <provider>` keeps one Claude Code login per `CLAUDE_CONFIG_DIR` under the plugin's data directory (or adopts an existing one via `CLAUDE_SUBSCRIPTION_DIRECTSDK_ADOPT_CONFIG_DIR`), and a session/weekly limit is raised as a 429 with `usage_limit_reached` + `resets_at` so Hermes core rotates to the next login and benches the exhausted one until its window reopens. Pool rows hold a directory reference, never a token.

## [0.3.1] - Unreleased

### Added

- Per-profile `claude_config_dir` setting selects the Claude login for every native spawn in a multiplexed gateway (#34).

### Fixed

- Accept Hermes tool names of 51–64 characters through stable 50-character aliases, mapping native tool calls back to the original names and preserving replay.
- Accept Hermes Relay's host-local `traceparent` request metadata without forwarding it to the native CLI or upstream, preventing full agent and fallback requests from failing on `extra_headers`.
- Continue rejecting arbitrary headers, credential overrides, and malformed header containers at the transport boundary.
- Carry the login identity Claude Code's local check reads (`USER`/`LOGNAME`, `USERNAME` on Windows) into the child env, derived from the parent process or the running uid: without it a perfectly logged-in CLI reports `loggedIn: false` instantly, with no stderr and no upstream request — the confirmed mechanism behind gateway-only `ClaudeCodeLoggedOut` failures (#44).
- Capture native stderr (bounded, drained off-thread) and include its first lines in `ClaudeCodeLoggedOut` and native-failure errors, so a gateway-only failure is diagnosable instead of vanishing into `DEVNULL` (#44).
