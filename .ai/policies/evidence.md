# Evidence Policy

Load when verification is nontrivial, a known repro/negative exists, identity/provenance matters, or evidence may be expensive/stale.

## Evidence order

Prefer direct authoritative evidence:

1. Exact triggering repro or acceptance check.
2. Focused tests/checks for changed behavior.
3. Relevant integration/runtime evidence.
4. Broader regression suites only when causal surface warrants them.

Do not substitute comments, logs, mocks, static inspection, or worker assertion for a required authoritative check.

## Causal reuse

Accepted evidence remains valid until a later delta can causally invalidate it. Session restart, model replacement, generation increment, or lost conversation alone does not invalidate evidence.

During requirement reconciliation, revalidate only impacted milestones and downstream invariants that depend on changed semantics.

## Adversarial checks

For bug fixes, reproduce the original failure when feasible and prove it no longer occurs. For negative/forbidden behavior, test the prohibited path directly when safe. For identity-sensitive work, bind evidence to the exact artifact/ref/runtime being accepted.

Do not weaken tests, gates, invariants, or acceptance criteria to create PASS.

## Expensive/contaminating checks

Run cheap/focused checks first. Bound expensive tests. Do not overlap checks that can contaminate each other through shared accounts, ports, devices, caches, fixtures, or mutable external state.

Repeat an expensive check only when prior evidence was invalidated or the method/state materially changed.

## Baseline failures

Unrelated pre-existing failures are not automatically task scope. Record enough evidence to distinguish baseline from regression. If baseline prevents proof, stop at that causal boundary rather than silently broadening the task.
