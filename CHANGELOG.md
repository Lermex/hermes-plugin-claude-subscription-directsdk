# Changelog

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
