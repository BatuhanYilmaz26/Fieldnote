import { createPublishingController } from "./publishing.js?handoff=1";
import { migratePreferences } from "./preferences.js";
import { createFormValidation } from "./validation.js";
let publishing;
let briefValidation;
// Fieldnote browser client.
// Security boundary: untrusted Markdown is rendered with DOM text nodes only.
// Raw HTML, images, remote assets and authored links are never inserted.

const $ = (id) => document.getElementById(id);
const TERMINAL = new Set(["succeeded", "needs_review", "failed", "cancelled"]);
const STATE_LABELS = {
  queued: "Queued",
  running: "Writing",
  retry_wait: "Waiting to retry",
  cancel_requested: "Cancelling",
  succeeded: "Generated",
  needs_review: "Needs review",
  failed: "Could not finish",
  cancelled: "Cancelled",
};
const LANGUAGE_LABELS = { en: "English", tr: "Türkçe", es: "Español", pt: "Português", de: "Deutsch" };
const EVENT_NAMES = [
  "queued", "running", "policy_ready", "research_completed", "source_added",
  "plan_ready", "section_completed", "review_completed", "retry_wait",
  "cancel_requested", "article_edited", "completed", "failed", "cancelled",
];
const EVENT_LABELS = {
  queued: "Queued",
  running: "Writing",
  policy_ready: "Research decision recorded",
  research_completed: "Research complete",
  source_added: "Source recorded",
  plan_ready: "Plan ready",
  section_completed: "Section drafted",
  review_completed: "Review complete",
  retry_wait: "Waiting to retry",
  cancel_requested: "Cancelling",
  article_edited: "Revision saved",
  completed: "Draft complete",
  failed: "Draft stopped",
  cancelled: "Cancelled",
};

const state = {
  csrf: "",
  activeRun: null,
  run: null,
  article: null,
  revisions: [],
  ledger: new Map(),
  stream: null,
  reconnectTimer: null,
  reconnectAttempts: 0,
  pollTimer: null,
  refreshTimer: null,
  lastEventId: 0,
  nextCursor: null,
  pendingKey: null,
  pendingPayload: null,
  editorDirty: false,
  publicationDirty: false,
  conflictText: null,
  loadingArticle: false,
  saving: false,
  events: new Set(),
  historyMarked: null,
  history: [],
  view: "compose",
  articleTicket: 0,
  historyTicket: 0,
  evidenceLoading: false,
  reviewTimer: null,
  reviewSubmitting: false,
  reviewKey: null,
};

class ApiError extends Error {
  constructor(message, status, code, correlationId, details) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.correlationId = correlationId;
    this.details = details;
  }
}

function announce(message) {
  const live = $("live");
  live.textContent = "";
  window.setTimeout(() => { live.textContent = message; }, 20);
}

function setConnection(text, tone = "") {
  const connection = $("connection");
  connection.textContent = text;
  if (tone) connection.dataset.tone = tone;
  else delete connection.dataset.tone;
}

const THEME_KEY = "fieldnote.theme";

function applyTheme(theme) {
  const dark = theme === "dark";
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  const toggle = $("theme-toggle");
  if (toggle) {
    toggle.setAttribute("aria-pressed", String(dark));
    $("theme-label").textContent = dark ? "Dark" : "Light";
  }
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", dark ? "#151b23" : "#f5f7f9");
}

function loadStoredTheme() {
  let stored = null;
  try { stored = localStorage.getItem(THEME_KEY); } catch { stored = null; }
  if (stored !== "light" && stored !== "dark") {
    stored = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches
      ? "dark"
      : "light";
  }
  applyTheme(stored);
}

function toggleTheme() {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  try { localStorage.setItem(THEME_KEY, next); } catch { /* storage unavailable */ }
  applyTheme(next);
  announce(`${next === "dark" ? "Dark" : "Light"} theme applied.`);
}

function closeNavigation(returnFocus = true) {
  const wasOpen = document.body.dataset.navigation === "open";
  delete document.body.dataset.navigation;
  document.querySelector(".app-stage").inert = false;
  $("navigation-scrim").hidden = true;
  $("sidebar").removeAttribute("role");
  $("sidebar").removeAttribute("aria-modal");
  $("menu-toggle").setAttribute("aria-expanded", "false");
  if (wasOpen && returnFocus) $("menu-toggle").focus();
}

function openNavigation() {
  setFocusMode(false);
  document.body.dataset.navigation = "open";
  $("sidebar").setAttribute("role", "dialog");
  $("sidebar").setAttribute("aria-modal", "true");
  $("menu-toggle").setAttribute("aria-expanded", "true");
  $("navigation-scrim").hidden = false;
  document.querySelector(".app-stage").inert = true;
  // Move focus after the visibility change, including when background tabs
  // suspend animation frames. Making the old focus inert must not strand it.
  window.setTimeout(() => {
    if (document.body.dataset.navigation === "open") $("navigation-close").focus();
  }, 0);
}

function setFocusMode(enabled) {
  document.body.dataset.focus = String(enabled);
  $("focus-toggle").setAttribute("aria-pressed", String(enabled));
  $("focus-toggle").textContent = enabled ? "Exit focus mode" : "Focus mode";
}

function setDraftTask(task) {
  state.draftTask = task;
  const publication = task === "publish" && Boolean(state.article);
  document.body.dataset.task = publication ? "publish" : "write";
  $("writing-surface").hidden = publication;
  $("publishing-surface").hidden = !publication;
  $("writing-task").setAttribute("aria-pressed", String(!publication));
  $("publishing-task").setAttribute("aria-pressed", String(publication));
}

function showWorkspace(view, focus = false) {
  state.view = view;
  document.body.dataset.workspace = view;
  for (const name of ["compose", "library", "draft"]) {
    $(name === "compose" ? "composer-view" : `${name}-view`).hidden = name !== view;
  }
  $("workspace-label").textContent = view === "library" ? "Draft library" : "Writing studio";
  $("nav-studio").toggleAttribute("aria-current", view !== "library");
  $("nav-library").toggleAttribute("aria-current", view === "library");
  if (view === "library") $("nav-library").setAttribute("aria-current", "page");
  else $("nav-studio").setAttribute("aria-current", "page");
  if (view !== "draft") setFocusMode(false);
  closeNavigation();
  document.title = view === "draft" && state.run
    ? `${state.run.request.topic} — Fieldnote`
    : `${view === "library" ? "Draft library" : "Writing studio"} — Fieldnote`;
  if (focus) {
    const target = view === "compose" ? "topic" : view === "library" ? "draft-search" : "draft-title";
    $(target).focus();
  }
}

async function confirmDiscard() {
  if (!state.editorDirty && !state.publicationDirty) return true;
  const dialog = $("leave-dialog");
  dialog.returnValue = "stay";
  return new Promise((resolve) => {
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "leave"), { once: true });
    dialog.showModal();
  });
}

async function openComposer(fresh = false) {
  if (!await confirmDiscard()) return;
  stopObserving();
  state.activeRun = null;
  state.run = null;
  state.article = null;
  state.articleTicket += 1;
  state.loadingArticle = false;
  state.editorDirty = false;
  state.publicationDirty = false;
  state.conflictText = null;
  localStorage.removeItem("fieldnote.run");
  if (fresh) {
    $("brief-form").reset();
    $("brief-extra").open = false;
    state.pendingKey = null;
    state.pendingPayload = null;
  }
  $("empty").hidden = false;
  clearError();
  updateBriefDetails();
  renderHistory();
  showWorkspace("compose", true);
}

function updateBriefDetails() {
  $("topic-count").textContent = `${$("topic").value.length} / 500`;
  const words = Number($("target_words").value);
  for (const button of document.querySelectorAll("[data-words]")) {
    button.setAttribute("aria-pressed", String(Number(button.dataset.words) === words));
  }
  researchWarning();
}

function statusTag(run) {
  const tag = document.createElement("span");
  tag.className = "pill";
  tag.dataset.state = run.state;
  tag.textContent = STATE_LABELS[run.state] || run.state;
  return tag;
}

function documentIcon() {
  const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  icon.setAttribute("viewBox", "0 0 24 24");
  icon.setAttribute("class", "icon");
  icon.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", "/assets/icons.svg#document");
  icon.append(use);
  return icon;
}

function renderHistory() {
  const history = $("history");
  history.replaceChildren();
  $("library-count").textContent = `${state.history.length}${state.nextCursor ? "+" : ""}`;
  if (!state.history.length) {
    const empty = document.createElement("li");
    empty.className = "history-placeholder";
    empty.textContent = "Your next idea has a home here. Start a brief to save your first draft.";
    history.append(empty);
  }
  for (const run of state.history.slice(0, 6)) {
    const item = document.createElement("li");
    if (run.id === state.activeRun) item.setAttribute("aria-current", "true");
    const button = document.createElement("button");
    button.type = "button";
    button.className = "history-item";
    const topic = document.createElement("span");
    topic.className = "history-topic";
    topic.textContent = run.request.topic;
    const meta = document.createElement("span");
    meta.className = "history-meta";
    const status = document.createElement("span");
    status.className = "history-state";
    status.dataset.state = run.state;
    status.textContent = STATE_LABELS[run.state] || run.state;
    const time = document.createElement("span");
    time.textContent = relativeTime(run.accepted_at);
    meta.append(status, time);
    button.append(topic, meta);
    button.addEventListener("click", () => selectRun(run.id).catch(showError));
    item.append(button);
    history.append(item);
  }
  renderLibrary();
}

function renderLibrary() {
  const query = $("draft-search").value.trim().toLocaleLowerCase();
  const filter = $("draft-filter").value;
  const rows = state.history.filter((run) => {
    const statusMatches = filter === "all" || (filter === "running" ? !TERMINAL.has(run.state) : run.state === filter);
    return statusMatches && run.request.topic.toLocaleLowerCase().includes(query);
  });
  $("library-summary").textContent = state.history.length
    ? `${rows.length} of ${state.history.length} recent draft${state.history.length === 1 ? "" : "s"}${state.nextCursor ? " · More saved drafts are available below." : ""}`
    : "Every saved draft will be here, ready to reopen.";
  const list = $("library-list");
  list.replaceChildren();
  if (!rows.length) {
    const item = document.createElement("li");
    item.className = "library-empty";
    const title = document.createElement("strong");
    title.textContent = state.history.length ? "No matching drafts." : "No saved drafts yet.";
    const detail = document.createElement("p");
    detail.textContent = state.history.length ? "Try another phrase or choose All statuses." : "Create your first draft. Your sources, progress and revisions will stay together.";
    item.append(title, detail);
    list.append(item);
  }
  for (const run of rows) {
    const item = document.createElement("li");
    const card = document.createElement("button");
    card.type = "button";
    card.className = "library-card";
    const head = document.createElement("span");
    head.className = "library-card-head";
    const mark = document.createElement("span");
    mark.className = "library-file";
    mark.append(documentIcon());
    mark.setAttribute("aria-hidden", "true");
    head.append(mark, statusTag(run));
    const title = document.createElement("span");
    title.className = "library-card-title";
    title.textContent = run.request.topic;
    const meta = document.createElement("span");
    meta.className = "library-card-meta";
    const length = document.createElement("span");
    length.textContent = `${LANGUAGE_LABELS[run.request.language || "en"]} · Target ${Number(run.request.target_words || 0).toLocaleString()} words`;
    const date = document.createElement("span");
    date.textContent = relativeTime(run.accepted_at);
    meta.append(length, date);
    card.append(head, title, meta);
    card.addEventListener("click", () => selectRun(run.id).catch(showError));
    item.append(card);
    list.append(item);
  }
  $("more").hidden = !state.nextCursor;
}

function relativeTime(value) {
  const time = new Date(value).getTime();
  if (!Number.isFinite(time)) return "";
  const minutes = Math.round((Date.now() - time) / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days} d ago`;
  return new Date(value).toLocaleDateString();
}

async function copyHash() {
  const text = $("hash").textContent.trim();
  if (!text) {
    announce("No revision hash is shown yet.");
    return;
  }
  const hash = text.replace(/^SHA-256\s+/, "");
  try {
    await navigator.clipboard.writeText(hash);
    announce("Revision hash copied to the clipboard.");
  } catch {
    announce(`Revision hash: ${hash}`);
  }
}

function clearError() {
  const error = $("error");
  error.hidden = true;
  error.textContent = "";
}

function showError(error) {
  const message = error instanceof ApiError
    ? error.message
    : "The service could not be reached. Your saved work is safe; try again.";
  const reference = error instanceof ApiError && error.correlationId
    ? ` Reference: ${error.correlationId}.`
    : "";
  const box = $("error");
  box.textContent = `${message}${reference}`;
  box.hidden = false;
  box.focus();
  announce(message);
}

function humanMessage(status, payload) {
  if (payload && typeof payload.message === "string" && payload.message.trim()) {
    return payload.message.trim();
  }
  if (status === 401) return "Your session expired. Reload the page to start a new local session.";
  if (status === 403) return "Reload the page before trying this action.";
  if (status === 404) return "That draft is no longer available.";
  if (status === 410) return "This draft was deleted. Start a new one.";
  if (status === 413) return "The brief is too large. Shorten it and try again.";
  if (status === 429) return "You have reached the current run limit. Wait for a draft to finish or delete one.";
  if (status === 503) return "The service is temporarily unavailable. Your saved work is safe.";
  return "The request could not be completed. Check the highlighted fields.";
}

async function api(path, options = {}) {
  const method = options.method || "GET";
  const headers = { Accept: "application/json", ...(options.headers || {}) };
  if (state.csrf && !["GET", "HEAD", "OPTIONS"].includes(method)) {
    headers["X-CSRF-Token"] = state.csrf;
  }
  const init = { method, headers, credentials: "same-origin", signal: options.signal };
  if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(options.body);
  }
  const response = await fetch(path, init);
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    const detail = Array.isArray(payload.details) && payload.details.length
      ? ` ${payload.details.map((item) => item.message || item.type).filter(Boolean).join("; ")}`
      : "";
    throw new ApiError(
      humanMessage(response.status, payload) + detail,
      response.status,
      payload.code,
      payload.correlation_id,
      payload.details,
    );
  }
  return response.status === 204 ? null : response.json();
}

function safeExternalUrl(value) {
  try {
    const url = new URL(value, window.location.origin);
    if (!["http:", "https:"].includes(url.protocol)) return null;
    if (url.username || url.password) return null;
    return url.href;
  } catch {
    return null;
  }
}

function appendInline(node, text) {
  const tokens = /(`[^`\n]+`|\*\*[^*\n]+\*\*|(?<!\w)\*[^*\n]+\*|\[@[A-Za-z][A-Za-z0-9_.:-]{0,79}\]|\[[^\[\]\n]{1,240}\]\(<?https?:\/\/[^)\s]{1,2048}>?\))/g;
  for (const part of String(text).split(tokens)) {
    let tag = null;
    let content = part;
    if (/^`[^`]+`$/.test(part)) { tag = "code"; content = part.slice(1, -1); }
    else if (/^\*\*[^*]+\*\*$/.test(part)) { tag = "strong"; content = part.slice(2, -2); }
    else if (/^\*[^*]+\*$/.test(part)) { tag = "em"; content = part.slice(1, -1); }
    if (tag) {
      const element = document.createElement(tag); element.textContent = content; node.append(element); continue;
    }
    const marker = part.match(/^\[@(.+)\]$/);
    const reference = part.match(/^\[([^\]]+)\]\(<?(https?:\/\/[^)>]+)>?\)$/);
    const source = marker ? state.ledger.get(marker[1]) : reference
      ? [...state.ledger.values()].find((s) => safeExternalUrl(s.url) === safeExternalUrl(reference[2])) : null;
    const href = source && safeExternalUrl(source.url);
    if (href) {
      const link = document.createElement("a"); link.href = href;
      link.textContent = reference ? reference[1] : `[${source.title || source.url}]`;
      link.target = "_blank"; link.rel = "noopener noreferrer"; node.append(link);
    } else node.append(document.createTextNode(part));
  }
}

// A deliberately small Markdown renderer. It never interprets HTML and never
// creates links except validated source citations from the stored ledger.
export function renderMarkdown(root, text) {
  root.replaceChildren();
  const lines = String(text).replace(/\r\n/g, "\n").split("\n");
  let index = 0;
  let paragraph = null;
  let list = null;
  let listTag = null;
  let code = null;
  let codeLines = [];

  const flushParagraph = () => {
    if (paragraph) {
      root.append(paragraph);
      paragraph = null;
    }
  };
  const flushList = () => {
    if (list) {
      root.append(list);
      list = null;
      listTag = null;
    }
  };

  while (index < lines.length) {
    const line = lines[index];
    if (line.startsWith("```")) {
      flushParagraph();
      flushList();
      if (code) {
        const pre = document.createElement("pre");
        const codeNode = document.createElement("code");
        codeNode.append(document.createTextNode(codeLines.join("\n")));
        pre.append(codeNode);
        root.append(pre);
        code = null;
        codeLines = [];
      } else {
        code = true;
      }
      index += 1;
      continue;
    }
    if (code) {
      codeLines.push(line);
      index += 1;
      continue;
    }
    const cells = (value) => value.trim().replace(/^\||\|$/g, "").split("|").map((s) => s.trim());
    const header = cells(line);
    const separator = index + 1 < lines.length ? cells(lines[index + 1]) : [];
    if (line.trim().startsWith("|") && header.length >= 2 && header.length <= 12
        && separator.length === header.length && separator.every((s) => /^:?-{3,}:?$/.test(s))) {
      flushParagraph(); flushList();
      const wrap = document.createElement("div"); wrap.className = "article-table";
      wrap.tabIndex = 0; wrap.setAttribute("role", "region"); wrap.setAttribute("aria-label", "Article comparison table");
      const table = document.createElement("table");
      const head = document.createElement("thead"); const row = document.createElement("tr");
      for (const cell of header) { const th = document.createElement("th"); th.scope = "col"; appendInline(th, cell); row.append(th); }
      head.append(row); table.append(head);
      const body = document.createElement("tbody"); index += 2;
      while (index < lines.length && lines[index].trim().startsWith("|") && cells(lines[index]).length === header.length) {
        const row = document.createElement("tr");
        for (const cell of cells(lines[index])) { const td = document.createElement("td"); appendInline(td, cell); row.append(td); }
        body.append(row); index += 1;
      }
      table.append(body); wrap.append(table); root.append(wrap); continue;
    }
    if (!line.trim()) {
      flushParagraph();
      flushList();
      index += 1;
      continue;
    }
    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      flushParagraph();
      flushList();
      const element = document.createElement(`h${heading[1].length}`);
      appendInline(element, heading[2]);
      root.append(element);
      index += 1;
      continue;
    }
    const quote = line.match(/^>\s?(.*)$/);
    if (quote) {
      flushParagraph();
      flushList();
      const blockquote = document.createElement("blockquote");
      const p = document.createElement("p");
      appendInline(p, quote[1]);
      blockquote.append(p);
      root.append(blockquote);
      index += 1;
      continue;
    }
    const bullet = line.match(/^\s*[-*]\s+(.*)$/);
    const ordered = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (bullet || ordered) {
      flushParagraph();
      const tag = bullet ? "ul" : "ol";
      if (!list || listTag !== tag) {
        flushList();
        list = document.createElement(tag);
        listTag = tag;
      }
      const item = document.createElement("li");
      appendInline(item, (bullet || ordered)[1]);
      list.append(item);
      index += 1;
      continue;
    }
    if (/^\s*---+\s*$/.test(line)) {
      flushParagraph();
      flushList();
      root.append(document.createElement("hr"));
      index += 1;
      continue;
    }
    if (!paragraph) paragraph = document.createElement("p");
    else paragraph.append(document.createTextNode(" "));
    appendInline(paragraph, line);
    index += 1;
  }
  flushParagraph();
  flushList();
  if (code) {
    const pre = document.createElement("pre");
    const codeNode = document.createElement("code");
    codeNode.append(document.createTextNode(codeLines.join("\n")));
    pre.append(codeNode);
    root.append(pre);
  }
}

function stopObserving() {
  window.clearTimeout(state.reviewTimer);
  state.reviewTimer = null;
  if (state.stream) {
    state.stream.close();
    state.stream = null;
  }
  window.clearTimeout(state.reconnectTimer);
  state.reconnectTimer = null;
  window.clearInterval(state.pollTimer);
  state.pollTimer = null;
  window.clearTimeout(state.refreshTimer);
  state.refreshTimer = null;
}

function scheduleRefresh() {
  if (state.refreshTimer) return;
  state.refreshTimer = window.setTimeout(() => {
    state.refreshTimer = null;
    refreshRun().catch(showError);
  }, 250);
}

function connectStream() {
  if (!state.activeRun || !state.run || TERMINAL.has(state.run.state)) return;
  if (state.stream) state.stream.close();
  const requestedRun = state.activeRun;
  const cursor = state.lastEventId ? `?after=${encodeURIComponent(state.lastEventId)}` : "";
  const stream = new EventSource(`/api/v1/runs/${state.activeRun}/events${cursor}`);
  state.stream = stream;
  stream.onopen = () => {
    if (state.stream !== stream || requestedRun !== state.activeRun) return;
    state.reconnectAttempts = 0;
    setConnection("Live progress", "live");
  };
  stream.onerror = () => {
    if (state.stream !== stream || requestedRun !== state.activeRun) return;
    if (!state.activeRun || !state.run || TERMINAL.has(state.run.state)) {
      stream.close();
      return;
    }
    stream.close();
    state.stream = null;
    setConnection("Reconnecting…", "offline");
    const delay = Math.min(10000, 1000 * (2 ** state.reconnectAttempts));
    state.reconnectAttempts += 1;
    state.reconnectTimer = window.setTimeout(() => {
      refreshRun().then(() => connectStream()).catch(() => {
        state.reconnectTimer = window.setTimeout(connectStream, 5000);
      });
    }, delay);
  };
  const onEvent = (event) => {
    if (state.stream !== stream || requestedRun !== state.activeRun) return;
    let payload = {};
    try { payload = JSON.parse(event.data); } catch { payload = {}; }
    const sequence = Number(event.lastEventId || payload.sequence || 0);
    if (Number.isFinite(sequence) && sequence > state.lastEventId) state.lastEventId = sequence;
    const label = EVENT_LABELS[event.type] || event.type.replaceAll("_", " ");
    state.events.add(event.type);
    renderPipeline();
    const progress = $("progress");
    if (progress) progress.textContent = label;
    announce(label);
    scheduleRefresh();
  };
  for (const name of EVENT_NAMES) stream.addEventListener(name, onEvent);
  stream.addEventListener("stream_error", () => {
    if (state.stream !== stream || requestedRun !== state.activeRun) return;
    stopObserving();
    setConnection("Reloading progress…", "offline");
    refreshRun().catch(showError);
  });
}

function formatProgress(run) {
  const progress = run && run.progress ? run.progress : {};
  const parts = [];
  if (Number.isInteger(progress.sections) && Number.isInteger(progress.tasks) && progress.tasks > 0) {
    parts.push(`${progress.sections}/${progress.tasks} sections`);
  }
  if (Number.isInteger(progress.sources)) parts.push(`${progress.sources} source${progress.sources === 1 ? "" : "s"}`);
  if (Number.isInteger(progress.unsupported_claims) && progress.unsupported_claims > 0) {
    parts.push(`${progress.unsupported_claims} claims need attention`);
  }
  return parts.length ? parts.join(" · ") : "Waiting for the first durable update.";
}

const PIPELINE = [
  ["accepted", "Brief accepted"],
  ["research", "Research"],
  ["plan", "Plan"],
  ["sections", "Sections"],
  ["review", "Review"],
];

function pipelineReached(run) {
  // Deepest stage this run is known to have reached, inferred from live
  // stream events (state.events) plus the durable progress on the run.
  const seen = state.events;
  const progress = run && run.progress ? run.progress : {};
  const sources = Number(progress.sources) || 0;
  const sections = Number(progress.sections) || 0;
  let reached = 0;
  if (run.state === "running" || seen.has("policy_ready")
    || seen.has("research_completed") || seen.has("source_added") || sources > 0) reached = 1;
  if (seen.has("plan_ready")) reached = 2;
  if (seen.has("section_completed") || sections > 0) reached = Math.max(reached, 3);
  if (seen.has("review_completed") || run.state === "needs_review" || run.state === "succeeded") reached = 4;
  return reached;
}

function renderPipeline() {
  const pipeline = $("pipeline");
  const run = state.run;
  pipeline.replaceChildren();
  if (!run) return;
  const reached = pipelineReached(run);
  const halted = run.state === "failed" || run.state === "cancelled";
  const completed = run.state === "succeeded";
  const progress = run.progress || {};
  const sources = Number(progress.sources) || 0;
  const suffixes = {
    research: sources > 0 ? `${sources} source${sources === 1 ? "" : "s"}` : "",
    sections: Number.isInteger(progress.tasks) && progress.tasks > 0
      ? `${progress.sections}/${progress.tasks}`
      : "",
    review: Number(progress.unsupported_claims) > 0
      ? `${progress.unsupported_claims} to check`
      : "",
  };
  for (const [index, [stageId, label]] of PIPELINE.entries()) {
    const item = document.createElement("li");
    item.dataset.stage = stageId;
    if (index < reached || completed) item.dataset.status = "done";
    else if (index === reached) item.dataset.status = halted ? "halted" : "active";
    else item.dataset.status = "pending";
    const mark = document.createElement("span");
    mark.className = "stage-mark";
    mark.setAttribute("aria-hidden", "true");
    const name = document.createElement("span");
    name.className = "stage-name";
    name.textContent = label;
    item.append(mark, name);
    const value = suffixes[stageId];
    if (value) {
      const chip = document.createElement("span");
      chip.className = "stage-value";
      chip.textContent = value;
      item.append(chip);
    }
    if (index === reached && !halted && !completed) item.setAttribute("aria-current", "step");
    pipeline.append(item);
  }
}

function renderRun(run) {
  const previousRevision = state.run && state.run.current_revision;
  state.run = run;
  const language = run.request.language || "en";
  $("preview").lang = language;
  $("editor").lang = language;
  $("draft-title").textContent = run.request.topic;
  if (state.view === "draft") document.title = `${run.request.topic} — Fieldnote`;
  const statePill = $("state");
  statePill.textContent = STATE_LABELS[run.state] || run.state;
  statePill.dataset.state = run.state;
  $("progress").textContent = run.error_message || formatProgress(run);
  const finished = TERMINAL.has(run.state);
  $("draft-loading").dataset.finished = String(finished);
  $("loading-title").textContent = run.current_revision ? "Opening your saved draft" : finished ? STATE_LABELS[run.state] || "Run finished" : "Working on your draft";
  $("loading-note").textContent = run.current_revision
    ? "Loading its exact revision, sources and review."
    : finished
    ? run.error_message || "This run ended without an article revision. You can edit the brief and start a new draft."
    : "Each step is saved as it happens. You can come back any time.";
  $("retry-brief").hidden = !finished || Boolean(run.current_revision);
  $("revision").disabled = !run.current_revision;
  $("copy-hash").disabled = !run.current_revision;
  if (Number(run.progress && run.progress.sources) > state.ledger.size) refreshEvidence().catch(() => {});
  renderPipeline();
  $("cancel").hidden = TERMINAL.has(run.state);
  $("cancel").disabled = run.state === "cancel_requested";
  $("cancel").textContent = run.state === "cancel_requested" ? "Cancelling…" : "Cancel run";

  const limitations = Array.isArray(run.research_limitations) ? run.research_limitations : [];
  $("limitations-panel").hidden = limitations.length === 0;
  const list = $("limitations");
  list.replaceChildren();
  for (const limitation of limitations) {
    const item = document.createElement("li");
    item.textContent = String(limitation);
    list.append(item);
  }

  if (TERMINAL.has(run.state)) {
    stopObserving();
    setConnection("Saved", "live");
    $("run-note").textContent = "This run is saved. Reopen it from Recent drafts at any time.";
    if (state.historyMarked !== run.id) {
      state.historyMarked = run.id;
      refreshHistory().catch(() => {});
    }
    if (run.current_revision && !state.editorDirty && (!state.article
        || (previousRevision !== run.current_revision && state.article.id !== run.current_revision))) {
      loadArticle().catch(showError);
    }
  } else {
    $("run-note").textContent = "Your run continues even if you close this page. Reopen it from Recent drafts.";
    if (!state.stream) connectStream();
    if (!state.pollTimer) {
      state.pollTimer = window.setInterval(() => {
        refreshRun().catch(showError);
      }, 5000);
    }
  }
  announce(`${STATE_LABELS[run.state] || run.state}. ${formatProgress(run)}`);
  scheduleReviewPoll();
}

async function refreshRun() {
  if (!state.activeRun) return null;
  const requestedRun = state.activeRun;
  const run = await api(`/api/v1/runs/${requestedRun}`);
  if (requestedRun !== state.activeRun) return null;
  renderRun(run);
  return run;
}

async function refreshEvidence() {
  if (!state.activeRun || state.evidenceLoading) return;
  const requestedRun = state.activeRun;
  state.evidenceLoading = true;
  try {
    const data = await api(`/api/v1/runs/${requestedRun}/sources?limit=100`);
    if (requestedRun !== state.activeRun) return;
    state.ledger = new Map(data.sources.map((source) => [source.id, source]));
    renderSources(data.sources);
  } finally {
    state.evidenceLoading = false;
  }
}

async function refreshHistory(append = false) {
  const ticket = ++state.historyTicket;
  const before = append && state.nextCursor
    ? `&before=${encodeURIComponent(state.nextCursor)}`
    : "";
  const data = await api(`/api/v1/runs?limit=20${before}`);
  if (ticket !== state.historyTicket) return;
  const combined = append ? [...state.history, ...data.runs] : data.runs;
  state.history = [...new Map(combined.map((run) => [run.id, run])).values()];
  state.nextCursor = data.next_cursor;
  renderHistory();
}

function renderReview(article) {
  publishing?.render();
  $("review-panel").hidden = false;
  const status = $("review-status");
  const messages = {
    approved: "This saved revision passed its configured checks. Review the evidence before publishing.",
    needs_review: "This draft needs your review. Check its evidence, limitations and claim support.",
    failed: "Generation stopped before approval. A partial draft is retained for review only.",
    unassessed: "This human-edited revision has not been assessed. Earlier approval does not carry over.",
  };
  const assessment = article.assessment && article.assessment.revision_id === article.id
    && article.assessment.content_hash === article.content_hash ? article.assessment : null;
  const reviewStatus = assessment ? assessment.review_status : article.review_status;
  status.textContent = messages[reviewStatus] || messages.needs_review;
  status.dataset.status = reviewStatus || "needs_review";

  const issues = $("issues");
  issues.replaceChildren();
  const reviewedIssues = assessment ? assessment.review.issues : article.issues;
  const findings = Array.isArray(reviewedIssues) ? reviewedIssues : [];
  $("issue-count").textContent = String(findings.length);
  for (const issue of findings) {
    const item = document.createElement("li");
    item.dataset.severity = issue.severity || "info";
    const code = document.createElement("strong");
    code.textContent = String(issue.code || "review_finding").replaceAll("_", " ");
    const explanation = document.createElement("p");
    explanation.textContent = issue.explanation || "Review this finding.";
    item.append(code, explanation);
    if (issue.action) {
      const action = document.createElement("small");
      action.textContent = issue.action;
      item.append(action);
    }
    issues.append(item);
  }
  if (!findings.length) {
    const item = document.createElement("li");
    item.className = "help";
    item.textContent = "No editorial findings were recorded for this revision.";
    issues.append(item);
  }

  const claims = $("claims");
  claims.replaceChildren();
  const assessments = Array.isArray(article.claims) ? article.claims : [];
  $("claim-count").textContent = String(assessments.length);
  for (const claim of assessments) {
    const item = document.createElement("li");
    item.dataset.assessment = claim.assessment || "unassessed";
    const text = document.createElement("p");
    text.className = "claim-text";
    text.textContent = claim.claim_text || "Cited sentence";
    const detail = document.createElement("small");
    detail.textContent = `${claim.assessment || "unassessed"} · ${claim.reason || "no reason recorded"} · ${claim.claim_locator || "locator unavailable"}`;
    item.append(text, detail);
    if (Array.isArray(claim.excerpt_locators) && claim.excerpt_locators.length) {
      const citations = document.createElement("p");
      citations.className = "claim-citations";
      citations.textContent = `Evidence: ${claim.excerpt_locators.join("; ")}`;
      item.append(citations);
    }
    claims.append(item);
  }
  if (!assessments.length) {
    const item = document.createElement("li");
    item.className = "help";
    item.textContent = "No cited factual sentences were assessed for this revision.";
    claims.append(item);
  }
  updateReviewControls();
  scheduleReviewPoll();
  if (assessment) {
    const known = state.revisions.find((item) => item.id === article.id);
    if (known && known.assessment_status !== assessment.review_status) {
      known.assessment_status = assessment.review_status;
      renderRevisionOptions();
    }
  }
}

function updateReviewControls() {
  const article = state.article;
  const human = article && article.author_type === "human";
  $("revision-review-tools").hidden = !human;
  if (!human) return;
  const job = article.assessment_job;
  const pending = job && !TERMINAL.has(job.state);
  const latest = state.revisions.length && state.revisions[0].id === article.id;
  const button = $("request-review");
  button.disabled = state.reviewSubmitting || state.saving || state.editorDirty || !latest || pending;
  button.setAttribute("aria-busy", String(state.reviewSubmitting));
  button.textContent = state.reviewSubmitting ? "Queuing review…" : pending ? "Review in progress…"
    : article.assessment ? "Review again" : "Review saved revision";
  button.title = state.editorDirty ? "Save your changes before requesting a review."
    : !latest ? "Select the latest saved revision to request a review." : "";
  $("cancel-review").hidden = !pending;
  $("cancel-review").disabled = pending && job.state === "cancel_requested";
  $("cancel-review").textContent = job && job.state === "cancel_requested" ? "Cancelling…" : "Cancel review";
  const last = article.assessment
    ? ` Last assessment: ${new Date(article.assessment.created_at).toLocaleString()}.` : "";
  $("review-job").textContent = pending
    ? `Saved revision review: ${STATE_LABELS[job.state] || job.state}.${last}`
    : job && (job.state === "failed" || job.state === "cancelled")
      ? `${job.error_message || (job.state === "cancelled" ? "Review cancelled." : "Review stopped.")}${last}`
      : article.assessment ? `Assessment saved for this exact revision.${last}`
        : "This saved edit has no fresh assessment yet.";
}

function scheduleReviewPoll() {
  window.clearTimeout(state.reviewTimer);
  state.reviewTimer = null;
  const job = state.article && state.article.assessment_job;
  if (job && !TERMINAL.has(job.state)) state.reviewTimer = window.setTimeout(refreshReviewJob, 3000);
}

async function refreshReviewJob() {
  if (!state.activeRun || !state.article) return;
  if (state.editorDirty) { scheduleReviewPoll(); return; }
  const run = state.activeRun;
  const revision = state.article.id;
  try {
    const article = await api(`/api/v1/runs/${run}/article?revision=${encodeURIComponent(revision)}`);
    if (run !== state.activeRun || !state.article || revision !== state.article.id) return;
    state.article = article;
    if (state.reviewKey && state.reviewKey.signature === `${run}:${revision}:${article.content_hash}`
        && article.assessment_job && article.assessment_job.id !== state.reviewKey.previousJobId) {
      state.reviewKey = null;
    }
    if (!state.editorDirty) renderReview(article);
    if (article.assessment_job && TERMINAL.has(article.assessment_job.state)) {
      await refreshRun();
      announce("Saved revision review finished. Read its assessment and findings before publishing.");
    }
  } catch (error) {
    if (run !== state.activeRun) return;
    showError(error);
    if (!(error instanceof ApiError && [401, 403, 404, 410].includes(error.status))) scheduleReviewPoll();
  }
}

async function requestRevisionReview() {
  if (!state.article || !state.activeRun || state.reviewSubmitting || state.editorDirty || state.saving) return;
  const run = state.activeRun;
  const revision = state.article.id;
  const hash = state.article.content_hash;
  const signature = `${run}:${revision}:${hash}`;
  if (!state.reviewKey || state.reviewKey.signature !== signature) {
    state.reviewKey = {
      signature, key: crypto.randomUUID(),
      previousJobId: state.article.assessment_job ? state.article.assessment_job.id : null,
    };
  }
  state.reviewSubmitting = true;
  updateReviewControls();
  clearError();
  try {
    const job = await api(`/api/v1/runs/${run}/revisions/${revision}/reviews`, {
      method: "POST", headers: { "Idempotency-Key": state.reviewKey.key }, body: { content_hash: hash },
    });
    state.reviewKey = null;
    if (run !== state.activeRun || !state.article || revision !== state.article.id) return;
    state.article.assessment_job = job;
    updateReviewControls();
    scheduleReviewPoll();
    if (TERMINAL.has(job.state)) await refreshReviewJob();
    announce("Review queued for this saved revision. Your text stays unchanged.");
  } catch (error) {
    if (run === state.activeRun) { showError(error); await refreshReviewJob(); }
  } finally {
    state.reviewSubmitting = false;
    updateReviewControls();
  }
}

async function cancelRevisionReview() {
  const article = state.article;
  if (!article || !article.assessment_job || TERMINAL.has(article.assessment_job.state)) return;
  const run = state.activeRun;
  try {
    const job = await api(`/api/v1/runs/${article.assessment_job.id}/cancel`, { method: "POST" });
    if (run !== state.activeRun || !state.article || state.article.id !== article.id) return;
    state.article.assessment_job = job;
    updateReviewControls();
    scheduleReviewPoll();
    if (TERMINAL.has(job.state)) await refreshReviewJob();
  } catch (error) { if (run === state.activeRun) showError(error); }
}

function renderSources(sources) {
  $("source-count").textContent = String(sources.length);
  const list = $("sources");
  list.replaceChildren();
  for (const source of sources) {
    const item = document.createElement("li");
    const href = safeExternalUrl(source.url);
    if (href) {
      const link = document.createElement("a");
      link.href = href;
      link.textContent = source.title || href;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      item.append(link);
    } else {
      const title = document.createElement("strong");
      title.textContent = source.title || "Source";
      item.append(title);
    }
    const meta = document.createElement("small");
    meta.textContent = `${source.source_kind || "Unclassified source"} · ${source.provider || "unknown"} · published ${source.published_at || "unknown"} · updated ${source.updated_at || "unknown"} · retrieved ${source.retrieved_at || "unknown"} · ${source.extraction_state || "stored"}`;
    item.append(meta);
    list.append(item);
  }
  if (!sources.length) {
    const item = document.createElement("li");
    item.className = "help";
    item.textContent = "No sources have been stored for this draft.";
    list.append(item);
  }
}

function setDownload(revisionId, contentHash) {
  const link = $("download");
  link.href = `/api/v1/runs/${state.activeRun}/revisions/${revisionId}/download`;
  link.setAttribute("download", `fieldnote-${revisionId}.md`);
  $("hash").textContent = contentHash ? `SHA-256 ${contentHash}` : "";
}

function setEditor(text) {
  $("editor").value = text;
  $("editor-count").textContent = `${text.length.toLocaleString()} characters · Markdown preserved exactly`;
  state.editorDirty = false;
}

function updateEditingControls() {
  const editing = !$("edit-panel").hidden;
  const latest = state.revisions.length ? state.revisions[0].id : null;
  const selected = state.article ? state.article.id : null;
  $("save").hidden = !editing;
  $("save").disabled = state.saving || !editing || !selected || selected !== latest;
  $("save").setAttribute("aria-busy", String(state.saving));
  $("save").textContent = state.saving ? "Saving…" : "Save new revision";
  $("editor").readOnly = state.saving;
  $("save").title = selected && latest && selected !== latest
    ? "Select the latest revision before saving an edit."
    : "";
  updateReviewControls();
  publishing?.update();
}

function renderRevisionOptions() {
  const select = $("revision");
  select.replaceChildren();
  for (const [index, revision] of state.revisions.entries()) {
    const option = document.createElement("option");
    option.value = revision.id;
    option.textContent = `${index === 0 ? "Latest · " : ""}${revision.author_type === "human" ? "Your edit" : "Generated"} · ${revision.assessment_status || revision.review_status} · ${new Date(revision.created_at).toLocaleString()}`;
    select.append(option);
  }
  if (state.article) select.value = state.article.id;
}

function renderArticle() {
  const article = state.article;
  $("draft-task-tools").hidden = false;
  setDraftTask(state.draftTask || "write");
  $("article-view").hidden = false;
  $("draft-loading").hidden = true;
  const wordCount = article.markdown.trim().split(/\s+/).filter(Boolean).length;
  $("reading-stats").textContent = `${wordCount.toLocaleString()} words · ${Math.max(1, Math.ceil(wordCount / 220))} min read`;
  const editable = article.citation_map && article.citation_map.editable_markdown
    ? article.citation_map.editable_markdown
    : article.markdown;
  if (!state.editorDirty) setEditor(editable);
  // Preview the immutable stored revision, including any synthetic/partial
  // disclosure that must never be hidden by the editable body.
  renderMarkdown($("preview"), article.markdown);
  renderDiscovery(article);
  renderReview(article);
  renderRevisionOptions();
  setDownload(article.id, article.content_hash);
  publishing?.render();
  updateEditingControls();
  $("conflict").hidden = state.conflictText === null;
  if (state.conflictText !== null) $("conflict-message").textContent = state.conflictText;
}

function renderDiscovery(article) {
  const discovery = article.discovery || {};
  $("article-headline").value = discovery.title || "";
  $("headline-options").replaceChildren(new Option("Choose a planning suggestion", ""));
  for (const title of discovery.headline_options || []) $("headline-options").append(new Option(title, title));
  $("headline-options").disabled = !(discovery.headline_options || []).length;
  $("discovery-notes").replaceChildren();
  for (const tip of [...(discovery.tips || []), discovery.reminder].filter(Boolean)) {
    const item = document.createElement("li"); item.textContent = tip; $("discovery-notes").append(item);
  }
}

async function loadArticle(revisionId = null) {
  if (!state.activeRun || state.loadingArticle) return;
  state.loadingArticle = true;
  const ticket = ++state.articleTicket;
  const requestedRun = state.activeRun;
  try {
    const query = revisionId ? `?revision=${encodeURIComponent(revisionId)}` : "";
    const [article, evidence, versions] = await Promise.all([
      api(`/api/v1/runs/${state.activeRun}/article${query}`),
      api(`/api/v1/runs/${state.activeRun}/sources?limit=100`),
      api(`/api/v1/runs/${state.activeRun}/revisions`),
    ]);
    if (ticket !== state.articleTicket || requestedRun !== state.activeRun) return;
    state.article = article;
    state.revisions = versions.revisions || [];
    state.ledger = new Map((evidence.sources || []).map((source) => [source.id, source]));
    renderSources(evidence.sources || []);
    renderArticle();
    $("empty").hidden = true;
    $("run-view").hidden = false;
  } finally {
    if (ticket === state.articleTicket) state.loadingArticle = false;
  }
}

async function selectRun(id, options = {}) {
  if (state.editorDirty || state.publicationDirty) {
    if (id === state.activeRun) { showWorkspace("draft", true); return; }
    if (!await confirmDiscard()) return;
  }
  stopObserving();
  clearError();
  state.activeRun = id;
  state.run = null;
  state.article = null;
  $("draft-task-tools").hidden = true;
  setDraftTask("write");
  state.revisions = [];
  state.ledger = new Map();
  state.lastEventId = 0;
  state.events.clear();
  state.reconnectAttempts = 0;
  state.editorDirty = false;
  state.publicationDirty = false;
  state.conflictText = null;
  state.historyMarked = null;
  state.articleTicket += 1;
  state.loadingArticle = false;
  showWorkspace("draft");
  $("draft-loading").hidden = false;
  localStorage.setItem("fieldnote.run", id);
  $("empty").hidden = true;
  $("run-view").hidden = false;
  $("article-view").hidden = true;
  $("review-panel").hidden = true;
  $("limitations-panel").hidden = true;
  $("issues").replaceChildren();
  $("claims").replaceChildren();
  $("issue-count").textContent = "0";
  $("claim-count").textContent = "0";
  $("revision").replaceChildren();
  $("revision").disabled = true;
  $("hash").textContent = "";
  $("copy-hash").disabled = true;
  $("reading-stats").textContent = "";
  $("draft-title").textContent = "Opening draft…";
  renderSources([]);
  $("progress").textContent = "";
  $("state").textContent = "Loading run…";
  delete $("state").dataset.state;
  renderPipeline();
  try {
    const run = await refreshRun();
    if (state.activeRun !== id) return;
    if (run) {
      for (const field of ["topic", "audience", "tone", "language", "target_words", "research_mode", "instructions", "editorial_profile", "angle", "author_notes"]) {
        $(field).value = run.request[field] ?? (field === "language" ? "en" : field === "editorial_profile" ? "standard" : "");
      }
      $("reference_urls").value = (run.request.reference_urls || []).join("\n");
      for (const destination of ["medium", "linkedin", "x"]) $("destination-" + destination).checked = (run.request.destinations || ["medium", "linkedin", "x"]).includes(destination);
      $("brief-extra").open = Boolean(run.request.instructions);
      updateBriefDetails();
    }
    if (run && run.current_revision && TERMINAL.has(run.state)) await loadArticle();
    await refreshHistory();
    if (!options.restore) $("draft-title").focus();
  } catch (error) {
    if (state.activeRun !== id) return;
    if (error instanceof ApiError && error.status === 404) {
      localStorage.removeItem("fieldnote.run");
      state.activeRun = null;
      $("run-view").hidden = true;
      $("empty").hidden = false;
      showWorkspace("compose");
      $("state").textContent = "No run selected";
      delete $("state").dataset.state;
      announce("That draft is no longer available.");
      return;
    }
    throw error;
  }
}

function researchWarning() {
  const topic = $("topic").value;
  const instructions = [$("instructions").value, $("angle").value, $("author_notes").value, $("reference_urls").value].join(" ");
  const mode = $("research_mode").value;
  const sourceDependent = /\b(latest|current|currently|today|news|recent|price|pricing|cost|statistic|study|report|version|release|law|regulation|medical|financial|tax)\b|\d{4}|\d+%/i.test(`${topic} ${instructions}`);
  const localizedFacts = /güncel|bugün|fiyat|maliyet|istatistik|sürüm|mevzuat|tıbbi|vergi|yatırım|fuentes?|precios?|noticias|médic|impuesto|preço|atual|notícias|pesquisa|investimento|aktuell|nachrichten|preise?|gesetz|medizin|steuer|quellen/iu.test(`${topic} ${instructions}`);
  const warning = $("research-warning");
  if (mode === "off" && (sourceDependent || localizedFacts)) {
    warning.textContent = "This brief looks source-dependent. Choose Required or Automatic so the draft can cite evidence, or narrow it to a clearly creative topic.";
    warning.hidden = false;
    return false;
  }
  warning.hidden = true;
  warning.textContent = "";
  return true;
}

function buildBrief() {
  return {
    topic: $("topic").value.trim(),
    audience: $("audience").value.trim(),
    tone: $("tone").value,
    language: $("language").value,
    target_words: Number($("target_words").value),
    research_mode: $("research_mode").value,
    instructions: $("instructions").value.trim(),
    destinations: ["medium", "linkedin", "x"].filter((name) => $("destination-" + name).checked),
    editorial_profile: $("editorial_profile").value,
    angle: $("angle").value.trim(),
    author_notes: $("author_notes").value.trim(),
    reference_urls: $("reference_urls").value.split(/\r?\n/).map((url) => url.trim()).filter(Boolean),
  };
}

function payloadKey(payload) {
  return JSON.stringify(payload);
}

async function submitBrief(event) {
  event.preventDefault();
  clearError();
  if (!briefValidation.validate()) return;
  if (!researchWarning()) {
    $("research_mode").focus();
    announce("Choose a research mode that fits this source-dependent brief.");
    return;
  }
  const payload = buildBrief();
  const signature = payloadKey(payload);
  if (!state.pendingKey || state.pendingPayload !== signature) {
    state.pendingKey = crypto.randomUUID();
    state.pendingPayload = signature;
  }
  const button = $("submit");
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  $("submit-label").textContent = "Creating draft…";
  try {
    const result = await api("/api/v1/runs", {
      method: "POST",
      headers: { "Idempotency-Key": state.pendingKey },
      body: payload,
    });
    state.pendingKey = null;
    state.pendingPayload = null;
    await refreshHistory();
    await selectRun(result.id);
    announce("Draft queued. Progress will appear here and stays available if you close the page.");
  } catch (error) {
    if (error instanceof ApiError && error.status === 409 && error.code === "idempotency_conflict") {
      state.pendingKey = null;
      state.pendingPayload = null;
    }
    showError(error);
  } finally {
    button.disabled = false;
    button.removeAttribute("aria-busy");
    $("submit-label").textContent = "Create draft";
  }
}

function editing(enabled) {
  $("edit-panel").hidden = !enabled;
  $("preview-panel").hidden = enabled;
  $("preview-tab").setAttribute("aria-selected", String(!enabled));
  $("edit-tab").setAttribute("aria-selected", String(enabled));
  $("preview-tab").tabIndex = enabled ? -1 : 0;
  $("edit-tab").tabIndex = enabled ? 0 : -1;
  updateEditingControls();
  if (enabled) {
    $("editor").focus();
  } else {
    const previewText = state.editorDirty || !state.article
      ? $("editor").value
      : state.article.markdown;
    renderMarkdown($("preview"), previewText);
    if (state.editorDirty) {
      $("reading-stats").textContent = "Unsaved preview";
      $("review-status").textContent = "You are previewing unsaved changes. Save a new revision to preserve them. This text has not been assessed.";
      $("review-status").dataset.status = "unassessed";
    }
    $("preview-tab").focus();
  }
}

async function saveEdit(parentRevision = null) {
  clearError();
  if (!state.article || !state.activeRun || state.saving) return;
  const requestedRun = state.activeRun;
  const parent = parentRevision || state.article.id;
  const markdown = $("editor").value;
  state.saving = true;
  updateEditingControls();
  try {
    const created = await api(`/api/v1/runs/${requestedRun}/revisions`, {
      method: "POST",
      body: { parent_revision: parent, markdown },
    });
    if (requestedRun !== state.activeRun) return;
    state.conflictText = null;
    state.editorDirty = false;
    editing(false);
    await loadArticle(created.id);
    await refreshRun();
    announce("New revision saved. Human edits remain unassessed.");
    $("review-status").focus();
  } catch (error) {
    if (requestedRun !== state.activeRun) return;
    if (error instanceof ApiError && error.code === "revision_conflict") {
      state.conflictText = "The article changed while you were editing. Your text is still here. Reload the latest draft, or save your text as a new revision on the latest draft.";
      $("conflict").hidden = false;
      $("conflict-message").textContent = state.conflictText;
      $("conflict-save").focus();
      announce("Edit conflict. Your text is preserved.");
      return;
    }
    showError(error);
  } finally {
    state.saving = false;
    updateEditingControls();
  }
}

async function recoverOnLatest() {
  clearError();
  if (!state.activeRun) return;
  try {
    const latest = await api(`/api/v1/runs/${state.activeRun}/article`);
    state.article = latest;
    state.conflictText = null;
    $("conflict").hidden = true;
    await saveEdit(latest.id);
  } catch (error) {
    showError(error);
  }
}

async function copyEditorText() {
  try {
    await navigator.clipboard.writeText($("editor").value);
    announce("Your text was copied to the clipboard.");
  } catch {
    $("editor").focus();
    $("editor").select();
    announce("Select and copy your text from the editor.");
  }
}

async function cancelRun() {
  if (!state.activeRun) return;
  try {
    await api(`/api/v1/runs/${state.activeRun}/cancel`, { method: "POST" });
    await refreshRun();
  } catch (error) {
    showError(error);
  }
}

async function deleteRun() {
  if (!state.activeRun) return;
  if (!window.confirm("Delete this draft and its stored revisions? This cannot be undone.")) return;
  try {
    await api(`/api/v1/runs/${state.activeRun}`, { method: "DELETE" });
    stopObserving();
    state.activeRun = null;
    state.run = null;
    state.article = null;
    localStorage.removeItem("fieldnote.run");
    $("run-view").hidden = true;
    $("empty").hidden = false;
    $("state").textContent = "No run selected";
    delete $("state").dataset.state;
    await refreshHistory();
    showWorkspace("compose");
    announce("Draft deleted.");
  } catch (error) {
    showError(error);
  }
}

async function refreshTopics() {
  const button = $("refresh-topics");
  button.disabled = true;
  $("topic-status").textContent = "Loading topic ideas…";
  try {
    const result = await api(`/api/v1/topics/refresh?days=${$("topic-window").value}`, { method: "POST" });
    $("topic-ideas").replaceChildren();
    $("topic-status").textContent = `${result.fixture ? "Synthetic demonstration. " : ""}${result.candidates.length} ${result.candidates.length === 1 ? "idea" : "ideas"}. ${result.notice} ${result.failures.length ? `${result.failures.length} feeds unavailable. ` : ""}Refreshes reuse the last result for five minutes.`;
    for (const topic of result.candidates) {
      const item = document.createElement("li");
      const heading = document.createElement("h3");
      heading.textContent = topic.title;
      const meta = document.createElement("p");
      meta.className = "help";
      meta.textContent = `${topic.publisher} · ${topic.source_kind} · published ${topic.published_at || "unknown"} · retrieved ${topic.retrieved_at}. ${topic.reason} ${topic.warning}`;
      const description = document.createElement("p"); description.textContent = topic.summary;
      const link = document.createElement("a"); link.textContent = "Inspect source";
      link.href = safeExternalUrl(topic.url); link.target = "_blank"; link.rel = "noopener noreferrer";
      const use = document.createElement("button"); use.type = "button"; use.className = "secondary"; use.textContent = "Use this idea";
      use.addEventListener("click", () => {
        $("topic").value = topic.title;
        $("reference_urls").value = topic.related_urls.slice(0, 4).join("\n");
        $("research_mode").value = "required";
        updateBriefDetails(); $("topic").focus();
        announce("Topic and source URLs added to your brief. Choose your own angle before drafting.");
      });
      const actions = document.createElement("div"); actions.className = "actions"; actions.append(link, use);
      item.append(heading, meta, description, actions); $("topic-ideas").append(item);
    }
  } catch (error) { $("topic-status").textContent = "Topic ideas could not be loaded. You can still enter your own topic."; showError(error); }
  finally { button.disabled = false; }
}

function wireEvents() {
  briefValidation = createFormValidation($("brief-form"), { announce, custom: {
    "destination-medium": () => ["medium", "linkedin", "x"].some(name => $("destination-" + name).checked)
      ? "" : "Select at least one publishing destination.",
    reference_urls: field => {
      const urls = field.value.split(/\r?\n/).map(url => url.trim()).filter(Boolean);
      if (urls.length > 4) return "Add up to four source URLs, one per line.";
      for (const url of urls) {
        try { if (!["http:", "https:"].includes(new URL(url).protocol)) throw new Error("invalid_url"); }
        catch { return "Enter a complete source URL on each line, such as https://example.com/article."; }
      }
      return "";
    },
  } });
  publishing = createPublishingController({ api, state, showError, announce, renderMarkdown });
  $("writing-task").addEventListener("click", () => setDraftTask("write"));
  $("publishing-task").addEventListener("click", () => { setFocusMode(false); setDraftTask("publish"); });
  $("refresh-topics").addEventListener("click", refreshTopics);
  $("new-draft").addEventListener("click", () => openComposer(true));
  $("library-new").addEventListener("click", () => openComposer(true));
  $("back-to-brief").addEventListener("click", () => openComposer(false));
  $("retry-brief").addEventListener("click", () => openComposer(false));
  $("nav-studio").addEventListener("click", () => showWorkspace(state.activeRun ? "draft" : "compose", true));
  for (const id of ["nav-library", "view-library"]) {
    $(id).addEventListener("click", () => { renderLibrary(); showWorkspace("library", true); });
  }
  $("draft-search").addEventListener("input", renderLibrary);
  $("draft-filter").addEventListener("change", renderLibrary);
  $("focus-toggle").addEventListener("click", () => setFocusMode(document.body.dataset.focus !== "true"));
  $("menu-toggle").addEventListener("click", (event) => {
    event.stopPropagation();
    const open = document.body.dataset.navigation !== "open";
    if (open) openNavigation();
    else closeNavigation();
    $("menu-toggle").setAttribute("aria-expanded", String(open));
  });
  $("navigation-close").addEventListener("click", () => closeNavigation());
  $("navigation-scrim").addEventListener("click", () => closeNavigation());
  window.matchMedia("(max-width: 780px)").addEventListener("change", () => closeNavigation(false));
  document.addEventListener("click", (event) => {
    if (document.body.dataset.navigation === "open" && !$("sidebar").contains(event.target)) closeNavigation();
  });
  document.addEventListener("keydown", (event) => {
    if (document.body.dataset.navigation === "open") {
      if (event.key === "Escape") { event.preventDefault(); closeNavigation(); }
      if (event.key === "Tab") {
        const targets = [...$("sidebar").querySelectorAll("a[href],button:not(:disabled)")]
          .filter((element) => element.getClientRects().length);
        const first = targets[0], last = targets[targets.length - 1];
        if (!$("sidebar").contains(document.activeElement)) {
          event.preventDefault(); (event.shiftKey ? last : first).focus();
        }
        else if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
      }
      return;
    }
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      showWorkspace("library", true);
    }
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter" && state.view === "compose") {
      event.preventDefault();
      if (!$("submit").disabled) $("brief-form").requestSubmit();
    }
    if (event.key === "Escape" && document.body.dataset.navigation === "open") {
      closeNavigation();
      $("menu-toggle").focus();
    }
  });
  for (const button of document.querySelectorAll("[data-brief]")) {
    button.addEventListener("click", () => {
      $("topic").value = button.dataset.brief;
      updateBriefDetails();
      $("topic").focus();
    });
  }
  for (const button of document.querySelectorAll("[data-words]")) {
    button.addEventListener("click", () => {
      $("target_words").value = button.dataset.words;
      updateBriefDetails();
    });
  }
  $("target_words").addEventListener("input", updateBriefDetails);
  $("brief-form").addEventListener("submit", submitBrief);
  $("topic").addEventListener("input", updateBriefDetails);
  $("instructions").addEventListener("input", researchWarning);
  $("research_mode").addEventListener("change", researchWarning);
  $("cancel").addEventListener("click", cancelRun);
  $("delete").addEventListener("click", deleteRun);
  $("theme-toggle").addEventListener("click", toggleTheme);
  $("copy-hash").addEventListener("click", copyHash);
  $("refresh").addEventListener("click", () => refreshHistory().catch(showError));
  $("more").addEventListener("click", () => refreshHistory(true).catch(showError));
  $("preview-tab").addEventListener("click", () => editing(false));
  $("edit-tab").addEventListener("click", () => editing(true));
  const selectTab = (editingMode) => {
    editing(editingMode);
    $(editingMode ? "edit-tab" : "preview-tab").focus();
  };
  $("preview-tab").addEventListener("keydown", (event) => {
    if (event.key === "ArrowRight" || event.key === "End") {
      event.preventDefault();
      selectTab(true);
    } else if (event.key === "Home") {
      event.preventDefault();
      selectTab(false);
    }
  });
  $("edit-tab").addEventListener("keydown", (event) => {
    if (event.key === "ArrowLeft" || event.key === "Home") {
      event.preventDefault();
      selectTab(false);
    } else if (event.key === "End") {
      event.preventDefault();
      selectTab(true);
    }
  });
  $("headline-options").addEventListener("change", () => {
    if ($("headline-options").value) $("article-headline").value = $("headline-options").value;
  });
  $("apply-headline").addEventListener("click", () => {
    const title = $("article-headline").value.replace(/[\r\n]/g, " ").trim();
    if (!title || !state.article || state.saving || state.publicationDirty) {
      announce("Enter a title and save any publication edits first."); return;
    }
    const lines = $("editor").value.split("\n");
    let fenced = false;
    let found = false;
    for (let i = 0; i < lines.length; i++) {
      if (lines[i].startsWith("```")) fenced = !fenced;
      else if (!fenced && /^#\s+/.test(lines[i])) { lines[i] = `# ${title}`; found = true; break; }
    }
    if (!found) lines.unshift(`# ${title}`, "");
    $("editor").value = lines.join("\n");
    $("editor").dispatchEvent(new Event("input", { bubbles: true }));
    editing(true);
    announce("Title changed in the editor. Save a new revision, then review its promise against the body.");
  });
  $("editor").addEventListener("input", () => {
    state.editorDirty = true;
    publishing?.update();
    $("editor-count").textContent = `${$("editor").value.length.toLocaleString()} characters · unsaved changes`;
    updateReviewControls();
  });
  $("save").addEventListener("click", () => saveEdit());
  $("request-review").addEventListener("click", requestRevisionReview);
  $("cancel-review").addEventListener("click", cancelRevisionReview);
  $("conflict-reload").addEventListener("click", () => {
    state.conflictText = null;
    $("conflict").hidden = true;
    state.editorDirty = false;
    loadArticle().catch(showError);
  });
  $("conflict-save").addEventListener("click", recoverOnLatest);
  $("conflict-copy").addEventListener("click", copyEditorText);
  $("revision").addEventListener("change", async () => {
    const selected = $("revision").value;
    if (!await confirmDiscard()) {
      $("revision").value = state.article.id;
      return;
    }
    state.editorDirty = false;
    loadArticle(selected).catch(showError);
  });
  window.addEventListener("beforeunload", (event) => {
    if (state.editorDirty || state.publicationDirty) { event.preventDefault(); event.returnValue = ""; }
  });
  window.addEventListener("online", () => {
    setConnection("Online", "live");
    refreshRun().then(() => connectStream()).catch(showError);
  });
  window.addEventListener("offline", () => {
    setConnection("Offline — saved progress will refresh when you reconnect.", "offline");
  });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && state.activeRun) {
      refreshRun().catch(showError);
    }
  });
}

async function start() {
  try { migratePreferences(localStorage); } catch { /* Storage may be unavailable. */ }
  loadStoredTheme();
  wireEvents();
  updateBriefDetails();
  showWorkspace("compose");
  try {
    let session;
    try {
      session = await api("/api/v1/session");
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        session = await api("/api/v1/session", { method: "POST" });
      } else {
        throw error;
      }
    }
    state.csrf = session.csrf_token;
    $("submit").disabled = false;
    setConnection(session.fixture ? "Local fixture profile" : "Local profile", "live");
    $("notice-text").textContent = session.fixture
      ? "Local fixture profile: synthetic model and research fixtures. No live AI or web research is used."
      : "Local profile. Check the configured provider and evidence labels before publishing.";
    await refreshHistory();
    const saved = localStorage.getItem("fieldnote.run");
    // A session restore must not replace navigation or a brief the person
    // started while the initial history request was still in flight.
    if (saved && state.view === "compose" && !state.activeRun && !$("topic").value.trim()) {
      await selectRun(saved, { restore: true }).catch(showError);
    }
  } catch (error) {
    setConnection("Service unavailable", "offline");
    showError(error);
  }
}

start();
