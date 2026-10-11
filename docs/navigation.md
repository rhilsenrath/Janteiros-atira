# Shared navigation supervision

Tender, Supplier, ERP and Mail retain their existing agents, contracts and browser assignments.
`SupervisedNavigator` wraps the existing navigator interface. It also intercepts ERP helper actions and
checks each row toggle in a model-selected `reveal` operation. Business extraction and agent scheduling
are unchanged.

After every action the supervisor takes a fresh bounded observation. It checks text, live control state,
DOM text, images' accessible text, and downloads. A changed URL alone is insufficient. Form operations must
produce the requested field value, checked state or selected option; unrelated text changes cannot prove
a successful fill. Repeated cached documents and renewed citation IDs do not create progress.

Normalized state ignores row ordering, display clocks, ephemeral target IDs and known session/cache URL
parameters. Business identifiers, record dialogs and field values remain significant. Revisiting already
seen states does not reset the no-progress budget, so alternating routes are detected. Controls are
revalidated immediately before execution, including row identity, visibility, disabled/ARIA-disabled
state, read-only state and credential field type. A DOM index moving to another identical table button
is refused.

Each agent retains an in-memory checkpoint with objective, phase, navigation stage, URL, normalized last
verified state, a bounded page summary, completed steps, evidence references, and remaining questions.
The LLM receives that context with recent outcomes and failed controls on every navigation/authentication
decision. No browser destinations or documents are invented by the supervisor.

## Recovery and budgets

- Two failed attempts at an action veto further repetition and request an alternative strategy.
- Four stagnant outcomes require a checkpoint/different-route recovery action.
- The default budget allows two such recovery attempts and ten stagnant outcomes before blocking.
- An unexpected login form preserves the business checkpoint and uses existing runtime credential
  injection. Rejected credentials stop authentication; they are never treated as successful login.
- Session recovery is limited to two attempts. After login, restoration is verified separately; the LLM
  has four observed actions to restore the saved page. An unresolved changed checkpoint blocks with
  collected evidence retained.
- Defaults cap supervised actions at 160, each action and observation at the existing action timeout,
  and each model request at 60 seconds. Existing authentication/navigation step bounds remain in force.
  The invocation budget also respects the Atira expiry and submission reserve. All budgets are configurable
  in `RunConfig`; retries cannot run past the scored deadline.

Outcomes distinguish authentication failure, session expiry, blocked navigation, missing records, timeouts,
extraction errors and deadline exhaustion. Structured diagnostics log operation/category, transitions,
recovery counts and evidence counts. They exclude page text, control labels, query values, URLs,
credentials and tokens. Checkpoint page summaries stay in memory/model context and are redacted before
model requests; they are not emitted to logs.

## Limits

The supervisor verifies mechanical progress, not the business correctness of an LLM-selected record or
section. Existing agents must still match identifiers and validate extracted facts. Site outages, rejected
runtime credentials, inaccessible records and inaccessible human verification can remain blocked. A changed
page that cannot be verified against its checkpoint requires fresh investigation instead of silently
assuming restoration. Checkpoints survive follow-ups within the same execution, not process restarts.

Local fault simulations and browser integration tests verify these mechanisms. They do not establish a
successful live Atira extraction or submission; no scored `atira test-run .` was performed for this change.
