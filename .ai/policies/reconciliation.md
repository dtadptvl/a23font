# Reconciliation Policy

Load on Human changes, worker return/failure, recovery, Git/filesystem drift, stale result, or external divergence.

## Boundary algorithm

1. Read `.prime/state.yaml`, `.prime/plan.yaml`, and the current Human event/instruction.
2. Resolve active task/rev/dispatch/generation and current milestone/spec revision.
3. Inspect result/checkpoint as claims, not truth.
4. Inspect Git/worktree and relevant authoritative external state.
5. Classify deltas as task-owned, Human/external, unrelated, overlapping, or unknown.
6. Preserve all non-disposable deltas.
7. Update canonical state/contracts before new write delegation.
8. Use `ctl.py check` before declaring coherence.

## Human changes

Persist Human text with `ctl.py human-change`. Increment milestone `spec_rev` only where milestone semantics changed. Use `ctl.py impact --apply` from semantic roots after the plan is updated.

Impact is causal closure, not automatic rewrite. Root accepted realization is invalidated when the root semantic contract changed. Downstream accepted realizations remain current until revalidation proves otherwise.

Revalidate upstream to downstream. If a downstream realization remains semantically valid, keep it and mark that milestone done. If not, invalidate it, then recontract/implement/reverify.

Do not rewrite historical terminal tasks.

## Active tasks across generation changes

A Human change makes an active task's generation stale until Prime causally revalidates it.

- WHAT unchanged: update `validated_generation`; retain `contract_rev`.
- WHAT/scope/acceptance/dependencies/required architecture changed: increment `contract_rev` and bind the current milestone `spec_rev`.
- Replacement worker: always issue a new `dispatch_id`.

A current task must not proceed while one of its transitive impacted dependencies remains pending or invalidated.

## Stale and duplicate workers

Promotion requires exact current task id + rev + dispatch. Late old-dispatch output is stale evidence/source delta, never a winner by timing.

If a stale worker changed source, preserve useful delta for inspection, but do not let stale task files overwrite canonical state.

## Interrupted workers

No valid current completed result means not accepted. Recover from task + current-rev checkpoint + Git. Reuse proven partial work only when still causally valid.

## Filesystem/Git drift

A first dispatch rejects dirty paths overlapping task scope. Non-overlapping dirty paths are fingerprinted and protected. Drift in protected state during a dispatch is a reconciliation boundary.

If Human/external edits overlap active task scope, preserve both deltas, invalidate/recontract as necessary, and continue from present reality. Never discard one side to obtain a clean tree.

## Completion reconciliation

Worker `completed` is a claim. Prime verifies semantic evidence and actual diff. `ctl.py accept` then enforces structural identity/scope/dependency/diff invariants and updates milestone realization when the task proves the complete current acceptance set.
