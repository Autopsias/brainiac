/**
 * COS capture hook — the app's own requests, captured in the page's MAIN world.
 *
 * WHY THIS FILE EXISTS. Both lanes REPLAY a request the app already made: the
 * read lane needs one `service.svc` call carrying `authorization` and an
 * accepted `FindItem` body, and the mutation lane needs one accepted shape per
 * verb. Nothing here is ever hand-built (doctrine v4.7).
 *
 * INSTALL TIME: measured, and it is NOT what s03 concluded. s03 saw 137 calls
 * with zero `FindItem` and read that as "the app fires FindItem during BOOT
 * only" — true for a SETTLED tab. Measured 2026-08-11, three times: a hook
 * installed at `readyState=complete` DOES capture an authenticated `FindItem`,
 * once the tab becomes the ACTIVE tab of its window and the list settles. So
 * the gate is the ENVELOPE (`stats().boot_finditem`), not the clock; a
 * document_start install (CDP `Page.addScriptToEvaluateOnNewDocument`, a
 * content script at `run_at: document_start`, a `@run-at document-start`
 * userscript) is the reliable route where one is available, and neither is
 * available on the owner's Chrome 151.
 *
 * WHERE THE ACTION LIVES: also measured, and also not where it was assumed.
 * Reads carry `action=` in the query. WRITES DO NOT — an archive is a
 * `POST /owa/service.svc` that names itself only in its JSON body (`__type`:
 * "MoveItemJsonRequest:#Exchange"). A hook that classifies from the address
 * alone therefore reports a perfect read lane and an empty mutation lane, which
 * is exactly what four owner archives produced on 2026-08-11. `actionOf` now
 * reads the query, then the header, then the body.
 *
 * WHAT IT KEEPS, AND WHERE. Everything stays in the page's main world: request
 * headers (including the bearer) and request bodies are held in memory on
 * `window.__cosCap` and are NEVER serialized to the DOM bridge, a file, or a
 * host process. The host reads `stats()` — counts and header NAMES — and
 * nothing else. `tests/` asserts that the exporter cannot emit a header value.
 *
 * IDEMPOTENT AND BUFFER-PRESERVING. Re-evaluating this file keeps the calls
 * already captured (a second install that reset the buffer would discard the
 * envelope it exists to catch) and never double-wraps `fetch`.
 */

/* eslint-env browser */

(function () {
  var VERSION = 3;
  var W = (typeof window !== "undefined") ? window : globalThis;
  var prev = W.__cosCap;
  if (prev && prev.version === VERSION && prev.hooked) {
    return "cos-capture-hook: already installed, buffer kept ("
      + prev.calls.length + " calls)";
  }

  var MAX = 400;                      // a bounded ring; mail bodies are not kept
  var cap = {
    version: VERSION,
    installed_at: new Date().toISOString(),
    /* The load state AT INSTALL TIME. `loading` is the only value that proves a
     * document_start install; anything else means the app may already have
     * fired its boot calls into a world with no hook in it. */
    installed_at_readystate: (typeof document !== "undefined")
      ? document.readyState : "no-document",
    calls: (prev && prev.calls) ? prev.calls.slice(-MAX) : [],
    dropped: (prev && prev.dropped) || 0,
    hooked: false,
    /* The UNHOOKED transport. Our own replays call this directly, so they never
     * land in the buffer — which is what makes "the app's call" a decidable
     * question instead of a first-match heuristic over mixed traffic. */
    rawFetch: (prev && prev.rawFetch) || (W.fetch && W.fetch.bind(W)),
  };

  /* The EWS action, from wherever this build happens to put it. MEASURED
   * 2026-08-11: the owner archived a message four times with the hook verified
   * live, and every capture reported `MoveItem: 0` — because the write is a
   * `POST /owa/service.svc` with NO `action=` in the query, while every READ
   * carries one. Classifying from the address alone therefore made the whole
   * mutation lane invisible while the reads looked perfect. So: query first,
   * then the header, then the request BODY, where an EWS request names itself
   * in `__type` ("MoveItemJsonRequest:#Exchange"). */
  function typeAction(doc) {
    var t = doc && (doc.__type || (doc.Body && doc.Body.__type)
      || (doc.request && doc.request.__type));
    if (!t) return null;
    var m = /^([A-Za-z]+?)(?:Json)?(?:Request)?(?::|$)/.exec(String(t));
    return m ? m[1] : null;
  }

  function actionOf(url, headers, body) {
    try {
      var u = new URL(String(url), (typeof location !== "undefined")
        ? location.origin : "https://invalid.example");
      var a = u.searchParams.get("action");
      if (a) return a;
    } catch (e) { /* a relative or malformed url has no action */ }
    if (headers) {
      var h = headers["x-owa-action"] || headers.action || headers.Action;
      if (h) return String(h);
      if (headers["x-owa-urlpostdata"]) {
        try {
          var fromHeader = typeAction(
            JSON.parse(decodeURIComponent(headers["x-owa-urlpostdata"])));
          if (fromHeader) return fromHeader;
        } catch (e) { /* not the shape we know */ }
      }
    }
    if (typeof body === "string" && body.charAt(0) === "{") {
      try { return typeAction(JSON.parse(body)); } catch (e) { return null; }
    }
    return null;
  }

  function headerMap(init) {
    var out = {};
    var h = init && init.headers;
    if (!h) return out;
    if (typeof h.forEach === "function" && !Array.isArray(h)) {
      h.forEach(function (v, k) { out[String(k).toLowerCase()] = String(v); });
      return out;
    }
    Object.keys(h).forEach(function (k) { out[String(k).toLowerCase()] = String(h[k]); });
    return out;
  }

  function record(url, init, body) {
    var headers = headerMap(init);
    var entry = {
      url: String(url),
      method: (init && init.method) || "GET",
      ts: new Date().toISOString(),
      action: actionOf(url, headers, body),
      headers: headers,
      /* KEPT IN THE PAGE, like the bearer beside it. `stats()` emits counts and
       * header NAMES only, and the shape exporter scrubs before anything
       * crosses the bridge — the body is here so a mutation can be REPLAYED,
       * never so it can be read from outside this world. */
      body: (typeof body === "string") ? body : null,
      status: null,
    };
    cap.calls.push(entry);
    while (cap.calls.length > MAX) { cap.calls.shift(); cap.dropped += 1; }
    return entry;
  }

  if (W.fetch && !cap.hooked) {
    var raw = cap.rawFetch;
    W.fetch = function (input, init) {
      var url = (input && input.url) ? input.url : input;
      var entry;
      try {
        var opts = init || (input && input.headers ? input : {});
        /* `fetch(new Request(...))` puts the body on a stream that can be read
         * ONCE, so the app's copy must not be the one consumed: clone first.
         * The read is async, so the entry is filed now and its action is
         * upgraded when the text arrives — a call is never dropped waiting. */
        var body = (opts && typeof opts.body === "string") ? opts.body : null;
        entry = record(url, opts, body);
        if (!body && input && typeof input.clone === "function" && entry) {
          try {
            input.clone().text().then(function (txt) {
              entry.body = txt;
              if (!entry.action) entry.action = actionOf(url, entry.headers, txt);
            }, function () { /* an unreadable body is not a failure */ });
          } catch (e) { /* not a Request, or already consumed */ }
        }
      } catch (e) { entry = null; }
      return raw(input, init).then(function (res) {
        if (entry) entry.status = res.status;
        return res;
      }, function (err) {
        if (entry) entry.status = "network-error";
        throw err;
      });
    };
    cap.hooked = true;
  }

  /* XHR is hooked because it costs fifteen lines, not because it fires: s03
   * measured an XHR hook capturing zero calls of any kind on this build. A
   * capture path that is empty for a measured reason is documentation; one that
   * is absent is a gap nobody can see. */
  if (typeof XMLHttpRequest !== "undefined" && !XMLHttpRequest.prototype.__cosHooked) {
    var open = XMLHttpRequest.prototype.open;
    var send = XMLHttpRequest.prototype.send;
    var setH = XMLHttpRequest.prototype.setRequestHeader;
    XMLHttpRequest.prototype.open = function (method, url) {
      this.__cosEntry = {url: String(url), method: String(method), headers: {},
                         ts: new Date().toISOString(), action: null, status: null,
                         transport: "xhr"};
      return open.apply(this, arguments);
    };
    XMLHttpRequest.prototype.setRequestHeader = function (k, v) {
      if (this.__cosEntry) this.__cosEntry.headers[String(k).toLowerCase()] = String(v);
      return setH.apply(this, arguments);
    };
    XMLHttpRequest.prototype.send = function (body) {
      var e = this.__cosEntry;
      if (e) {
        e.body = (typeof body === "string") ? body : null;
        e.action = actionOf(e.url, e.headers, e.body);
        cap.calls.push(e);
        while (cap.calls.length > MAX) { cap.calls.shift(); cap.dropped += 1; }
        this.addEventListener("load", function () { e.status = this.status; });
      }
      return send.apply(this, arguments);
    };
    XMLHttpRequest.prototype.__cosHooked = true;
  }

  /* ---------------- the read surface ---------------------------------------
   * `seed` and `body` return LIVE objects that carry header values, so they are
   * for in-page use only. `stats` is the only thing shaped for the bridge, and
   * it emits header NAMES, never values. */
  cap.seed = function (action) {
    for (var i = 0; i < cap.calls.length; i++) {
      var c = cap.calls[i];
      if (c.action !== action) continue;
      if (!c.headers || !c.headers.authorization || !c.headers["x-owa-urlpostdata"]) continue;
      return c;
    }
    return null;
  };

  /* The newest usable envelope, which is what a canary re-prime needs: after a
   * 449 the stale one is exactly the thing not to reuse. */
  cap.freshestSeed = function (action, afterIso) {
    var best = null;
    for (var i = 0; i < cap.calls.length; i++) {
      var c = cap.calls[i];
      if (c.action !== action) continue;
      if (!c.headers || !c.headers.authorization) continue;
      if (afterIso && !(c.ts > afterIso)) continue;
      if (!best || c.ts > best.ts) best = c;
    }
    return best;
  };

  cap.body = function (call) {
    try {
      return JSON.parse(decodeURIComponent(call.headers["x-owa-urlpostdata"]));
    } catch (e) { return null; }
  };

  cap.stats = function () {
    var byAction = {};
    var withAuth = 0;
    for (var i = 0; i < cap.calls.length; i++) {
      var c = cap.calls[i];
      var k = c.action || "(no-action)";
      byAction[k] = (byAction[k] || 0) + 1;
      if (c.headers && c.headers.authorization) withAuth += 1;
    }
    return {
      version: cap.version,
      installed_at: cap.installed_at,
      installed_at_readystate: cap.installed_at_readystate,
      /* The whole point of the file, as one boolean the host can gate on. */
      document_start: cap.installed_at_readystate === "loading",
      boot_finditem: !!cap.seed("FindItem"),
      captured: cap.calls.length,
      dropped: cap.dropped,
      with_authorization: withAuth,
      by_action: byAction,
      /* NAMES only. A header VALUE crossing this boundary is the bearer
       * leaving the page, which the whole two-world design exists to prevent. */
      seed_header_names: (function () {
        var s = cap.seed("FindItem");
        return s ? Object.keys(s.headers).sort() : [];
      })(),
    };
  };

  W.__cosCap = cap;
  return "cos-capture-hook: installed at readyState="
    + cap.installed_at_readystate + " (" + cap.calls.length + " calls kept)";
})();
