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

  /* ---------------- attachments, off the SAME GetItem ----------------------
   * `AllProperties` already asks for every first-class property, attachments
   * included, so this is a READ of a response the run has always paid for —
   * no second call, no new authorization, nothing added to the envelope.
   *
   * The read lane has NEVER written an attachment name. `attachment_lane:
   * "not-exercised"` was hardcoded on every ledger row, `row["attachments"]`
   * was consumed by `cos_ingest_bridge_content._attachment_names` and produced
   * by nothing, and the live taxonomy routes 3 of its 12 categories to a
   * file-carrying lane (`regulatory-filing` -> attachment, `market-digest` and
   * `system-notification` -> both). A candidate in any of those quarantines
   * `attachment-names-missing` — the bridge correctly refuses to guess a file
   * the manifest would claim.
   *
   * `has_attachments` and `item_keys` ride along DELIBERATELY: if this build
   * withholds `Attachments` under `AllProperties`, the run says so in its own
   * evidence instead of reporting an empty list that reads as "no attachment".
   * An absent list and an empty one are different facts.
   */
  function attachmentsOf(item) {
    var raw = (item && item.Attachments) || [];
    if (!raw.length) return [];
    return raw.map(function (a) {
      return {
        filename: (a && a.Name) || "",
        approx_size_bytes: (a && typeof a.Size === "number") ? a.Size : null,
        content_type: (a && a.ContentType) || null,
        attachment_id: (a && a.AttachmentId && a.AttachmentId.Id) || null,
        is_inline: !!(a && a.IsInline),
      };
    }).filter(function (a) { return a.filename; });
  }

  /* ---------------- GetItem body ------------------------------------------ */
  /* HTML -> readable text. Runs ONLY on the fallback shape below, so a message
   * whose Text body worked is never put through it. `DOMParser` is not a script
   * sink, so Trusted Types does not block it; on any failure keep the raw
   * markup rather than lose the body entirely. */
  function htmlToText(html) {
    try {
      var doc = new DOMParser().parseFromString(html, "text/html");
      return (doc && doc.body && doc.body.textContent) || html;
    } catch (e) { return html; }
  }

  function getItemOnce(itemId, budget, shape, bodyType) {
    var body = {
      __type: "GetItemJsonRequest:#Exchange",
      Header: header(),
      Body: {
        __type: "GetItemRequest:#Exchange",
        // No read-flag field exists on this request and none is added: read
        // state is proven by re-enumeration, never asserted.
        ItemShape: {__type: "ItemResponseShape:#Exchange",
                    BaseShape: shape, BodyType: bodyType},
        ItemIds: [{__type: "ItemId:#Exchange", Id: itemId}],
      },
    };
    return call("GetItem", body).then(function (r) {
      var msg = firstItem(r);
      var item = msg && msg.Items && msg.Items[0];
      var text = (item && item.Body && item.Body.Value) || "";
      if (bodyType === "HTML" && text) text = htmlToText(text);
      var clipped = text.length > budget ? text.slice(0, budget) : text;
      return sha256(clipped).then(function (digest) {
        return {
          status: r.status, ms: r.ms, code: msg && msg.ResponseCode,
          ok: !!(msg && msg.ResponseCode === "NoError" && item),
          body_shape: shape + "/" + bodyType,
          // WHAT THE ITEM IS, not only how the fetch went (2026-09-04). Seven
          // threads answer 500 on `AllProperties/Text` and NoError/200 with
          // zero characters on `Default/HTML`, every night. Both answers are
          // about the REQUEST; neither says what was requested. Their subjects
          // all read as meeting invitations, and a calendar item's body is not
          // where a message's body is -- but a subject is a guess and
          // `ItemClass` is the answer, so record it rather than reason about it.
          item_class: (item && item.ItemClass) || null,
          text: clipped,
          body_chars: clipped.length,
          raw_chars: text.length,
          body_sha256: clipped ? digest : null,
          is_read_after_fetch: item ? item.IsRead : null,
          attachments: attachmentsOf(item),
          has_attachments: item ? !!item.HasAttachments : null,
          item_keys: (item && !(item.Attachments || []).length
                      && item.HasAttachments) ? Object.keys(item).sort() : null,
          sender: (item && item.From && item.From.Mailbox
                   && (item.From.Mailbox.EmailAddress || item.From.Mailbox.Name)) || null,
          sent: (item && (item.DateTimeSent || item.DateTimeReceived)) || null,
          subject: (item && item.Subject) || "",
        };
      });
    }).catch(function (e) {
      return {status: null, ms: null, code: null, ok: false, text: "",
              body_shape: shape + "/" + bodyType,
              body_chars: 0, raw_chars: 0, body_sha256: null,
              is_read_after_fetch: null, error: String(e).slice(0, 200)};
    });
  }

  /* A SECOND SHAPE FOR A BODY THE FIRST ONE DID NOT GET (2026-09-03).
   * Run 254 named both failures for the first time, after eleven nights that
   * recorded only the word `error`: six threads answered HTTP 500 and one
   * answered `NoError`/200 with a 30-character body. Both are answers about
   * the REQUEST. `AllProperties` asks the server to serialise every property
   * the item has, which is the thing a server can fail on; `Text` is empty for
   * an item that carries only an HTML body. `Default`/`HTML` asks for neither.
   *
   * It runs ONLY when the first shape produced no usable body, so a healthy
   * read is never touched, and a failed fallback keeps the FIRST attempt's
   * recorded reason with its own beside it — never replacing one cause with
   * another.
   *
   * `shellChars` is THREADED FROM THE HOST, never restated here. The threshold
   * has one definition (`brain.cos_runverify_checks._EMPTY_SHELL_CHARS`) and a
   * second copy in this file would be the same defect `body_open_succeeded`
   * was written to close. Absent, only a genuinely empty body counts as
   * missing — a safe degradation, not a guessed number. */
  function bodyLanded(r, shellChars) {
    return !!(r && r.ok && r.body_chars > (shellChars == null ? 0 : shellChars));
  }

  function fetchBody(itemId, isRead, budget, shellChars) {
    if (isRead !== true) {
      return Promise.reject(new Error("refusing to fetch a message not known to be read"));
    }
    return getItemOnce(itemId, budget, "AllProperties", "Text").then(function (r) {
      if (bodyLanded(r, shellChars)) return r;
      return getItemOnce(itemId, budget, "Default", "HTML").then(function (r2) {
        if (bodyLanded(r2, shellChars)) return r2;
        r.retry_status = r2.status;
        r.retry_code = r2.code;
        r.retry_error = r2.error || null;
        r.retry_shape = r2.body_shape;
        // AND HOW MUCH IT RETURNED. Run 256 recorded that the fallback
        // answered `NoError`/200 on six threads whose first shape answered
        // 500 -- but not whether it came back with nothing or with a stub, and
        // those are different diagnoses. Recording the status without the size
        // is the same half-answer the ledger gave for eleven nights.
        r.retry_chars = r2.body_chars;
        // The fallback is the shape that ANSWERS on these rows, so it is the
        // one whose `ItemClass` is worth keeping; the first shape 500s and
        // returns no item at all.
        r.retry_item_class = r2.item_class;
        return r;
      });
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

  /* SCROLL CADENCE. Measured 2026-08-18: these were tuned to Chrome's row
   * hydration rate. ego lite renders a Space as an isolated BrowserContext and
   * hydrates rows about half as fast, so 0.9-viewport steps at 330 ms outran it
   * and the scan ended early and CLEAN — atEnd, stagnant, no error — at 119 of
   * 228. Half-viewport steps at 1200 ms collect all 228 on both browsers. A scan
   * that stops early without failing is the dangerous shape, so the cadence is
   * slow enough for the slowest surface rather than fastest for the quickest. */
  var STEP_FRACTION = 0.5, DWELL_MS = 1200, STAGNANT_STOP = 12;

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
      if ((atEnd && stagnant >= STAGNANT_STOP) || scrolls >= maxScrolls) {
        return {ids: Object.keys(seen), declared: declared, scrolls: scrolls,
                stagnant: stagnant, at_end: atEnd, view: selectedView(),
                complete: atEnd && stagnant >= STAGNANT_STOP && declared > 0 && count === declared};
      }
      var prev = c.scrollTop;
      c.scrollTop = c.scrollTop + Math.max(160, Math.floor(c.clientHeight * STEP_FRACTION));
      scrolls += 1;
      return sleep(DWELL_MS).then(function () {
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
      // The bridge carries an ACTION now. Absent, it is the read pass — every
      // caller that predates the attachment lane keeps working unchanged.
      if (msg.action === "attachments") window.__cosFetchAttachments(msg.opts || {});
      else window.__cosDriverRun(msg.opts || {});
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

  /* ---------------- the attachment BYTES ----------------------------------
   * `GetAttachment` over the SAME captured envelope every other read uses.
   * There is deliberately no browser download here: v5.38 (ING-06) had the
   * lane trigger an in-browser download and hope it landed where the host
   * sweeper reads, and every file went to the browser's default folder
   * instead — which is why the ingest manifests stop at 2026-07-17. Bytes
   * that come back through the response cannot land in the wrong folder,
   * cannot be `download_status: "landed-elsewhere"`, and need no
   * `Browser.setDownloadBehavior` on anyone's profile.
   *
   * NO `AttachmentShape`, AND THAT IS MEASURED, NOT TIDINESS. Probed on the
   * live mailbox 2026-08-22 over one real 19 KB CSV, six variants, one thing
   * varied at a time: sending an `AttachmentResponseShape` is HTTP 500
   * `ErrorInternalServerError` with `IncludeMimeContent` either true OR false,
   * while omitting the shape entirely returns 200 `NoError` and 25,412 base64
   * characters. The `RequestAttachmentId` `__type` is optional — bare, typed
   * and mistyped all returned the same bytes. The GET route OWA is often said
   * to use, `/owa/service.svc/s/GetFileAttachment?id=…`, is also 500 here.
   */
  function fetchAttachment(attachmentId, maxBytes) {
    var body = {
      __type: "GetAttachmentJsonRequest:#Exchange",
      Header: header(),
      Body: {
        __type: "GetAttachmentRequest:#Exchange",
        AttachmentIds: [{__type: "RequestAttachmentId:#Exchange",
                         Id: attachmentId}],
      },
    };
    return call("GetAttachment", body).then(function (r) {
      var msg = firstItem(r);
      if (!msg) {
        /* A REFUSAL MUST SAY WHAT IT WAS. `no-content` told run-166's probe
         * nothing: the call was HTTP 500 and the report could not say so.
         * Only typed fault fields travel — never the error page's text, which
         * can carry session detail. */
        var b = r.json && r.json.Body;
        return {ok: false, status: r.status, code: (b && b.ResponseCode) || null,
                fault: (b && (b.MessageText || b.ResponseClass)) || null,
                non_json: r.non_json, bytes: 0, content: null,
                error: "no-response-message"};
      }
      var att = msg && msg.Attachments && msg.Attachments[0];
      var b64 = (att && att.Content) || "";
      /* The DECODED length, not the base64 length: the host writes bytes and
       * checks bytes, and a size compared in the wrong unit is a check that
       * passes on a truncated file. */
      var bytes = b64 ? Math.floor(b64.replace(/=+$/, "").length * 3 / 4) : 0;
      if (maxBytes && bytes > maxBytes) {
        return {ok: false, status: r.status, code: msg && msg.ResponseCode,
                bytes: bytes, content: null, error: "over-max-bytes"};
      }
      /* A SUCCESSFUL CALL THAT CARRIES NO BYTES MUST SAY SO BY NAME
       * (2026-09-06). `ok` was already false in this case, but the only thing
       * travelling back was `code: "NoError"` — which reads as SUCCESS to
       * every reader downstream. The backfill's report showed two files as
       * `written: false, reason: "NoError"`, and nothing in that sentence says
       * whether the fetch should be retried, waited on, or never attempted
       * again.
       *
       * It is the last of those. EWS returns an ItemAttachment — an email
       * attached to another email — with an `Item`, never a `Content`, so
       * there are no bytes to write and there never will be. Measured on the
       * reference host that day: both files the backfill called "still
       * fetchable" were attached MESSAGES whose filename is a subject line
       * ("RE: ...", "[Draft] ..."), each reported with an empty ContentType.
       * Retrying them forever is the failure mode this names away. */
      var attType = (att && att.__type) || "";
      var noBytes = !b64 && msg && msg.ResponseCode === "NoError" && att;
      var itemAttachment = attType.indexOf("ItemAttachment") === 0
                           || (noBytes && !!att.Item);
      return {
        ok: !!(msg && msg.ResponseCode === "NoError" && att && b64),
        status: r.status, ms: r.ms, code: msg && msg.ResponseCode,
        name: (att && att.Name) || "", content_type: (att && att.ContentType) || null,
        attachment_type: attType || null,
        bytes: bytes, content: b64 || null,
        error: noBytes ? (itemAttachment ? "item-attachment-has-no-bytes"
                                         : "no-content-despite-noerror")
                       : undefined,
      };
    }).catch(function (e) {
      return {ok: false, status: null, code: null, bytes: 0, content: null,
              error: String(e).slice(0, 200)};
    });
  }

  /* One call, one attachment, results in request order. The host drives the
   * list so the SELECTION (which files are worth the bytes) stays a host
   * decision — the page never picks. */
  window.__cosFetchAttachments = function (opts) {
    var o = opts || {};
    var ids = o.ids || [];
    var maxBytes = o.max_bytes || 0;
    state.phase = "attachments"; state.done = false; state.error = null;
    try { buildSeed(); } catch (e) {
      state.error = String(e).slice(0, 400); state.phase = "error";
      state.done = true; mirror(); return "error";
    }
    var out = [];
    function next(i) {
      if (i >= ids.length) return Promise.resolve();
      return fetchAttachment(ids[i], maxBytes).then(function (r) {
        out.push(Object.assign({attachment_id: ids[i], seq: i + 1}, r));
        state.out.attachments = out;
        return next(i + 1);
      });
    }
    return next(0).then(function () {
      state.out.attachments = out;
      state.phase = "done"; state.done = true; return "ok";
    }).catch(function (e) {
      state.error = String(e).slice(0, 400); state.phase = "error";
      state.done = true; return "error";
    });
  };

  /* ---------------- the run ------------------------------------------------ */
  window.__cosDriverRun = function (opts) {
    var o = opts || {};
    var cap = o.cap || 20;
    var budget = o.budget || 4000;
    var pageSize = o.page_size || 100;
    var maxPages = o.max_pages || 60;
    var sentWindowStart = o.sent_window_start;   // ISO string
    /* Pass-1 sent items, kept in the CLOSURE and never on `state.out`: the
     * mirrored state reaches the host and the evidence file, and these rows
     * carry the subjects and recipients of the owner's own sent mail. Pen 3
     * needs only the ids, and only long enough to pick which bodies to fetch. */
    var sentRaw = [];

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
        /* THE DRAFTS CENSUS (FIX-01, one draft per thread). The mutation
         * planner refuses to draft a thread carrying a draft THE OWNER wrote
         * (a porter draft is replaced instead — owner ruling 2026-08-28), but
         * the fact it refuses ON must come from somewhere: the drafts live in
         * the Drafts folder, which the inbox enumeration never sees, so the
         * ledger row had no field to carry and the promise was vacuous — six
         * drafts stacked on each of four threads across runs 124-188 before
         * anyone could tell. This enumerates the SAME shape on the drafts
         * distinguished folder (one extra FindItem, the sentitems window above
         * is the precedent) and stamps `isDraft` on every inbox item whose
         * conversation carries one. FAIL-SOFT on purpose: a census that cannot
         * run records why and leaves the flag off rather than stopping the
         * night — the planner's second belt (the lane's own prior undo rows)
         * still names the threads the automation itself drafted, which it
         * replaces. What is lost when this census fails is the OWNER'S draft,
         * so the failure direction is: his draft may be re-drafted beside,
         * never deleted. The discard lane admits only this lane's own signed
         * saves, so it cannot reach his. */
        state.phase = "drafts-census";
        return enumFolder("drafts", 100, 4).then(function (dr) {
          var drafted = {};
          (dr.items || []).forEach(function (it) {
            if (it.convId) drafted[it.convId] = true;
          });
          (state.out.enumeration.items || []).forEach(function (it) {
            if (it.convId && drafted[it.convId]) it.isDraft = true;
          });
          state.out.enumeration.drafts_census = {
            folder_total: dr.folder_total, terminated: dr.terminated,
            drafted_conversations: Object.keys(drafted).length};
        }, function (e) {
          state.out.enumeration.drafts_census = {
            failed: String(e).slice(0, 200)};
        });
      })
      .then(function () {
        state.phase = "sent";
        return enumFolder("sentitems", 50, 4).then(function (s) {
          var items = s.items.filter(function (it) {
            return it.received && new Date(it.received) >= new Date(sentWindowStart);
          }).map(function (it) {
            /* `conv_id` IS THE WHOLE OF FB-05 (PEN 3). This projection kept
             * `{item_id, timestamp}` because its consumer is the zero-send
             * proof, which only ever compares id SETS — so the one field that
             * joins a sent reply to the thread the porter drafted on was read
             * every night and dropped. `cos_signals`' docstring named it as
             * "the one change that would close it". The zero-send criteria
             * validate `item_id` and `timestamp` and tolerate extra keys, so
             * this adds a field and changes no proof. */
            return {item_id: it.itemId, conv_id: it.convId || null,
                    timestamp: new Date(it.received).toISOString()};
          });
          state.out.sent = {items: items, folder_total: s.folder_total,
                            terminated: s.terminated,
                            boundary: s.terminated ? "list-end" : "older-than-window",
                            captured_at: new Date().toISOString()};
          sentRaw = s.items;
        });
      })
      .then(function () {
        /* PEN 3's BODIES — OPT-IN, SIZE-BOUNDED, AND OFF UNLESS ASKED. The host
         * hands `sent_body_convs` (only conversations its undo ledger says this
         * lane drafted on) and `sent_body_cap`; with neither, this phase does
         * nothing and the night is byte-identical to before. A fetch that fails
         * records its error and NOTHING else: the classifier refuses to call an
         * unreadable body `sent-as-is`, and it can only refuse what it is told
         * about. */
        var want = o.sent_body_convs || [];
        var bodyCap = o.sent_body_cap || 0;
        state.out.sent_bodies = [];
        if (!want.length || bodyCap <= 0) return;
        state.phase = "sent-bodies";
        var wanted = {};
        want.forEach(function (c) { wanted[c] = true; });
        var picks = sentRaw.filter(function (it) {
          return it.convId && wanted[it.convId]
                 && it.received
                 && new Date(it.received) >= new Date(sentWindowStart);
        }).slice(0, bodyCap);
        function nextSent(i) {
          if (i >= picks.length) return Promise.resolve();
          var it = picks[i];
          return fetchBody(it.itemId, true, budget, o.shell_chars).then(
            function (r) {
              state.out.sent_bodies.push(Object.assign(
                {conv_id: it.convId, item_id: it.itemId}, r));
              return nextSent(i + 1);
            },
            function (e) {
              state.out.sent_bodies.push({conv_id: it.convId,
                                          item_id: it.itemId,
                                          error: String(e).slice(0, 200)});
              return nextSent(i + 1);
            });
        }
        return nextSent(0);
      })
      .then(function () {
        state.phase = "bodies";
        var draw = (o.draw || []).slice(0, cap);          // [{itemId, convId}]
        var out = [];
        function next(i) {
          if (i >= draw.length) return Promise.resolve();
          var d = draw[i];
          return fetchBody(d.itemId, true, budget, o.shell_chars).then(function (r) {
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
