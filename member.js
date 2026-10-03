/* Tennessee Weather Network - membership client (shared by pricing/member
 * admin pages and the Models-page members strip). Plain JS, no deps. The
 * worker origin comes from window.TWN_WORKER, stamped by website.py. */
(function () {
  "use strict";
  var WORKER = (window.TWN_WORKER || "").replace(/\/$/, "");
  if (!WORKER) { console.warn("TWN_WORKER not set - member features disabled"); }

  function req(path, opts) {
    opts = opts || {};
    return fetch(WORKER + path, {
      method: opts.method || "GET",
      credentials: "include",
      headers: opts.body ? { "content-type": "application/json" } : {},
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    }).then(function (r) {
      return r.json().then(function (d) { return { status: r.status, data: d }; });
    });
  }

  var ME = null;

  function refreshMe() {
    return req("/api/me").then(function (r) { ME = r.data; return ME; });
  }
  function settled(r) {
    /* Resolve to a consistent {status, data} shape: after a 200 auth call
       refreshMe() hands back the bare ME object, and the auth-card handlers
       test r.status on it - a SUCCESSFUL signup/login used to show
       "Something went wrong." (found in the 2026-10-02 browser test). */
    if (r && typeof r.status === "number") return r;
    return { status: 200, data: r };
  }
  function signup(email, password) {
    return req("/api/auth/signup", { method: "POST", body: { email: email, password: password } })
      .then(function (r) { if (r.status === 200) return refreshMe().then(settled); return r; });
  }
  function login(email, password) {
    return req("/api/auth/login", { method: "POST", body: { email: email, password: password } })
      .then(function (r) { if (r.status === 200) return refreshMe().then(settled); return r; });
  }
  function logout() {
    return req("/api/auth/logout", { method: "POST" }).then(function () { ME = null; return refreshMe(); });
  }
  function startCheckout() {
    return req("/api/billing/checkout", { method: "POST" }).then(function (r) {
      if (r.data && r.data.url) { location.href = r.data.url; return r; }
      alert((r.data && r.data.error) || "Checkout unavailable - Stripe not configured yet.");
      return r;
    });
  }
  function openPortal() {
    return req("/api/billing/portal", { method: "POST" }).then(function (r) {
      if (r.data && r.data.url) { location.href = r.data.url; return r; }
      alert((r.data && r.data.error) || "Billing portal unavailable.");
      return r;
    });
  }
  function premiumIndex() { return req("/api/premium").then(function (r) { return r.data; }); }
  function premiumUrl(rel) { return WORKER + "/api/premium/" + rel; }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  /* ---- auth card renderer: swaps between login form and member status ---- */
  function authCard(el, opts) {
    opts = opts || {};
    refreshMe().then(function (me) { paint(el, me, opts); });
  }

  function paint(el, me, opts) {
    if (me && me.member) {
      el.innerHTML =
        '<div class="mem-ok">' +
        "<b>\u{1F464} " + esc(me.email) + "</b>" +
        (me.admin ? ' <span style="background:#7b1fa2;color:#fff;border-radius:6px;padding:1px 8px;font-size:12px">ADMIN</span>' : "") +
        '<br/><span class="src">' +
        (me.premiumActive
          ? "\u2b50 Premium active" + (me.premiumUntil ? " through " + new Date(me.premiumUntil).toLocaleDateString() : "")
          : "Free account - premium not active") +
        "</span><br/>" +
        (me.premiumActive
          ? '<button class="bBtn" id="memPortal">Manage billing</button> '
          : '<button class="bBtn" id="memUpgrade">\u2b50 Upgrade</button> ') +
        '<button class="bBtn" id="memLogout">Log out</button>' +
        "</div>";
      var po = document.getElementById("memPortal");
      if (po) po.onclick = openPortal;
      var up = document.getElementById("memUpgrade");
      if (up) up.onclick = startCheckout;
      document.getElementById("memLogout").onclick = function () {
        logout().then(function () {
          paint(el, ME, opts);
          /* the footer strip painted on DOMContentLoaded and knows nothing
             about in-page logouts - flip it to the logged-out nudge now,
             else it shows the member as still logged in until a reload
             (found in the 2026-10-02 browser test) */
          var st = document.getElementById("memStrip");
          if (st) st.innerHTML = "<span class='src'>\u2b50 <a href='pricing.html'>Go premium: weather centers, AI model maps, ad-free - $4.99/mo</a></span><br/>";
        });
      };
      if (opts.onChange) opts.onChange(me);
      return;
    }
    el.innerHTML =
      '<div class="mem-form">' +
      '<div style="display:flex;gap:8px;flex-wrap:wrap;align-items:end">' +
      '<label>Email<br><input id="memEmail" type="email" autocomplete="email" style="min-width:210px"></label>' +
      '<label>Password<br><input id="memPass" type="password" autocomplete="current-password" style="min-width:160px"></label>' +
      '<button class="bBtn" id="memLogin">Log in</button>' +
      '<button class="bBtn" id="memSignup">Create account</button>' +
      "</div>" +
      '<div class="src" id="memMsg" style="margin-top:6px"></div></div>';
    var msg = document.getElementById("memMsg");
    function showErr(r) { msg.textContent = (r.data && r.data.error) || "Something went wrong."; }
    document.getElementById("memLogin").onclick = function () {
      msg.textContent = "...";
      login(document.getElementById("memEmail").value.trim(), document.getElementById("memPass").value)
        .then(function (r) { if (r.status === 200) { paint(el, ME, opts); } else showErr(r); });
    };
    document.getElementById("memSignup").onclick = function () {
      msg.textContent = "...";
      signup(document.getElementById("memEmail").value.trim(), document.getElementById("memPass").value)
        .then(function (r) { if (r.status === 200) { paint(el, ME, opts); } else showErr(r); });
    };
    if (opts.onChange) opts.onChange(null);
  }

  window.TWN = window.TWN || {};
  window.TWN.member = {
    refreshMe: refreshMe, signup: signup, login: login, logout: logout,
    startCheckout: startCheckout, openPortal: openPortal,
    premiumIndex: premiumIndex, premiumUrl: premiumUrl, authCard: authCard, esc: esc,
    get me() { return ME; },
  };
})();
