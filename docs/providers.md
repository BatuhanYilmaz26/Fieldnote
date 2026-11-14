# Model and research providers

Fieldnote keeps provider selection in server configuration. Your browser never
receives an API key or selects a provider URL. Fixture mode uses deterministic
synthetic content and makes no model or research network requests.

## Local fixtures

Use fixtures to develop the article workflow without credits or provider
accounts. Fixture articles and sources are labeled synthetic. They demonstrate
the workflow; they are not researched articles or evidence of writing quality.

`FixtureGateway` accepts a mapping from call identity or schema name to a JSON
payload. It validates that payload through the same Pydantic schema used by the
live adapter. `FixtureResearchGateway` accepts a local corpus of public-shaped
URLs, titles and text. Its fixed retrieval time and hashes make replay tests
reproducible. An empty corpus remains empty evidence.

## OpenRouter

The model adapter sends structured, nonstreaming chat requests to
`https://openrouter.ai/api/v1/chat/completions` through one reusable HTTPX client.
The workflow can still stream progress to the browser. Provider responses are
bounded to 1 MiB, redirects are disabled, and returned JSON must satisfy the
application model and its business validation.

Free-only configuration accepts `openrouter/free` or explicitly configured
`:free` model IDs. Requests also carry a zero maximum price for prompt,
completion and per-request charges. It never substitutes a paid model. Requests require parameter
support and deny provider data collection; no route satisfying that policy is a
visible provider failure, not permission to weaken the policy. The requested
router and actual returned model remain separate metadata.

Budgeted configuration requires explicit model/provider allowlists and a
positive reservation amount supplied by the trusted worker dispatch service.
The adapter does not create a durable reservation or decide that a price is
known. Before enabling paid calls, verify shared budget admission and record
successful account qualification for that configuration.

To check the current UTC day's free request allowance without sending a model
prompt or consuming an inference request:

```powershell
.\.venv\Scripts\python.exe -m fieldnote.providers.qualification --env-file .env --quota-only
```

The result shows OpenRouter's daily request counter when the account returns it.
A model endpoint can still return a rate-limit error with daily requests
remaining. See [OpenRouter's rate limit documentation](https://openrouter.ai/docs/api_reference/limits).

Failures use public-safe codes. The scheduler owns retry/backoff/deadline
decisions; this transport never retries by itself. A rate-limit error preserves
`Retry-After` where valid. `MODEL_TIMEOUT_SECONDS` caps the entire inference
request, including a response that keeps sending small chunks. A timeout can mean the remote request completed, so
the caller must retain its reservation. Missing usage or cost remains unknown,
not zero. Local cancellation does not prove remote processing or billing stopped.

### When an AI response is rejected

An HTTP success does not guarantee an acceptable article. Fieldnote checks
the response envelope, JSON fields and application rules, including section
budgets that must add up to the requested total. Invalid content is rejected.

The local call record retains available model/provider attribution, request ID,
token counts and reported cost even when content validation fails. Its
`validation_kind` distinguishes invalid JSON (`json`), field or type constraints
(`schema`), application rules (`semantics`) and the response wrapper (`envelope`).
`schema_name` identifies the expected contract. These categories contain no
generated text, field values or raw validation errors. They help investigate
future failures; they cannot recover information discarded by an older version.

A failed output can still consume quota or incur a reported charge. Missing
usage or cost stays unknown. The configured schema-repair allowance remains
bounded and each repair requires its own quota reservation. A reported charge
on a free-only route stops the run before any repair is attempted.

### Check your OpenRouter account

Keep your key in an ignored file or `OPENROUTER_API_KEY`; never pass it on the
command line. After setting `PYTHONPATH` to the project's `src` directory, run:

```powershell
.\.venv\Scripts\python.exe -m fieldnote.providers.qualification --env-file .env --output .local\openrouter\qualification.json
```

The check reads only `OPENROUTER_API_KEY` from the selected file, captures an
allowlisted quota summary, and sends one synthetic strict-schema request through
the production adapter. It makes no paid fallback and sends no project content.
It can consume one free request. It never retries, weakens data policies, or
enables live providers automatically. Use a new report filename for each check.

An exit code of zero means the probe returned valid structured output with model
and provider attribution, usage and a reported cost of exactly zero. Unknown
cost, no eligible route, authentication or quota errors fail the check. Inspect
the local report before choosing a live application profile. A successful probe
does not qualify article quality, every model's schemas, or continued account
capacity. Contract tests use HTTPX's in-memory transport and remain distinct
from authenticated qualification.

If the check returns `provider_rate_limited`, stop and inspect
`retry_after_seconds` in the report. Wait at least that long when supplied;
otherwise wait before choosing another manual check. A reported remaining daily
quota does not guarantee that an eligible upstream model has capacity. Do not
run a polling loop or change models to work around a failed check.

The free router chooses among eligible models and filters for requested
capabilities, including structured output. A later request can use another
model. Keep the returned model and provider with each result, and evaluate real
article revisions separately, including fluency in the selected language.

Official references, consulted 25 October 2026:

- [Free router selection and capabilities](https://openrouter.ai/docs/guides/routing/routers/free-router)
- [Structured output and endpoint support](https://openrouter.ai/docs/guides/features/structured-outputs)
- [Provider routing and data policies](https://openrouter.ai/docs/guides/routing/provider-selection)
- [Account limits](https://openrouter.ai/docs/api_reference/limits)
- [Pydantic validation errors](https://docs.pydantic.dev/latest/errors/errors/)
- [HTTPX async client lifecycle](https://www.python-httpx.org/async/)

## Treg

Treg is the implemented research service. The worker enables it only with
`RESEARCH_PROVIDER=treg`, `ENABLE_EXTERNAL_CALLS=true`, `TREG_QUALIFIED=true` and
`TREG_TOKEN`. The qualified route uses `tinyfish.web.search` and
`tinyfish.web.fetch`; free-only requests use a zero route-cost cap and disable
paid fallback. A paid or unknown charge is rejected. Fixture mode never calls it.
Unknown billing or a charge exceeding the route cap stops the entire research
collection before another query or page request can be sent. Ordinary missing
pages can still produce a bounded, explicitly incomplete evidence pack.

Shared quota and spend enforcement requires migration `0007_research_accounting.sql`.
Each request has its own billing receipt. Search counts toward request limits,
and extraction also counts toward the URL limit. See [budgets](budgets.md) for
configuration and unresolved charges.

The worker checks the run's cancellation state, deadline and execution lease
before each search or page extraction. Losing permission to continue stops
later research requests. A request already dispatched may still finish remotely;
cancellation does not establish that the provider stopped processing it.

Before enabling another account or changing the route, save that account's catalog
definitions and schema hashes, verify token/organization requirements, capture
sanitized successful and failed search/extract fixtures, and test routing cost
controls. A preferred free vendor is not a free-only guarantee. If no verified
zero-cost non-paid-fallback route is available, free-only research must stop.

Official references:

- [Treg REST reference](https://treg.to/docs)
- [Maintainer WebAccess instructions](https://github.com/superdesigndev/treg/blob/main/skills/web-access/SKILL.md)
- [Maintainer usage reference](https://github.com/superdesigndev/treg/blob/main/USAGE.md)

## Direct TinyFish

Set `RESEARCH_PROVIDER=tinyfish` to use TinySearch and TinyFetch directly. Put
`TINYFISH_API_KEY` in the private environment. Set `TINYFISH_FREE_QUALIFIED=true`
only after confirming the key's Search and Fetch rates and account limits.
Fieldnote uses this contract as the pricing basis and records connection
failures with no response as unknown outcomes. The connector in Codex and the
Fieldnote worker authenticate separately.

The worker sends language preferences to TinySearch and batches up to four
public URLs per TinyFetch request. PostgreSQL accounts separately for requests
and URLs. Publication dates and the origin's HTTP Last-Modified timestamp are
stored separately. A modification timestamp does not establish publication.

Set `TINYFISH_CACHE_SECONDS` to a value from 1 to 3600 to reuse a recent
extraction from another saved run by the same owner. The default is 0. Briefs
requiring current or sensitive facts bypass reuse. When only one URL needs
fetching and a recent snapshot has an ETag, Fieldnote can send a conditional
request. An unchanged response records a separate validation time and preserves
the excerpt and its original retrieval time. Conditional requests still consume
request and URL quota.

[Pricing](https://www.tinyfish.ai/pricing) · [Search API](https://docs.tinyfish.ai/api-reference/search-the-web) · [Fetch API](https://docs.tinyfish.ai/api-reference/fetch-and-extract-content-from-urls)

## Source links and citations

Generated drafts use `[@source_id]` markers. Fieldnote resolves them from its
source ledger; models cannot invent clickable citation URLs. Deterministic
checks reject unknown/malformed markers, unextracted sources, authored links,
images and raw HTML. These checks establish reference identity, not whether the
source supports the claim. Claim-to-excerpt review remains a separate gate.

Source links must use public DNS HTTP(S) hostnames and standard ports, without
credentials, local hostnames, IP literals or markup/control characters. DNS
preflight additionally rejects any private/non-global resolved address. This is
not a direct fetcher: direct page fetching remains disabled until connection-time
and redirect-level egress enforcement is qualified. DNS preflight alone does
not prevent rebinding.

Citation rendering preserves source title, ledger URL and retrieval time. Human
edits must not inherit the previous generated revision's review approval.

New excerpt IDs include their source, exact content hash and locator. Identical
text at two URLs retains both source associations. Previously saved excerpt IDs
remain readable; this identity scheme does not rewrite existing revisions.
