# Dependency-aware RFQ workflow

`core/orchestrator.py` owns all scheduling and mutable run state. Agents keep their existing
`execute(AgentTask, AgentContext)` interface and assigned browser sessions. The constructor's
legacy planner argument is retained for compatibility; stage barriers no longer control execution.
No production task reads saved run results or uses fixture business values.

## Events and gates

All four website workers start together. Their existing website loop authenticates, then calls
`context.get_task()` before business actions and completion checks. That callback waits on partial
result events, releases the browser for no additional work while waiting, and returns an isolated
snapshot. It includes outstanding targeted requests for that worker.

| Event | Subsequent work |
| --- | --- |
| Tender publishes specification/requirement references | Supplier extracts referenced specifications and revisions |
| Tender publishes its exact reference | Mail investigates relevant commercial messages |
| Supplier publishes revision references | Mail investigates revision-specific technical releases |
| Mail publishes a technical release, or Tender publishes an amendment | Existing specification resolver recomputes effective requirements and conversions from collected records |
| A position has verified quantities and all five effective attributes plus Kataloggruppe | ERP receives that position and its requirements; unresolved positions are excluded |
| ERP publishes FX or metal-index keys | Mail receives updated references, including after an earlier investigation finished |
| At least one position has complete ERP coverage and commercial evidence exists | Pricing reevaluates the full portfolio while other workers can continue |
| Any worker reports missing information | Orchestrator coalesces requests by destination; running workers receive them at their next snapshot, otherwise a bounded follow-up is scheduled |
| Validated complete bid | Mail is asked for the signing TAN; only successful retrieval permits the submission service to delegate to Tender |

Production ERP gates require Tender Q&A coverage, positive quantity and unit, position and requirement
provenance, the governing revision, all effective attributes, and a current positive pieces conversion.
Usable travel without its per-end reserve cannot pass. Conflicting technical/tender records block matching.
A searched catalogue group provides coverage only when every relevant candidate is sufficiently identified
and every active exact match has complete material, inventory and pricing sections. An observed exhausted
group with no matching active candidate can also pass to Pricing for a no-bid decision.

## Incremental state and bounded work

`RunState.position_statuses` exposes dependency status per position, alongside the existing worker statuses.
The coordinator distinguishes pending, running, blocked, complete and failed; partial extraction records
remain valid shared contracts. Mandatory gaps and conflicts downgrade completion claims. Completed ERP
batches merge across positions instead of erasing earlier materials.

One job owns each worker/browser at a time. Relevant upstream input fingerprints schedule new investigations;
publication of a worker's own output does not itself restart it. Equivalent citations with renewed IDs or
observation timestamps do not create new work. Identical requests are sent once per input version, and
`max_reinvestigations` bounds additional request rounds. New keys can still trigger new work. Failures retain
the existing bounded retry policy. Upstream exhaustion produces blocked tasks instead of endless waiting.

Pricing always sees the full tender and recomputes the portfolio. Completed position prices remain provisional
until the full bid is validated: shared stock, setup pools and bonuses cannot safely be priced independently.
A changed input snapshot invalidates a previously calculated bid for final submission.

Investigation stops before the execution deadline by `deadline_reserve`. All pending jobs are cancelled and
awaited before sessions close. Signing retrieval uses at most half the remaining execution time, leaving time
for the final submission. Session initialization and worker invocations retain their existing timeouts.

## Limits and validation

The existing Mail agent still blocks `retrieve_signing_token`, and Tender's actual configurator filling,
signing and receipt confirmation remain unimplemented. Coordination now routes these phases and blocks
submission if unavailable. This change does not implement those browser workflows or repair the existing
ERP navigation/extraction limitations.

Unit tests exercise concurrency, per-position gates, partial results, release resolution, targeted request
routing, duplicate suppression, provenance/conflicts, blocked upstreams, failure/retry bounds, deadline cleanup,
and signing order. Local browser/HTTP integration and lifecycle tests remain compatible with the existing
Atira adapter. A real `atira test-run .` is still required to establish end-to-end challenge performance;
passing local tests does not establish complete live ERP extraction or successful submission.
