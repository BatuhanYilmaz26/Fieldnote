# Fieldnote user guide

This guide describes the browser workspace in the local development profile. The
default profile uses synthetic model and research fixtures. It is a complete
workflow demonstration, not a live research or publishing service.

## Before you start

Open the app at `http://127.0.0.1:8000` after following
[Setup and operations](setup-and-operations.md). The header shows one of:

- **Local fixture profile** — synthetic model and research; no live AI or web
  research is used.
- **Local profile** — a local non-fixture provider is configured. Check the
  evidence labels before publishing.

The browser starts a loopback-only local session automatically. A session expires
after `SESSION_TTL_HOURS` (8 hours by default); reload the page to start a new
one.

The header also has a light/dark theme toggle. It follows your system colour
scheme until you choose a theme; the choice is remembered in this browser
(`fieldnote.theme`).

## Find a technical topic

Expand **Find your next technical topic**, choose a recent window, and select **Refresh ideas**. In the default fixture profile you see a clearly labeled demonstration. Live discovery reads fixed Hugging Face, Google Research, Google DeepMind and arXiv machine-learning feeds when enabled in [configuration](configuration.md). No model or search-provider quota is used.

Each idea links to its source and shows publisher, source type, publication and retrieval dates. Feed metadata is a lead, not verified evidence. Publisher claims, community posts and preprints need your judgment. Unknown publication dates stay explicit; updating an old page does not make it new. Unavailable feeds do not prevent entering your own topic. Refreshes reuse a saved snapshot for five minutes to avoid unnecessary requests.

**Use this idea** adds the topic and source URLs to your brief and selects Required research. Add your angle before drafting. Repeated links/titles are merged; a warning identifies a source/topic used in your most recent 200 saved briefs. This is a drafting-history check, not a claim that an article has been published.

## Create a draft

Choose **New draft** to open the writing studio. Start with your own topic or use
**A practical guide**, **Explain an idea** or **Tell a story** as an editable
starting point. The character counter helps you keep the topic within its limit.
Use the word-target buttons for a quick choice, or enter another supported target.

Open **Additional direction** for structure, exclusions or extra questions. These
instructions are optional. The preferences beside the brief control the reader,
tone, length and research mode.

Fill in:

| Field | Required | Notes |
|---|---|---|
| Topic | Yes | 1–500 characters. Be specific about the reader, promise and facts to check. |
| Reader | Yes | 1–240 characters. The studio defaults to “Technical practitioners”. |
| Writing style | Yes | **Concise technical** focuses on one useful idea, examples and limitations; **General writing** keeps the original profile. |
| Your angle | No | Up to 1,000 characters describing your point of view. When research is enabled, the search provider can receive a bounded query built from your topic and angle. |
| Your observations | No | Up to 4,000 characters of your own experience or notes; these are sent to your configured model with the brief. |
| Primary source URLs | No | Up to four public URLs, one per line, prioritized during research. Unsupported or unavailable sources remain explicit gaps. |
| Prepare for | Yes | Select Medium, LinkedIn and/or X. The article remains the canonical source for later adaptations. |
| Tone | Yes | One of the offered tones; stored with the brief. |
| Language | Yes | English, Türkçe, Español, Português or Deutsch. Defaults to English. |
| Target words | Yes | 200–3,000. The studio starts at 550, with quick targets of 350, 550 and 800. |
| Research | Yes | **Required**, **Automatic** or **Off**. |
| Additional direction → Anything else? | No | Up to 4,000 characters for structure, exclusions or questions. |

### Article language

Choose **Language** beneath the brief to write in English, Turkish, Spanish,
Portuguese or German. The choice follows the draft through planning, writing,
revisions and downloads. Reopening a saved draft restores its original language.
**New draft** returns the settings to their defaults, including English.

The workspace, progress and review controls stay in English. Article headings,
prose, reference headings and generated disclosure notices use the selected
language. Source titles and quotations can remain in their original language.
Specify a regional preference such as Brazilian Portuguese or Spanish from Spain
in **Additional direction** when it matters to your reader.

Language selection is a writing instruction, not a guarantee of fluency from
every model. Other-language generated drafts remain **Needs review**: check
grammar, regional wording and whether translated claims match their evidence.
This warning alone does not cause extra model rewrite calls. The free router
can choose different models, so quality may vary; see the
[official router description](https://openrouter.ai/docs/guides/routing/routers/free-router).

The local fixture profile shows repetitive, translated demonstration paragraphs
with a synthetic disclosure. It does not translate arbitrary topics or measure
live model quality. It preserves the topic as you entered it. Downloads use UTF-8,
preserving characters such as `ğ`, `ñ`, `ã`, `ü` and `ß`.

### Research modes

- **Required** always gathers supporting evidence.
- **Automatic** researches when the brief contains current, numeric, named-product,
  legal, medical, financial or otherwise source-dependent language.
- **Off** is only for clear creative or general explanatory briefs. Choosing Off
  for a source-dependent topic shows a warning; the server rejects the brief with
  an explanation and a correlation reference if it is still submitted. This is a
  deliberate boundary, not a model judgement.

While a submission is in flight, retrying the same brief reuses one durable run
through an idempotency key. It does not create a duplicate draft. After the run
is accepted, a new submission of the same brief is a new run.

## Watch a run

The draft panel shows `Queued`, `Writing`, `Waiting to retry`, `Cancelling`,
`Needs review`, `Generated`, `Cancelled` or `Could not finish`. Progress is
factual: sections completed, sources recorded and claims needing attention. There
is no fabricated ETA.

A stage list shows how far the run has reached — **Brief accepted → Research →
Plan → Sections → Review**. Completed stages have a check and the current stage
is highlighted. The progress summary shows counts such as `2/2` sections or
`1 source`. A failed or cancelled run marks the stage where it stopped. While a
draft is in **Needs review**, the Review stage remains open. Saving your edits
creates an unassessed revision; it does not approve the article.

- Closing the tab does not stop the run. Reopen it from **Recent drafts**.
- A dropped live connection reconnects and reconciles from the durable run; it
  does not duplicate sections.
- **Cancel run** is available while the run is non-terminal. It reaches
  `Cancelled` and hides the review and article panes.

## Review evidence

When a draft reaches **Needs review**, check:

- **Research limitations** — shown when research was partial or unavailable.
- **Review findings** — errors and warnings such as unsupported claims or a
  synthetic-fixture notice.
- **Claim support** — cited sentences with their assessment and reason. Use
  **View evidence** to read the saved excerpt and its source. **Show in preview**
  moves keyboard focus to the passage when it can be identified uniquely.
- **Sources** — provider, retrieval time and extraction state.

The fixture profile deliberately produces two unsupported claims and a
`synthetic_fixture` warning. That is expected: the fixture exercises the review
gate and never claims factual approval.

## Edit and revisions

The draft opens on a reading canvas, with editorial review, claim support,
sources and saved revisions in **Behind the draft**. Expand the evidence groups
you want to inspect. **Focus mode** gives the article more room and hides the
navigation and evidence inspector; **Exit focus mode** brings them back. On a
small screen, the inspector appears below the article.

Switch to **Edit Markdown**, change the article and choose **Save new revision**.
Human edits:

- create a new immutable revision;
- are marked **unassessed**;
- never inherit an earlier approval.

Choose **Review saved revision** to request a fresh assessment of your latest
saved edit. The review runs in the background and leaves the Markdown unchanged.
Its findings belong to that revision and hash. You can keep editing while it
runs; a newer save starts unassessed and does not inherit the result. Select the
older revision to read its assessment later.

The review uses the configured model and consumes its quota. It makes at most
two model requests, including an optional schema repair, and does not fetch new
research or rewrite the draft. **Cancel review** stops further accepted work;
an in-flight request can still consume quota. Fixture reviews are synthetic,
and drafts in other languages continue to require your fluency review.

If stored evidence has expired or the edit introduces facts that need sources,
the assessment calls this out. Start a researched draft to gather fresh evidence.
If a worker is interrupted after dispatch, the review stops rather than silently
repeating a request with an unknown outcome. Request another review explicitly
when you are ready.

If the article changed while you were editing, the app preserves your text and
offers **Reload latest draft**, **Save my text on the latest draft** or **Copy my
text**. The revision chooser lists generated and human revisions with type, review
status and time; selecting one previews that exact immutable revision.
Leaving unsaved edits for a different draft, a new brief or a saved revision asks
for confirmation. Opening the library keeps the current edit in memory; return
with **Writing studio**. Reloading the page can lose unsaved text, so save before
closing it.

Raw HTML, images, external links and unsafe URL schemes are rejected in generated
Markdown. The preview uses text nodes only.

## Download

**Download Markdown** downloads the selected revision, not a newer or older one.
The page displays its SHA-256 next to a **Copy** button that places the hash
characters (without the `SHA-256` prefix) on the clipboard. To verify a download:

```powershell
Get-FileHash .\draft.md -Algorithm SHA256
```

or on Linux:

```bash
sha256sum draft.md
```

The computed hash must match the displayed hash exactly.

## History, reopening and deletion

The sidebar's **Recent drafts** lists up to six recent durable runs with a state chip and relative time
(such as “Needs review · 1 h ago”); the list refreshes when the selected run
reaches a terminal state, so completed drafts are never shown as queued.
Select one to recover its current revision and progress without regenerating it.
Open **Draft library** for draft rows with titles, brief angles when supplied,
status and date. Search matches the title or angle of the drafts currently
loaded. **Load more drafts** includes older pages
in the search. The count and summary show how many drafts are loaded rather than
claiming to count unloaded records. The refresh button reloads the latest page.

**Back to studio** opens the selected run's original brief for a new attempt.
**New draft** clears the brief. Neither action removes a saved draft.

**Delete run** asks for confirmation, then removes the run from history and clears
the saved run id. Deletion is a durable tombstone: the run is no longer readable,
and retention removes it (including its checkpoint thread) after
`DELETED_RUN_GRACE_HOURS` (24 hours by default). A deleted run's idempotency key
returns `410` rather than silently regenerating it.

Each local owner sees only their own runs. The local profile is a single-owner
development identity; it is not a multi-tenant account system.

## Cost, quotas and privacy

- The fixture profile calls no paid provider and uses zero budgets.
- `MODEL_POLICY=free_only` is the default. Budgeted profiles enforce
  `RUN_BUDGET_USD` and `USER_DAILY_BUDGET_USD` in the worker, not in the browser.
- `MAX_USER_NONTERMINAL_RUNS` (2 by default) limits concurrent non-terminal runs.
  When the queue or quota is full, the app shows a retryable `429` message.
- Provider credentials and database URLs are never sent to the browser.
- Local logs redact bearer tokens, cookies, API keys, passwords and DSNs.

## Accessibility

- On small screens, **Open navigation** reveals the sidebar. Choose a destination,
  click outside it or press Escape to close it.
- Ctrl+K (Command+K on macOS) opens and focuses library search. Ctrl+Enter
  (Command+Enter) submits the brief while the composer is open.
- The skip link moves focus to the main content.
- The Preview/Edit tabs use arrow keys, Home and End.
- Form controls have programmatic labels; status changes are announced; errors
  move focus to the error region.
- The stage list marks only the reachable stage with `aria-current="step"`.
- Light and dark themes use a shared text-contrast palette; the theme toggle is
  an accessible button (`aria-pressed`) and respects `prefers-color-scheme` until
  changed.
- Reduced-motion preferences are respected, including the stage pulse and loading
  animation.
- Long unbroken text does not create horizontal page overflow.

A manual screen-reader pass is still recommended before a public release.

The interface bundles [Manrope](https://github.com/google/fonts/tree/main/ofl/manrope)
under the SIL Open Font License and [Heroicons](https://github.com/tailwindlabs/heroicons)
under the MIT License. Their license notices ship with the local assets; no
external font or icon service is contacted when you use the app.

## Troubleshooting

| Symptom | Meaning | Action |
|---|---|---|
| “Your session expired. Reload the page…” (`401`) | The local session ended or the process restarted. | Reload the page. |
| “Reload the page before trying this action.” (`403`) | CSRF token is stale or the request came from another origin. | Reload; use the app's own origin. |
| “That draft is no longer available.” (`404`) | The run does not exist or belongs to another owner. | Check Recent drafts. |
| “This draft was deleted. Start a new one.” (`410`) | The run is a deletion tombstone. | Start a new brief. |
| “The article changed while you were editing…” (`409`) | A newer revision exists. | Reload or save your text on the latest draft. |
| “The brief is too large…” (`413`) | Request body exceeds the configured limit. | Shorten the brief. |
| “You have reached the current run limit…” (`429`) | Too many non-terminal runs or queue full. | Wait, cancel or delete a run. |
| “The service is temporarily unavailable…” (`503`) | Database or provider dependency is unavailable. | Retry after the service recovers; saved work is durable. |
| “The AI response did not match the required writing format…” | The provider returned content that failed validation. | Check provider status and limits before starting a new draft. Invalid content was not accepted; partial work, if available, still needs review. |
| “The configured AI model or writing capability is currently unavailable…” | The configured route could not provide the required model capability. | Check model availability and try later. A free-only profile will not switch to a paid route. |
| “The AI provider's request limit was reached…” | Provider capacity remained limited when the run stopped. | Wait before starting a new draft; repeatedly restarting can consume more quota. |

## Boundaries

The local profile binds to loopback and must not be exposed publicly. Hosted OIDC,
live provider qualification and human factual review are separate gates; see the
root [README](../README.md#local-use-and-providers).

### Saving a long edit

Saved Markdown can contain up to 200,000 characters, including non-English text. Article edits have a separate upload limit; writing briefs retain their smaller limit. If an edit exceeds the limit, shorten it before saving. Every successful save creates a new, initially unassessed revision.

## Prepare to publish

Choose **Prepare to publish** beside **Write & review** above a saved article. This opens a dedicated task surface with Medium, LinkedIn and X views. **Save publication package** prepares LinkedIn and X drafts from whole sentences in that revision, without model calls. These extracts are a starting point for editing: check the opening, technical context and caveats. The suggested LinkedIn length is 120–200 words, with a [3,000-character platform limit](https://www.linkedin.com/help/linkedin/answer/a528176). The workspace counts UTF-16 units conservatively for supplementary characters.

Choose a single X post or a 3–5-post thread. **Replace drafts with article extracts** regenerates these local extracts and clears the publishing checklist. It can produce fewer posts if the article has too few suitable short sentences; the workspace reports that limitation. You can edit the result, separating posts with a line containing `---`. Each post is validated against the [standard weighted 280-character rules](https://docs.x.com/fundamentals/counting-characters), including numbering, URL shortening and the versioned parser's emoji/Unicode data. New emoji sequences or TLD changes can require a parser update; double-check the platform composer near its limit.

Add a canonical article URL if this is a cross-post; otherwise a source link is used for the initial social extracts. Edit the AI-assistance disclosure so it accurately describes your work. A disclosure in the chosen article language is supplied by default. [Medium's policy](https://help.medium.com/hc/en-us/articles/22576852947223-Artificial-Intelligence-AI-content-policy) requires disclosure for generated text and restricts AI-generated writing from its Partner Program paywall. It also requires captions identifying any AI images you add yourself. Grammar, outlining and fact-checking assistance are treated differently in its FAQ. Fieldnote cannot promise distribution or monetization eligibility.

Use the checklist to record your own source, voice, language, social-context and disclosure review. These are your attestations, not an independent certification. An unassessed or synthetic article remains **Review required** even if every checkbox is selected. Technical review also flags missing selected sources, unsupported freshness wording, unlabeled code examples and certain first-person experiment claims absent from your observations. These conservative signals supplement your reading; they do not prove that all remaining claims are true. Retrieved code is never executed automatically.

Editing platform text clears its fact/context checks; changing the disclosure clears its disclosure check. Save after editing platform text or review fields. Unsaved publication changes disable copy/download and prompt before switching drafts. Each save creates or reuses an immutable package. Selecting an older article revision shows its own packages. A changed article or later assessment marks an earlier package stale; its original text is retained without inheriting approval.

### Copy and download

The Medium view offers **Copy title**, **Copy formatted body** and **Copy body text**. The body omits the first article H1 and retains the disclosure. Formatted copying supplies HTML and plain text; if the browser cannot write HTML, it tries plain text and tells you which format was copied. Inspect formatting after pasting into the destination editor. Older saved packages remain readable; save a new package to obtain the separate body fields.

The LinkedIn view keeps its independently editable draft. The X view shows each saved post in order, with its server-validated weighted count and a separate copy button. **Copy thread as text** includes separators for reference; it does not import a native thread. Save variant edits before copying. Unsaved article/package edits, stale assessments and revision mismatches disable copy/download controls.

Use **Download text bundle** for a ZIP containing:

- `article-original.md`: the exact saved article, matching its revision hash.
- `medium.md`: the article with the selected disclosure near the beginning.
- `article.txt` and `article.html`: plain text and an escaped reading/copy document. Open the HTML file to copy its rendered text into an editor, then inspect formatting on the destination platform.
- `linkedin.txt` and `x-*.txt`: the selected social drafts, where those destinations were selected.
- `references.json`, `manifest.json` and `HANDOFF.txt`: source metadata, file hashes, revision/language/provenance and review reminders.
- New packages also include `title.txt`, `medium-body.html` and `medium-body.txt` for separate title/body handoff. The original article remains unchanged.

The package does not include private brief observations, credentials or configuration. Article content itself can contain observations you chose to include, so review it before sharing. Image generation is deferred; exports are text-only.

### Record where you published

Publish manually on your chosen platform, then expand **Record a published link**. Enter its URL, platform and actual date/time. The workspace saves your report against the package and displays it when you reopen the draft. Recording a link makes no request to that platform and does not verify or publish its contents. Repeating the same link for the same package does not create another record. Future publication dates are rejected; this is a log, not a scheduling service.


## Titles, search previews and readable technical articles

Choose **Concise technical** for one focused practitioner article. The planning request asks for three different, descriptive title ideas in the article language. This uses the existing planning call, not an extra model request. Providers can omit suggestions, and older drafts may have none. Fixture mode supplies only the synthetic draft's existing title. Suggestions are starting points; they are not tested click-through predictions.

Open **Title & reading notes** below the article. Select a suggestion or write your own title, then choose **Use title in editor**. This changes the first article heading in the Markdown editor while preserving the body. **Save new revision** preserves the change as a new, initially unassessed article. Review the title against the body before requesting another assessment. Original planning suggestions remain available after editing, so discard any that no longer fit.

A useful technical title names the subject and the specific question or implication. For example, “RAG evaluation: why retrieval similarity is not answer quality” tells a reader what to expect without claiming an unmeasured improvement. Check every number, version, date and comparison in a headline against the article and its primary sources. Avoid exaggerated promises or an added year that makes unchanged information look new. [Google recommends descriptive, concise titles](https://developers.google.com/search/docs/appearance/title-link); it sets no fixed character limit, and displayed titles may be shortened or rewritten.

The reading notes flag a few editing opportunities: long prose paragraphs, missing or skipped headings, and certain English hype or canned phrases. Their 90-word paragraph and 90-character title suggestions are local editorial heuristics, not search-engine rules. Code, quotes and simple table rows are excluded from prose-length notes. The checks do not assess fluency, prove factual accuracy or detect all formulaic writing. Add a useful observation, example or technical judgment you can stand behind. Never invent personal experiments to make a draft sound natural.

### Search and AI discovery

In **Prepare to publish → Search preview**, edit the search title and description. The initial description is extracted from the opening paragraph without inventing new facts; it can be blank when no suitable bounded opening exists. Keep its claim consistent with the article. Save the publication package to preserve these fields against that exact revision and hash. Editing metadata clears your fact-check attestation. The ZIP adds `discovery.json` with the selected metadata and reading notes. Unselected planning titles stay in the workspace; existing older packages retain their original contents.

Copy this metadata into your destination's settings where supported and inspect the preview there. These fields do not update Medium, LinkedIn, X or Google. Medium also offers a separate [display title/subtitle](https://help.medium.com/hc/en-us/articles/214895188-Custom-titles-subtitles) for previews; it can differ from the article heading. The publishing host controls its page metadata, crawling, indexing and structured data. Search snippets and titles can differ from your suggestions.

SEO, answer engine optimization (AEO) and generative engine optimization (GEO) share a practical foundation here: answer a real reader's question clearly, contribute useful analysis, identify technologies precisely, and make factual claims independently checkable. [Google's current AI-search guide](https://developers.google.com/search/docs/fundamentals/ai-optimization-guide) emphasizes original, useful content and ordinary SEO fundamentals. It does not require special AI markup, `llms.txt`, forced keyword variations or an ideal article length. [Bing's guidance](https://www.bing.com/webmasters/help/bing-webmaster-guidelines-30fba23a) likewise stresses clear structure, focused content and verifiable information. Inclusion, ranking, AI citations, views and clicks are not guaranteed.

For current developments, read the linked primary announcement or documentation again before posting. Distinguish publication, update, retrieval and event dates; fetching a page today does not make its claims new. Keep version numbers, benchmark conditions and vendor/preprint limitations intact. A saved assessment uses stored evidence; it does not refresh the web. Start a newly researched draft when the evidence needs refreshing.

### Technical article layout

Start with a direct, useful opening, then use descriptive sections and short paragraphs. Use lists for parallel checks or steps, inline backticks for identifiers, fenced blocks for code and emphasis sparingly. Label unexecuted code as illustrative. Keep important caveats beside the claims they qualify. Add a comparison only when it saves the reader work; avoid filler FAQs, repeated summaries and keyword stuffing.

The preview and portable HTML document support headings, bold/italic text, inline and fenced code, lists, quotes and basic pipe tables with 2–12 columns. Basic tables use a leading pipe and a separator row; embedded or escaped pipes and nested formatting are outside this small subset. Wide tables scroll within the preview. Raw authored HTML, scripts, images and embeds are never interpreted. In the workspace, only validated source-ledger links become clickable.

Use the rendered HTML export as a copying aid, then check the destination editor. [Medium's editor](https://help.medium.com/hc/en-us/articles/215194537-Using-the-story-editor) supports headings, emphasis, lists, quotes and code, but pasting does not guarantee identical formatting. Reformat complex tables manually, or use a compact list. Keep AI-assistance disclosure visible and review the final draft on the platform before publishing.
