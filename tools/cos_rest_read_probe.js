/**
 * COS in-page REST read probe (S01 spike, 2026-08-10).
 *
 * Paste-and-run source for the read lane proven in `_evidence/s01/rest-read-proof.json`:
 * enumerate a mail folder and fetch message bodies through OWA's OWN in-page backend
 * (`outlook.cloud.microsoft/owa/service.svc`) from the signed-in tab, with no window
 * visible and no unread mail touched.
 *
 * HARD RULES this file encodes (do not relax them):
 *  - The bearer NEVER leaves page context. Every call is a `fetch()` issued by the page
 *    itself, reusing a request the page already made. Lifting the token into a shell or a
 *    Python process is the exact token-theft pattern SOCs hunt for.
 *  - READ-ONLY verbs only: `FindItem` and `GetItem`. Both are fail-safe — a bad shape
 *    errors and mutates nothing. Mutation verbs must replay a shape the server already
 *    accepted for that verb (chief-of-staff SKILL.md, request-construction split).
 *  - Never fetch an item whose `IsRead` is false. `fetchBody()` refuses.
 *  - Never emit body text. Callers get `body_chars` + a SHA-256 digest.
 *
 * Usage, in the signed-in OWA tab:
 *   1. `installCapture()`  — before OWA issues the call you want to replay.
 *   2. Scroll the message list past its cached prefix. Folder CLICKS do not re-enumerate
 *      (Monarch serves them from cache / the Loki MessageService); paging the virtualized
 *      list is what makes OWA fire a real `FindItem`.
 *   3. `const cos = buildProbe()` — seals the captured envelope.
 *   4. `await cos.enumFolder('inbox')`, `await cos.fetchBody(itemId, {isRead:true})`.
 *
 * ponytail: plain functions on `window`, no build step, no module system — this is pasted
 * into a live page by an agent, not imported. Promote to a bundled module only if a second
 * caller appears.
 */

/* eslint-env browser */

function installCapture(max = 600) {
  if (window.__cosCap) return "already installed";
  const cap = { calls: [], max };
  window.__cosCap = cap;
  const orig = window.fetch;
  window.__cosOrigFetch = orig;
  window.fetch = function (input, init) {
    let url;
    let method = "GET";
    const headers = {};
    let bodyPromise = null;
    try {
      if (input instanceof Request) {
        url = input.url;
        method = input.method;
        input.headers.forEach((v, k) => { headers[k] = v; });
        try { bodyPromise = input.clone().text(); } catch (e) { /* body already consumed */ }
      } else {
        url = String(input);
      }
      if (init) {
        if (init.method) method = init.method;
        const h = init.headers;
        if (h instanceof Headers) h.forEach((v, k) => { headers[k] = v; });
        else if (Array.isArray(h)) h.forEach(([k, v]) => { headers[k] = v; });
        else if (h) Object.assign(headers, h);
        if (typeof init.body === "string") bodyPromise = Promise.resolve(init.body);
      }
    } catch (e) { /* never break the page's own request */ }
    if (cap.calls.length < cap.max) {
      const rec = { t: Date.now(), url, method, headers, body: null };
      cap.calls.push(rec);
      if (bodyPromise) Promise.resolve(bodyPromise).then((t) => { rec.body = t; }).catch(() => {});
    }
    return orig.apply(this, arguments);
  };
  return "installed";
}

function findCapturedAction(action) {
  return (window.__cosCap ? window.__cosCap.calls : []).find((c) => {
    try { return new URL(c.url, location.origin).searchParams.get("action") === action; }
    catch (e) { return false; }
  });
}

function buildProbe() {
  const seed = findCapturedAction("FindItem");
  if (!seed) throw new Error("no captured FindItem — scroll the message list first");

  // OWA carries the JSON request in `x-owa-urlpostdata` as
  // encodeURIComponent(JSON.stringify(body)), with an EMPTY HTTP body. Not base64.
  const encode = (obj) => encodeURIComponent(JSON.stringify(obj));
  const seedBody = JSON.parse(decodeURIComponent(seed.headers["x-owa-urlpostdata"]));
  if (encode(seedBody) !== seed.headers["x-owa-urlpostdata"]) {
    throw new Error("envelope re-encode is not byte-identical — refusing to replay");
  }

  async function replay(bodyObj, action) {
    const url = new URL(seed.url, location.origin);
    if (action) url.searchParams.set("action", action);
    const headers = Object.assign({}, seed.headers);   // sealed envelope, in-page only
    headers["x-owa-urlpostdata"] = encode(bodyObj);
    if (action) headers.action = action;
    const t0 = performance.now();
    const res = await window.__cosOrigFetch(url.toString(), {
      method: "POST", headers, credentials: "include",
    });
    const text = await res.text();
    let json = null;
    try { json = JSON.parse(text); } catch (e) { /* non-JSON error page */ }
    return { status: res.status, ms: Math.round(performance.now() - t0), json, raw: json ? null : text.slice(0, 300) };
  }

  const sha256 = async (s) => {
    const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
    return [...new Uint8Array(buf)].map((x) => x.toString(16).padStart(2, "0")).join("");
  };

  async function enumFolder(distinguishedId, pageSize = 100, maxPages = 50) {
    const items = [];
    let offset = 0;
    let total = null;
    for (let page = 0; page < maxPages; page += 1) {
      const body = JSON.parse(JSON.stringify(seedBody));
      body.Body.ParentFolderIds = [{ __type: "DistinguishedFolderId:#Exchange", Id: distinguishedId }];
      // MailListItem is the only shape that returns ConversationId + IsRead +
      // InferenceClassification; the captured BulkActionItem shape returns ids only.
      body.Body.ShapeName = "MailListItem";
      body.Body.Paging = {
        __type: "IndexedPageView:#Exchange", BasePoint: "Beginning", Offset: offset, MaxEntriesReturned: pageSize,
      };
      const r = await replay(body);
      const msg = r.json && r.json.Body && r.json.Body.ResponseMessages
        && r.json.Body.ResponseMessages.Items && r.json.Body.ResponseMessages.Items[0];
      if (!msg || msg.ResponseCode !== "NoError") {
        throw new Error(`FindItem failed: status ${r.status} code ${msg && msg.ResponseCode}`);
      }
      total = msg.RootFolder.TotalItemsInView;
      for (const it of msg.RootFolder.Items || []) {
        items.push({
          itemId: it.ItemId && it.ItemId.Id,
          convId: it.ConversationId && it.ConversationId.Id,
          isRead: it.IsRead,
          cls: it.InferenceClassification,
          subj: it.Subject,
          recv: it.DateTimeReceived,
        });
      }
      if (msg.RootFolder.IncludesLastItemInRange) break;
      offset += pageSize;
    }
    return { total, items, at: new Date().toISOString(), visibilityState: document.visibilityState };
  }

  // Read one message body. Refuses anything not already read, so the probe can never be
  // the thing that marks mail as read.
  async function fetchBody(itemId, { isRead } = {}) {
    if (isRead !== true) throw new Error("refusing to fetch a message not known to be read");
    const req = {
      __type: "GetItemJsonRequest:#Exchange",
      Header: JSON.parse(JSON.stringify(seedBody.Header)),
      Body: {
        __type: "GetItemRequest:#Exchange",
        // AllProperties + BodyType Text. No read-flag field exists on this request, and
        // none is added — read state is proven by re-enumeration, never by the shape.
        ItemShape: { __type: "ItemResponseShape:#Exchange", BaseShape: "AllProperties", BodyType: "Text" },
        ItemIds: [{ __type: "ItemId:#Exchange", Id: itemId }],
      },
    };
    const r = await replay(req, "GetItem");
    const msg = r.json && r.json.Body && r.json.Body.ResponseMessages
      && r.json.Body.ResponseMessages.Items && r.json.Body.ResponseMessages.Items[0];
    const item = msg && msg.Items && msg.Items[0];
    const text = (item && item.Body && item.Body.Value) || "";
    return {
      status: r.status,
      ms: r.ms,
      code: msg && msg.ResponseCode,
      body_chars: text.length,                       // never the text itself (MNPI)
      body_sha256: text ? await sha256(text) : null,
      is_read_after_fetch: item && item.IsRead,
      visibilityState: document.visibilityState,
    };
  }

  return { replay, enumFolder, fetchBody, sha256, seedBody };
}

/* ------------------------------------------------------------------ *
 * DOM scanner port.
 *
 * `tools/cos_browser_scan.mjs` takes a Codex-runtime tab object exposing
 * `tab.playwright` and `tab.cua`. A Chrome-extension surface has neither, so this is the
 * same algorithm re-expressed as in-page DOM work: SAME identity field
 * (`[role="option"][data-convid]`), SAME declaredSize source (`aria-setsize`), SAME loop
 * (return to top, page down by 0.9 * clientHeight, stop after 3 stagnant scans at list
 * end). `tab.cua.scroll` becomes a `scrollTop` write on the same chosen container.
 *
 * Needs the tab VISIBLE. The virtualized list does not re-render without paint, and the
 * `--disable-backgrounding-occluded-windows` / `--disable-renderer-backgrounding` flags
 * defeat occluded-WINDOW starvation, not background-TAB starvation. The REST lane above
 * needs no rendering at all — that asymmetry is the point of the S01 spike.
 * ------------------------------------------------------------------ */

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const listRows = () => [...document.querySelectorAll('[role="option"][data-convid]')];

function chooseScrollContainer(el) {
  for (let n = el, d = 0; n && d < 14; n = n.parentElement, d += 1) {
    const s = getComputedStyle(n);
    if (/auto|scroll/.test(s.overflowY) && n.scrollHeight > n.clientHeight + 1) return n;
  }
  return null;
}

function inspectView() {
  const els = listRows();
  const container = els[0] ? chooseScrollContainer(els[0]) : null;
  return {
    ids: els.map((e) => e.getAttribute("data-convid")),
    declaredSize: Math.max(0, ...els.map((e) => Number(e.getAttribute("aria-setsize")) || 0)),
    container,
    scrollTop: container ? container.scrollTop : 0,
    scrollHeight: container ? container.scrollHeight : 0,
    clientHeight: container ? container.clientHeight : 0,
  };
}

async function selectView(name) {
  const tab = [...document.querySelectorAll('[role="tab"]')]
    .find((e) => (e.innerText || "").trim() === name);
  if (!tab) throw new Error(`Outlook ${name} tab not found`);
  if (tab.getAttribute("aria-selected") !== "true") { tab.click(); await sleep(1200); }
  for (let i = 0; i < 20 && listRows().length === 0; i += 1) await sleep(400);
}

async function scanView(name, maxScrolls = 120) {
  await selectView(name);
  let st = inspectView();
  for (let i = 0; st.container && st.scrollTop > 1 && i < maxScrolls; i += 1) {
    st.container.scrollTop = 0; await sleep(250); st = inspectView();
  }
  const seen = new Set();
  let declared = st.declaredSize;
  let stagnant = 0;
  let scrolls = 0;
  while (scrolls <= maxScrolls) {
    const before = seen.size;
    for (const id of st.ids) seen.add(id);
    if (st.declaredSize > 0) declared = st.declaredSize;
    const atEnd = st.scrollHeight > 0 && st.scrollTop + st.clientHeight >= st.scrollHeight - 2;
    if (atEnd && seen.size === before) stagnant += 1; else if (!atEnd) stagnant = 0;
    if (atEnd && stagnant >= 3) break;
    if (scrolls === maxScrolls) break;
    const previousTop = st.scrollTop;
    if (st.container) st.container.scrollTop = st.scrollTop + Math.max(320, Math.floor(st.clientHeight * 0.9));
    await sleep(350);
    scrolls += 1;
    st = inspectView();
    if (st.scrollTop !== previousTop) stagnant = 0;
  }
  const atEnd = st.scrollHeight > 0 && st.scrollTop + st.clientHeight >= st.scrollHeight - 2;
  return {
    view: name,
    ids: [...seen],
    declaredSize: declared,
    scrolls,
    terminalStagnantScans: stagnant,
    scrollAtEnd: atEnd,
    complete: atEnd && stagnant >= 3 && declared > 0 && seen.size === declared,
  };
}

async function scanInbox() {
  const focused = await scanView("Focused");
  const other = await scanView("Other");
  const ids = [...new Set([...focused.ids, ...other.ids])];
  return {
    focused,
    other,
    ids,
    declaredSize: focused.declaredSize + other.declaredSize,
    duplicateCount: (focused.ids.length + other.ids.length) - ids.length,
    at: new Date().toISOString(),
    visibilityState: document.visibilityState,
  };
}

/* Self-check: the pure helpers, runnable under node with no browser.
 * ponytail: one runnable assert over the only non-trivial logic (the stagnation loop's
 * completeness rule and the set algebra); the fetch paths need a live signed-in mailbox. */
function demo() {
  const combine = (views) => {
    const ids = [...new Set(views.flatMap((v) => v.ids))];
    const declaredSize = views.reduce((s, v) => s + v.declaredSize, 0);
    const duplicateCount = views.reduce((s, v) => s + v.ids.length, 0) - ids.length;
    return { ids, declaredSize, duplicateCount, complete: ids.length === declaredSize && duplicateCount === 0 };
  };
  const focused = { ids: ["a", "b", "c"], declaredSize: 3 };
  const other = { ids: ["d"], declaredSize: 1 };
  const ok = combine([focused, other]);
  console.assert(ok.ids.length === 4 && ok.declaredSize === 4 && ok.duplicateCount === 0 && ok.complete,
    "clean two-view union must be complete");
  const overlap = combine([{ ids: ["a", "b"], declaredSize: 2 }, { ids: ["b"], declaredSize: 1 }]);
  console.assert(overlap.duplicateCount === 1 && overlap.complete === false,
    "an id seen in both views must fail completeness, never be silently deduped");
  const short = combine([{ ids: ["a"], declaredSize: 270 }]);
  console.assert(short.complete === false, "a partial scan must never report complete");
  return "cos_rest_read_probe demo ok";
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { installCapture, buildProbe, scanView, scanInbox, demo };
  if (require.main === module) console.log(demo());
}
