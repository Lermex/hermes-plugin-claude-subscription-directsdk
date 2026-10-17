/**
 * Claude Accounts — dashboard tab for the claude-subscription-directsdk-experimental plugin.
 * Plain IIFE on window.__HERMES_PLUGIN_SDK__ (no build step). Shows each pooled Claude login:
 * plan, rotation order, benching (with reset countdown), request count and the account's own
 * session/weekly limits from Anthropic's usage API. One action: clear a bench early.
 */
(function () {
  "use strict";
  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;

  const React = SDK.React;
  const h = React.createElement;
  const { useState, useEffect, useCallback } = SDK.hooks;
  const { Card, CardHeader, CardTitle, CardContent, Badge, Button, Input, ConfirmDialog } = SDK.components;
  const cn = SDK.utils.cn;
  const BASE = "/api/plugins/claude-subscription-directsdk-experimental";

  function api(path, init) { return SDK.fetchJSON(BASE + path, init); }

  function fmtIn(ts) {
    if (!ts) return "";
    const s = Math.round(ts - Date.now() / 1000);
    if (s <= 0) return "now";
    const d = Math.floor(s / 86400), hh = Math.floor((s % 86400) / 3600), mm = Math.floor((s % 3600) / 60);
    if (d) return d + "d " + hh + "h";
    if (hh) return hh + "h " + mm + "m";
    return mm + "m";
  }
  function fmtAt(ts) { return ts ? new Date(ts * 1000).toLocaleString() : ""; }

  function Bar(props) {
    const pct = props.pct == null ? null : Math.max(0, Math.min(100, props.pct));
    // Inline colours: the host's Tailwind build only ships classes it uses itself, so bg-emerald-500 & co. would be purged.
    const tone = pct == null ? "transparent" : pct >= 90 ? "#ef4444" : pct >= 70 ? "#f59e0b" : "#10b981";
    return h("div", { className: "space-y-1" },
      h("div", { className: "flex justify-between text-xs" },
        h("span", null, props.label),
        h("span", { className: "text-muted-foreground" },
          pct == null ? "—" : pct.toFixed(0) + "%",
          props.resetAt ? " · resets in " + fmtIn(props.resetAt) : "")),
      h("div", { className: "h-2 w-full rounded bg-muted overflow-hidden", style: { border: "1px solid hsl(var(--border, 0 0% 50%) / 0.4)" } },
        h("div", { className: "h-full rounded", style: { width: (pct == null ? 0 : pct) + "%", backgroundColor: tone, transition: "width .3s" } })));
  }

  function StatusBadge(a) {
    if (!a.logged_in) return h(Badge, { variant: "destructive" }, "logged out");
    if (a.benched) return h(Badge, { variant: "destructive" }, "benched" + (a.error_reset_at ? " · " + fmtIn(a.error_reset_at) : ""));
    if (a.status && a.status !== "ok") return h(Badge, { variant: "secondary" }, a.status);
    return h(Badge, { variant: "default" }, "ok");
  }

  function AccountCard(props) {
    const a = props.account;
    const u = a.usage || {};
    return h(Card, { className: cn(a.current && "border-primary") },
      h(CardHeader, { className: "pb-2" },
        h(CardTitle, { className: "flex items-center gap-2 text-base" },
          h("span", { className: "text-muted-foreground" }, "#" + (a.priority + 1)),
          a.label,
          a.plan ? h(Badge, { variant: "outline" }, a.plan) : null,
          a.current ? h(Badge, { variant: "outline" }, "active") : null,
          h("span", { className: "flex-1" }),
          StatusBadge(a))),
      h(CardContent, { className: "space-y-3" },
        u.unavailable
          ? h("p", { className: "text-sm text-muted-foreground" }, "Limits unavailable: " + u.unavailable)
          : h("div", { className: "space-y-2" }, (u.windows || []).map(function (w, i) {
              return h(Bar, { key: i, label: w.label, pct: w.used_percent, resetAt: w.reset_at });
            })),
        (u.details || []).length ? h("p", { className: "text-xs text-muted-foreground" }, u.details.join(" · ")) : null,
        a.benched || (a.status && a.status !== "ok")
          ? h("div", { className: "rounded border border-destructive/40 bg-destructive/5 p-2 text-xs space-y-1" },
              h("div", null, h("b", null, a.error_reason || a.status),
                a.error_reset_at ? " — reopens " + fmtAt(a.error_reset_at) : ""),
              a.error_message ? h("div", { className: "text-muted-foreground break-words" }, a.error_message) : null,
              h(Button, { size: "sm", variant: "outline", onClick: function () { props.onClear(a.id); } }, "Clear bench now"))
          : null,
        h("div", { className: "flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground" },
          a.status_at ? h("span", null, "status " + SDK.utils.timeAgo(a.status_at * 1000)) : null,
          h("span", { className: "font-mono truncate", title: a.config_dir }, a.config_dir),
          a.login_error ? h("span", { className: "text-destructive" }, a.login_error) : null,
          h("span", { className: "flex-1" }),
          h(Button, { size: "sm", variant: "ghost", onClick: function () { props.onRemove(a); } }, "Remove"))));
  }

  // Add-account flow: the official `claude auth login` runs on the server; the page shows its sign-in
  // link and sends the pasted code straight to that process (never through chat or the model).
  function AddDialog(props) {
    const [label, setLabel] = useState("");
    const [login, setLogin] = useState(null);
    const [code, setCode] = useState("");
    const [busy, setBusy] = useState(false);
    const [err, setErr] = useState(null);

    useEffect(function () {
      if (!login || login.state === "done" || login.state === "failed") return undefined;
      const id = setInterval(function () {
        api("/logins/" + login.id).then(setLogin).catch(function () {});
      }, 3000);
      return function () { clearInterval(id); };
    }, [login && login.id, login && login.state]);

    useEffect(function () { if (login && login.state === "done") props.onAdded(); }, [login && login.state]);

    function start() {
      setBusy(true); setErr(null);
      api("/logins", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ label: label }) })
        .then(setLogin).catch(function (e) { setErr(String(e)); }).finally(function () { setBusy(false); });
    }
    function submit() {
      if (!login || !code.trim()) return;
      setBusy(true); setErr(null);
      api("/logins/" + login.id + "/code", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ code: code }) })
        .then(function (l) { setLogin(l); setCode(""); }).catch(function (e) { setErr(String(e)); }).finally(function () { setBusy(false); });
    }
    function close() {
      if (login && login.state !== "done") api("/logins/" + login.id, { method: "DELETE" }).catch(function () {});
      props.onClose();
    }

    const state = login ? login.state : "idle";
    return h(Card, { className: "border-primary" },
      h(CardHeader, { className: "pb-2" }, h(CardTitle, { className: "text-base" }, "Add a Claude subscription")),
      h(CardContent, { className: "space-y-3" },
        state === "idle" ? h("div", { className: "space-y-2" },
          h("p", { className: "text-sm text-muted-foreground" },
            "Starts the official ", h("code", null, "claude auth login"), " for a new login directory on the server."),
          h(Input, { placeholder: "Label (e.g. work, home)", value: label, onChange: function (e) { setLabel(e.target.value); } }),
          h("div", { className: "flex gap-2" },
            h(Button, { size: "sm", disabled: busy, onClick: start }, busy ? "Starting…" : "Start sign-in"),
            h(Button, { size: "sm", variant: "ghost", onClick: close }, "Cancel"))) : null,
        state === "starting" ? h("p", { className: "text-sm" }, "Starting the Claude CLI…") : null,
        state === "awaiting_code" || state === "verifying" ? h("div", { className: "space-y-2" },
          h("ol", { className: "list-decimal pl-5 text-sm space-y-1" },
            h("li", null, "Open ", login.url
              ? h("a", { href: login.url, target: "_blank", rel: "noopener noreferrer", className: "underline" }, "the Claude sign-in page")
              : "the sign-in page (waiting for the CLI to print it…)",
              " and sign in with the subscription you want to add (use a private window if you're already signed in as another account)."),
            h("li", null, "Claude shows a code — copy the whole thing and paste it here.")),
          h(Input, { placeholder: "Paste the code", value: code, autoComplete: "off", spellCheck: false,
                     onChange: function (e) { setCode(e.target.value); },
                     onKeyDown: function (e) { if (e.key === "Enter") submit(); } }),
          login.error ? h("p", { className: "text-sm text-destructive" }, login.error) : null,
          h("div", { className: "flex items-center gap-2" },
            h(Button, { size: "sm", disabled: busy || state === "verifying" || !code.trim(), onClick: submit },
              state === "verifying" ? "Verifying…" : "Submit code"),
            h(Button, { size: "sm", variant: "ghost", onClick: close }, "Cancel"),
            h("span", { className: "text-xs text-muted-foreground" }, "expires in " + Math.ceil(login.expires_in / 60) + " min"))) : null,
        state === "done" ? h("div", { className: "space-y-2" },
          h("p", { className: "text-sm" }, "Added ", h("b", null, login.label || "the account"), ". It's now in the rotation."),
          h(Button, { size: "sm", onClick: props.onClose }, "Close")) : null,
        state === "failed" ? h("div", { className: "space-y-2" },
          h("p", { className: "text-sm text-destructive" }, login.error || "Sign-in failed."),
          h(Button, { size: "sm", variant: "outline", onClick: function () { setLogin(null); } }, "Try again"),
          h(Button, { size: "sm", variant: "ghost", onClick: close }, "Close")) : null,
        err ? h("p", { className: "text-sm text-destructive" }, err) : null));
  }

  function Page() {
    const [data, setData] = useState(null);
    const [error, setError] = useState(null);
    const [busy, setBusy] = useState(false);
    const [adding, setAdding] = useState(false);
    const [pendingRemove, setPendingRemove] = useState(null);

    const load = useCallback(function () {
      api("/accounts").then(function (d) { setData(d); setError(null); })
        .catch(function (e) { setError(String(e)); });
    }, []);
    useEffect(function () { load(); const id = setInterval(load, 60000); return function () { clearInterval(id); }; }, [load]);

    function refresh() {
      setBusy(true);
      api("/accounts/refresh", { method: "POST" }).then(load).finally(function () { setBusy(false); });
    }
    function clear(id) {
      api("/accounts/" + encodeURIComponent(id) + "/clear-bench", { method: "POST" }).then(load)
        .catch(function (e) { setError(String(e)); });
    }

    function remove() {
      const a = pendingRemove; if (!a) return Promise.resolve();
      return api("/accounts/" + encodeURIComponent(a.id), { method: "DELETE" }).then(load)
        .catch(function (e) { setError(String(e)); }).finally(function () { setPendingRemove(null); });
    }

    const accounts = (data && data.accounts) || [];
    const dflt = data && data.default_login;
    return h("div", { className: "space-y-4 p-1" },
      h("div", { className: "flex items-center gap-3" },
        h("h2", { className: "text-lg font-semibold" }, "Claude Accounts"),
        h("span", { className: "text-sm text-muted-foreground" },
          accounts.length ? accounts.length + " pooled login" + (accounts.length === 1 ? "" : "s") + " · rotation order = priority" : "pool empty"),
        h("span", { className: "flex-1" }),
        h(Button, { size: "sm", variant: "outline", disabled: busy, onClick: refresh }, busy ? "Refreshing…" : "Refresh limits"),
        h(Button, { size: "sm", disabled: adding, onClick: function () { setAdding(true); } }, "Add account")),
      error ? h("p", { className: "text-sm text-destructive" }, error) : null,
      !data ? h("p", { className: "text-sm text-muted-foreground" }, "Loading…") : null,
      adding ? h(AddDialog, { onClose: function () { setAdding(false); load(); }, onAdded: load }) : null,
      pendingRemove ? h(ConfirmDialog, {
        open: true, title: "Remove " + pendingRemove.label + "?",
        description: "Signs this login out of Claude Code and drops it from the rotation.",
        confirmLabel: "Remove", onConfirm: remove, onCancel: function () { setPendingRemove(null); },
        onOpenChange: function (o) { if (!o) setPendingRemove(null); } }) : null,
      accounts.map(function (a) { return h(AccountCard, { key: a.id, account: a, onClear: clear, onRemove: setPendingRemove }); }),
      dflt ? h(Card, null,
        h(CardHeader, { className: "pb-2" }, h(CardTitle, { className: "flex items-center gap-2 text-base" },
          "Default Claude Code login", dflt.plan ? h(Badge, { variant: "outline" }, dflt.plan) : null,
          h("span", { className: "flex-1" }),
          h(Badge, { variant: dflt.logged_in ? "default" : "destructive" }, dflt.logged_in ? "logged in" : "logged out"))),
        h(CardContent, { className: "space-y-2" },
          dflt.usage && dflt.usage.unavailable
            ? h("p", { className: "text-sm text-muted-foreground" }, "Limits unavailable: " + dflt.usage.unavailable)
            : (dflt.usage.windows || []).map(function (w, i) { return h(Bar, { key: i, label: w.label, pct: w.used_percent, resetAt: w.reset_at }); }),
          h("p", { className: "text-xs text-muted-foreground font-mono" }, dflt.config_dir))) : null,
      data ? h("p", { className: "text-xs text-muted-foreground" },
        "Add a subscription from a terminal: ", h("code", null, data.add_hint),
        " · limits refresh every 60 s; a bench clears itself at the reset time.") : null);
  }

  window.__HERMES_PLUGINS__.register("claude-subscription-directsdk-experimental", Page);
})();
