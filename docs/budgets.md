# Call limits, spending and retry behavior

Fieldnote authorizes each physical model request before contacting its
provider. PostgreSQL stores that authorization and the maximum reserved spend.
Multiple API or worker processes consume the same ledger rather than separate
in-memory allowances.

Treg research also reserves each outbound attempt in PostgreSQL. Research has
its own request quota and extraction URL quota and shares the run and user
spending limits with model calls. These limits are application defaults. Set
them to match the limits of the configured route and account. Workers sharing
the database use one Treg quota scope. Synthetic research uses no allocation.

## What the limits mean

- A run may authorize at most the configured model-call count, initially 32.
  Retries and repair calls count again.
- A user also has a daily request ceiling, initially 100 in the dispatch service.
  This is an application cap, not a claim about a provider's free allowance.
- Provider request/token limits use a shared rolling 60-second window. The
  provider scope must identify all workers using the same configured credential
  and quota bucket. Never use a fresh random scope to bypass throttling.
- Token reservations include the conservative **input plus output** bound. A
  model's output-token parameter alone is insufficient. Until actual usage is
  known, the full reserved token count remains in the window.
- Run and user-day spending limits use Decimal values; zero forbids paid
  dispatch. An unknown price is refused instead of interpreted as zero. Monetary
  amounts are conservatively rounded up to the ledger's eight decimal places.
- User daily counters reset at UTC midnight. This accounting boundary is
  explicit and does not depend on a worker machine's local timezone.

Fixture requests are synthetic, cost zero and bypass real-provider RPM/TPM.
They still consume run/user call counts so offline tests exercise the call
ceiling. Fixture behavior does not establish a live account's quota or bill.

## One authorization per outbound attempt

Each attempt has a stable call identity scoped to its run. Repeating the same
reservation returns `dispatch_allowed=false` without consuming another count;
it must **not** send the request again. Changed estimates or provider scope for
the same identity are conflicts. A deliberate retry uses a new identity and
reserves capacity again. A checkpoint or reservation does not establish
exactly-once remote processing or billing.

Admission checks active run state, deadline, unexpired lease, ownership
generation and advisory-lock session under a transaction. Cancellation prevents
new authorizations. Already authorized/in-flight work may still complete; late
responses cannot grant permission for new generated output.

## Unknown outcomes and reconciliation

A timeout, connection loss or cancellation can leave the provider outcome
unknown. Keep the reservation. Do not release it merely because the browser or
worker stopped waiting.

Reconciliation records known actual cost/tokens and safe provider identifiers.
It can run after cancellation or lease loss because recording an existing bill
does not authorize generation. Unknown values remain null while the reservation
continues to count. Repeated identical reconciliation is idempotent; conflicting
known figures are rejected. A late unknown update cannot erase known usage.

An unresolved research charge or a charge over the route cap pauses new Treg
dispatches across workers. The hold survives midnight while the reservation is
retained. An expired lease makes an unfinished reservation eligible for this
hold. A confirmed provider receipt can resolve an unknown charge. A recorded
overrun keeps the hold for the lifetime of its accounting record. Run retention
governs the lifetime of these records.

If reported usage or cost exceeds the reservation, the service records it and
returns `reservation_overrun=true`; operators must investigate the estimate or
provider behavior. Subsequent admission uses recorded actual spend. The service
does not discard the bill to make a local budget appear satisfied. Price
qualification, alert activation and external billing reconciliation remain
release gates.

## Retry and circuit policy

Saved-revision reviews have separate run identities and share the owner's
daily quota and queue limits. Each review allows at most two model requests,
including a schema repair, with no scheduled provider retry. Reclaiming an
interrupted review after a dispatch does not authorize a duplicate request;
the user must explicitly request a new review. Reviews do not perform research
or rewrite the article.

Content acceptance and billing are separate. A structured AI response that
fails validation is a failed output, while its reported usage and cost are
still recorded when available. A known zero charge can be reconciled; missing
cost retains the conservative reservation. Rejected content never becomes an
article merely because its billing is known.

The dispatch service provides one retry decision; gateways do not add their own
retries. The default maximum is three attempts, including the first. Transient
errors use bounded exponential delay/jitter and honor a valid provider wait.
Authentication, policy, schema and budget failures do not become retry storms.
No retry may exceed remaining call count or the accepted-at run deadline.
Treg rate limits with a known charge share their `Retry-After` cooldown across
workers. A successful request that was already in flight leaves that cooldown
in effect.

Long waits belong in the durable `retry_wait` scheduler state, not a sleeping
HTTP handler or occupied worker slot. Three consecutive transient failures
initially open the shared provider circuit for 30 seconds. Duplicate accounting
does not multiply circuit failures. A later successful response resets the
failure sequence; the next failure after an expired open interval may reopen it.

## Verification boundary

The service has real PostgreSQL tests for concurrent spend reservations,
duplicate identity, rolling token/request quotas, UTC daily limits, unknown
charges, cancellation/connection-loss accounting, and circuit state. A test
launches two independent Python processes against one quota scope and verifies
that only one request is admitted. No real model request or provider charge is
created by these tests.

Worker wiring and deployment measurements must still be evidenced separately.
Select actual limits from the qualified provider account; do not copy the
service's conservative defaults as provider facts.
