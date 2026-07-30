/**
 * Reusable, zero-mutation Outlook Inbox/Sent scanner for Browser/Chrome tabs.
 *
 * The browser tab is supplied by the Codex browser runtime. This module never
 * opens messages: it only reads folder/list DOM, switches Focused/Other tabs,
 * and scrolls the virtualized message list.
 */

export function parseFolderItemCount(nodes, folderName = "Inbox") {
  const escaped = folderName.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const pattern = new RegExp(`^${escaped}\\s*-\\s*([\\d,]+)\\s+items?\\b`, "i");
  for (const node of nodes) {
    for (const value of [node.title, node.ariaLabel]) {
      const match = typeof value === "string" && value.match(pattern);
      if (match) return Number(match[1].replaceAll(",", ""));
    }
  }
  return null;
}

export function visibleScrollPoint(rect, viewport) {
  return {
    x: Math.max(1, Math.min(viewport.width - 20, Math.round(rect.x + rect.width / 2))),
    y: Math.max(1, Math.min(viewport.height - 20, Math.round(rect.y + rect.height - 20))),
  };
}

export function chooseScrollContainer(ancestors) {
  return (
    ancestors.find((node) => (
      /auto|scroll/.test(node.overflowY)
      && node.scrollHeight > node.clientHeight + 1
    ))
    || ancestors.find((node) => node.role === "listbox")
    || ancestors.find((node) => /auto|scroll/.test(node.overflowY))
    || null
  );
}

export function combineViewEvidence(views) {
  const ids = [...new Set(views.flatMap((view) => view.ids))];
  const declaredSize = views.reduce((sum, view) => sum + view.declaredSize, 0);
  const duplicateCount = views.reduce((sum, view) => sum + view.ids.length, 0) - ids.length;
  const complete = (
    views.length > 0
    && views.every((view) => (
      view.scrollAtEnd
      && view.terminalStagnantScans >= 3
      && view.ids.length === view.declaredSize
    ))
    && duplicateCount === 0
    && ids.length === declaredSize
  );
  return { ids, declaredSize, duplicateCount, complete, views };
}

export function buildSentZeroSendProof(
  rows,
  { windowStart, capturedAt, atListEnd = false },
) {
  const windowMs = Date.parse(windowStart);
  const capturedMs = Date.parse(capturedAt);
  if (!Number.isFinite(windowMs) || !Number.isFinite(capturedMs) || windowMs > capturedMs) {
    throw new Error("Sent proof requires a valid windowStart <= capturedAt");
  }

  const seen = new Set();
  let previousMs = Number.POSITIVE_INFINITY;
  let boundaryTimestamp = null;
  const items = [];
  for (const row of rows) {
    if (typeof row.itemId !== "string" || !row.itemId) {
      throw new Error("Outlook Sent row is missing its native item id");
    }
    if (seen.has(row.itemId)) throw new Error(`Duplicate Outlook Sent item id ${row.itemId}`);
    seen.add(row.itemId);

    const timestampMs = Date.parse(row.timestamp);
    if (!Number.isFinite(timestampMs) || timestampMs > capturedMs) {
      throw new Error(`Outlook Sent row ${row.itemId} has an invalid timestamp`);
    }
    if (timestampMs > previousMs) {
      throw new Error("Outlook Sent list is not sorted newest-first");
    }
    previousMs = timestampMs;

    if (timestampMs < windowMs) {
      boundaryTimestamp = new Date(timestampMs).toISOString();
      break;
    }
    items.push({
      item_id: row.itemId,
      timestamp: new Date(timestampMs).toISOString(),
    });
  }

  const complete = boundaryTimestamp !== null || atListEnd;
  return {
    identity_field: "item_id",
    identity_source: "owa-role-option-id",
    window_start: new Date(windowMs).toISOString(),
    captured_at: new Date(capturedMs).toISOString(),
    sort: "newest-first",
    complete,
    boundary: boundaryTimestamp === null ? "list-end" : "older-than-window",
    boundary_timestamp: boundaryTimestamp,
    items,
  };
}

async function inspectView(tab) {
  const observed = await tab.playwright.evaluate(() => {
    const rendered = [...document.querySelectorAll('[role="option"][data-convid]')];
    const rows = rendered.map((element) => ({
      conversationId: element.dataset.convid,
      itemId: element.id || null,
      timestamp: [...element.querySelectorAll("span[title]")]
        .map((node) => node.getAttribute("title"))
        .find((value) => Number.isFinite(Date.parse(value))) || null,
      ariaLabel: element.getAttribute("aria-label") || "",
      text: (element.innerText || "").trim(),
    }));
    const declaredSize = Math.max(
      0,
      ...rendered.map((element) => Number(element.getAttribute("aria-setsize")) || 0),
    );
    const ancestors = [];
    for (let element = rendered[0], depth = 0; element && depth < 14;
      element = element.parentElement, depth += 1) {
      const style = getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      ancestors.push({
        role: element.getAttribute("role"),
        overflowY: style.overflowY,
        scrollTop: element.scrollTop,
        scrollHeight: element.scrollHeight,
        clientHeight: element.clientHeight,
        rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
      });
    }
    return {
      rows, declaredSize, ancestors,
      viewport: { width: innerWidth, height: innerHeight },
    };
  });
  const scroller = chooseScrollContainer(observed.ancestors);
  return {
    rows: observed.rows,
    declaredSize: observed.declaredSize,
    scrollTop: scroller?.scrollTop || 0,
    scrollHeight: scroller?.scrollHeight || 0,
    clientHeight: scroller?.clientHeight || 0,
    rect: scroller?.rect || null,
    viewport: observed.viewport,
  };
}

async function selectView(tab, view, renderDelayMs) {
  const locator = tab.playwright.getByRole("tab", { name: view, exact: true });
  if (await locator.count() !== 1) throw new Error(`Outlook ${view} tab is not unique`);
  if (await locator.getAttribute("aria-selected") !== "true") {
    await locator.click();
  }
  await waitForRenderedRows(tab, renderDelayMs);
}

export async function waitForRenderedRows(tab, renderDelayMs, maxAttempts = 10) {
  const rows = tab.playwright.locator('[role="option"][data-convid]');
  for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
    const count = await rows.count();
    if (count > 0) return count;
    await tab.playwright.waitForTimeout(renderDelayMs);
  }
  throw new Error("Outlook message rows did not render");
}

async function scroll(tab, state, delta, renderDelayMs) {
  if (!state.rect) throw new Error("Outlook message-list scroll container is unavailable");
  await tab.cua.scroll({
    ...visibleScrollPoint(state.rect, state.viewport),
    scrollX: 0,
    scrollY: delta,
  });
  await tab.playwright.waitForTimeout(renderDelayMs);
  return inspectView(tab);
}

export async function scanOutlookView(
  tab,
  view,
  { maxScrolls = 20, renderDelayMs = 250 } = {},
) {
  await selectView(tab, view, renderDelayMs);
  let state = await inspectView(tab);

  // Each view is an independent virtual list. Start from its real top.
  for (let attempt = 0; state.scrollTop > 1 && attempt < maxScrolls; attempt += 1) {
    state = await scroll(
      tab,
      state,
      -Math.max(320, Math.floor(state.clientHeight * 0.9)),
      renderDelayMs,
    );
  }
  if (state.scrollTop > 1) throw new Error(`Outlook ${view} list did not return to top`);

  const rows = new Map();
  let declaredSize = state.declaredSize;
  let stagnant = 0;
  let scrolls = 0;

  while (scrolls <= maxScrolls) {
    const sizeBefore = rows.size;
    for (const row of state.rows) rows.set(row.conversationId, row);
    if (state.declaredSize > 0) declaredSize = state.declaredSize;
    const atEnd = (
      state.scrollHeight > 0
      && state.scrollTop + state.clientHeight >= state.scrollHeight - 2
    );

    if (atEnd && rows.size === sizeBefore) stagnant += 1;
    else if (!atEnd) stagnant = 0;

    if (atEnd && stagnant >= 3) break;
    if (scrolls === maxScrolls) break;

    const previousTop = state.scrollTop;
    state = await scroll(
      tab,
      state,
      Math.max(320, Math.floor(state.clientHeight * 0.9)),
      renderDelayMs,
    );
    scrolls += 1;

    if (state.scrollTop !== previousTop) stagnant = 0;
  }

  const ids = [...rows.keys()];
  const scrollAtEnd = (
    state.scrollHeight > 0
    && state.scrollTop + state.clientHeight >= state.scrollHeight - 2
  );
  return {
    view,
    ids,
    rows: [...rows.values()],
    declaredSize,
    terminalStagnantScans: stagnant,
    scrollAtEnd,
    scrollTop: state.scrollTop,
    scrollHeight: state.scrollHeight,
    clientHeight: state.clientHeight,
    complete: (
      scrollAtEnd
      && stagnant >= 3
      && declaredSize > 0
      && ids.length === declaredSize
    ),
  };
}

export async function scanOutlookInbox(tab, options = {}) {
  const folderNodes = await tab.playwright.evaluate(() => (
    [...document.querySelectorAll("[title],[aria-label]")]
      .filter((element) => /Inbox/i.test(
        `${element.getAttribute("title") || ""} ${element.getAttribute("aria-label") || ""}`,
      ))
      .map((element) => ({
        title: element.getAttribute("title"),
        ariaLabel: element.getAttribute("aria-label"),
      }))
  ));
  const folderItemCount = parseFolderItemCount(folderNodes, "Inbox");
  const focused = await scanOutlookView(tab, "Focused", options);
  const other = await scanOutlookView(tab, "Other", options);
  return { folderItemCount, ...combineViewEvidence([focused, other]) };
}

export async function scanOutlookSent(
  tab,
  {
    windowStart,
    capturedAt = new Date().toISOString(),
    maxScrolls = 20,
    renderDelayMs = 250,
  } = {},
) {
  if (!windowStart) throw new Error("scanOutlookSent requires windowStart");
  await waitForRenderedRows(tab, renderDelayMs);
  let state = await inspectView(tab);

  for (let attempt = 0; state.scrollTop > 1 && attempt < maxScrolls; attempt += 1) {
    state = await scroll(
      tab,
      state,
      -Math.max(320, Math.floor(state.clientHeight * 0.9)),
      renderDelayMs,
    );
  }
  if (state.scrollTop > 1) throw new Error("Outlook Sent list did not return to top");

  const rows = new Map();
  let stagnant = 0;
  let scrolls = 0;
  while (scrolls <= maxScrolls) {
    const sizeBefore = rows.size;
    for (const row of state.rows) {
      if (!row.itemId || !row.timestamp) {
        throw new Error("Outlook Sent row lacks a native item id or timestamp");
      }
      if (!rows.has(row.itemId)) rows.set(row.itemId, row);
    }

    const atEnd = (
      state.scrollHeight > 0
      && state.scrollTop + state.clientHeight >= state.scrollHeight - 2
    );
    if (atEnd && rows.size === sizeBefore) stagnant += 1;
    else if (!atEnd) stagnant = 0;

    const proof = buildSentZeroSendProof([...rows.values()], {
      windowStart,
      capturedAt,
      atListEnd: atEnd && stagnant >= 3,
    });
    if (proof.complete || scrolls === maxScrolls) {
      return {
        ...proof,
        scrolls,
        terminalStagnantScans: stagnant,
        scrollAtEnd: atEnd,
      };
    }

    const previousTop = state.scrollTop;
    state = await scroll(
      tab,
      state,
      Math.max(320, Math.floor(state.clientHeight * 0.9)),
      renderDelayMs,
    );
    scrolls += 1;
    if (state.scrollTop !== previousTop) stagnant = 0;
  }

  throw new Error("Outlook Sent scan exceeded its bounded scroll budget");
}
