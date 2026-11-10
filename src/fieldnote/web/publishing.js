// Publication controls use saved revision identities; no external posting.
export function createPublishingController({ api, state, showError, announce, renderMarkdown }) {
  const $ = (id) => document.getElementById(id);
  const checks = ["facts_checked", "voice_checked", "language_checked", "variants_checked", "disclosure_checked"];
  let current = null;
  let identity = null;
  let ticket = 0;
  let busy = false;
  const base = () => `/api/v1/runs/${state.activeRun}`;
  const active = () => `${state.activeRun}:${state.article?.id}:${state.article?.assessment?.review_run_id || "generation"}`;
  const copyAllowed = () => current && !current.stale && !busy && !state.saving
    && !state.editorDirty && !state.publicationDirty
    && current.data.revision_id === state.article?.id
    && current.data.content_hash === state.article?.content_hash;

  async function copyText(text, packageId = current?.id) {
    if (!copyAllowed() || packageId !== current.id) return;
    try { await navigator.clipboard.writeText(text); announce("Saved publication text copied."); }
    catch { announce("Clipboard access is unavailable. Download the text package instead."); }
  }

  function selectChannel(channel) {
    for (const name of ["medium", "linkedin", "x"]) {
      $("channel-" + name).hidden = name !== channel;
      $("channel-" + name + "-button").setAttribute("aria-pressed", String(name === channel));
    }
  }

  function update() {
    const latest = state.revisions[0]?.id === state.article?.id;
    $("prepare-package").disabled = busy || state.editorDirty || state.saving || !latest;
    $("replace-extracts").disabled = busy || state.editorDirty || !latest;
    $("publication-download").hidden = !copyAllowed();
    $("publication-save-hint").textContent = state.editorDirty ? "Save the article before preparing publication."
      : state.publicationDirty ? "Publication edits are unsaved. Save a package before copying or downloading."
        : current?.stale ? "This package is stale. Save a new package for the current article and assessment before copying."
          : current && !current.data.medium_body_html ? "Older package: text includes the title. Save a new package for separate title and formatted body."
        : "Each saved package preserves one exact article revision. No content is posted automatically.";
    for (const button of document.querySelectorAll("[data-package-copy]")) button.disabled = !copyAllowed();
    $("copy-publication-formatted").disabled = !copyAllowed() || !current.data.medium_body_html;
    $("record-publication").disabled = !current || busy;
  }

  function showPackage(value) {
    current = value;
    const data = value.data;
    state.publicationDirty = false;
    $("publication-drafts").hidden = false;
    $("linkedin-draft").value = data.linkedin_text;
    $("x-drafts").value = data.x_posts.join("\n---\n");
    renderMarkdown($("medium-preview"), data.medium_markdown);
    $("saved-x-posts").replaceChildren();
    for (const [index, post] of data.x_posts.entries()) {
      const item = document.createElement("li");
      const text = document.createElement("p"); text.textContent = post;
      const count = document.createElement("p"); count.className = "help";
      count.textContent = `Post ${index + 1} of ${data.x_posts.length} · ${data.counts.x[index].weighted_length}/280 weighted characters (saved)`;
      const copy = document.createElement("button"); copy.type = "button"; copy.className = "secondary";
      copy.textContent = `Copy post ${index + 1}`; copy.setAttribute("data-package-copy", "");
      copy.addEventListener("click", () => copyText(post, value.id));
      item.append(count, text, copy); $("saved-x-posts").append(item);
    }
    $("publication-canonical").value = data.canonical_url || "";
    $("publication-disclosure").value = data.disclosure;
    $("search-title").value = data.discovery?.search_title || data.title;
    $("search-description").value = data.discovery?.search_description || "";
    for (const key of checks) $("publication-" + key).checked = !value.stale && Boolean(data.checklist[key]);
    $("publication-status").textContent = `${value.stale ? "Stale package: article or assessment changed. " : ""}${data.ready && !value.stale ? "Checklist complete. " : "Review required. "}LinkedIn ${data.counts.linkedin}/3,000 characters. X ${data.counts.x.map((p) => `${p.weighted_length}/280`).join(", ")}.`;
    $("publication-warnings").replaceChildren();
    for (const warning of data.warnings) {
      const item = document.createElement("li"); item.textContent = warning; $("publication-warnings").append(item);
    }
    $("publication-download").href = `${base()}/packages/${value.id}/download`;
    $("publication-history").value = value.id;
    update();
  }

  async function history() {
    const expected = active();
    const response = await api(`${base()}/publications`);
    if (expected !== active()) return;
    $("publication-log").replaceChildren();
    for (const record of response.publications) {
      const item = document.createElement("li");
      const link = document.createElement("a"); link.href = record.url;
      link.target = "_blank"; link.rel = "noopener noreferrer";
      link.textContent = `${record.platform} · ${new Date(record.published_at).toLocaleString()} (recorded by you)`;
      item.append(link); $("publication-log").append(item);
    }
  }

  async function render() {
    const expected = active();
    if (!state.article || identity === expected) { update(); return; }
    identity = expected; current = null; state.publicationDirty = false;
    const ownTicket = ++ticket;
    $("publication-drafts").hidden = true;
    $("publication-status").textContent = "Prepare platform drafts from this saved article. No model requests are needed.";
    $("publication-canonical").value = ""; $("publication-disclosure").value = "";
    $("search-title").value = state.article.discovery?.title || "";
    $("search-description").value = state.article.discovery?.description_suggestion || "";
    for (const key of checks) $("publication-" + key).checked = false;
    update();
    try {
      const response = await api(`${base()}/packages`);
      if (ownTicket !== ticket || expected !== active()) return;
      const options = response.packages.filter((p) => p.revision_id === state.article.id);
      $("publication-history").replaceChildren(new Option("Choose a saved package", ""));
      for (const p of options) $("publication-history").append(new Option(new Date(p.created_at).toLocaleString(), p.id));
      if (options.length) {
        const value = await api(`${base()}/packages/${options[0].id}`);
        if (ownTicket === ticket && expected === active() && !state.publicationDirty) showPackage(value);
      }
      await history();
    } catch (error) { if (expected === active()) showError(error); }
  }

  async function prepare(replace = false) {
    if (!state.article || state.editorDirty || busy) return;
    const expected = active();
    const payload = { content_hash: state.article.content_hash,
      thread_length: Number($("publication-thread").value),
      canonical_url: $("publication-canonical").value.trim() || null,
      disclosure: $("publication-disclosure").value.trim() || null,
      search_title: $("search-title").value.trim() || null,
      search_description: $("search-description").value.trim() || null };
    for (const key of checks) payload[key] = $("publication-" + key).checked;
    if (current && !replace) {
      payload.linkedin_text = $("linkedin-draft").value;
      payload.x_posts = $("x-drafts").value.split(/\n---\n/).map((p) => p.trim()).filter(Boolean);
    }
    if (replace) for (const key of checks) payload[key] = false;
    busy = true; update();
    try {
      const value = await api(`${base()}/revisions/${state.article.id}/packages`, { method: "POST", body: payload });
      if (expected !== active()) return;
      showPackage(value); identity = null; await render();
      announce("Publication package saved. Review it before posting manually.");
    } catch (error) { if (expected === active()) showError(error); }
    finally { busy = false; update(); }
  }

  $("prepare-package").addEventListener("click", () => prepare());
  for (const channel of ["medium", "linkedin", "x"]) {
    $("channel-" + channel + "-button").addEventListener("click", () => selectChannel(channel));
  }
  $("replace-extracts").addEventListener("click", () => prepare(true));
  for (const id of ["linkedin-draft", "x-drafts", "publication-canonical", "publication-disclosure", "publication-thread", "search-title", "search-description", ...checks.map((key) => "publication-" + key)]) {
    $(id).addEventListener("input", () => {
      state.publicationDirty = true;
      if (["linkedin-draft", "x-drafts", "publication-canonical", "publication-thread"].includes(id)) $("publication-variants_checked").checked = false;
      if (["linkedin-draft", "x-drafts"].includes(id)) $("publication-facts_checked").checked = false;
      if (["search-title", "search-description"].includes(id)) $("publication-facts_checked").checked = false;
      if (id === "publication-disclosure") $("publication-disclosure_checked").checked = false;
      update();
    });
  }
  $("publication-history").addEventListener("change", async () => {
    const id = $("publication-history").value;
    if (!id) return;
    if (state.publicationDirty) { announce("Save your publication edits before switching packages."); $("publication-history").value = current?.id || ""; return; }
    const expected = active();
    try { const value = await api(`${base()}/packages/${id}`); if (expected === active()) showPackage(value); }
    catch (error) { showError(error); }
  });
  for (const [id, text] of [["copy-publication-title", () => current.data.title],
    ["copy-publication-article", () => current.data.medium_body_text || current.data.article_text],
    ["copy-linkedin", () => current.data.linkedin_text], ["copy-x", () => current.data.x_posts.join("\n\n---\n\n")]]) {
    $(id).addEventListener("click", () => {
      if (copyAllowed()) return copyText(text());
    });
  }
  $("copy-publication-formatted").addEventListener("click", async () => {
    if (!copyAllowed() || !current.data.medium_body_html) return;
    const value = current;
    try {
      if (typeof ClipboardItem === "undefined" || !navigator.clipboard.write) throw new Error("plain_text_fallback");
      await navigator.clipboard.write([new ClipboardItem({
        "text/html": new Blob([value.data.medium_body_html], {type: "text/html"}),
        "text/plain": new Blob([value.data.medium_body_text], {type: "text/plain"}),
      })]);
      announce("Formatted article body copied. Check formatting in the destination editor.");
    } catch {
      if (!copyAllowed() || value.id !== current.id) return;
      try {
        await navigator.clipboard.writeText(value.data.medium_body_text);
        announce("Formatted copying is unavailable. Article body copied as plain text.");
      } catch { announce("Clipboard access is unavailable. Download the text package instead."); }
    }
  });
  $("record-publication").addEventListener("click", async () => {
    if (!current) return;
    const expected = active();
    try {
      await api(`${base()}/packages/${current.id}/publications`, { method: "POST", body: {
        platform: $("publication-platform").value, url: $("publication-url").value.trim(),
        published_at: new Date($("publication-date").value).toISOString(),
      } });
      if (expected === active()) { await history(); announce("Published link recorded. Fieldnote did not post any content."); }
    } catch (error) { showError(error); }
  });
  const now = new Date();
  $("publication-date").value = new Date(now.getTime() - now.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
  return { render, update };
}
