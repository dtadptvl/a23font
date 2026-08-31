# Prime Lean v2

Local-first Kilo template for greenfield vibe coding with one persistent primary orchestrator and disposable context-isolated workers.

> Agents are disposable. Project state is persistent.

v2 keeps the AI model assignments from Prime Lean v1 while replacing the orchestration layer with a canonical intent graph plus deterministic reconciliation/state-machine guards.

## Included

- `core.md`: canonical lean governance kernel.
- `.ai/policies/`: lazy execution, evidence, reconciliation, and safety modules.
- `.ai/tools/ctl.py`: dependency-free bootstrap, governance hash, intent-event, impact, reconciliation, dispatch, validation, recovery, and acceptance control tool.
- `.ai/tools/test_prime_lean_v2.py`: full template suite.
- `.ai/templates/prime-memory/`: canonical state/plan/task/checkpoint/result/ADR templates.
- `.kilo/agents/prime.md`: persistent primary role.
- `.kilo/agents/worker-fast*.md`: retained fast Gemini/Qwen runtimes.
- `.kilo/agents/worker-deep*.md`: retained deep Qwen/Gemini runtimes.
- `.kilo/agents/inspector.md`: retained read-mostly Gemini inspector.

## Persistent project state

Bootstrap creates:

```text
.prime/
  state.yaml        # hot orchestration frontier only
  plan.yaml         # canonical objective + milestone/spec/dependency/acceptance graph
  events.jsonl      # lossless Human intent events
  decisions/
  tasks/
    T-xxx/
      task.yaml
      checkpoint.yaml   # optional current-contract recovery boundary
      result.yaml       # current dispatch claim
```

There is no separate roadmap, bootstrap-memory file, chat recap, or model-memory file.

## Start in a local folder

Copy the template into the project folder and start Kilo with `prime`. Prime bootstraps automatically. Deterministic equivalent:

```sh
python3 .ai/tools/ctl.py bootstrap --init-git
python3 .ai/tools/ctl.py resume
```

If Git is absent, bootstrap initializes it and sets repository-local fallback identity only when needed. It deliberately does **not** auto-create the first source commit. Prime must inspect staged names/diff and exclude secrets/generated/transient material first.

After recording the initial Human intent, update `.prime/plan.yaml`, apply impact roots, and reconcile:

```sh
python3 .ai/tools/ctl.py human-change --text "<lossless Human instruction>"
python3 .ai/tools/ctl.py impact --roots M1 --apply
python3 .ai/tools/ctl.py check
```

## Core identities

- `generation`: one Human-intent epoch.
- milestone `spec_rev`: semantic revision of one milestone.
- task `contract_rev`: semantic revision of one task contract.
- `dispatch_id`: one disposable worker incarnation.

A late result is promotable only when task + contract rev + dispatch id match the current task.

## Requirement changes in long projects

When Human changes an earlier milestone:

1. `human-change` persists the instruction and increments generation exactly once.
2. Prime edits the affected milestone semantics and increments its `spec_rev`.
3. `impact --roots ... --apply` computes deterministic downstream closure and invalidates only semantic roots that previously had accepted realizations.
4. Revalidate pending milestones upstream to downstream. Downstream work is suspect, not automatically discarded.
5. Keep causally valid accepted realizations. Invalidate only those proved stale.
6. Recontract/redispatch only where semantics changed.
7. `reconcile --clean` only when pending/invalidated frontier is empty.

This lets an M10 session recover from an M1 change without reopening unrelated milestones or relying on predecessor agent memory.

## Single writer and dirty-tree safety

The default architecture has exactly one mutation lease: `state.active_task`. No parallel writers/worktrees are part of the default contract.

On first dispatch, `ctl.py dispatch`:

- requires a committed Git baseline;
- rejects pre-existing dirty paths inside task scope;
- fingerprints unrelated dirty paths as protected Human/external state;
- binds current governance/generation and issues the next dispatch id.

On acceptance, `ctl.py accept` verifies the protected dirty fingerprint, actual task delta, result changed-path claim, acceptance refs, dependency readiness, generation, governance, and dispatch identity.

## Bounded recovery/retry

Task liveness persists only the causal minimum: same-boundary failure count and last failure class/boundary. A replacement worker gets a new dispatch id. A compatible checkpoint may survive failover through `source_dispatch`.

A second failure at the same causal boundary exhausts blind retry. The next action must split, recontract, or block.

## Useful commands

```sh
python3 .ai/tools/ctl.py hash
python3 .ai/tools/ctl.py check
python3 .ai/tools/ctl.py resume
python3 .ai/tools/ctl.py human-change --text "..." --source-ref "..."
python3 .ai/tools/ctl.py impact --roots M1 --apply
python3 .ai/tools/ctl.py reconcile --done M1,M3
python3 .ai/tools/ctl.py reconcile --invalidate M5
python3 .ai/tools/ctl.py reconcile --clean
python3 .ai/tools/ctl.py dispatch T-001
python3 .ai/tools/ctl.py accept T-001
```

## Full suite

```sh
python3 .ai/tools/test_prime_lean_v2.py
```

The suite covers retained models, greenfield bootstrap, canonical plan/event generation, graph/cycle/reference enforcement, single-writer ownership, stale results, accepted-result invariants, scope/diff guards, dirty-tree protection, bounded retries, impact closure, reconciliation invalidation, and recovery capsule identity.
