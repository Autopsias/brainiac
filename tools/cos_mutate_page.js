/**
 * COS mutations — the IN-PAGE half (MUT-01, S04).
 *
 * WHAT THIS IS. Archive, priority-chip write, and reply-draft SAVE, issued as
 * `service.svc` replays from the page's MAIN world — the same lane the read
 * driver uses, with the auth-bearing half never leaving the page. It is a
 * SEPARATE file from `cos_driver_page.js` on purpose: the read driver's proven
 * property is that no mutation verb appears anywhere in it, and
 * `tests/test_cos_driver.py` asserts exactly that. Adding mutations there would
 * have deleted the proof. Here the property is different and it is enforced at
 * RUNTIME rather than by grepping source.
 *
 * THE ALLOWLIST IS THE POINT (adv-9). "No send string in the Python source"
 * proves nothing about an engine whose whole design is to REPLAY CAPTURED
 * PAYLOADS: a captured or substituted fixture can carry a sending disposition
 * with no such literal anywhere in our code, the audit passes, and mail goes
 * out. So every outgoing payload is validated HERE, immediately before
 * `fetch`, against:
 *   - an exact set of permitted actions (three, and they are named positively);
 *   - `MessageDisposition` asserted POSITIVELY equal to save-only — a missing
 *     disposition is a rejection, not a default;
 *   - an exact set of permitted destination folders (archive and inbox; inbox
 *     only because undo moves back);
 *   - the approved captured-request FINGERPRINT for that verb — the payload's
 *     normalized key-path set must equal the shape the server already accepted,
 *     so an extra field cannot ride along;
 *   - recipients bounded to the thread's own participants, with blind-copy
 *     empty.
 * Anything not matching is rejected, recorded, and never dispatched.
 *
 * MUTATIONS REPLAY, THEY NEVER SYNTHESIZE (doctrine v4.7, SKILL.md:1855). Each
 * verb is built by cloning an APPROVED SKELETON — a request shape the server
 * already accepted for that verb, captured from the app's own traffic — and
 * substituting ids at known paths. There is no hand-built mutation body and no
 * fallback that builds one: a verb with no approved shape simply cannot run.
 *
 * IDS ROTATE (G_R2). `ItemId`/`ChangeKey` rotate, and a replay carrying a stale
 * `ChangeKey` can fail OR SILENTLY NO-OP. So the target is re-resolved by
 * `conversation_id` and its `ChangeKey` re-fetched immediately before every
 * mutation, and a response that changed nothing is a VERIFICATION FAILURE, not
 * a success.
 *
 * 449 IS NOT A RETRY (G_R3). A missing/stale canary answers HTTP 449, which no
 * amount of retrying fixes. A 449 DISARMS mutation dispatch and NOTHING IN
 * THIS FILE CAN ARM IT AGAIN: the only path back is the HOST's — a fresh
 * envelope from `cos_cdp_capture.py --prepare`, then a fresh `init` op. Any
 * mutation in flight when it happens is STOPPED and RECONCILED, never retried.
 *
 * 401 IS THE BEARER AGEING OUT, IT IS NAMED HERE, AND IT IS RECOVERED
 * HOST-SIDE — NEVER IN THIS FILE (run 130, 2026-08-13; corrected by review
 * 2026-08-13 round 2). The nightly captures its seed envelope at the top of the
 * run and the mutation lane runs 15-40 minutes later, after the model leg —
 * measured 15m31s on run 126 (which applied) and 17m38s on run 130 (which did
 * not). Run 130's whole rehearsal and its first resolve answered `http 401 code
 * null`, and a 401 had no path here at all: it fell through as an ordinary read
 * failure, so 19 of 19 planned mutations were blocked and the night stopped
 * having done nothing. A 401 now DISARMS exactly as a 449 does, is tagged
 * `auth401`, and is MIRRORED to the host, which is the only leg that can act
 * on it.
 *
 * IT DOES NOT BUY AN IN-PAGE RE-PRIME, AND THE FIRST VERSION OF THIS FIX
 * CLAIMED IT DID. `reprime()` needed an envelope captured AFTER the failure,
 * and this page does not produce one during a mutation pass: the lane never
 * navigates, and its own reads go out through the UNWRAPPED `cap.rawFetch` and
 * are never captured. So `refreshSeed` returned null every time and the
 * recovery was a log line — while `cos_mutate.py` told the operator "one
 * re-prime did not recover it", asserting an attempt the mechanism cannot
 * make. The node suite reported it working because both its cases STUBBED
 * `refreshSeed`.
 *
 * SO IT IS GONE (review 2026-08-13, round 3). Round 2 removed the 401 CALLER
 * and kept `reprime()`, `seedRefresher` and the `reprime` op for "the explicit
 * op" — a caller that does not exist anywhere in `tools/`, and a justification
 * that cannot be tested against production because production never reaches
 * it. Unreachable machinery is not a spare capability, it is a claim nobody
 * checks. The lane that CAN re-seed is the host's: `cos_cdp_capture.py
 * --prepare` navigates, re-installs the hook and takes a fresh envelope, and
 * `cos_nightly.sh` runs it after the model leg and before any mutation-lane
 * traffic; the next `init` op arms this file again. That is the ONE arming
 * path, for a 449 and for a 401 alike.
 *
 * Runs under node for its tests: nothing touches the DOM at load, and the API
 * takes its transport, clock and shapes by injection.
 */

/* eslint-env browser, node */

(function () {
  "use strict";

  /* ---------------- the allowlist ------------------------------------------ */
  var ALLOWED_ACTIONS = {MoveItem: 1, UpdateItem: 1, CreateItem: 1,
                         ApplyConversationAction: 1};

  /* `ApplyConversationAction` is a MULTIPLEXER, and that is why the allowlist
   * pins the Action VALUE rather than the verb. Measured 2026-08-11: this build
   * archives with `Action: "Move"` and categorises with
   * `Action: "UpdateAlwaysCategorizeRule"` — the same verb — and per EWS the
   * same verb also carries Delete, SetReadState and AlwaysDelete. An allowlist
   * on the verb alone would therefore admit deleting the owner's mail.
   *
   * `UpdateAlwaysCategorizeRule` is NOT permitted, and its absence is a
   * decision, not an oversight: it does not write a per-conversation label, it
   * leaves a STANDING RULE that categorises future messages in the thread too.
   * The chip lane is specified as a reversible label, and removing a chip does
   * not remove a rule. Until a per-item categorize shape is captured, or the
   * owner restates what the chip lane may do, this stays refused. */
  /* `UpdateAlwaysCategorizeRule` is admitted by OWNER RULING 2026-08-11 — "I
   * want the chip to be working no matter what; we can use the categories as
   * they work now" — and NOT because it became harmless. It still leaves a
   * standing always-categorize rule on the conversation, the run report says so
   * per chip, and removal stays a human action until a clear shape is captured.
   * `Delete`, `SetReadState` and `AlwaysDelete` remain refused: the ruling
   * admitted an ACTION, not the verb. */
  var ALLOWED_CONVERSATION_ACTIONS = {Move: 1, UpdateAlwaysCategorizeRule: 1};
  var READ_ACTIONS = {FindItem: 1, GetItem: 1};
  var REQUEST_TYPE = {
    MoveItem: "MoveItemRequest",
    UpdateItem: "UpdateItemRequest",
    CreateItem: "CreateItemRequest",
    ApplyConversationAction: "ApplyConversationActionRequest",
  };
  var PERMITTED_FOLDERS = {archive: 1, inbox: 1};
  var DRAFT_FOLDER = "drafts";
  var SAVE_ONLY = "SaveOnly";
  var MANAGED_CHIPS = ["P0 · Now", "P1 · Today", "P2 · This week", "P3 · Read"];
  var SET_FIELD = "SetItemField:#Exchange";
  var CATEGORIES_URI = "item:Categories";

  /* --- denylist (the SECOND belt; the allowlist above is the first) ---------
   * Named so a payload carrying one is REFUSED. These literals exist in this
   * file for exactly that reason, which is why the source audit in
   * `tests/test_cos_mutate.py` reads this block by its markers instead of
   * failing on the mere presence of the words. */
  var BANNED_REQUESTS = ["SendItem", "DeleteItem", "MarkAsJunk",
                         "MarkAllItemsAsRead", "EmptyFolder", "ExportItems",
                         "UploadItems", "Subscribe", "CreateAttachment"];
  var BANNED_DISPOSITIONS = ["SendOnly", "SendAndSaveCopy", "SendToNone",
                             "SendOnlyToAll", "SendOnlyToChanged",
                             "SendToAllAndSaveCopy", "SendToChangedAndSaveCopy"];
  /* --- end denylist -------------------------------------------------------- */

  /* ---------------- shape fingerprints ------------------------------------- */
  /* The normalized KEY-PATH SET of a payload. Array indices collapse to `[]`,
   * so substituting an id or a category list never changes the fingerprint —
   * but adding a field does, which is the only thing this has to catch.
   *
   * ponytail: FNV-1a, not SHA-256. This is a SHAPE identity, compared against a
   * value we wrote ourselves; it is not a security digest and nothing downstream
   * treats it as one. Synchronous matters here — validation happens on the last
   * line before `fetch`, where an `await` is a place for a race to live. */
  function keyPaths(obj, prefix, out) {
    out = out || [];
    prefix = prefix || "";
    if (obj === null || typeof obj !== "object") return out;
    if (Array.isArray(obj)) {
      for (var i = 0; i < obj.length; i++) keyPaths(obj[i], prefix + "[]", out);
      return out;
    }
    var keys = Object.keys(obj).sort();
    for (var k = 0; k < keys.length; k++) {
      var path = prefix ? prefix + "." + keys[k] : keys[k];
      out.push(path);
      keyPaths(obj[keys[k]], path, out);
    }
    return out;
  }

  /* Two path families are OUTSIDE the fingerprint, each for a stated reason —
   * and the list is short on purpose, because this is where a shape check goes
   * quietly vacuous (the same failure the read lane's determinism-diff
   * exclusion list is pinned against).
   *  - `Header.*`: the Header block is the LIVE envelope's own, substituted
   *    wholesale from the call the server just accepted. Pinning it would fail
   *    on a header the server itself blessed.
   *  - the ChangeKey slots: G_R2 REQUIRES a freshly re-fetched ChangeKey on
   *    every mutation, so whether the captured sample happened to carry one
   *    cannot be allowed to decide whether we may send one. */
  var FINGERPRINT_IGNORED = {
    "Body.ItemIds[].ChangeKey": 1,
    "Body.ItemChanges[].ItemId.ChangeKey": 1,
  };

  function ignoredPath(p) {
    return p === "Header" || p.indexOf("Header.") === 0 || !!FINGERPRINT_IGNORED[p];
  }

  function fnv1a(s) {
    var h = 0x811c9dc5;
    for (var i = 0; i < s.length; i++) {
      h ^= s.charCodeAt(i);
      h = (h + ((h << 1) + (h << 4) + (h << 7) + (h << 8) + (h << 24))) >>> 0;
    }
    return ("00000000" + h.toString(16)).slice(-8);
  }

  function fingerprint(body) {
    var uniq = {};
    keyPaths(body).forEach(function (p) { if (!ignoredPath(p)) uniq[p] = 1; });
    return fnv1a(Object.keys(uniq).sort().join("\n")) + "-"
      + Object.keys(uniq).length;
  }

  /* Every string VALUE in the payload, with its path — the walk both the
   * disposition assert and the banned-literal belt read. */
  function stringValues(obj, prefix, out) {
    out = out || [];
    prefix = prefix || "";
    if (typeof obj === "string") { out.push({path: prefix, value: obj}); return out; }
    if (obj === null || typeof obj !== "object") return out;
    if (Array.isArray(obj)) {
      for (var i = 0; i < obj.length; i++) stringValues(obj[i], prefix + "[]", out);
      return out;
    }
    Object.keys(obj).forEach(function (k) {
      stringValues(obj[k], prefix ? prefix + "." + k : k, out);
    });
    return out;
  }

  function get(obj, path) {
    var cur = obj;
    var parts = path.split(".");
    for (var i = 0; i < parts.length; i++) {
      if (cur === null || typeof cur !== "object") return undefined;
      cur = cur[parts[i]];
    }
    return cur;
  }

  function setEq(a, b) {
    var x = (a || []).slice().sort();
    var y = (b || []).slice().sort();
    return x.length === y.length && x.every(function (v, i) { return v === y[i]; });
  }

  function isManaged(c) { return MANAGED_CHIPS.indexOf(c) !== -1; }

  /* ---------------- the validator ------------------------------------------
   * Returns null when the payload may be dispatched, or a plain-language reason
   * why it may not. Never throws: a validator that can die is a validator that
   * can be skipped by a `catch`. */
  function validate(action, body, ctx) {
    ctx = ctx || {};
    if (!ALLOWED_ACTIONS[action]) {
      return "action " + JSON.stringify(action) + " is not one of the three "
        + "permitted mutation actions (" + Object.keys(ALLOWED_ACTIONS).join(", ") + ")";
    }
    if (!body || typeof body !== "object") return "payload is not an object";
    if (!body.Header) return "payload carries no captured request Header to replay";
    var reqType = String((body.Body && body.Body.__type) || "");
    if (reqType.indexOf(REQUEST_TYPE[action]) !== 0) {
      return "payload Body.__type is " + JSON.stringify(reqType) + ", not a "
        + REQUEST_TYPE[action];
    }

    var values = stringValues(body);
    for (var i = 0; i < values.length; i++) {
      var v = values[i].value;
      for (var b = 0; b < BANNED_REQUESTS.length; b++) {
        if (v.indexOf(BANNED_REQUESTS[b] + "Request") === 0
            || v.indexOf(BANNED_REQUESTS[b] + "Type") === 0) {
          return "payload carries a refused request type " + JSON.stringify(v)
            + " at " + values[i].path;
        }
      }
      if (BANNED_DISPOSITIONS.indexOf(v) !== -1) {
        return "payload carries a refused disposition " + JSON.stringify(v)
          + " at " + values[i].path;
      }
    }

    var convAct = String(get(body, "Body.ConversationActions.0.Action") || "");
    /* NO FALLBACK when the payload names a conversation action. Falling back to
     * the bare verb is precisely how an ARCHIVE's approved shape would come to
     * authorise a CHIP — one verb, two jobs, and the shape is what says which
     * job the server accepted. */
    var shape = convAct
      ? (ctx.shapes || {})[shapeKey(action, convAct)]
      : (ctx.shapes || {})[action];
    /* THE SLOT IS BOUND TO ITS VARIANT (review 2026-08-12). The chip's ADD
     * (`Categories`) and its REMOVE (`CategoriesToRemove`) are the same verb,
     * the same Action and two different shapes the server accepted separately
     * (FINDING 2026-08-12), so one entry carries both fingerprints. Offering
     * BOTH of them to one `indexOf` made them interchangeable, which is not
     * what having two slots means — probed: a shape file carrying only the
     * REMOVE fingerprint authorised an ADD payload. So the payload's variant is
     * decided FIRST, from the field that defines it, and only THAT variant's
     * fingerprint can authorise it. */
    var isRemove = get(body, "Body.ConversationActions.0.CategoriesToRemove")
                   !== undefined;
    var approved = shape && (isRemove ? shape.fingerprint_remove
                                      : shape.fingerprint);
    if (!approved) {
      return "no APPROVED captured " + (isRemove ? "REMOVE " : "")
        + "shape on file for " + action
        + " — a mutation request is replayed, never synthesized (doctrine v4.7); "
        + "capture the shape from the app's own action first";
    }
    var got = fingerprint(body);
    if (got !== approved) {
      return "payload fingerprint " + got + " does not match the approved "
        + action + (isRemove ? " REMOVE" : "") + " shape " + approved
        + " — a field was added, removed or renamed relative to the request the "
        + "server already accepted";
    }

    if (action === "ApplyConversationAction") {
      var acts = get(body, "Body.ConversationActions");
      if (!Array.isArray(acts) || acts.length !== 1) {
        return "ApplyConversationAction must carry exactly one ConversationAction"
          + " (got " + (Array.isArray(acts) ? acts.length : "none") + ") — one "
          + "mutation, one undo row";
      }
      var convAction = String(acts[0].Action || "");
      if (!ALLOWED_CONVERSATION_ACTIONS[convAction]) {
        return "ConversationAction " + JSON.stringify(convAction) + " is not "
          + "permitted (only " + Object.keys(ALLOWED_CONVERSATION_ACTIONS).join(", ")
          + "). This verb also carries Delete, SetReadState and "
          + "UpdateAlwaysCategorizeRule, so the ACTION is what is checked, never "
          + "the verb";
      }
      if (!get(body, "Body.ConversationActions.0.ConversationId.Id")) {
        return "the ConversationAction names no ConversationId";
      }

      if (convAction === "UpdateAlwaysCategorizeRule") {
        /* CLEARCATEGORIES IS ASSERTED POSITIVELY, AND ON BOTH VARIANTS (review
         * 2026-08-12). It used to be checked inside the REMOVE branch only, so
         * an ADD payload could carry `ClearCategories: true` and be dispatched
         * — probed: flipping only that field left the fingerprint identical
         * (it is a VALUE, and the fingerprint is over key paths) and
         * `validate()` returned null. `true` takes EVERY category off every
         * thread the uncapped chip lane touches, including the owner's own, so
         * it is asserted EQUAL TO FALSE rather than merely "not true": absent
         * is a rejection and never a default, exactly as `MessageDisposition`
         * is on the draft lane. Both builders set it explicitly for the same
         * reason — a destructive boolean is never inherited by accident. */
        var clearing = get(body, "Body.ConversationActions.0.ClearCategories");
        if (clearing !== false) {
          return "the chip payload carries ClearCategories: "
            + JSON.stringify(clearing) + ", and the ONLY permitted value is "
            + "false (absent is a rejection, never a default) — true takes "
            + "EVERY category off the thread rather than the one managed chip";
        }
        /* THE REMOVE VARIANT (FINDING 2026-08-12). Taking a chip off is NOT a
         * reduced `Categories` — that updates the forward RULE and leaves the
         * thread's current category exactly where it was (server `NoError`,
         * verified-failed-noop) — and it is NOT `ClearCategories: true`, which
         * the UI never sent and the server answered 500. It is its own captured
         * field, so it gets its own gate: exactly one MANAGED chip, one the
         * thread was really carrying, and nothing else riding along. */
        var removing = get(body, "Body.ConversationActions.0.CategoriesToRemove");
        if (removing !== undefined) {
          if (!Array.isArray(removing) || removing.length !== 1) {
            return "a chip removal takes exactly one category off (got "
              + (Array.isArray(removing) ? removing.length : "no array")
              + ") — one mutation, one undo row";
          }
          if (typeof removing[0] !== "string" || !isManaged(removing[0])) {
            return "the chip removal would take off "
              + JSON.stringify(removing[0]) + ", which is not one of the managed "
              + "priority chips (" + MANAGED_CHIPS.join(", ") + "); every other "
              + "category on the thread is the owner's";
          }
          var had = ctx.before_categories;
          if (!Array.isArray(had)) {
            return "no before-image of the category set was read; a chip removal "
              + "whose prior value was never observed can be neither verified "
              + "nor undone";
          }
          if (had.indexOf(removing[0]) === -1) {
            return "the chip removal names " + JSON.stringify(removing[0])
              + ", which the thread was not carrying (" + had.join(", ")
              + ") — a removal that changes nothing cannot be told apart from a "
              + "silent failure";
          }
          if (get(body, "Body.ConversationActions.0.Categories") !== undefined) {
            return "the chip removal also carries a Categories array; the remove "
              + "shape replaces no set and the two never ride together";
          }
          if (get(body, "Body.ConversationActions.0.EnableAlwaysDelete")
              || get(body, "Body.ConversationActions.0.DeleteType")) {
            return "the chip removal carries a delete instruction on the same "
              + "conversation action";
          }
          return null;
        }
        /* Same discipline as the per-item chip write: exactly one MANAGED chip
         * moves, and every other category the owner put there is preserved. The
         * rule replaces the whole set, so an unpreserved set is data loss. */
        var after = get(body, "Body.ConversationActions.0.Categories");
        if (!Array.isArray(after)) {
          return "the chip write carries no Categories array";
        }
        for (var ci = 0; ci < after.length; ci++) {
          if (typeof after[ci] !== "string" || !after[ci]) {
            return "the chip write carries a non-string category";
          }
        }
        var was = ctx.before_categories;
        if (!Array.isArray(was)) {
          return "no before-image of the category set was read; a chip write "
            + "whose prior value was never observed cannot be undone";
        }
        var addedIn = after.filter(function (x) { return was.indexOf(x) === -1; });
        var takenOut = was.filter(function (x) { return after.indexOf(x) === -1; });
        var notManaged = addedIn.concat(takenOut).filter(function (x) {
          return !isManaged(x);
        });
        if (notManaged.length) {
          return "the chip write would add or remove non-managed categories ("
            + notManaged.join(", ") + "); only the managed priority chips may "
            + "change and every other category is preserved";
        }
        if (addedIn.length + takenOut.length !== 1) {
          return "a chip write moves exactly one managed chip (got "
            + (addedIn.length + takenOut.length) + " changes)";
        }
        if (get(body, "Body.ConversationActions.0.EnableAlwaysDelete")
            || get(body, "Body.ConversationActions.0.DeleteType")) {
          return "the chip write carries a delete instruction on the same "
            + "conversation action";
        }
        return null;
      }
      /* THE DESTINATION IS AN OPAQUE FOLDER ID on this build, so it cannot be
       * checked against a name the way `MoveItem` is. It is checked against the
       * APPROVED SHAPE instead: the destination the owner's own archive used.
       * Same principle as the fingerprint — the server already accepted this
       * exact destination from a real owner action. */
      var destId = String(get(body,
        "Body.ConversationActions.0.DestinationFolderId.BaseFolderId.Id") || "");
      var approved = String(shape.destination_id || "");
      var contextApproved = String(shape.context_id || "");
      if (!approved) {
        return "the approved ApplyConversationAction shape records no "
          + "destination_id, so an opaque destination cannot be checked — "
          + "re-capture the shape from an owner archive";
      }
      /* TWO folders are permitted and they are the two the captured request
       * itself named: its destination (archive) and its context (inbox), the
       * latter because the UNDO moves back there. Anything else is refused. */
      if (destId !== approved && !(contextApproved && destId === contextApproved)) {
        return "ApplyConversationAction destination does not match the approved "
          + "shape's destination — the captured archive went to one folder and "
          + "this payload names another";
      }
      /* Explicit, like the MoveItem branch: the tail of this function is the
       * CreateItem check, and falling into it would reject every archive for
       * carrying no draft disposition. */
      return null;
    }

    if (action === "MoveItem") {
      var dest = get(body, "Body.ToFolderId.BaseFolderId.Id");
      var destType = String(get(body, "Body.ToFolderId.BaseFolderId.__type") || "");
      if (destType.indexOf("DistinguishedFolderId") !== 0) {
        return "MoveItem destination is not a DistinguishedFolderId (" + destType
          + ") — an opaque folder id cannot be checked against the permitted set";
      }
      if (!PERMITTED_FOLDERS[String(dest)]) {
        return "MoveItem destination " + JSON.stringify(dest) + " is not one of "
          + Object.keys(PERMITTED_FOLDERS).join(", ");
      }
      var ids = get(body, "Body.ItemIds");
      if (!Array.isArray(ids) || ids.length !== 1) {
        return "MoveItem must carry exactly one ItemId (got "
          + (Array.isArray(ids) ? ids.length : "none") + ") — one mutation, one "
          + "verification, one undo row";
      }
      if (!get(body, "Body.ItemIds[0]") && !(ids[0] && ids[0].Id)) {
        return "MoveItem ItemId carries no Id";
      }
      return null;
    }

    if (action === "UpdateItem") {
      var changes = get(body, "Body.ItemChanges");
      if (!Array.isArray(changes) || changes.length !== 1) {
        return "UpdateItem must carry exactly one ItemChange";
      }
      var updates = changes[0].Updates;
      if (!Array.isArray(updates) || updates.length !== 1) {
        return "UpdateItem must carry exactly one field update";
      }
      var upd = updates[0];
      if (String(upd.__type || "") !== SET_FIELD) {
        return "UpdateItem update is " + JSON.stringify(String(upd.__type || ""))
          + ", and the only permitted update is " + SET_FIELD;
      }
      var uri = get(upd, "Path.FieldURI");
      if (uri !== CATEGORIES_URI) {
        return "UpdateItem targets " + JSON.stringify(uri) + "; the only writable "
          + "field on this lane is " + CATEGORIES_URI;
      }
      var after = get(upd, "Item.Categories");
      if (!Array.isArray(after)) return "UpdateItem carries no Categories array";
      for (var c = 0; c < after.length; c++) {
        if (typeof after[c] !== "string" || !after[c]) {
          return "UpdateItem Categories carries a non-string entry";
        }
      }
      var before = ctx.before_categories;
      if (!Array.isArray(before)) {
        return "no before-image of the category set was read; a category write "
          + "whose prior value was never observed cannot be undone";
      }
      var changedIn = after.filter(function (x) { return before.indexOf(x) === -1; });
      var changedOut = before.filter(function (x) { return after.indexOf(x) === -1; });
      var unmanaged = changedIn.concat(changedOut).filter(function (x) {
        return !isManaged(x);
      });
      if (unmanaged.length) {
        return "UpdateItem would add or remove non-managed categories ("
          + unmanaged.join(", ") + "); only the managed priority chips may change";
      }
      if (setEq(before, after)) {
        return "UpdateItem would write the category set it already has — a "
          + "no-op write cannot be verified apart from a silent failure";
      }
      return null;
    }

    /* CreateItem — the one that could send mail, so it is asserted positively. */
    var disp = get(body, "Body.MessageDisposition");
    if (disp !== SAVE_ONLY) {
      return "CreateItem MessageDisposition is " + JSON.stringify(disp)
        + ", and the ONLY permitted value is " + JSON.stringify(SAVE_ONLY)
        + " (absent is a rejection, never a default)";
    }
    /* NO FOLDER NAMED MEANS DRAFTS — owner ruling 2026-08-11, and it is a
     * ruling rather than a shortcut. Outlook's own reply-save names no folder
     * at all (measured: the captured `CreateItem` has no `SavedItemFolderId`),
     * and the server files it in Drafts. Adding the field here would make the
     * payload something the server never accepted from the app, which is the
     * opposite of replay. So the request stays byte-shaped as captured, the
     * ASSUMPTION lives here where it is visible, and it is not taken on trust:
     * the draft is verified by RE-READING the Drafts folder and matching this
     * run's signature (`reconcile`). If a draft ever lands elsewhere, that
     * verification fails and the run stops.
     *
     * A NAMED folder is still checked strictly — absent is permitted, wrong is
     * not. */
    var savedFolder = get(body, "Body.SavedItemFolderId");
    if (savedFolder !== undefined && savedFolder !== null) {
      var folder = get(body, "Body.SavedItemFolderId.BaseFolderId.Id");
      var folderType = String(
        get(body, "Body.SavedItemFolderId.BaseFolderId.__type") || "");
      if (folderType.indexOf("DistinguishedFolderId") !== 0
          || folder !== DRAFT_FOLDER) {
        return "CreateItem names a save folder that is not the distinguished "
          + DRAFT_FOLDER + " (got " + JSON.stringify(folder) + " / "
          + folderType + "). Absent is permitted (the server files it in "
          + DRAFT_FOLDER + "); wrong is not.";
      }
    }
    var items = get(body, "Body.Items");
    if (!Array.isArray(items) || items.length !== 1) {
      return "CreateItem must carry exactly one item";
    }
    var msg = items[0];
    var bcc = msg.BccRecipients;
    if (Array.isArray(bcc) && bcc.length) {
      return "CreateItem carries blind-copy recipients; the draft lane is "
        + "in-thread only and BccRecipients must be empty";
    }
    var allowed = ctx.allowed_recipients;
    if (!Array.isArray(allowed) || !allowed.length) {
      return "no thread participant list was read, so the draft's recipients "
        + "cannot be bounded to the thread (rule 12: in-thread only)";
    }
    var lowered = allowed.map(function (a) { return String(a).toLowerCase(); });
    var addressed = [];
    ["ToRecipients", "CcRecipients"].forEach(function (field) {
      (msg[field] || []).forEach(function (r) {
        var a = (r && r.EmailAddress) || (r && r.Mailbox && r.Mailbox.EmailAddress);
        addressed.push(String(a || "").toLowerCase());
      });
    });
    if (!addressed.length) {
      /* EXCEPT for a reply that inherits its recipients from the message it
       * references — this build's own shape. That is not "addresses nobody", it
       * is "addresses exactly the thread, chosen by the server", and it carries
       * no field an address could leak through. A reply with NEITHER is still
       * refused. */
      if (!(String(msg.__type || "").indexOf("ReplyToItem") === 0
            && get(msg, "ReferenceItemId.Id"))) {
        return "CreateItem addresses nobody; a reply draft with no recipient is "
          + "not the draft the ledger says was written";
      }
      /* Falls THROUGH to the signature check below. Skipping the recipient loop
       * must never skip the rest of the gate — an early `return null` here let
       * an UNSIGNED draft pass, caught by its own test. */
    } else {
      for (var r = 0; r < addressed.length; r++) {
        if (lowered.indexOf(addressed[r]) === -1) {
          return "CreateItem addresses " + JSON.stringify(addressed[r])
            + ", who is not a participant in this thread (rule 12: in-thread "
            + "only)";
        }
      }
    }
    /* A REPLY carries its text in `NewBodyContent`, not `Body` — measured on
     * the captured `ReplyToItem`. Reading only `Body.Value` made the signature
     * check pass over an empty string, which would have let an UNSIGNED draft
     * through the one gate that makes a lost response reconcilable. */
    var text = String(get(msg, "NewBodyContent.Value")
                      || get(msg, "Body.Value") || "");
    if (!ctx.signature || text.indexOf(ctx.signature) === -1) {
      return "the draft body does not carry this run's machine signature, so a "
        + "lost response could not be reconciled against the Drafts folder";
    }
    return null;
  }

  /* ---------------- builders: clone an approved skeleton, substitute ids ---- */
  function clone(x) { return JSON.parse(JSON.stringify(x)); }

  /* ONE VERB, TWO JOBS. `ApplyConversationAction` archives (`Move`) AND chips
   * (`UpdateAlwaysCategorizeRule`) on this build, and their payloads are
   * different shapes the server accepted separately. Keying the approved shapes
   * by the verb alone would let an archive's shape authorise a chip. So the key
   * is verb + action, and the plain verb remains valid for the single-job
   * verbs. */
  function shapeKey(action, conversationAction) {
    return conversationAction ? action + ":" + conversationAction : action;
  }

  /* `field` names the VARIANT slot on the entry — `skeleton` for the ordinary
   * shape, `skeleton_remove` for the chip's captured `CategoriesToRemove` shape.
   * Two slots, one entry, one refusal path: a variant with no capture on file
   * cannot be built out of the other one. */
  function requireShape(shapes, action, field) {
    var s = (shapes || {})[action];
    var skeleton = s && s[field || "skeleton"];
    if (!skeleton) {
      throw new Error("no approved captured "
        + (field === "skeleton_remove" ? "REMOVE " : "") + "shape for " + action
        + " — mutations replay a shape the server already accepted, never a "
        + "hand-built one (doctrine v4.7 Layer-2 hard deny)");
    }
    return clone(skeleton);
  }

  /* SUBSTITUTE INTO THE APPROVED SHAPE — never assemble a new one. Each builder
   * clones the skeleton's own subtree and overwrites the fields it must, so the
   * payload's key paths are the accepted request's by construction. A skeleton
   * that lacks a path we need is a REFUSAL with the recapture instruction, not
   * a field invented on the spot. */
  function need(node, path, action) {
    var v = get(node, path);
    if (v === undefined || v === null) {
      throw new Error("the approved " + action + " shape has no " + path
        + " to substitute into — recapture it from the app's own action; a "
        + "missing path is never filled in by hand (doctrine v4.7)");
    }
    return v;
  }

  function buildMove(shapes, header, itemId, changeKey, destination) {
    var body = requireShape(shapes, "MoveItem");
    body.Header = clone(header);
    var idTemplate = clone(need(body, "Body.ItemIds", "MoveItem")[0]
      || {__type: "ItemId:#Exchange"});
    idTemplate.Id = itemId;
    idTemplate.ChangeKey = changeKey;
    body.Body.ItemIds = [idTemplate];
    var folder = need(body, "Body.ToFolderId.BaseFolderId", "MoveItem");
    folder.__type = "DistinguishedFolderId:#Exchange";
    folder.Id = destination;
    delete folder.ChangeKey;
    return body;
  }

  function buildConversationMove(shapes, header, conversationId, toArchive) {
    var moveKey = shapeKey("ApplyConversationAction", "Move");
    var body = requireShape(shapes, moveKey);
    var shape = (shapes || {})[moveKey] || {};
    body.Header = clone(header);
    var act = clone(need(body, "Body.ConversationActions", "ApplyConversationAction")[0]);
    act.Action = "Move";
    act.ConversationId = Object.assign(
      clone(act.ConversationId || {__type: "ItemId:#Exchange"}),
      {Id: conversationId});
    delete act.ConversationId.ChangeKey;
    var from = toArchive ? shape.context_id : shape.destination_id;
    var to = toArchive ? shape.destination_id : shape.context_id;
    act.ContextFolderId = {__type: "TargetFolderId:#Exchange",
                           BaseFolderId: {__type: "FolderId:#Exchange", Id: from}};
    act.DestinationFolderId = {__type: "TargetFolderId:#Exchange",
                               BaseFolderId: {__type: "FolderId:#Exchange", Id: to}};
    delete act.ConversationLastSyncTime;
    body.Body.ConversationActions = [act];
    return body;
  }

  function buildConversationCategorize(shapes, header, conversationId, categories) {
    var body = requireShape(shapes, shapeKey("ApplyConversationAction",
                                             "UpdateAlwaysCategorizeRule"));
    body.Header = clone(header);
    var act = clone(need(body, "Body.ConversationActions",
                         "ApplyConversationAction")[0]);
    act.Action = "UpdateAlwaysCategorizeRule";
    act.ConversationId = Object.assign(
      clone(act.ConversationId || {__type: "ItemId:#Exchange"}),
      {Id: conversationId});
    delete act.ConversationId.ChangeKey;
    act.Categories = categories.slice();
    /* STATED, NOT INHERITED (review 2026-08-12). The captured shape carries
     * `false` and the validator now demands it on both variants; setting it
     * here means a chip payload cannot be built carrying anything else, so the
     * destructive value never depends on what a re-capture happened to hold. */
    act.ClearCategories = false;
    /* The SKELETON's folder id is `<scrubbed>` — ids identify a real mailbox, so
     * the capture keeps them out of the shape and preserves the real value
     * beside it as `context_id`. Sending the skeleton's placeholder is what run
     * 118's first chip did: `ErrorInvalidIdMalformed`, refused by the server.
     * The move builder always substituted it; this one did not. */
    var ctxId = (shapes || {})[shapeKey("ApplyConversationAction",
                                        "UpdateAlwaysCategorizeRule")].context_id;
    if (ctxId) {
      act.ContextFolderId = {__type: "TargetFolderId:#Exchange",
                             BaseFolderId: {__type: "FolderId:#Exchange",
                                            Id: ctxId}};
    }
    /* The captured chip carried its own sync time; replaying it would tell the
     * server this decision was made at capture time. */
    if (act.ConversationLastSyncTime !== undefined) {
      act.ConversationLastSyncTime = null;
    }
    body.Body.ConversationActions = [act];
    return body;
  }

  /* THE CHIP COMES OFF THROUGH ITS OWN CAPTURED SHAPE (FINDING 2026-08-12).
   * Same verb, same Action, a different accepted payload: `CategoriesToRemove`
   * instead of `Categories`. Substituted into `skeleton_remove` exactly as the
   * add is substituted into `skeleton`, so the built payload's key paths — and
   * therefore its fingerprint — are the captured request's by construction. */
  function buildConversationUnchip(shapes, header, conversationId, chip) {
    var key = shapeKey("ApplyConversationAction", "UpdateAlwaysCategorizeRule");
    var body = requireShape(shapes, key, "skeleton_remove");
    body.Header = clone(header);
    var act = clone(need(body, "Body.ConversationActions",
                         "ApplyConversationAction")[0]);
    act.Action = "UpdateAlwaysCategorizeRule";
    act.ConversationId = Object.assign(
      clone(act.ConversationId || {__type: "ItemId:#Exchange"}),
      {Id: conversationId});
    delete act.ConversationId.ChangeKey;
    act.CategoriesToRemove = [chip];
    /* Stated here for the ADD builder's reason (review 2026-08-12): the value
     * that would wipe every category on the thread is written by us, never
     * inherited from a capture. */
    act.ClearCategories = false;
    /* The same substitution the ADD builder had to be taught (run 118 defect 4):
     * the skeleton's folder id is the capture's `<scrubbed>` placeholder, and
     * sending it earns `ErrorInvalidIdMalformed`. */
    var ctxId = (shapes || {})[key].context_id;
    if (ctxId) {
      act.ContextFolderId = {__type: "TargetFolderId:#Exchange",
                             BaseFolderId: {__type: "FolderId:#Exchange",
                                            Id: ctxId}};
    }
    /* Kept, blanked — the add builder's rule and for the add builder's reason:
     * the key must stay or the fingerprint refuses the payload, and replaying
     * the captured value would tell the server this decision was made when the
     * shape was captured. */
    if (act.ConversationLastSyncTime !== undefined) {
      act.ConversationLastSyncTime = null;
    }
    body.Body.ConversationActions = [act];
    return body;
  }

  function buildCategorize(shapes, header, itemId, changeKey, categories) {
    var body = requireShape(shapes, "UpdateItem");
    body.Header = clone(header);
    var change = clone(need(body, "Body.ItemChanges", "UpdateItem")[0]);
    var update = clone(need({c: change}, "c.Updates", "UpdateItem")[0]);
    change.ItemId = Object.assign(clone(change.ItemId || {__type: "ItemId:#Exchange"}),
                                  {Id: itemId, ChangeKey: changeKey});
    update.__type = SET_FIELD;
    update.Path = Object.assign(clone(update.Path || {__type: "PropertyUri:#Exchange"}),
                                {FieldURI: CATEGORIES_URI});
    update.Item = Object.assign(clone(update.Item || {__type: "Message:#Exchange"}),
                                {Categories: categories.slice()});
    change.Updates = [update];
    body.Body.ItemChanges = [change];
    return body;
  }

  function buildDraft(shapes, header, fields) {
    var body = requireShape(shapes, "CreateItem");
    body.Header = clone(header);
    body.Body.MessageDisposition = SAVE_ONLY;
    /* Substituted ONLY if the captured shape has the field. When it does not —
     * which is what this build sends — nothing is invented and the server's own
     * default (Drafts) applies, verified afterwards by re-reading Drafts. */
    if (body.Body.SavedItemFolderId) {
      var saved = need(body, "Body.SavedItemFolderId.BaseFolderId", "CreateItem");
      saved.__type = "DistinguishedFolderId:#Exchange";
      saved.Id = DRAFT_FOLDER;
      delete saved.ChangeKey;
    }
    var msg = clone(need(body, "Body.Items", "CreateItem")[0]);
    /* The text field depends on the item type: a `ReplyToItem` carries
     * `NewBodyContent`, a plain `Message` carries `Body`. Substitute into
     * whichever the APPROVED SHAPE actually has, and refuse if it has neither
     * rather than inventing one. */
    if (msg.NewBodyContent && msg.NewBodyContent.Value !== undefined) {
      msg.NewBodyContent = Object.assign(clone(msg.NewBodyContent),
                                         {Value: fields.text});
    } else if (msg.Body && msg.Body.Value !== undefined) {
      msg.Body = Object.assign(clone(msg.Body), {Value: fields.text});
    } else {
      throw new Error("the approved CreateItem shape carries neither "
        + "NewBodyContent.Value nor Body.Value to substitute the draft text "
        + "into — recapture it from the app's own reply-draft save");
    }
    /* THE REFERENCE IS THE WHOLE REPLY. A `ReplyToItem` replays with the
     * CAPTURED `ReferenceItemId` unless it is substituted — which would attach
     * this run's draft to the message the SHAPE was captured from, i.e. a reply
     * saved into someone else's thread. It is therefore a hard failure, never a
     * best effort. */
    if (msg.ReferenceItemId !== undefined) {
      if (!fields.item_id) {
        throw new Error("a ReplyToItem needs the id of the message being "
          + "replied to; without it the captured reference would be replayed");
      }
      msg.ReferenceItemId = Object.assign(
        clone(msg.ReferenceItemId || {__type: "ItemId:#Exchange"}),
        {Id: fields.item_id});
      if (fields.change_key) msg.ReferenceItemId.ChangeKey = fields.change_key;
      else delete msg.ReferenceItemId.ChangeKey;
      /* KEPT, AND KEPT AS THE CAPTURED NUMBER. This was blanked to `null` until
       * 2026-08-12 on the reasoning that a captured value would point the draft
       * at the document the shape came from, and values are outside the
       * fingerprint. The server disagrees BEFORE it ever reads the mail: the
       * field is value-typed, so `null` fails DESERIALIZATION and the whole
       * request comes back HTTP 500 `OwaSerializationException` with no
       * response envelope at all — measured on run 124, three live attempts,
       * zero drafts created. Deleting the key is not the alternative either
       * (the key set moves and the fingerprint gate refuses the payload), so
       * the captured number stands: it is a client-side COMPOSE-CACHE id, while
       * `ReferenceItemId` — substituted just above — is what actually binds the
       * reply to its thread. Replayed, not synthesized, which is the rule. */
    }
    if (msg.Subject !== undefined) msg.Subject = fields.subject;
    var toTemplate = (msg.ToRecipients && msg.ToRecipients[0]) || null;
    if (!toTemplate) {
      /* A reply that names nobody inherits the thread's recipients from
       * `ReferenceItemId` — which is STRICTER than addressing it ourselves, not
       * looser: there is no field in which an address could leak. */
      if (msg.ReferenceItemId !== undefined) {
        body.Body.Items = [msg];
        return body;
      }
      throw new Error("the approved CreateItem shape carries no recipient entry "
        + "to clone, so a reply draft cannot be addressed within its accepted "
        + "shape — recapture it from a reply-draft save");
    }
    msg.ToRecipients = fields.recipients.map(function (a) {
      var r = clone(toTemplate);
      if (r.EmailAddress !== undefined) r.EmailAddress = String(a);
      else if (r.Mailbox) r.Mailbox = Object.assign(clone(r.Mailbox),
                                                    {EmailAddress: String(a)});
      /* The template is a REAL PERSON from the captured save. Substituting only
       * the address would leave THEIR display name and SIP uri attached to a
       * draft addressed to someone else — measured on the first live dry run,
       * where both drafts carried a stranger's `SipUri`. Every other identity
       * field on the clone is therefore blanked; the keys stay (the fingerprint
       * is over key paths) and only truthful values survive. */
      ["Name", "RoutingType", "SipUri", "ItemId", "OriginalDisplayName",
       "SmtpAddress"].forEach(function (f) {
        if (r[f] !== undefined) r[f] = (f === "Name") ? String(a) : null;
      });
      return r;
    });
    if (msg.CcRecipients !== undefined) msg.CcRecipients = [];
    if (msg.BccRecipients !== undefined) msg.BccRecipients = [];
    if (msg.ConversationId && fields.conversation_id) {
      msg.ConversationId = Object.assign(clone(msg.ConversationId),
                                         {Id: fields.conversation_id});
    }
    body.Body.Items = [msg];
    return body;
  }

  /* ---------------- runtime state ------------------------------------------ */
  var cfg = null;
  var armed = false;
  var events = [];          // 449/401 transitions (nothing here re-primes)
  var rejected = [];        // every payload the allowlist refused
  var dispatched = 0;
  function now() { return cfg && cfg.now ? cfg.now() : new Date().toISOString(); }

  function note(kind, extra) {
    var e = {kind: kind, at: now()};
    Object.keys(extra || {}).forEach(function (k) { e[k] = extra[k]; });
    events.push(e);
    return e;
  }

  /* The most recent status that DISARMED dispatch — 449 or 401. It names the
   * transition in the refusal below, because the two want different mornings
   * (a stale canary vs a stale bearer) and one hard-coded number would have
   * reported run 130's 401 as a canary failure for as long as anyone read it. */
  function lastTransition() {
    for (var i = events.length - 1; i >= 0; i--) {
      if (events[i].kind === "http-449" || events[i].kind === "http-401") {
        return events[i];
      }
    }
    return null;
  }

  function api_init(opts) {
    cfg = {
      fetch: opts.fetch,
      envelope: opts.envelope,          // {url, headers} — never leaves the page
      header: opts.header || {},        // the captured request `Header` block
      findItemBody: opts.findItemBody,  // a FindItem the server already accepted
      shapes: opts.shapes || {},
      now: opts.now,
      origin: opts.origin || "https://outlook.cloud.microsoft",
      pageSize: opts.pageSize || 100,
      maxPages: opts.maxPages || 60,
      signature: opts.signature || null,
    };
    armed = true;
    events = [];
    rejected = [];
    dispatched = 0;
    return {armed: armed, shapes: Object.keys(cfg.shapes).sort()};
  }

  /* WHAT A FAILED DISPATCH IS ALLOWED TO PERSIST. Run 122's draft dispatch
   * reported `response_code: null` and nothing else — twice — and finding out
   * why cost a hand-rendered Drafts folder, so an unattended lane does have to
   * say why a dispatch failed in its own run report.
   *
   * It said too much (review 2026-08-12). The mask was EMAIL-SHAPED only, so a
   * subject, a display name, an internal id or a token in an error page went
   * straight through it, and the parsed `MessageText` bypassed even that —
   * into `_evidence/nightly/` under the vault, unclassified and kept. So the
   * routine record is now the SHAPE of a failure rather than its text: a
   * length, and a digest that tells "the same failure again" from "a different
   * one" without carrying either. Truncation limited size; it never limited
   * sensitivity.
   *
   * IT THEN SAID TOO LITTLE (review 2026-08-12). This block was added BECAUSE a
   * failure said nothing, and the defect it was built for needed the string
   * `OwaSerializationException`; a status, a length and an opaque digest cannot
   * tell a serialization defect from a gateway page or an auth challenge at the
   * same HTTP 500. Privacy and diagnosability are not a choice here — the body
   * is CLASSIFIED IN MEMORY against a closed list of signatures we wrote
   * ourselves, and only that constant name is kept. An unrecognised body stays
   * exactly as opaque as it was. */
  var BODY_KINDS = [
    ["owa-serialization", /OwaSerializationException/],
    ["exchange-error-envelope", /ErrorInternalServerError|<faultstring|ServerBusyException/],
    ["auth-challenge", /login\.microsoftonline\.com|WWW-Authenticate|invalid_grant|AADSTS\d/],
    ["throttled", /ErrorServerBusy|ErrorTooManyObjectsOpened|Retry-After/],
    ["html-page", /^\s*<(?:!doctype\s+html|html\b)/i],
  ];

  function bodyKind(s) {
    for (var i = 0; i < BODY_KINDS.length; i++) {
      if (BODY_KINDS[i][1].test(s)) return BODY_KINDS[i][0];
    }
    if (!s) return "empty";
    /* Cheap and non-sensitive: whether it even claims to be the envelope shape
     * this lane expects. Nothing here quotes a byte of the body. */
    return /^\s*[{[]/.test(s) ? "json-not-the-expected-envelope" : "unknown";
  }

  function bodyShape(text) {
    var s = String(text == null ? "" : text);
    return {len: s.length, digest: s ? fnv1a(s) : null, kind: bodyKind(s)};
  }

  /* An Exchange `ResponseCode` is an ENUM TOKEN — `ErrorInvalidIdMalformed`,
   * `NoError` — and that is the whole reason it is safe to keep: it names a
   * cause without quoting a mailbox. So the allowlist is its SHAPE, checked
   * rather than assumed.
   *
   * SANITISED ONCE, HERE, AND NOWHERE ELSE (review 2026-08-12). The check used
   * to live inside `failureDetail` only, while five other sites emitted the raw
   * value — three dispatch branches, the unreadable-item skips, and the
   * enumeration error string — and the host persists all of them into the run
   * artifact under the vault. A reviewer's end-to-end probe put the literal
   * string `"Error: owner@example.com in Q3 pricing"` on disk while the nested
   * sanitised value beside it read `null`. Every read of `ResponseCode` in this
   * file now goes through this function.
   *
   * THE SHAPE IS EXCHANGE'S OWN, not "one word" (review 2026-08-13, round 3).
   * `/^[A-Za-z][A-Za-z0-9]{0,63}$/` passed ANY single alphanumeric token —
   * `AliceConfidential`, `Q3Pricing` — so a server that echoed request text
   * into that slot would have walked it straight through the redaction. Every
   * real EWS ResponseCode is `NoError` or `Error…`/`Warning…` (a closed
   * PREFIX, not a closed list — a full enum list would null real codes the
   * first time Microsoft adds one, which is the diagnosability this block
   * exists to keep). An unknown token is dropped AND flagged
   * (`response_code_not_an_enum`), so the loss is visible, never silent. */
  var RESPONSE_CODE =
    /^(NoError|Error[A-Za-z0-9]{1,60}|Warning[A-Za-z0-9]{1,60})$/;

  function safeCode(code) {
    var s = code == null ? null : String(code);
    return s && RESPONSE_CODE.test(s) ? s : null;
  }

  /* Every HTTP call in this file goes through here. Reads are permitted while
   * disarmed — `reconcile` after a 449 IS a read, and refusing it would leave
   * an in-flight mutation's outcome permanently unknown — but a MUTATION while
   * disarmed is impossible by construction rather than by discipline. */
  function send(action, body, opts) {
    opts = opts || {};
    var mutation = !!ALLOWED_ACTIONS[action];
    if (!mutation && !READ_ACTIONS[action]) {
      return Promise.reject(new Error("refusing an unknown action " + action));
    }
    if (mutation && !armed) {
      /* NAME THE TRANSITION THAT DISARMED IT. Two statuses reach this state
       * now, they want different mornings (a stale canary vs a stale bearer),
       * and hard-coding "449" into the message would have reported run 130's
       * 401 as a canary failure for as long as anyone read the line. */
      var last = lastTransition();
      var why = "mutation dispatch is DISARMED ("
        + (last ? "HTTP " + last.kind.replace("http-", "") + " seen at " + last.at
                : "no transition recorded")
        + "); nothing in this page can arm it again — the host takes a fresh "
        + "envelope (`cos_cdp_capture.py --prepare`) and re-runs `init`";
      rejected.push({action: action, at: now(), reason: why,
                     conversation_id: opts.conversation_id || null});
      return Promise.reject(new Error(why));
    }
    if (mutation) {
      var reason = validate(action, body, opts.ctx || {});
      if (reason) {
        rejected.push({action: action, at: now(), reason: reason,
                       conversation_id: opts.conversation_id || null,
                       fingerprint: fingerprint(body)});
        return Promise.reject(new Error("REFUSED BEFORE DISPATCH: " + reason));
      }
    }
    /* NOTHING IS RECOVERED HERE, AND THAT IS THE HONEST SHAPE (review
     * 2026-08-13, rounds 2 and 3 — see the 401 paragraph at the top of this
     * file). A 401 has already disarmed dispatch and tagged itself inside
     * `dispatch`; it propagates to the host, which is the only leg that can
     * take a fresh envelope. There is no in-page recovery to call: the round-3
     * review deleted the machinery rather than leave an unreachable recovery
     * standing next to the path that must never use it. */
    return dispatch(action, body, opts, mutation);
  }

  /* The wire half of `send`: no guards, no recovery, one request. Split out so
   * the guard block above stays one place, not one per call site. */
  function dispatch(action, body, opts, mutation) {
    var url = new URL(cfg.envelope.url, cfg.origin);
    url.searchParams.set("action", action);
    var headers = {};
    Object.keys(cfg.envelope.headers).forEach(function (k) {
      headers[k] = cfg.envelope.headers[k];
    });
    headers["x-owa-urlpostdata"] = encodeURIComponent(JSON.stringify(body));
    headers.action = action;
    if (mutation) dispatched += 1;
    return cfg.fetch(url.toString(), {method: "POST", headers: headers,
                                      credentials: "include"})
      .then(function (res) {
        return res.text().then(function (text) {
          if (res.status === 449) {
            armed = false;
            note("http-449", {action: action, mutation_in_flight: mutation,
                              conversation_id: opts.conversation_id || null});
            var err = new Error("HTTP 449 — the OWA canary is missing or stale; "
              + "this is not a retryable status and dispatch is now disarmed");
            err.canary449 = true;
            err.mutation_in_flight = mutation;
            throw err;
          }
          if (res.status === 401) {
            /* SAME DIRECTION AS THE 449, never a weaker one: disarm, and stay
             * disarmed. Nothing here retries and nothing here re-arms — only a
             * host `init` after `cos_cdp_capture.py --prepare` does. */
            armed = false;
            note("http-401", {action: action, mutation_in_flight: mutation,
                              conversation_id: opts.conversation_id || null});
            var stale = new Error("HTTP 401 — the replayed envelope's bearer is "
              + "no longer accepted. The seed is captured before the model leg "
              + "and the mutation lane runs 15-40 minutes later, so this is the "
              + "token ageing out. Dispatch is disarmed and stays disarmed: "
              + "re-seeding is the HOST's call (`cos_cdp_capture.py --prepare`, "
              + "then a fresh init).");
            stale.auth401 = true;
            stale.mutation_in_flight = mutation;
            throw stale;
          }
          var json = null;
          try { json = JSON.parse(text); } catch (e) { json = null; }
          var shape = bodyShape(text);
          return {status: res.status, json: json,
                  non_json: json ? null : text.length,
                  body_len: shape.len, body_digest: shape.digest,
                  body_kind: shape.kind};
        });
      });
  }

  function firstItem(r) {
    var m = r && r.json && r.json.Body && r.json.Body.ResponseMessages
      && r.json.Body.ResponseMessages.Items && r.json.Body.ResponseMessages.Items[0];
    return m || null;
  }

  /* ---------------- reads (the verification and reconciliation surface) -----
   * PAGING ADVANCES ON THE SERVER'S OWN NEXT INDEX, never on arithmetic
   * (review 2026-08-12). `Offset: offset` went out and `offset += pageSize`
   * came back, so a page returning FEWER rows than it was asked for — without
   * saying it had reached the last item — moved the window past rows nobody
   * read. The enumeration still ended on `IncludesLastItemInRange` and
   * `absence()` then certified a thread absent that was sitting in the skipped
   * range. The conclusive-absence flag exists precisely so a truncated read
   * cannot look like a moved thread; paging past a short page defeated it.
   *
   * Three ways to advance, in order, and none of them guesses:
   *   1. `IndexedPagingOffset` — the server naming the next index. It wins.
   *   2. a FULL page and no next index — `offset + count` is then the only
   *      index consistent with what came back.
   *   3. a SHORT page with neither — REFUSE. That is the starved read itself,
   *      and continuing past it is what manufactured the false absence.
   * A next index that does not advance is refused too: it either loops or
   * skips, and both are worse than stopping.
   *
   * A REFUSAL STOPS PAGING; IT DOES NOT THROW. `terminated` stays false, which
   * is already the honest answer and already routes through the inconclusive
   * -absence path the host stops on with a diagnosis. Throwing here would swap
   * that labelled stop for a generic driver failure, so the fix would have
   * closed a silent skip by adding a louder, less informative death.
   * `advance_refused` names which rule refused, for the run report.
   *
   * COVERAGE IS A SECOND, INDEPENDENT FACT. `TotalItemsInView` is the server's
   * own count of the folder, so an enumeration that ends on the last-item flag
   * having collected fewer rows than that did NOT see the folder, whatever the
   * flag says. `complete` is the conjunction and it is what every absence and
   * verification claim now reads; `terminated` stays the raw flag so a report
   * can still tell the two apart.
   *
   * COVERAGE FAILS CLOSED, three ways (verify round, 2026-08-13):
   *   - a MISSING total is not coverage. `total === null` used to count as
   *     covered, so a build that omitted the field would have lost the whole
   *     belt silently. If the live surface omits it, absence certification
   *     refuses loudly — and the attended dry run that must precede re-arm is
   *     where that surfaces, not an unattended morning.
   *   - totals that DISAGREE across pages are the concurrent-change signal
   *     inside one scan; a folder that changed size mid-walk was not seen.
   *   - a DUPLICATE ItemId is dropped, not counted: an insertion shifts the
   *     window the other way and repeats a row, and counting the repeat would
   *     let `items_seen >= total` mask a skipped one. */
  function enumerate(folder, limitPages) {
    if (!cfg.findItemBody) {
      return Promise.reject(new Error("no captured FindItem body to replay"));
    }
    var items = [];
    var seenIds = {};
    var duplicatesDropped = 0;
    var page = 0;
    var offset = 0;
    var terminated = false;
    var totals = [];
    var refused = null;
    var maxPages = limitPages || cfg.maxPages;

    function step() {
      if (page >= maxPages) return Promise.resolve();
      var body = clone(cfg.findItemBody);
      body.Body.ParentFolderIds =
        [{__type: "DistinguishedFolderId:#Exchange", Id: folder}];
      body.Body.ShapeName = "MailListItem";
      body.Body.Paging = {__type: "IndexedPageView:#Exchange", BasePoint: "Beginning",
                          Offset: offset, MaxEntriesReturned: cfg.pageSize};
      return send("FindItem", body).then(function (r) {
        var msg = firstItem(r);
        if (!msg || msg.ResponseCode !== "NoError") {
          /* The code is SANITISED even into an error string: this message
           * becomes a `MutationStop` the host persists verbatim into the run
           * artifact (`connector_result`), so an unexpected value in that slot
           * would reach disk by the back door (review 2026-08-12). */
          throw new Error("FindItem " + folder + " page " + page + " failed: http "
            + r.status + " code " + safeCode(msg && msg.ResponseCode));
        }
        var root = msg.RootFolder || {};
        var got = (root.Items || []).length;
        (root.Items || []).forEach(function (it) {
          var id = it.ItemId && it.ItemId.Id;
          if (id && seenIds[id]) { duplicatesDropped += 1; return; }
          if (id) seenIds[id] = 1;
          items.push({itemId: id,
                      changeKey: it.ItemId && it.ItemId.ChangeKey,
                      convId: it.ConversationId && it.ConversationId.Id,
                      isRead: it.IsRead === true,
                      categories: it.Categories || [],
                      received: it.DateTimeReceived || null});
        });
        page += 1;
        if (typeof root.TotalItemsInView === "number") {
          totals.push(root.TotalItemsInView);
        }
        if (root.IncludesLastItemInRange) { terminated = true; return; }
        var next = root.IndexedPagingOffset;
        if (typeof next === "number" && next > offset) {
          offset = next;
        } else if (typeof next === "number") {
          refused = "the server named a next offset (" + next + ") that does "
            + "not advance past " + offset + "; paging on it would loop or skip";
          return;
        } else if (got === cfg.pageSize) {
          offset += got;
        } else {
          refused = "page " + page + " returned " + got + " of " + cfg.pageSize
            + " rows, did not say it reached the last item, and named no next "
            + "offset; paging past it would skip the rows it did not return";
          return;
        }
        return step();
      });
    }
    return step().then(function () {
      var total = totals.length ? totals[totals.length - 1] : null;
      /* EVERY page must report the total, and they must all agree (verify
       * round 2): a total on one page out of three is one snapshot claim
       * certifying a walk it did not witness. */
      var totalsStable = totals.length === page && page > 0
        && Math.min.apply(null, totals) === Math.max.apply(null, totals);
      var covered = totalsStable && items.length >= total;
      return {folder: folder, items: items, page_count: page,
              terminated: terminated,
              total_items_in_view: total,
              totals_stable: totalsStable,
              items_seen: items.length,
              duplicates_dropped: duplicatesDropped,
              coverage_complete: covered, advance_refused: refused,
              complete: terminated && covered};
    });
  }

  function _getItemShaped(itemId, baseShape, bodyType) {
    var shape = {__type: "ItemResponseShape:#Exchange", BaseShape: baseShape};
    if (bodyType) shape.BodyType = bodyType;
    var body = {
      __type: "GetItemJsonRequest:#Exchange",
      Header: clone(cfg.header || {}),
      Body: {
        __type: "GetItemRequest:#Exchange",
        ItemShape: shape,
        ItemIds: [{__type: "ItemId:#Exchange", Id: itemId}],
      },
    };
    return send("GetItem", body).then(function (r) {
      var msg = firstItem(r);
      var item = msg && msg.Items && msg.Items[0];
      if (!item) {
        /* Sanitised at the SOURCE: this `code` is what the unreadable-item
         * skips carry to disk as `item_response_code`. */
        return {ok: false, code: safeCode(msg && msg.ResponseCode),
                status: r.status};
      }
      var addrs = [];
      var from = item.From && item.From.Mailbox
        && (item.From.Mailbox.EmailAddress || item.From.Mailbox.Name);
      /* A MEETING REQUEST has no From; its Organizer plays that role — the
       * person a reply draft should be bounded to (measured 2026-08-13:
       * Default returns Organizer + ToRecipients on exactly the items the
       * full shape cannot read). */
      if (!from && item.Organizer && item.Organizer.Mailbox) {
        from = item.Organizer.Mailbox.EmailAddress || item.Organizer.Mailbox.Name;
      }
      if (from) addrs.push(String(from));
      ["ToRecipients", "CcRecipients"].forEach(function (f) {
        (item[f] || []).forEach(function (m) {
          var a = (m.Mailbox && m.Mailbox.EmailAddress) || m.EmailAddress;
          if (a) addrs.push(String(a));
        });
      });
      return {
        ok: true, status: r.status, code: safeCode(msg.ResponseCode),
        itemId: item.ItemId && item.ItemId.Id,
        changeKey: item.ItemId && item.ItemId.ChangeKey,
        convId: item.ConversationId && item.ConversationId.Id,
        /* null, not [] — a degraded read cannot tell "no categories" from
         * "the narrow shape omits the key", and the caller that needs the
         * before-image must know which it got. The full shape always carries
         * the key semantics, so [] stays truthful there. */
        categories: ("Categories" in item) ? (item.Categories || [])
                                           : (baseShape === "AllProperties"
                                              ? [] : null),
        isRead: item.IsRead === true,
        isDraft: item.IsDraft === true,
        /* E17 wants the PROVIDER-IMMUTABLE id on a rest-lane archive row, and
         * this is where it comes from. The undo KEY is still the conversation
         * id (v4.7: list-view ItemIds change when an item moves folders) —
         * recording both is what satisfies the field discipline without
         * keying restore on a handle that rotates. */
        internetMessageId: item.InternetMessageId || null,
        parentFolderId: item.ParentFolderId && item.ParentFolderId.Id,
        participants: addrs,
        text: (item.Body && item.Body.Value) || "",
        subject: item.Subject || "",
      };
    });
  }

  /* MEETING REQUESTS 500 UNDER `AllProperties` (measured 2026-08-13, run 128
   * + live probe). OWA's serializer fails SERVER-SIDE on the full property
   * set for MeetingRequest items: the control message answered 200, the
   * meeting request answered 500 `ErrorInternalServerError` under
   * AllProperties in BOTH body types — and 200 under `Default` on the SAME
   * item. The 500's body carries no ResponseMessages block, which is why the
   * run-128 skips recorded `item_response_code: null`. Deterministic: the
   * same 4 inbox rows failed every resolve, and under the unreadable-stops-
   * the-run rule the first of them would have halted EVERY night while they
   * sat in the inbox.
   *
   * So a full read that dies >=500 with no parseable envelope RETRIES ONCE
   * with `Default` — proven live — and reports itself DEGRADED: Default
   * carries no Categories, no Body and no ConversationId, and the CALLERS
   * hold the fallbacks (the enumeration row already carries categories +
   * changeKey; `resolveTarget` knows the conversation id it asked for). A
   * REAL absence (ErrorItemNotFound answers 200 with a code) never reaches
   * the retry, and a retry that also fails leaves the original honest
   * `ok: false`. */
  function getItem(itemId) {
    return _getItemShaped(itemId, "AllProperties", "Text").then(function (r) {
      if (r.ok || !(r.status >= 500)) return r;
      return _getItemShaped(itemId, "Default", null).then(function (d) {
        if (!d.ok) return r;               // the original failure, unmasked
        d.degraded = true;
        d.degraded_from = {status: r.status, code: r.code};
        return d;
      });
    });
  }

  /* ---------------- resolve: ids rotate, so read them fresh ----------------
   *
   * A NOT-FOUND IS CONFIRMED BY A SECOND SCAN (review 2026-08-13, round 3).
   * One complete scan is not snapshot-safe: delete a neighboring row between
   * page N and page N+1 and the indexed window shifts — a still-present row
   * is never returned while the LAST page's `TotalItemsInView` drops in step,
   * so `items_seen >= total` still holds and one scan certifies a present
   * thread absent (probe-demonstrated by the round-3 review). Two consecutive
   * complete scans that BOTH miss the thread and AGREE on the folder's size
   * cannot both be the same race: the deletion that shifted scan one's window
   * has already happened when scan two starts from offset 0. This runs ONLY
   * on the not-found path, so the common case (target present) pays nothing.
   * A residual remains — an unlucky deletion during EACH scan — and is
   * accepted as the bound: the misread is a skipped row that re-plans the
   * next night, never a wrong mutation. */
  function resolveTarget(convId, folder) {
    return enumerate(folder).then(function (en) {
      var hits = en.items.filter(function (i) { return i.convId === convId; });
      if (!hits.length) {
        /* The confirm scan runs on EVERY not-found — including one whose
         * first scan was itself disturbed (unstable totals, short pages):
         * that is precisely when the thread is most likely to have been
         * skipped rather than gone, and scan two starting fresh from offset
         * 0 is what finds it. Certification still requires BOTH scans
         * complete and agreeing; recovery only requires scan two to look. */
        return enumerate(folder).then(function (en2) {
          var hits2 = en2.items.filter(function (i) {
            return i.convId === convId;
          });
          var confirmed = en.complete && en2.complete && !hits2.length
            && en2.items_seen === en.items_seen;
          if (hits2.length) {
            /* The second scan FOUND it: scan one was the race. Resolve it. */
            hits2.sort(function (a, b) {
              return String(b.received || "")
                .localeCompare(String(a.received || ""));
            });
            return getItem(hits2[0].itemId).then(function (full) {
              return {found: true, folder: folder, members: hits2.length,
                      terminated: en2.terminated, complete: en2.complete,
                      items_seen: en2.items_seen,
                      total_items_in_view: en2.total_items_in_view,
                      page_count: en2.page_count,
                      itemId: hits2[0].itemId, item: full,
                      changekey_refetched_at: now()};
            });
          }
          return {found: false, folder: folder,
                  terminated: en.terminated && en2.terminated,
                  /* `complete` is what absence() certifies on, so it now
                   * means: two consecutive complete scans, both absent,
                   * agreeing on the folder's size. */
                  complete: confirmed,
                  absence_scans: 2,
                  scans_agree: en2.items_seen === en.items_seen,
                  items_seen: en2.items_seen,
                  total_items_in_view: en2.total_items_in_view,
                  page_count: en.page_count + en2.page_count};
        });
      }
      hits.sort(function (a, b) {
        return String(b.received || "").localeCompare(String(a.received || ""));
      });
      return getItem(hits[0].itemId).then(function (full) {
        if (full.ok && full.degraded) {
          /* The LIST ROW is the fallback for what the narrow shape omits
           * (2026-08-13): the enumeration already carries this item's
           * categories and changeKey, and this function knows the
           * conversation id it was asked for. The chip before-image and the
           * MoveItem fallback need nothing else. */
          full.categories = hits[0].categories || [];
          full.changeKey = full.changeKey || hits[0].changeKey;
          full.convId = full.convId || convId;
        }
        return {found: true, folder: folder, members: hits.length,
                terminated: en.terminated, complete: en.complete,
                items_seen: en.items_seen,
                total_items_in_view: en.total_items_in_view,
                page_count: en.page_count,
                itemId: hits[0].itemId, item: full,
                changekey_refetched_at: now()};
      });
    });
  }

  /* THE ABSENT-TARGET OUTCOME WORDS, in ONE place. The host half keeps its own
   * copy — `cos_mutate.ABSENT_TARGET_FLAG`, which is what decides whether a night
   * carries on — and until the review of 2026-08-12 that copy was two string
   * literals nothing tied to their producer. `tests/test_cos_mutate.py` now
   * reads THIS list out of THIS file and asserts the two are equal, and the JS
   * suite asserts every skip the preparers emit is a member of it. */
  var ABSENT_TARGET_OUTCOMES = ["target-not-found", "source-thread-not-found"];

  /* CONCLUSIVE ABSENCE, OR NO ABSENCE AT ALL (review 2026-08-12). "The thread
   * moved" and "I could not see the thread" both arrive here as a
   * `resolveTarget` that found nothing, and the host used to skip on either —
   * so a truncated enumeration (this deployment has a measured case: an
   * occluded window starved 12 of 290 rows) or a throttled `GetItem` could
   * drop every row of a night while the run still reported completion. An
   * enumeration is conclusive only when the server said it reached the last
   * item in the range (`IncludesLastItemInRange`, the same flag the archive
   * verification and the reconciliation queries already treat as load-bearing).
   * The evidence travels with the skip so the run report can be audited rather
   * than believed.
   *
   * `complete`, NOT `terminated` (review 2026-08-12). The flag alone says the
   * server reached the last item of the range it was paging; it says nothing
   * about the rows a mis-advanced offset stepped over on the way, and it does
   * not know the folder holds more items than were collected. `enumerate` now
   * reports both and `complete` is their conjunction.
   *
   * `absent_target: true` IS THE TYPED FACT the host decides on. It used to
   * decide by matching an outcome STRING, and the archive lane's own absence
   * word ("already-absent-from-inbox") was not in that list — so no number of
   * vanished archive targets ever reached the skip cap, and the night reported
   * completion. A flag cannot drift out of a list it is not in. */
  function absence(t) {
    return {absent_target: true,
            absence_conclusive: t.found === false && t.complete === true,
            /* How many complete scans agreed (round 3): 2 on the confirmed
             * path, null when the first scan was already inconclusive. */
            absence_scans: t.absence_scans || null,
            enumeration_terminated: t.terminated === true,
            enumeration_complete: t.complete === true,
            enumeration_items_seen: t.items_seen === undefined
              ? null : t.items_seen,
            enumeration_total_in_view: t.total_items_in_view === undefined
              ? null : t.total_items_in_view,
            enumeration_pages: t.page_count || 0,
            enumeration_folder: t.folder || null};
  }

  /* ---------------- prepare -> (apply | dry) -------------------------------
   * ONE build path. The dry run is not a description of what the driver would
   * do, it is the driver stopping one line before `fetch` — same resolve, same
   * re-fetched ChangeKey, same builder, same validator. A rehearsal that runs
   * different code proves nothing about the performance. */
  function prepare(m) {
    if (m.verb === "archive") return prepareArchive(m);
    if (m.verb === "categorize") return prepareCategorize(m);
    if (m.verb === "draft") return prepareDraft(m);
    return Promise.reject(new Error("unknown mutation verb "
      + JSON.stringify(m.verb)));
  }

  /* `restore: true` is the UNDO — the same primitive reversed, which is what
   * makes the canary drill a drill of the real path rather than of a
   * lookalike. Absence is checked in the folder we moved OUT of, so the
   * verification means the same thing in both directions. */
  function prepareArchive(m) {
    var source = m.restore ? "archive" : "inbox";
    var dest = m.restore ? "inbox" : "archive";
    return resolveTarget(m.conversation_id, source).then(function (t) {
      if (!t.found) {
        /* "ALREADY ARCHIVED" IS A CLAIM, and an incomplete enumeration cannot
         * make it (review 2026-08-12). Read off a truncated folder listing it
         * said `reconciled` — the strongest word this machine has, meaning a
         * re-read found the effect — about a thread that may well still be
         * sitting in the Inbox. Conclusive absence keeps that claim; an
         * incomplete read gets its own word and stops the run.
         *
         * AND IT IS NOT `reconciled` (review 2026-08-12). `reconciled` is the
         * strongest word this machine has and it means a re-read found the
         * effect of something THIS RUN DID. Nothing was dispatched here, so
         * the honest terminal word is the one the other two verbs already get:
         * `aborted-not-applied`. Saying `reconciled` also walked the row past
         * the host's `if state != "reconciled"` accounting, which is why no
         * number of vanished archive targets ever reached the skip cap.
         * `verification` stays `response-confirmed` — the mailbox really was
         * re-read and really does hold the goal state — and `undo_pass` counts
         * its restores on that word. */
        var ab = absence(t);
        if (!ab.absence_conclusive) {
          return {t: t, source: source, dest: dest,
                  skip: Object.assign({
                    verb: "archive", conversation_id: m.conversation_id,
                    state: "sent", outcome: "enumeration-incomplete",
                    verification: "verified-failed",
                    dispatched: false}, ab)};
        }
        /* "ABSENT FROM THE SOURCE" IS ONLY HALF THE GOAL STATE (review
         * 2026-08-13, round 3). A RESTORE's goal is "back in the Inbox", and
         * this skip claimed `response-confirmed` — which `undo_pass` counts
         * as restored and never retries — without ever reading the
         * destination. A thread the owner deleted, or moved to a third
         * folder, between the archive and the undo was reported restored. So
         * a conclusive source-absence now confirms the DESTINATION before it
         * claims the goal state; absent from both folders is `unknown` and
         * says so, for a human. The forward direction keeps its source-only
         * claim: its skip is "nothing to dispatch", counted by the cap,
         * never counted as a success. */
        if (!m.restore) {
          return {t: t, source: source, dest: dest,
                  skip: Object.assign({
                    verb: "archive", conversation_id: m.conversation_id,
                    state: "aborted-not-applied",
                    outcome: "already-absent-from-" + source,
                    verification: "response-confirmed",
                    dispatched: false}, ab)};
        }
        return resolveTarget(m.conversation_id, dest).then(function (d) {
          var inDest = d.found === true;
          return {t: t, source: source, dest: dest,
                  skip: Object.assign({
                    verb: "archive", conversation_id: m.conversation_id,
                    state: inDest ? "aborted-not-applied" : "unknown",
                    outcome: inDest
                      ? "already-absent-from-" + source
                      : "absent-from-source-and-destination",
                    verification: inDest ? "response-confirmed"
                                         : "verified-failed",
                    destination_confirmed: inDest,
                    dispatched: false}, ab)};
        });
      }
      /* This build archives with `ApplyConversationAction`, not `MoveItem`
       * (measured 2026-08-11). `MoveItem` stays supported because a build that
       * sends it is still valid — the APPROVED SHAPE decides, never a constant
       * in this file. */
      /* Keyed by JOB (`ApplyConversationAction:Move`), because one verb carries
       * two of them — the bare key stopped existing when the chip lane shipped,
       * and testing for it routed every archive back to `MoveItem` and a hard
       * deny (measured on run 118's dry run). */
      if ((cfg.shapes || {})[shapeKey("ApplyConversationAction", "Move")]) {
        return {t: t, action: "ApplyConversationAction", source: source,
                dest: dest,
                body: buildConversationMove(cfg.shapes, cfg.header,
                                            m.conversation_id, !m.restore),
                ctx: {shapes: cfg.shapes}};
      }
      return {t: t, action: "MoveItem", source: source, dest: dest,
              body: buildMove(cfg.shapes, cfg.header, t.itemId,
                              t.item.changeKey, dest),
              ctx: {shapes: cfg.shapes}};
    });
  }

  function prepareCategorize(m) {
    return resolveTarget(m.conversation_id, m.folder || "inbox").then(function (t) {
      /* handled below: the conversation chip needs the SAME before-image and
       * the same one-chip discipline as the per-item write. */
      if (!t.found) {
        return {t: t, skip: Object.assign(
          {verb: "categorize", conversation_id: m.conversation_id,
           state: "sent", outcome: ABSENT_TARGET_OUTCOMES[0],
           verification: "verified-failed", dispatched: false}, absence(t))};
      }
      if (!t.item.ok) {
        /* FOUND, AND UNREADABLE — not an absence in any sense: the row IS in
         * the folder and the `GetItem` on it failed. It shared the absent word
         * until the review of 2026-08-12, which made a throttled read
         * indistinguishable from a thread that moved. Its own word, so the
         * host stops on it. */
        return {t: t, skip: {verb: "categorize", conversation_id: m.conversation_id,
                             state: "sent", outcome: "target-unreadable",
                             absence_conclusive: false,
                             item_response_code: t.item.code || null,
                             item_http_status: t.item.status || null,
                             verification: "verified-failed", dispatched: false}};
      }
      var before = t.item.categories.slice();
      var after = before.slice();
      if (m.mode === "remove") {
        after = after.filter(function (c) { return c !== m.chip; });
      } else if (after.indexOf(m.chip) === -1) {
        after.push(m.chip);
      }
      if (setEq(before, after)) {
        return {t: t, before: before, after: after,
                skip: {verb: "categorize", conversation_id: m.conversation_id,
                       state: "reconciled", outcome: "already-applied",
                       before_image: before, after: after,
                       verification: "response-confirmed", dispatched: false}};
      }
      if ((cfg.shapes || {})[shapeKey("ApplyConversationAction",
                                      "UpdateAlwaysCategorizeRule")]) {
        /* `after` TRAVELS WITH THE PREPARATION. Verification compares the
         * re-read categories against it, so a branch that omits it makes every
         * successful chip report `verified-failed` — measured on run 118's
         * second chip, where the re-read showed exactly the wanted set and the
         * run stopped anyway. Same class as the archive response reader. */
        /* ADD AND REMOVE ARE DIFFERENT PAYLOADS, not the same payload with a
         * shorter list. Routing a removal through the add builder sent a reduced
         * `Categories`, which updates the forward RULE and leaves the chip on the
         * thread — server `NoError`, and the re-read below correctly called it
         * `verified-failed-noop` (FINDING 2026-08-12). `after` still travels with
         * the preparation either way, because it is what that re-read compares. */
        return {t: t, action: "ApplyConversationAction", before: before,
                after: after,
                body: m.mode === "remove"
                  ? buildConversationUnchip(cfg.shapes, cfg.header,
                                            m.conversation_id, m.chip)
                  : buildConversationCategorize(cfg.shapes, cfg.header,
                                                m.conversation_id, after),
                ctx: {shapes: cfg.shapes, before_categories: before},
                /* SAID OUT LOUD on every chip: this build has no per-item
                 * category write, so the chip also leaves a standing rule. */
                side_effect: "standing-always-categorize-rule"};
      }
      return {t: t, action: "UpdateItem", before: before, after: after,
              body: buildCategorize(cfg.shapes, cfg.header, t.itemId,
                                    t.item.changeKey, after),
              ctx: {shapes: cfg.shapes, before_categories: before}};
    });
  }

  function prepareDraft(m) {
    return resolveTarget(m.conversation_id, "inbox").then(function (t) {
      if (!t.found) {
        return {t: t, skip: Object.assign(
          {verb: "draft", conversation_id: m.conversation_id,
           state: "sent", outcome: ABSENT_TARGET_OUTCOMES[1],
           verification: "verified-failed", dispatched: false}, absence(t))};
      }
      if (!t.item.ok) {
        /* The categorize lane's rule, one verb over: found and unreadable is a
         * read failure, never an absence (review 2026-08-12). */
        return {t: t, skip: {verb: "draft", conversation_id: m.conversation_id,
                             state: "sent", outcome: "source-thread-unreadable",
                             absence_conclusive: false,
                             item_response_code: t.item.code || null,
                             item_http_status: t.item.status || null,
                             verification: "verified-failed", dispatched: false}};
      }
      /* THE SIGNATURE IS PER-MUTATION, and it must be the SAME string the
       * reconciliation query will later search Drafts for. The host computes it
       * (`draft_signature(run_id, conversation_id)`) and sends it with the
       * mutation; `cfg.signature` is only the run-wide prefix fallback. A body
       * carrying the prefix while reconciliation hunts the full string would
       * report a draft that exists as never created — the exact wrong answer in
       * the exact case this machinery exists for. */
      var sig = m.signature || cfg.signature;
      var text = String(m.text || "");
      if (text.indexOf(sig) === -1) text = text + "\n\n" + sig;
      var recipients = (m.recipients && m.recipients.length)
        ? m.recipients : t.item.participants.slice(0, 1);
      return {t: t, action: "CreateItem", signature: sig,
              body: buildDraft(cfg.shapes, cfg.header, {
                text: text,
                subject: m.subject || ("RE: " + t.item.subject),
                recipients: recipients,
                conversation_id: m.conversation_id,
                item_id: t.itemId,
                change_key: t.item.changeKey,
              }),
              ctx: {shapes: cfg.shapes, signature: sig,
                    allowed_recipients: t.item.participants}};
    });
  }

  /* WHAT WOULD BE SENT — built, validated, and then NOT sent. The Header is
   * stripped on the way out: it is the live bearer envelope and it does not
   * leave this world, dry run or otherwise. */
  function dryOne(m) {
    return prepare(m).then(function (p) {
      var out = {
        verb: m.verb, conversation_id: m.conversation_id,
        resolved: {
          found: !!(p.t && p.t.found), item_id: (p.t && p.t.itemId) || null,
          changekey_refetched_at: (p.t && p.t.changekey_refetched_at) || null,
          before_categories: (p.t && p.t.item && p.t.item.categories) || null,
          participants: (p.t && p.t.item && p.t.item.participants) || [],
          internet_message_id: (p.t && p.t.item && p.t.item.internetMessageId) || null,
        },
        skip: p.skip || null,
        action: p.action || null,
        dispatched: false,
      };
      if (!p.body) return out;
      var reason = validate(p.action, p.body, p.ctx);
      out.fingerprint = fingerprint(p.body);
      out.allowlist = reason ? ("REJECTED: " + reason) : "ACCEPTED";
      out.would_dispatch = !reason;
      var body = clone(p.body);
      delete body.Header;
      out.payload_without_header = body;
      return out;
    });
  }

  function applyOne(m) {
    return prepare(m).then(function (p) {
      if (p.skip) return p.skip;
      return send(p.action, p.body, {conversation_id: m.conversation_id,
                                     ctx: p.ctx})
        .then(function (r) { return verifyOne(m, p, r); });
    });
  }

  /* The moved-item id from an `ApplyConversationAction` response, wherever this
   * build puts it. `ReturnMovedItemIds: true` is in the captured request, so the
   * server does return them; the KEY is read defensively rather than assumed,
   * and `response_shape_keys` below reports what was actually there when none of
   * the known keys match — a diagnosis beats a guess. */
  function firstMovedId(msg) {
    if (!msg) return null;
    var pools = [msg.MovedItemIds, msg.MovedItems, msg.ConversationActionResults,
                 msg.ItemIds];
    for (var i = 0; i < pools.length; i++) {
      var pool = pools[i];
      if (!pool) continue;
      var arr = Array.isArray(pool) ? pool : (pool.ItemId ? [pool] : null);
      if (!arr || !arr.length) continue;
      var first = arr[0];
      var id = (first && first.Id) || (first && first.ItemId && first.ItemId.Id);
      if (id) return id;
    }
    return null;
  }

  function responseShapeKeys(msg) {
    if (!msg || typeof msg !== "object") return [];
    return Object.keys(msg).slice(0, 20);
  }

  /* WHY A FAILED DISPATCH CARRIES THIS. `{"response_code": null}` is not a
   * diagnosis — it cannot even tell "the server said nothing" apart from "we
   * could not read what the server said", and `missing` is the field that
   * names which one it was.
   *
   * `MessageText` used to ride along whole and is now reduced to a length and
   * a digest (review 2026-08-12): the server composes that prose out of the
   * request, so it quotes subjects, addresses and ids into an artifact kept
   * under the vault. The enum code survives, which is what actually named the
   * cause the two times this block earned its place (run 118's
   * `ErrorInvalidIdMalformed`, run 122's missing envelope). The body's shape
   * rides along ONLY when the expected envelope was absent, and at the
   * DISPATCH failure sites only — a re-read that fails after a `NoError`
   * already carries its receipts, and the happy path is untouched. */
  function failureDetail(r, msg) {
    var missing = [];
    if (!msg) missing.push("response_envelope");
    if (!(msg && msg.ResponseCode)) missing.push("response_code");
    if (!(msg && msg.MessageText)) missing.push("message_text");
    if (!msg && !(r && r.body_len)) missing.push("response_body");
    var code = msg && msg.ResponseCode ? String(msg.ResponseCode) : null;
    var safe = safeCode(code);
    var text = msg && msg.MessageText ? String(msg.MessageText) : null;
    return {
      http_status: r ? r.status : null,
      response_code: safe,
      response_code_not_an_enum: !!(code && !safe),
      message_text_len: text === null ? null : text.length,
      message_text_digest: text === null ? null : fnv1a(text),
      /* The CLASS of an unrecognised body, so a serialization defect can be
       * told from a gateway page at the same status (review 2026-08-12). */
      message_text_kind: text === null ? null : bodyKind(text),
      body_len: msg ? null : ((r && r.body_len) || null),
      body_digest: msg ? null : ((r && r.body_digest) || null),
      body_kind: msg ? null : ((r && r.body_kind) || null),
      missing: missing,
    };
  }

  function verifyOne(m, p, r) {
    var msg = firstItem(r);
    /* SANITISED ONCE, HERE. Every `response_code:` this function emits reaches
     * the host's `connector_result` and then disk, and until 2026-08-12 all
     * three dispatch branches carried the raw value while the nested
     * `failure.response_code` beside them was the only sanitised copy. Reading
     * it through `safeCode` also keeps the `!== "NoError"` comparisons honest:
     * a value that is not the enum is not `NoError` either, so it fails
     * closed. */
    var code = safeCode(msg && msg.ResponseCode);
    if (m.verb === "archive") {
      /* TWO RESPONSE SHAPES, because there are two request shapes. `MoveItem`
       * answers with `Items[0].ItemId`; `ApplyConversationAction` answers with
       * moved ids under its own key and `Items` is absent — reading only the
       * first shape is what made a SUCCESSFUL archive report `verified-failed`
       * on 2026-08-11 (both the move and its undo had actually applied). */
      var newId = (msg && msg.Items && msg.Items[0]
        && msg.Items[0].ItemId && msg.Items[0].ItemId.Id)
        || firstMovedId(msg);
      if (code !== "NoError" || !newId) {
        /* G_R2: NoError with nothing moved is the documented silent no-op, and
         * it must never read as a success. */
        return Promise.resolve({verb: "archive", conversation_id: m.conversation_id,
                                state: "sent", outcome: "no-op-or-error",
                                response_code: code, new_item_id: newId || null,
                                response_shape_keys: responseShapeKeys(msg),
                                failure: failureDetail(r, msg),
                                verification: "verified-failed", dispatched: true,
                                changekey_refetched_at: p.t.changekey_refetched_at});
      }
      return verifyArchive(m, p, newId);
    }
    if (m.verb === "categorize") {
      if (code !== "NoError") {
        return Promise.resolve({verb: "categorize", conversation_id: m.conversation_id,
                                state: "sent", before_image: p.before,
                                response_code: code, verification: "verified-failed",
                                failure: failureDetail(r, msg), dispatched: true,
                                changekey_refetched_at: p.t.changekey_refetched_at});
      }
      return getItem(p.t.itemId).then(function (re) {
        if (re.ok && re.degraded) {
          /* A degraded re-read carries no Categories, so the compare would be
           * a lie in either direction. The LIST VIEW does carry them — the
           * same source the before-image fell back to — so the verification
           * re-reads the folder instead, and says which surface answered. */
          return resolveTarget(m.conversation_id, m.folder || "inbox")
            .then(function (t2) {
              var cats = (t2.found && t2.item && t2.item.categories) || null;
              var ok2 = cats !== null && setEq(cats, p.after);
              return {
                verb: "categorize", conversation_id: m.conversation_id,
                state: ok2 ? "reconciled" : "confirmed", dispatched: true,
                before_image: p.before, after: p.after, observed_after: cats,
                changekey_refetched_at: p.t.changekey_refetched_at,
                side_effect: p.side_effect || null,
                verified_via: "list-row (item read degraded: meeting-request "
                  + "class, full shape 500s server-side)",
                verification: ok2 ? "verified-categorized"
                  : (cats !== null && setEq(cats, p.before)
                     ? "verified-failed-noop" : "verified-failed"),
              };
            });
        }
        var ok = re.ok && setEq(re.categories, p.after);
        return {
          verb: "categorize", conversation_id: m.conversation_id,
          state: ok ? "reconciled" : "confirmed", dispatched: true,
          before_image: p.before, after: p.after, observed_after: re.categories,
          changekey_refetched_at: p.t.changekey_refetched_at,
          /* Carried into the RESULT, not just noted at preparation time: a
           * standing rule the run report never mentions is a side effect the
           * owner cannot see. */
          side_effect: p.side_effect || null,
          /* A response that changed nothing is a FAILURE to verify, never a
           * success (G_R2 — the documented silent-no-op case). */
          verification: ok ? "verified-categorized"
            : (setEq(re.categories, p.before) ? "verified-failed-noop"
                                              : "verified-failed"),
        };
      });
    }
    var draftId = msg && msg.Items && msg.Items[0]
      && msg.Items[0].ItemId && msg.Items[0].ItemId.Id;
    if (code !== "NoError" || !draftId) {
      return Promise.resolve({verb: "draft", conversation_id: m.conversation_id,
                              state: "sent", response_code: code,
                              failure: failureDetail(r, msg),
                              verification: "verified-failed", dispatched: true,
                              changekey_refetched_at: p.t.changekey_refetched_at});
    }
    return getItem(draftId).then(function (re) {
      var signed = String(re.text || "").indexOf(p.signature) !== -1;
      var ok = re.ok && re.isDraft === true && signed;
      return {
        verb: "draft", conversation_id: m.conversation_id,
        state: ok ? "reconciled" : "confirmed", dispatched: true,
        new_item_id: draftId, destination_folder: DRAFT_FOLDER,
        changekey_refetched_at: p.t.changekey_refetched_at,
        signature: p.signature,
        verification: ok ? "verified-draft-saved" : "verified-failed",
        receipts: {is_draft: re.isDraft === true, signature_present: signed,
                   send_attempted: false},
      };
    });
  }

  function verifyArchive(m, p, newId) {
    var t = p.t;
    return getItem(newId).then(function (moved) {
      return enumerate(p.source).then(function (src) {
        var stillInSource = src.items.some(function (i) {
          return i.convId === m.conversation_id;
        });
        /* The Deleted-Items watch (v5.9/v5.18): scoped to the member THIS run
         * moved. A convid with pre-existing members in Deleted Items is not a
         * breach — conversation ids span folders — so the check is by item id. */
        return enumerate("deleteditems").then(function (del) {
          var inDeleted = del.items.some(function (i) {
            return i.itemId === newId;
          });
          /* `complete`, not `terminated`: "the thread is no longer in the
           * source folder" is exactly the absence claim the paging fix exists
           * for, and a read that stepped over rows can make it wrongly. */
          var ok = moved.ok && !stillInSource && !inDeleted && del.complete
            && src.complete;
          return {
            verb: "archive", conversation_id: m.conversation_id,
            /* `confirmed` is not a softer `reconciled`: it says the server
             * ACCEPTED the write and the re-read did not find its effect. That
             * is a different fact from `sent` ("the request left, outcome
             * unknown") and the ledger has to be able to say which. */
            state: ok ? "reconciled" : "confirmed",
            dispatched: true,
            new_item_id: newId,
            original_folder: p.source, destination_folder: p.dest,
            changekey_refetched_at: t.changekey_refetched_at,
            verification: ok ? "verified-archived" : "verified-failed",
            receipts: {
              moved_item_resolves: moved.ok,
              source_folder: p.source,
              source_absent: !stillInSource,
              source_enumeration_complete: src.complete,
              source_enumeration_terminated: src.terminated,
              source_items_seen: src.items_seen,
              source_total_in_view: src.total_items_in_view,
              deleted_items_absent: !inDeleted,
              deleted_items_enumeration_complete: del.complete,
            },
          };
        });
      });
    });
  }

  /* ---------------- reconciliation: did the server apply it? ---------------
   * The distributed-transaction boundary (adv-11). A response lost after the
   * server accepted the write leaves the ledger in `sent` with no id; these
   * queries answer "did it happen" from the server's own state, which is the
   * only place the answer lives. */
  function reconcile(m) {
    if (m.verb === "archive") {
      /* Through `resolveTarget`, not a raw enumerate: "absent from the inbox"
       * is exactly the absence claim the double-scan confirm exists for
       * (review 2026-08-13, round 3) — a single scan can certify a present
       * thread absent under a concurrent deletion, and this path turns that
       * misread into a `reconciled` ledger row for a mutation that may never
       * have applied. */
      return resolveTarget(m.conversation_id, "inbox").then(function (t) {
        return {verb: "archive", conversation_id: m.conversation_id,
                applied: t.found === false && t.complete === true,
                conclusive: t.found === true || t.complete === true,
                absence_scans: t.absence_scans || null,
                query: "inbox enumeration, conversation absent "
                  + "(absence double-scanned)",
                observed: t.found ? "still-in-inbox" : "absent-from-inbox"};
      });
    }
    if (m.verb === "categorize") {
      return resolveTarget(m.conversation_id, m.folder || "inbox")
        .then(function (t) {
          if (!t.found || !t.item.ok) {
            return {verb: "categorize", conversation_id: m.conversation_id,
                    applied: false, conclusive: false,
                    query: "GetItem on the resolved target",
                    observed: "target-not-found"};
          }
          if (!m.chip) {
            /* FAIL CLOSED. A reconcile with no chip name cannot answer its own
             * question, and answering NO anyway is how run 118 filed a chip the
             * mailbox was carrying as `aborted-not-applied`. */
            return {verb: "categorize", conversation_id: m.conversation_id,
                    applied: false, conclusive: false,
                    query: "GetItem categories",
                    observed: "no chip name on the row to reconcile against"};
          }
          var has = t.item.categories.indexOf(m.chip) !== -1;
          var applied = (m.mode === "remove") ? !has : has;
          return {verb: "categorize", conversation_id: m.conversation_id,
                  applied: applied, conclusive: true,
                  query: "GetItem categories",
                  observed: t.item.categories};
        });
    }
    /* A draft is reconciled by the machine signature this run embedded in its
     * body: same conversation, same signature, exactly one match. Two matches
     * is a DUPLICATE and is reported, never silently accepted. */
    return enumerate(DRAFT_FOLDER).then(function (drafts) {
      var candidates = drafts.items.filter(function (i) {
        return i.convId === m.conversation_id;
      });
      return candidates.reduce(function (chain, c) {
        return chain.then(function (acc) {
          return getItem(c.itemId).then(function (full) {
            if (full.ok && String(full.text || "").indexOf(m.signature) !== -1) {
              acc.push(c.itemId);
            }
            return acc;
          });
        });
      }, Promise.resolve([])).then(function (matches) {
        return {verb: "draft", conversation_id: m.conversation_id,
                applied: matches.length === 1,
                duplicate: matches.length > 1,
                conclusive: drafts.complete,
                matches: matches,
                query: "drafts enumeration joined on conversation id, "
                  + "confirmed by the run's machine signature in the body",
                observed: matches.length + " signed draft(s)"};
      });
    });
  }

  /* ---------------- shape export: what the host is allowed to keep ---------
   * The approved skeletons cross the bridge SCRUBBED — structure and the
   * constants that make the shape what it is, never a mailbox id, an address, a
   * subject or a body. Header values never cross at all, at any stage: the
   * bearer stays in this world, which is the whole reason this world exists. */
  var SCRUB_KEYS = {Id: 1, ChangeKey: 1, Subject: 1, Value: 1, EmailAddress: 1,
                    Name: 1, RoutingType: 1, ItemId: 1, ConversationId: 1,
                    Categories: 1, CategoriesToRemove: 1, MailboxId: 1};
  var SCRUBBED = "<scrubbed>";

  function scrub(node, key, parent) {
    if (typeof node === "string") {
      if (!SCRUB_KEYS[key]) return node;
      /* A distinguished folder id ("inbox", "archive", "drafts") is STRUCTURE —
       * it is the difference between a shape that archives and one that does
       * something else — and it names no mailbox. */
      if (key === "Id" && parent
          && String(parent.__type || "").indexOf("DistinguishedFolderId") === 0) {
        return node;
      }
      return SCRUBBED;
    }
    if (Array.isArray(node)) {
      return node.map(function (x) { return scrub(x, key, parent); });
    }
    if (node && typeof node === "object") {
      var out = {};
      Object.keys(node).forEach(function (k) { out[k] = scrub(node[k], k, node); });
      return out;
    }
    return node;
  }

  function exportShapes(capture) {
    var out = {};
    var cap = capture || (typeof window !== "undefined" ? window.__cosCap : null);
    if (!cap || !cap.calls) return {shapes: out, error: "no capture buffer in this world"};
    /* One entry per JOB, not per verb: `ApplyConversationAction` is exported as
     * `ApplyConversationAction:Move` and `…:UpdateAlwaysCategorizeRule`, so an
     * archive's approved shape can never authorise a chip. */
    var jobs = [];
    Object.keys(ALLOWED_ACTIONS).forEach(function (action) {
      if (action === "ApplyConversationAction") {
        Object.keys(ALLOWED_CONVERSATION_ACTIONS).forEach(function (ca) {
          jobs.push({action: action, conv: ca, key: action + ":" + ca});
        });
      } else {
        jobs.push({action: action, conv: null, key: action});
      }
    });
    jobs.forEach(function (job) {
      var action = job.action;
      for (var i = 0; i < cap.calls.length; i++) {
        var c = cap.calls[i];
        if (c.action !== action) continue;
        var body = cap.body ? cap.body(c) : null;
        if (!body || !body.Body) continue;
        var ca = (body.Body.ConversationActions || [])[0] || {};
        if (job.conv && String(ca.Action || "") !== job.conv) continue;
        /* ONE ENTRY, TWO VARIANT SLOTS. The chip's add and its remove are the
         * same verb and the same Action and differ only in `Categories` vs
         * `CategoriesToRemove` (FINDING 2026-08-12), so they cannot be told
         * apart by the job key — they share the entry and occupy different
         * slots on it. First example per variant wins, exactly as the
         * single-variant export did, and the loop no longer stops at the first
         * match or the second variant could never be seen. */
        var slot = Array.isArray(ca.CategoriesToRemove) ? "_remove" : "";
        var entry = out[job.key] || (out[job.key] = {});
        if (entry["skeleton" + slot]) continue;
        var skeleton = scrub(body, null, null);
        delete skeleton.Header;
        entry["skeleton" + slot] = skeleton;
        entry["fingerprint" + slot] = fingerprint(skeleton);
        entry.captured_at = entry.captured_at || c.ts;
        if (entry.status === undefined) entry.status = c.status;
        /* The opaque destination, taken from the RAW body BEFORE scrubbing —
         * it is an id, so the scrubber removes it, and it is the only thing
         * that can tell "the archive folder" from "deleted items" on a build
         * that sends no distinguished folder name. */
        if (action === "ApplyConversationAction") {
          entry.conversation_action = entry.conversation_action
            || String(ca.Action || "");
          if (!entry.destination_id) {
            entry.destination_id = String(
              ((ca.DestinationFolderId || {}).BaseFolderId || {}).Id || "");
          }
          /* The SOURCE folder, kept because the UNDO moves back into it. Both
           * ids come from one request the server accepted, so the reversed move
           * is as replayed as the forward one — no folder id is ever invented. */
          if (!entry.context_id) {
            entry.context_id = String(
              ((ca.ContextFolderId || {}).BaseFolderId || {}).Id || "");
          }
        }
      }
    });
    /* MISSING means "nothing to build the primary payload from". An entry that
     * carries only the remove variant is not a captured chip lane — the add is
     * still missing and has to say so. */
    return {shapes: out, actions_missing: jobs
      .map(function (j) { return j.key; })
      .filter(function (k) { return !out[k] || !out[k].skeleton; })};
  }

  var api = {
    /* pure, and therefore the testable surface */
    scrub: function (b) { return scrub(b, null, null); },
    exportShapes: exportShapes,
    buildConversationMove: buildConversationMove,
    buildConversationCategorize: buildConversationCategorize,
    shapeKey: shapeKey,
    firstMovedId: firstMovedId,
    keyPaths: keyPaths,
    fingerprint: fingerprint,
    failureDetail: failureDetail,
    validate: validate,
    buildMove: buildMove,
    buildConversationMove: buildConversationMove,
    buildConversationCategorize: buildConversationCategorize,
    buildConversationUnchip: buildConversationUnchip,
    buildCategorize: buildCategorize,
    buildDraft: buildDraft,
    ALLOWED_ACTIONS: ALLOWED_ACTIONS,
    ABSENT_TARGET_OUTCOMES: ABSENT_TARGET_OUTCOMES,
    PERMITTED_FOLDERS: PERMITTED_FOLDERS,
    MANAGED_CHIPS: MANAGED_CHIPS,
    BANNED_DISPOSITIONS: BANNED_DISPOSITIONS,
    SAVE_ONLY: SAVE_ONLY,
    /* the run surface */
    init: api_init,
    setHeader: function (h) { cfg.header = clone(h); },
    enumerate: enumerate,
    getItem: getItem,
    resolveTarget: resolveTarget,
    prepare: prepare,
    applyOne: applyOne,
    dryOne: dryOne,
    reconcile: reconcile,
    state: function () {
      return {armed: armed, dispatched: dispatched, events: events.slice(),
              rejected: rejected.slice()};
    },
    disarmForTest: function () { armed = false; },
  };

  /* ---------------- the DOM bridge, and ONE op per pass --------------------
   * The host drives a single operation per evaluation, deliberately. A
   * self-driving batch is the right shape for a long READ pass (it survives the
   * flaky host->Chrome bridge); it is the wrong shape for mutations, where the
   * host must write the undo row BEFORE the call, re-read the kill switch
   * BETWEEN calls, and count the cap against what is already on disk. One
   * mutation is a few seconds of work, far inside the transport's limits.
   */
  var IN_ID = "__cos_min";
  var OUT_ID = "__cos_mout";

  function bootBridge() {
    var state = {seq: 0, done: true, error: null, out: null, phase: "idle"};

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
      catch (e) {
        node(OUT_ID).textContent = JSON.stringify(
          {phase: "mirror-error", done: true, error: String(e)});
      }
    }

    function handle(op, args) {
      args = args || {};
      if (op === "init") {
        var cap = window.__cosCap;
        if (!cap || !cap.calls) {
          throw new Error("no capture buffer in this world — install "
            + "tools/cos_capture_hook.js at document_start first");
        }
        /* THE FRESHEST ENVELOPE, NOT THE FIRST. `seed()` returns the BOOT call,
         * whose bearer expires within the hour — so a tab that has been open a
         * while inits against a token the server has already stopped accepting,
         * and every mutation blocks on a 401 that looks like a lane failure
         * (measured 2026-08-11: an 87-mutation rehearsal, all blocked, minutes
         * after `--prepare` reported ready). Both carry an accepted FindItem
         * body; only one still authenticates. */
        var seedCall = cap.freshestSeed("FindItem", null) || cap.seed("FindItem");
        if (!seedCall) {
          throw new Error("no captured FindItem envelope to replay. This "
            + "build's list is served from Loki, so a settled tab may fire no "
            + "FindItem at all (measured s03: 137 captured calls, zero "
            + "FindItem, hook installed after load) — install the hook at "
            + "document_start, or make the tab ACTIVE and let the list settle "
            + "with the hook already in place (measured 2026-08-11, three "
            + "times; see tools/cos_capture_hook.js).");
        }
        var accepted = cap.body(seedCall);
        api_init({
          fetch: cap.rawFetch,
          envelope: {url: seedCall.url, headers: seedCall.headers},
          header: accepted && accepted.Header,
          findItemBody: accepted,
          shapes: args.shapes || {},
          signature: args.signature,
          origin: location.origin,
        });
        return Promise.resolve({
          armed: true,
          shapes_loaded: Object.keys(args.shapes || {}).sort(),
          capture: cap.stats(),
        });
      }
      if (op === "seed_probe") {
        /* THE QUESTION THE 401 DIAGNOSTIC USED TO ANSWER FROM ASSUMPTION
         * (review 2026-08-13, round 4). The host's stop line asserted "no
         * fresher envelope had been captured by the time it failed" while
         * nothing in this build read the buffer at 401 time — and the capture
         * hook's own header records the opposite happening, measured three
         * times on 2026-08-11 (an authenticated FindItem DOES arrive once the
         * tab is active and the list settles).
         *
         * So the host asks, and this answers with the measurement rather than
         * the assumption. READ-ONLY and LOCAL: `freshestSeed` walks the
         * in-memory buffer and issues no request, so it is safe on the exact
         * path where a request just failed. It re-primes NOTHING: only a host
         * `init` arms this build, and this op does not touch `armed`. Header VALUES never cross: the reply
         * carries a boolean, a timestamp and counts. */
        var pcap = window.__cosCap;
        if (!pcap || !pcap.calls) {
          return Promise.resolve({measured: false, fresher_seed: null,
                                  why: "no capture buffer in this world"});
        }
        var fresher = pcap.freshestSeed("FindItem", args.since || null);
        var pstats = pcap.stats();
        return Promise.resolve({
          measured: true,
          since: args.since || null,
          fresher_seed: !!fresher,
          fresher_seed_at: fresher ? fresher.ts : null,
          captured_finditem: pstats.by_action.FindItem || 0,
        });
      }
      if (op === "shapes") return Promise.resolve(exportShapes(window.__cosCap));
      if (op === "resolve") {
        return resolveTarget(args.conversation_id, args.folder || "inbox")
          .then(function (t) {
            /* The host gets census facts, never the envelope and never the body
             * text: the undo row needs ids and the before-image, and nothing
             * else it could hold is worth holding. */
            return {
              found: t.found, folder: t.folder, members: t.members || 0,
              item_id: t.itemId || null,
              change_key_present: !!(t.item && t.item.changeKey),
              changekey_refetched_at: t.changekey_refetched_at || null,
              before_categories: (t.item && t.item.categories) || null,
              internet_message_id: (t.item && t.item.internetMessageId) || null,
              is_read: (t.item && t.item.isRead) || false,
              participants: (t.item && t.item.participants) || [],
              subject_len: (t.item && String(t.item.subject || "").length) || 0,
            };
          });
      }
      if (op === "apply") return applyOne(args.mutation || {});
      if (op === "dry") return dryOne(args.mutation || {});
      if (op === "reconcile") return reconcile(args.mutation || {});
      if (op === "state") return Promise.resolve(api.state());
      throw new Error("unknown op " + JSON.stringify(op));
    }

    var lastSeq = 0;
    function pump() {
      var el = document.getElementById(IN_ID);
      var msg = null;
      if (el && el.textContent) {
        try { msg = JSON.parse(el.textContent); } catch (e) { msg = null; }
      }
      if (msg && msg.seq > lastSeq) {
        lastSeq = msg.seq;
        state.seq = msg.seq;
        state.done = false;
        state.error = null;
        state.out = null;
        state.phase = msg.op;
        Promise.resolve().then(function () { return handle(msg.op, msg.args); })
          .then(function (out) {
            state.out = out;
            state.runtime = api.state();
            state.done = true;
          }, function (err) {
            state.error = String(err && err.message ? err.message : err).slice(0, 600);
            state.canary449 = !!(err && err.canary449);
            /* MIRRORED FOR THE HOST, which is the only thing that can act on
             * it: the page half disarms and stops, and re-seeding is the
             * host's. Without this the host sees run 130's failure exactly as
             * it did — a generic "FindItem … failed: http 401" with no name
             * for what it was. */
            state.auth401 = !!(err && err.auth401);
            state.mutation_in_flight = !!(err && err.mutation_in_flight);
            state.runtime = api.state();
            state.done = true;
          });
      }
      mirror();
    }

    if (window.__cosMutPump) clearInterval(window.__cosMutPump);
    window.__cosMutPump = setInterval(pump, 500);
    var stale = document.getElementById(IN_ID);
    if (stale) stale.textContent = "";
    mirror();
  }

  if (typeof module === "object" && module.exports) module.exports = api;
  if (typeof window !== "undefined") {
    window.__cosMut = api;
    if (typeof document !== "undefined" && document.documentElement) bootBridge();
  }
  return "cos-mutate-page-loaded";
})();
