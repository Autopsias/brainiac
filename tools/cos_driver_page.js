/**
 * COS driver — the IN-PAGE half (REST-02, 2026-08-10).
 *
 * This file runs in the page's MAIN JavaScript world and is self-driving:
 * everything slow happens here, inside one page task, because the host->Chrome
 * evaluation bridge is the thing that wedges (run 112's `host-eval-timeout`) —
 * a long job driven by many short evaluations dies with the bridge, a
 * self-driving job polled by short status reads does not.
 *
 * TWO WORLDS, ONE DOM (measured 2026-08-10, and it is the reason this file has a
 * DOM bridge at all). Chrome's `execute javascript` AppleScript surface — which
 * is `tools/cos_driver.py`'s transport — evaluates in an ISOLATED world: a
 * separate JS heap on the same document. Proven both ways in one sitting: a
 * `window.fetch` hook installed there captured 0 of the app's 35 `service.svc`
 * calls while the shared resource timeline recorded every one, and a marker
 * planted from each world was invisible to the other. The captured envelope
 * therefore CANNOT be read from the host's world, and a request issued there
 * carries no `authorization` — which is exactly the HTTP 401 run 113 recorded.
 *
 * So the auth-bearing half lives HERE, in the main world, and never leaves it.
 * The host writes options into `#__cos_in` and reads results from `#__cos_out`
 * — two DOM nodes, the one thing both worlds share. What crosses is census rows
 * and message text; the envelope does not cross, and cannot.
 *
 * HARD RULES (they are the reason this file exists rather than a Python port):
 *  - NO CLICK DISPATCH. This file contains no `.click()`, no pointer events, no
 *    keyboard synthesis. It scrolls a list and it calls `fetch`. `tests/
 *    test_cos_driver.py` asserts that mechanically.
 *  - READ-ONLY VERBS ONLY: `FindItem` and `GetItem`. Both are fail-safe — a bad
 *    shape errors and mutates nothing (SKILL.md:1855 request-construction split:
 *    a reconstructed request is legal for read verbs and ONLY for read verbs).
 *  - NEVER fetch a message whose `IsRead` is false. `fetchBody` refuses, and the
 *    caller filters unread ids out of the draw before it ever gets here.
 *  - AUTH NEVER LEAVES THE PAGE. Requests are issued by the page itself with
 *    `credentials: "include"`; when a captured envelope is present its headers
 *    are reused in place. No header value is ever returned to the host process.
 *
 * Result shape on `window.__cosRun`:
 *   {phase, done, error, out:{scan, enumeration, sent, bodies}}
 */

/* eslint-env browser */
/* global crypto, TextEncoder */

(function () {
  var ORIGIN = location.origin;

  var state = {
    phase: "init", done: false, error: null,
    out: {scan: null, enumeration: null, sent: null, bodies: null},
  };
  window.__cosRun = state;

  var sleep = function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };
  var listRows = function () {
    return [].slice.call(document.querySelectorAll('[role="option"][data-convid]'));
  };

  function sha256(s) {
    return crypto.subtle.digest("SHA-256", new TextEncoder().encode(s)).then(function (b) {
      return [].slice.call(new Uint8Array(b))
        .map(function (x) { return x.toString(16).padStart(2, "0"); }).join("");
    });
  }

  /* ---------------- the envelope ------------------------------------------
   * Preferred: a request the page already made (verbatim replay).
   * Fallback (SKILL.md:1855): a RECONSTRUCTED read-only envelope — same origin,
   * the page's own cookies, and OWA's CSRF canary read from `document.cookie`.
   * Post-Monarch the page serves its list from Loki/IndexedDB and fires nothing
   * capturable, which is exactly the case that clause was written for.
   */
  /* ONE captured call is the seed, and it must be a `FindItem`: its URL, its
   * headers, its `Header` block and its accepted `Body` all travel together.
   *
   * Mixing them is not a style question. Run 114's second attempt took the
   * envelope from whatever authorized `service.svc` call came first (an
   * attachment-preview call, as it happened) and pasted that call's `Header`
   * and URL onto a FindItem body — HTTP 500, every page, while the same body
   * under its OWN captured header returned 200 and 527 items.
   *
   * The app's call is the FIRST match: our own replays are issued through this
   * same hooked `fetch` and land in the same buffer behind it.
   */
  function capturedSeed() {
    var cap = window.__cosCap;
    if (!cap || !cap.calls) return null;
    for (var i = 0; i < cap.calls.length; i++) {
      var c = cap.calls[i];
      if (String(c.url).indexOf("action=FindItem") === -1) continue;
      if (!c.headers || !c.headers.authorization
          || !c.headers["x-owa-urlpostdata"]) continue;
      var b = capturedBody(c);
      if (b && b.Body && String(b.Body.__type || "").indexOf("FindItemRequest") === 0
          && b.Header) return c;
    }
    return null;
  }

  var seed = null;          // {url, headers} — never returned to the host
  var seedKind = null;

  /* A CAPTURED envelope or NO RUN. There is no reconstructed fallback, and the
   * reason is measured rather than cautious: on this build the mailbox has no
   * OWA auth cookie at all (`document.cookie` carries no `X-OWA-CANARY`; auth is
   * an MSAL bearer), and a reconstructed read-only `FindItem` was refused
   * `401 x-owa-resulttype: AuthError` on `/owa/service.svc`, `/owa/0/service.svc`
   * and with/without a canary header — five variants, 2026-08-10. Run 113 shipped
   * the fallback and spent a night proving the same 401. A path that cannot
   * succeed is not a fallback; it is a way for a run to fail late instead of
   * early, so it is deleted rather than demoted. */
  function buildSeed() {
    var cap = capturedSeed();
    if (!cap) {
      seedKind = "none";
      throw new Error(
        "no captured service.svc envelope in this world. The capture hook must "
        + "be installed in the page's MAIN world (an isolated-world hook sees "
        + "none of the app's traffic) and the app must have issued a call. "
        + "There is no reconstructed fallback: this build refuses one with 401.");
    }
    seed = {url: cap.url, headers: Object.assign({}, cap.headers)};
    seedKind = "captured";
  }

  function call(action, body) {
    var url = new URL(seed.url, ORIGIN);
    url.searchParams.set("action", action);
    var headers = Object.assign({}, seed.headers);
    headers["x-owa-urlpostdata"] = encodeURIComponent(JSON.stringify(body));
    headers.action = action;
    var t0 = performance.now();
    return fetch(url.toString(), {
      method: "POST", headers: headers, credentials: "include",
    }).then(function (res) {
      return res.text().then(function (text) {
        var json = null;
        try { json = JSON.parse(text); } catch (e) { /* an error page, not JSON */ }
        return {
          status: res.status, ms: Math.round(performance.now() - t0), json: json,
          // Never the body text of an error page — it can carry session detail.
          non_json: json ? null : text.length,
        };
      });
    });
  }

  function firstItem(r) {
    var m = r.json && r.json.Body && r.json.Body.ResponseMessages
      && r.json.Body.ResponseMessages.Items && r.json.Body.ResponseMessages.Items[0];
    return m || null;
  }

  /* The captured request BODY, parsed. `x-owa-urlpostdata` is
   * encodeURIComponent(JSON.stringify(body)) with an EMPTY HTTP body. */
  function capturedBody(call) {
    try {
      return JSON.parse(decodeURIComponent(call.headers["x-owa-urlpostdata"]));
    } catch (e) { return null; }
  }

  function header() {
    var cap = capturedSeed();
    var parsed = cap ? capturedBody(cap) : null;
    if (parsed && parsed.Header) return JSON.parse(JSON.stringify(parsed.Header));
    throw new Error("the captured envelope carries no request Header to reuse");
  }

  /* The seed's own body — a FindItem THE SERVER ALREADY ACCEPTED, to be mutated
   * rather than replaced. `SKILL.md:1855` says a read-only request MAY be
   * reconstructed; it does not promise the server will take one, and here it
   * does not. Clone it, change the folder, the shape and the paging, and leave
   * every field the server has already blessed exactly as captured. */
  function capturedFindItemBody() {
    var c = capturedSeed();
    return c ? capturedBody(c) : null;
  }

  /* ---------------- FindItem paging --------------------------------------- */
  function enumFolder(distinguishedId, pageSize, maxPages) {
    var items = [];
    var pages = [];
    var offset = 0;
    var total = null;
    var terminated = false;
    var page = 0;

    var accepted = capturedFindItemBody();
    if (!accepted) {
      return Promise.reject(new Error(
        "no captured FindItem to replay. The envelope alone is not enough on "
        + "this build: a hand-built FindItem body is refused with HTTP 500."));
    }

    function step() {
      if (page >= maxPages) return Promise.resolve();
      var body = JSON.parse(JSON.stringify(accepted));
      // THE ONLY THREE FIELDS WE CHANGE. `Header`, `SortOrder`, `Traversal`,
      // `ViewFilter`, `FocusedViewFilter` and `ItemShape` stay exactly as
      // captured — including the Header, which is the seed's own.
      body.Body.ParentFolderIds =
        [{__type: "DistinguishedFolderId:#Exchange", Id: distinguishedId}];
      // MailListItem is the only shape returning ConversationId + IsRead +
      // Categories + InferenceClassification together; the captured call asks
      // for BulkActionItem, which returns ids only.
      body.Body.ShapeName = "MailListItem";
      body.Body.Paging = {__type: "IndexedPageView:#Exchange",
                          BasePoint: "Beginning", Offset: offset,
                          MaxEntriesReturned: pageSize};
      return call("FindItem", body).then(function (r) {
        var msg = firstItem(r);
        pages.push({page: page, status: r.status, ms: r.ms,
                    code: msg && msg.ResponseCode, offset: offset});
        if (!msg || msg.ResponseCode !== "NoError") {
          throw new Error("FindItem " + distinguishedId + " page " + page
                          + " failed: http " + r.status + " code "
                          + (msg && msg.ResponseCode));
        }
        total = msg.RootFolder.TotalItemsInView;
        (msg.RootFolder.Items || []).forEach(function (it) {
          items.push({
            itemId: it.ItemId && it.ItemId.Id,
            convId: it.ConversationId && it.ConversationId.Id,
            isRead: it.IsRead === true,
            cls: it.InferenceClassification || null,
            categories: it.Categories || [],
            subject: it.Subject || "",
            /* The SENDER is a typed field on every enumerated item, and Phase
             * 1.5 cannot triage without it: the priority map is keyed by sender,
             * `recurring-automated-sender` counts rows per sender, and "a P0
             * sender is never noise" names it outright. Measured on run 117: 283
             * of 303 rows reached the judgment layer with `sender: null`. */
            sender: (it.From && it.From.Mailbox
                     && (it.From.Mailbox.EmailAddress || it.From.Mailbox.Name)) || null,
            received: it.DateTimeReceived || null,
          });
        });
        page += 1;
        if (msg.RootFolder.IncludesLastItemInRange) { terminated = true; return; }
        offset += pageSize;
        return step();
      });
    }

    return step().then(function () {
      return {folder: distinguishedId, folder_total: total, items: items,
              pages: pages, page_count: page, terminated: terminated,
              at: new Date().toISOString()};
    });
  }

  /* ---------------- GetItem body ------------------------------------------ */
  function fetchBody(itemId, isRead, budget) {
    if (isRead !== true) {
      return Promise.reject(new Error("refusing to fetch a message not known to be read"));
    }
    var body = {
      __type: "GetItemJsonRequest:#Exchange",
      Header: header(),
      Body: {
        __type: "GetItemRequest:#Exchange",
        // AllProperties + Text. No read-flag field exists on this request and
        // none is added: read state is proven by re-enumeration, never asserted.
        ItemShape: {__type: "ItemResponseShape:#Exchange",
                    BaseShape: "AllProperties", BodyType: "Text"},
        ItemIds: [{__type: "ItemId:#Exchange", Id: itemId}],
      },
    };
    return call("GetItem", body).then(function (r) {
      var msg = firstItem(r);
      var item = msg && msg.Items && msg.Items[0];
      var text = (item && item.Body && item.Body.Value) || "";
      var clipped = text.length > budget ? text.slice(0, budget) : text;
      return sha256(clipped).then(function (digest) {
        return {
          status: r.status, ms: r.ms, code: msg && msg.ResponseCode,
          ok: !!(msg && msg.ResponseCode === "NoError" && item),
          text: clipped,
          body_chars: clipped.length,
          raw_chars: text.length,
          body_sha256: clipped ? digest : null,
          is_read_after_fetch: item ? item.IsRead : null,
          sender: (item && item.From && item.From.Mailbox
                   && (item.From.Mailbox.EmailAddress || item.From.Mailbox.Name)) || null,
          sent: (item && (item.DateTimeSent || item.DateTimeReceived)) || null,
          subject: (item && item.Subject) || "",
        };
      });
    }).catch(function (e) {
      return {status: null, ms: null, code: null, ok: false, text: "",
              body_chars: 0, raw_chars: 0, body_sha256: null,
              is_read_after_fetch: null, error: String(e).slice(0, 200)};
    });
  }

  /* ---------------- the DOM scanner (the completeness cross-check) ---------
   * The SAME algorithm as tools/cos_browser_scan.mjs: same identity field
   * (`[role="option"][data-convid]`), same declaredSize source (`aria-setsize`),
   * same stop rule (3 stagnant scans at list end). Scrolling only — the tab has
   * to be rendering, which is why the REST leg above exists at all.
   */
  function scrollContainer() {
    var el = listRows()[0];
    if (!el) return null;
    for (var n = el, d = 0; n && d < 14; n = n.parentElement, d += 1) {
      var s = getComputedStyle(n);
      if (/auto|scroll/.test(s.overflowY) && n.scrollHeight > n.clientHeight + 1) return n;
    }
    return null;
  }

  /* WHICH view the scan covers. The Focused/Other split is a UI filter and
   * switching it means CLICKING a tab, which this file may not do. So the scan
   * reports the view it actually saw and the host cross-checks it against the
   * matching `InferenceClassification` partition of the REST census — a set
   * comparison over the same population, with no input synthesis. */
  function selectedView() {
    var t = [].slice.call(document.querySelectorAll('[role="tab"][aria-selected="true"]'))
      .map(function (e) { return (e.innerText || "").trim(); })
      .filter(function (s) { return s === "Focused" || s === "Other"; });
    return t[0] || null;
  }

  function scanView(maxScrolls) {
    var c = scrollContainer();
    if (!c) return Promise.resolve({ids: [], declared: 0, scrolls: 0,
                                    stagnant: 0, at_end: false, complete: false,
                                    view: selectedView()});
    var seen = Object.create(null);
    var count = 0, declared = 0, stagnant = 0, scrolls = 0;
    c.scrollTop = 0;
    return sleep(700).then(function loop() {
      var before = count;
      listRows().forEach(function (e) {
        var id = e.getAttribute("data-convid");
        if (id && !seen[id]) { seen[id] = 1; count += 1; }
        var d = Number(e.getAttribute("aria-setsize")) || 0;
        if (d > declared) declared = d;
      });
      var atEnd = c.scrollHeight > 0 && c.scrollTop + c.clientHeight >= c.scrollHeight - 2;
      if (atEnd && count === before) stagnant += 1; else if (!atEnd) stagnant = 0;
      if ((atEnd && stagnant >= 3) || scrolls >= maxScrolls) {
        return {ids: Object.keys(seen), declared: declared, scrolls: scrolls,
                stagnant: stagnant, at_end: atEnd, view: selectedView(),
                complete: atEnd && stagnant >= 3 && declared > 0 && count === declared};
      }
      var prev = c.scrollTop;
      c.scrollTop = c.scrollTop + Math.max(320, Math.floor(c.clientHeight * 0.9));
      scrolls += 1;
      return sleep(330).then(function () {
        if (c.scrollTop !== prev) stagnant = 0;
        return loop();
      });
    });
  }

  /* ---------------- the DOM bridge ----------------------------------------
   * The host's world cannot read `window.__cosRun` (separate heap) but both
   * worlds address the same document. `#__cos_out` mirrors the run state; the
   * host writes `#__cos_in` with `{seq, opts}` and a rising `seq` starts a pass.
   *
   * A HIDDEN `<div>`, not a `<script type="application/json">`. OWA enforces
   * Trusted Types, so assigning `textContent` on a script element throws
   * `This document requires 'TrustedScript' assignment` — and the interesting
   * part is where that threw: the host's write returned no error and simply
   * stored nothing (measured 2026-08-10, 0 of 20,534 characters). A div's
   * `textContent` is not a script sink, so it is neither blocked nor a code
   * channel.
   */
  var OUT_ID = "__cos_out";
  var IN_ID = "__cos_in";

  function node(id) {
    var el = document.getElementById(id);
    if (!el) {
      el = document.createElement("div");
      el.id = id;
      el.hidden = true;
      document.documentElement.appendChild(el);
    }
    return el;
  }

  function mirror() {
    try { node(OUT_ID).textContent = JSON.stringify(state); }
    catch (e) { node(OUT_ID).textContent = JSON.stringify({phase: "mirror-error", done: true, error: String(e)}); }
  }

  var lastSeq = 0;
  function pump() {
    var el = document.getElementById(IN_ID);
    var msg = null;
    if (el && el.textContent) {
      // A half-written node is retried on the next tick; a FAILING RUN is not
      // swallowed with it, which is why the parse and the start are separate.
      try { msg = JSON.parse(el.textContent); } catch (e) { msg = null; }
    }
    if (msg && msg.seq > lastSeq) {
      lastSeq = msg.seq;
      // Stamped BEFORE the run starts. Without it the host's first poll can read
      // the PREVIOUS pass's `done: true` and accept pass 1's payload as pass 2's.
      state.seq = msg.seq;
      state.done = false;
      window.__cosDriverRun(msg.opts || {});
    }
    mirror();
  }

  /* A RE-BOOT REPLACES THE PUMP. Re-evaluating this file builds a fresh `state`
   * and a fresh closure, but an `if (!window.__cosPump)` guard left the PREVIOUS
   * interval running — and that one mirrors the PREVIOUS `state`. The host then
   * reads a bridge node written by the old closure and sees the old run's
   * outcome: measured on run 114, where three consecutive attempts reported the
   * identical HTTP 500 that the first attempt had earned and the live mailbox
   * was answering 200 the whole time. A stale mirror is indistinguishable from a
   * reproducible failure, which is the worst thing a diagnostic can be.
   */
  if (window.__cosPump) clearInterval(window.__cosPump);
  window.__cosPump = setInterval(pump, 500);
  // The previous boot's options must not start a pass under this one's closure.
  var stale = document.getElementById(IN_ID);
  if (stale) stale.textContent = "";
  mirror();

  /* ---------------- the run ------------------------------------------------ */
  window.__cosDriverRun = function (opts) {
    var o = opts || {};
    var cap = o.cap || 20;
    var budget = o.budget || 4000;
    var pageSize = o.page_size || 100;
    var maxPages = o.max_pages || 60;
    var sentWindowStart = o.sent_window_start;   // ISO string

    state.phase = "seed"; state.done = false; state.error = null;
    try {
      buildSeed();
    } catch (e) {
      // A seed failure is a RESULT, not an exception thrown at a pump that
      // cannot report it: the host must see `phase: "seed"` and the reason.
      state.seed_kind = seedKind;
      state.error = String(e).slice(0, 400);
      state.phase = "seed"; state.done = true;
      mirror();
      return Promise.resolve("error");
    }
    // Recorded the moment it is decided, so a run that dies on the first call
    // still says WHICH envelope it was using when the server refused it.
    state.seed_kind = seedKind;

    return Promise.resolve()
      .then(function () {
        state.phase = "scan";
        return scanView(o.max_scrolls || 200);
      })
      .then(function (scan) {
        state.out.scan = {ids: scan.ids, declared: scan.declared,
                          scrolls: scan.scrolls, stagnant_scans: scan.stagnant,
                          at_end: scan.at_end, complete: scan.complete,
                          view: scan.view, at: new Date().toISOString()};
        state.phase = "enumerate";
        return enumFolder("inbox", pageSize, maxPages);
      })
      .then(function (en) {
        state.out.enumeration = Object.assign({seed_kind: seedKind}, en);
        state.phase = "sent";
        return enumFolder("sentitems", 50, 4).then(function (s) {
          var items = s.items.filter(function (it) {
            return it.received && new Date(it.received) >= new Date(sentWindowStart);
          }).map(function (it) {
            return {item_id: it.itemId, timestamp: new Date(it.received).toISOString()};
          });
          state.out.sent = {items: items, folder_total: s.folder_total,
                            terminated: s.terminated,
                            boundary: s.terminated ? "list-end" : "older-than-window",
                            captured_at: new Date().toISOString()};
        });
      })
      .then(function () {
        state.phase = "bodies";
        var draw = (o.draw || []).slice(0, cap);          // [{itemId, convId}]
        var out = [];
        function next(i) {
          if (i >= draw.length) return Promise.resolve();
          var d = draw[i];
          return fetchBody(d.itemId, true, budget).then(function (r) {
            out.push(Object.assign({conv_id: d.convId, item_id: d.itemId, seq: i + 1}, r));
            state.out.bodies = out;
            return next(i + 1);
          });
        }
        return next(0).then(function () { state.out.bodies = out; });
      })
      .then(function () { state.phase = "done"; state.done = true; return "ok"; })
      .catch(function (e) {
        state.error = String(e).slice(0, 400);
        state.phase = "error";
        state.done = true;
        return "error";
      });
  };

  return "cos-driver-page-loaded";
})();
