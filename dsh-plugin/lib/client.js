window.__ModuleLoader__.load({ id: "vm-bord-launcher", factory: (require) => {
var module = { exports: {} };
var exports = module.exports;
var React = require("react");
var h = React.createElement;

var API = "/api/vm-bord";
var POLL_MS = 5000;

var T = {
  text: "var(--dsw-alias-text-primary, #1f2328)",
  muted: "var(--dsw-alias-text-secondary, #6b7280)",
  border: "var(--dsw-alias-border-secondary, rgba(128,128,128,.32))",
  panel: "var(--dsw-alias-bg-elevated, #ffffff)",
  accent: "var(--dsw-alias-state-business-primary, #3b82f6)",
  bad: "var(--dsw-alias-state-error-primary, #d93025)",
  good: "var(--dsw-alias-state-success-primary, #16a34a)"
};

function api(path, options) {
  return fetch(API + path, Object.assign({ cache: "no-store" }, options || {})).then(function (res) {
    return res.json().then(function (body) { return { status: res.status, body: body }; });
  });
}

function dot(color) {
  return h("span", {
    style: {
      display: "inline-block", width: 8, height: 8, borderRadius: 4,
      background: color, marginRight: 6, verticalAlign: "middle"
    }
  });
}

function button(label, onClick, opts) {
  var o = opts || {};
  return h("button", {
    type: "button",
    onClick: o.disabled ? undefined : onClick,
    title: o.title || undefined,
    style: {
      padding: "4px 10px", borderRadius: 6, fontSize: 12, cursor: o.disabled ? "not-allowed" : "pointer",
      border: "1px solid " + (o.danger ? T.bad : T.border),
      background: o.primary ? T.accent : "transparent",
      color: o.primary ? "#fff" : (o.danger ? T.bad : T.text),
      opacity: o.disabled ? 0.5 : 1
    }
  }, label);
}

function Launcher() {
  var openState = React.useState(false);
  var open = openState[0], setOpen = openState[1];
  var stState = React.useState({ running: false, port: null, version: null, loaded: false });
  var state = stState[0], setState = stState[1];
  var busyState = React.useState("");
  var busy = busyState[0], setBusy = busyState[1];
  var errState = React.useState("");
  var error = errState[0], setError = errState[1];

  var refresh = React.useCallback(function () {
    return api("/status").then(function (r) {
      if (r.body && r.body.ok) {
        setState({ running: !!r.body.running, port: r.body.port, version: r.body.version, loaded: true });
      } else {
        setState(function (old) { return Object.assign({}, old, { loaded: true }); });
      }
    }).catch(function () {
      setState(function (old) { return Object.assign({}, old, { loaded: true }); });
    });
  }, []);

  React.useEffect(function () { refresh(); }, [refresh]);

  React.useEffect(function () {
    if (!open) return undefined;
    var timer = setInterval(refresh, POLL_MS);
    return function () { clearInterval(timer); };
  }, [open, refresh]);

  var act = function (kind) {
    if (busy) return;
    setBusy(kind);
    setError("");
    api("/" + kind, { method: "POST" }).then(function (r) {
      var body = r.body || {};
      if (body.ok) {
        setState({ running: !!body.running, port: body.port, version: body.version, loaded: true });
      } else {
        setError((body.error && body.error.message) || "操作失败");
      }
    }).catch(function (e) {
      setError(String(e && e.message ? e.message : e));
    }).then(function () {
      setBusy("");
      refresh();
    });
  };

  var url = state.running && state.port ? "http://127.0.0.1:" + state.port + "/" : null;

  var popover = open ? h("div", {
    style: {
      position: "fixed", left: 12, bottom: 64, zIndex: 2147483000, width: 236,
      padding: "10px 12px", borderRadius: 10, border: "1px solid " + T.border,
      background: T.panel, color: T.text, boxShadow: "0 8px 24px rgba(0,0,0,.18)",
      fontSize: 12, lineHeight: 1.6
    }
  }, [
    h("div", { key: "t", style: { fontWeight: 600, marginBottom: 6, color: T.text } }, "vm_bord 看板"),
    h("div", { key: "s" },
      dot(state.running ? T.good : T.muted),
      state.loaded ? (state.running ? "运行中" : "未运行") : "检查中…"),
    state.running ? h("div", { key: "p", style: { color: T.muted, fontFamily: "ui-monospace, monospace" } },
      "127.0.0.1:" + state.port + (state.version ? " · v" + state.version : "")) : null,
    h("div", { key: "b", style: { display: "flex", gap: 6, marginTop: 8, flexWrap: "wrap" } }, [
      button(busy === "start" ? "启动中…" : "启动", function () { act("start"); },
        { primary: true, disabled: !!busy || state.running }),
      button(busy === "stop" ? "停止中…" : "停止", function () { act("stop"); },
        { danger: true, disabled: !!busy || !state.running }),
      button("打开浏览器", function () { if (url) window.open(url, "_blank", "noopener"); },
        { disabled: !url })
    ]),
    error ? h("div", { key: "e", style: { marginTop: 6, color: T.bad, wordBreak: "break-all" } }, error) : null,
    h("div", { key: "n", style: { marginTop: 6, color: T.muted, fontSize: 11 } },
      "关掉 DSH 不会停掉服务")
  ]) : null;

  return h("div", { style: { display: "inline-flex", alignItems: "center" } }, [
    h("button", {
      key: "btn",
      type: "button",
      title: "vm_bord 本地看板",
      onClick: function () { setOpen(!open); },
      style: {
        display: "inline-flex", alignItems: "center", gap: 6, padding: "4px 8px",
        borderRadius: 6, border: "1px solid " + T.border, background: "transparent",
        color: T.text, fontSize: 12, cursor: "pointer"
      }
    }, [dot(state.running ? T.good : T.muted), h("span", { key: "l" }, "VM 看板")]),
    popover
  ]);
}

module.exports.apply = function apply(ctx) {
  var slots = ctx && typeof ctx.get === "function" ? ctx.get("slots") : undefined;
  if (!slots || typeof slots.register !== "function") return;
  slots.inject("sidebar.footer.action", function () {
    return slots.register({
      name: "sidebar.footer.action",
      id: "vm-bord-launcher",
      order: 40,
      inject: function () { return {}; }
    }, Launcher);
  });
};

module.exports.inject = [];
return module.exports;
} });
