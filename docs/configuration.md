# Configure Fieldnote

Fieldnote starts with a local, synthetic-provider profile. It does not contact a model or research vendor with the default settings. Synthetic sources and generated text are development fixtures, not real research.

Configuration is read by `fieldnote.settings.Settings`. Set environment variables before starting the API or worker. Environment values override an explicitly loaded dotenv file. Dotenv files are **not** discovered automatically; the launcher must request the file or you may pass `Settings(_env_file=".env")` in a Python integration. Keep `.env` and credentials out of Git.

## Local development

The defaults select `APP_ENV=development`, `DEVELOPMENT_MODE=true`, `AUTH_MODE=local`, `MODEL_PROVIDER=fixture`, `RESEARCH_PROVIDER=fixture`, and `BIND_HOST=127.0.0.1`. Local authentication is a single developer identity, not an account system. It is rejected on public bind addresses and in staging/production. Do not override the ASGI server's bind independently of the validated settings.

Use a PostgreSQL runtime URL supplied by your local database setup. `DATABASE_URL` has no embedded usable default. `MIGRATION_DATABASE_URL`, when used by the migration command, identifies a separate role with schema privileges. Both are treated as secrets; use a real secret store for hosted deployments.

PowerShell environment examples:

```powershell
$env:APP_ENV = "development"
$env:DEVELOPMENT_MODE = "true"
$env:BIND_HOST = "127.0.0.1"
$env:MODEL_PROVIDER = "fixture"
$env:RESEARCH_PROVIDER = "fixture"
$env:ENABLE_EXTERNAL_CALLS = "false"
```

Follow [setup and operations](setup-and-operations.md) for database creation and application startup. These commands only configure the current PowerShell session; they do not create a database or enable a provider.

## Provider enablement

The names `OPENROUTER_API_KEY`, `TREG_TOKEN`, and `TAVILY_API_KEY` are canonical. The former `OPENROUTER_KEY` name is not an alias. A stored key alone does not enable a network call.

| Selection | Configuration requirements |
|---|---|
| `MODEL_PROVIDER=openrouter` | `ENABLE_EXTERNAL_CALLS=true`, `OPENROUTER_QUALIFIED=true`, nonblank `OPENROUTER_API_KEY` |
| `RESEARCH_PROVIDER=treg` | `ENABLE_EXTERNAL_CALLS=true`, `TREG_QUALIFIED=true`, `TREG_TOKEN`, and `TREG_ORG` where the qualified token requires it |
| `RESEARCH_PROVIDER=tinyfish` | `ENABLE_EXTERNAL_CALLS=true`, `TINYFISH_API_KEY` and `TINYFISH_FREE_QUALIFIED=true` after confirming that Search and Fetch are free for that key |
| `RESEARCH_PROVIDER=tavily` | The adapter remains available for qualification. The worker refuses to dispatch it until it has a qualified billing contract. |

Set qualification flags only after recording your account's successful checks
from the [provider guide](providers.md). The flags are not evidence themselves
and must not be flipped just to bypass validation. Recheck exact Treg schemas
and cost controls, and account-specific OpenRouter capabilities when changing
accounts or routing policy.

`OPENROUTER_BASE_URL` is fixed to `https://openrouter.ai/api/v1` and `TREG_BASE_URL` to `https://treg.to`. User requests cannot choose these origins. `OPENROUTER_MODEL` must occur in `MODEL_ALLOWLIST`. The default model policy is `free_only`; only `openrouter/free` or explicit `:free` model IDs are permitted under that policy. This string check does not qualify account availability or guarantee a zero total operating cost.

Budgeted model or Treg dispatch requires positive `RUN_BUDGET_USD` and `USER_DAILY_BUDGET_USD`. For Treg, set `TREG_ROUTING_POLICY=budgeted` and a qualified `TREG_MAX_COST_MICRO` in millionths of a US dollar. The worker reserves this per-request maximum against the same run and daily spend limits as model calls. Free-only Treg uses a zero cap.

## Hosted configuration

Staging and production reject local authentication, development mode, fixture providers, absent `DATABASE_URL`, and the test-only host `testserver` in `ALLOWED_HOSTS`. Select `AUTH_MODE=oidc`, supply `OIDC_ISSUER` as an HTTPS issuer, `OIDC_CLIENT_ID`, and a `SESSION_SECRET` of at least 32 characters generated and stored securely. The identity implementation must separately validate the real protocol/account configuration before hosted acceptance.

`ALLOWED_HOSTS` and `ALLOWED_ORIGINS` are JSON arrays in environment variables, for example:

```powershell
$env:ALLOWED_HOSTS = '["writer.example.org"]'
$env:ALLOWED_ORIGINS = '[]'
```

The example hostname is illustrative. Use your owned deployment host. Same-origin deployment needs no cross-origin allowances. Wildcard hosts/origins are rejected. If you choose cross-origin browser use, list exact trusted origins and qualify credentialed CORS separately.

## Bounds and operational controls

| Setting | Default | Meaning |
|---|---|---|
| `MAX_RUN_SECONDS` | 600 | Deadline from acceptance, including queue/retry time |
| `MAX_MODEL_CALLS` | 32 | Total model attempts, not only successful calls |
| `MAX_REVIEW_ROUNDS` | 2 | Maximum correction batches after initial draft |
| `MODEL_MAX_CONCURRENCY` | 3 | Process execution bound; not a shared RPM/TPM quota |
| `PROVIDER_RPM` / `PROVIDER_TPM` | 1 / 16000 | Conservative admission defaults to replace only with qualified capacity |
| `MAX_EVIDENCE_TOKENS` | 12000 | Estimated total excerpt-text budget; oversized excerpts are omitted with a research limitation |
| `MAX_RESEARCH_CALLS` | 12 | Research requests per run, including retries |
| `RESEARCH_PROVIDER_RPM` | 12 | Shared rolling 60-second research request ceiling |
| `RESEARCH_PROVIDER_DAILY_REQUESTS` | 100 | Shared provider request ceiling per UTC day |
| `RESEARCH_PROVIDER_DAILY_URLS` | 100 | Shared extraction URL ceiling per UTC day |
| `RESEARCH_USER_DAILY_CALLS` | 100 | Per-user research requests per UTC day |
| `MAX_USER_NONTERMINAL_RUNS` / `MAX_GLOBAL_NONTERMINAL_RUNS` | 2 / 100 | Include retry waits and unacknowledged cancellations |
| `JOB_LEASE_SECONDS` / `JOB_HEARTBEAT_SECONDS` | 90 / 20 | Heartbeat must be less than half the lease |
| `DATABASE_POOL_MIN_SIZE` / `DATABASE_POOL_MAX_SIZE` | 1 / 5 | Per-process pool bounds; budget all replicas and dedicated lock sessions |
| `MAX_REQUEST_BYTES` | 32768 | Application body limit; enforce at ingress too |
| `RUN_RETENTION_DAYS` | 30 | Initial run/article/checkpoint policy |
| `EVENT_RETENTION_DAYS` / `SOURCE_RETENTION_DAYS` | 7 / 7 | Cannot exceed run retention |
| `LANGGRAPH_STRICT_MSGPACK` | true | Restricted checkpoint types; disabling is rejected |

Settings validation rejects incompatible bounds, blank inputs, unsupported provider origins and unbounded review loops. Database startup, schema checks, quota reservations, cleanup and checkpoint recovery must still be exercised through the application; successful settings parsing does not verify them.

## Brief and research validation

Briefs require 1–500 topic characters, at most 4,000 instruction characters,
1–240 audience characters, 1–120 tone characters and 200–3,000 target words. The API retains its 1,200-word default for existing clients; the browser starts at 550. Optional technical briefs accept a versioned `technical-v1` editorial profile, a 1,000-character angle, 4,000-character author observations, up to four public reference URLs and selected publishing destinations. All fields are saved with the brief.
`language` accepts `en` (English, the default), `tr` (Turkish), `es` (Spanish),
`pt` (Portuguese) or `de` (German). Region-specific tags and other languages are
not enabled; put a regional preference in `instructions`. The language is part
of the request's idempotency identity and remains fixed for that saved run.
Other-language generated drafts require review of fluency and translated evidence
before publication; language selection does not certify a model's quality.
Unknown fields, client owner IDs, provider URLs and checkpoint IDs are rejected.
Numeric fields require JSON numbers rather than numeric strings or booleans.

`research_mode="required"` always requests research. `auto` retains research for explicit sources, current/sensitive facts, numbers, named products and ambiguous factual briefs. Clear creative/general explanations can avoid research. `off` accepts only clear creative/general briefs without those factual indicators. The rules deliberately err toward research: a numeric title, product name, or even a source request phrased negatively may need rewriting or research enabled. They do not claim to recognize every real-world entity or establish factual accuracy.

Accepted examples without research: “How forests grow” and “Write a short story about a dragon.” A brief about current pricing, numerical statistics, named software or medical guidance requires research. Instructions are evaluated with the topic, so they cannot silently add researched facts to an unresearched brief.

## Diagnosing configuration errors

Startup errors identify the setting or incompatible policy without displaying credential values. `Settings.sanitized_summary()` returns an allowlisted diagnostic summary; do not dump process environments or raw connection URLs into logs. If configuration fails, correct the named setting rather than disabling persistence, qualification or restricted deserialization.

The implementation follows the [official Pydantic models](https://docs.pydantic.dev/latest/concepts/models/), [validators](https://docs.pydantic.dev/latest/concepts/validators/) and [settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) interfaces. Keep your executed checks with your local operating records; configuration examples alone do not verify infrastructure or providers.

## Topic discovery

`TOPIC_DISCOVERY=fixture` is the default and uses no network. For on-demand publisher-feed ideas, set `TOPIC_DISCOVERY=public_feeds` and `ENABLE_EXTERNAL_CALLS=true`, then restart the native API. This requires no API key or hosting change. The supplied Compose profile fixes external calls to false for its fixture demonstration; enabling live feeds there requires an explicit API-service environment override as well. Model and research-provider settings remain independent. The four fixed HTTPS feeds use bounded responses, a ten-second per-feed timeout, no redirects or automatic retries, and a persisted five-minute refresh cooldown. No refresh runs on a schedule.

Publication drafts have a separate 128 KiB JSON envelope so escaped Unicode and emoji threads fit without increasing the writing-brief limit. LinkedIn drafts are bounded to 3,000 conservative UTF-16 units; each of at most five X posts must pass the versioned weighted 280-character check. Saved article edits retain their separate 1.25 MiB envelope and 200,000-character content limit.
