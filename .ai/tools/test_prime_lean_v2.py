#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

TEMPLATE_ROOT = Path(__file__).resolve().parents[2]
EXPECTED_MODELS = {
    "prime.md": 'model: "9router/qd/qmodel_38max"',
    "worker-fast.md": 'model: "9router/ag/gemini-3.7-flash-high"',
    "worker-fast-qwen.md": 'model: "9router/qd/qmodel_38max"',
    "worker-deep.md": 'model: "9router/qd/qmodel_38max"',
    "worker-deep-gemini.md": 'model: "9router/ag/gemini-3.7-flash-high"',
    "inspector.md": 'model: "9router/ag/gemini-3.7-flash-high"',
}


def clean_env() -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    return env


def cp_template() -> tuple[tempfile.TemporaryDirectory, Path]:
    td = tempfile.TemporaryDirectory()
    root = Path(td.name) / "project"
    shutil.copytree(TEMPLATE_ROOT, root, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return td, root


def ctl(root: Path, *args: str, expect: int | None = 0) -> subprocess.CompletedProcess[str]:
    p = subprocess.run(
        ["python3", str(root / ".ai" / "tools" / "ctl.py"), "--root", str(root), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=clean_env(),
    )
    if expect is not None and p.returncode != expect:
        raise AssertionError(
            f"ctl {' '.join(args)} rc={p.returncode}, expected {expect}\nSTDOUT:\n{p.stdout}\nSTDERR:\n{p.stderr}"
        )
    return p


def git(root: Path, *args: str, expect: int = 0) -> str:
    p = subprocess.run(
        ["git", *args],
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=clean_env(),
    )
    if p.returncode != expect:
        raise AssertionError(f"git {' '.join(args)} rc={p.returncode}\n{p.stdout}\n{p.stderr}")
    return p.stdout.strip()


def write_plan(root: Path, graph: dict[str, list[str]], spec_revs: dict[str, int] | None = None) -> None:
    spec_revs = spec_revs or {}
    lines = ['objective: "Ship the target"', "non_goals: []", "milestones:"]
    for mid, deps in graph.items():
        lines += [
            f"  {mid}:",
            f"    spec_rev: {spec_revs.get(mid, 1)}",
            f'    outcome: "Outcome {mid}"',
            "    acceptance:",
            f'      A1: "Acceptance {mid}"',
            f"    depends_on: {json.dumps(deps, separators=(',', ':'))}",
            "    accepted_ref: null",
        ]
    (root / ".prime" / "plan.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def init_project(graph: dict[str, list[str]] | None = None) -> tuple[tempfile.TemporaryDirectory, Path]:
    graph = graph or {"M1": []}
    td, root = cp_template()
    ctl(root, "bootstrap", "--init-git")
    ctl(root, "human-change", "--text", "Initial requirements", "--source-ref", "human:init")
    write_plan(root, graph)
    ctl(root, "impact", "--roots", "ALL", "--apply")
    ctl(root, "reconcile", "--done", ",".join(graph.keys()))
    ctl(root, "reconcile", "--clean")
    git(root, "add", ".")
    git(root, "commit", "-m", "baseline")
    return td, root


def task_path(root: Path, tid: str) -> Path:
    return root / ".prime" / "tasks" / tid / "task.yaml"


def make_task(
    root: Path,
    tid: str,
    milestone: str = "M1",
    spec_rev: int = 1,
    deps_m: list[str] | None = None,
    deps_t: list[str] | None = None,
    deps_d: list[str] | None = None,
    scope: str = "src/**",
    extra_policies: list[str] | None = None,
) -> None:
    deps_m = deps_m or []
    deps_t = deps_t or []
    deps_d = deps_d or []
    extra_policies = extra_policies or []
    d = root / ".prime" / "tasks" / tid
    d.mkdir(parents=True, exist_ok=True)
    text = f'''id: "{tid}"
status: "draft"
governance_hash: ""
contract_rev: 1
dispatch_id: 0
validated_generation: 0
objective_ref: "{milestone}"
objective_spec_rev: {spec_rev}
acceptance_refs: ["{milestone}.A1"]
scope:
  include: ["{scope}"]
  exclude: []
depends_on:
  decisions: {json.dumps(deps_d, separators=(',', ':'))}
  tasks: {json.dumps(deps_t, separators=(',', ':'))}
  milestones: {json.dumps(deps_m, separators=(',', ':'))}
recovery: "resume"
extra_policies: {json.dumps(extra_policies, separators=(',', ':'))}
forbidden: []
workspace:
  base_commit: null
  protected_dirty_paths: []
  protected_dirty_hash: null
liveness:
  contract_rev: 1
  same_boundary_failures: 0
  last_failure_class: null
  last_failure_boundary: null
'''
    (d / "task.yaml").write_text(text, encoding="utf-8")


def field_int(path: Path, key: str) -> int:
    m = re.search(rf"^{re.escape(key)}: (\d+)$", path.read_text(encoding="utf-8"), re.M)
    if not m:
        raise AssertionError(f"missing integer field {key} in {path}")
    return int(m.group(1))


def write_result(
    root: Path,
    tid: str,
    milestone: str = "M1",
    status: str = "completed",
    changed: list[str] | None = None,
    dispatch: int | None = None,
    failure_class: str | None = None,
    failure_boundary: str | None = None,
) -> None:
    changed = [] if changed is None else changed
    tpath = task_path(root, tid)
    rev = field_int(tpath, "contract_rev")
    dispatch = field_int(tpath, "dispatch_id") if dispatch is None else dispatch
    fc = "null" if failure_class is None else json.dumps(failure_class)
    fb = "null" if failure_boundary is None else json.dumps(failure_boundary)
    proved = [f"{milestone}.A1"] if status == "completed" else []
    text = f'''task: "{tid}"
contract_rev: {rev}
dispatch_id: {dispatch}
status: "{status}"
proved: {json.dumps(proved, separators=(',', ':'))}
changed: {json.dumps(changed, separators=(',', ':'))}
evidence: ["focused evidence"]
failure_class: {fc}
failure_boundary: {fb}
note: null
'''
    (tpath.parent / "result.yaml").write_text(text, encoding="utf-8")


def set_active(root: Path, value: str | None) -> None:
    path = root / ".prime" / "state.yaml"
    text = path.read_text(encoding="utf-8")
    replacement = "active_task: null" if value is None else f'active_task: "{value}"'
    text = re.sub(r"^active_task: .*$", replacement, text, flags=re.M)
    path.write_text(text, encoding="utf-8")


def set_task_status(root: Path, tid: str, status: str) -> None:
    path = task_path(root, tid)
    text = re.sub(r'^status: ".*"$', f'status: "{status}"', path.read_text(encoding="utf-8"), flags=re.M)
    path.write_text(text, encoding="utf-8")


def complete_task(root: Path, tid: str, milestone: str = "M1", changed_path: str | None = None, deps_m: list[str] | None = None) -> None:
    make_task(root, tid, milestone=milestone, deps_m=deps_m)
    ctl(root, "dispatch", tid)
    changed: list[str] = []
    if changed_path:
        path = root / changed_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{tid}\n", encoding="utf-8")
        changed = [changed_path]
    write_result(root, tid, milestone=milestone, changed=changed)
    ctl(root, "accept", tid)


class PrimeLeanV2Tests(unittest.TestCase):
    def test_01_structure_and_models_retained(self):
        self.assertTrue((TEMPLATE_ROOT / "core.md").is_file())
        self.assertTrue((TEMPLATE_ROOT / ".ai" / "tools" / "ctl.py").is_file())
        self.assertFalse((TEMPLATE_ROOT / ".ai" / "tools" / "statecheck.py").exists())
        self.assertFalse((TEMPLATE_ROOT / ".ai" / "templates" / "prime-memory" / "roadmap.yaml").exists())
        self.assertFalse((TEMPLATE_ROOT / ".ai" / "templates" / "prime-memory" / "BOOTSTRAP-TEMPLATE.md").exists())
        for name, needle in EXPECTED_MODELS.items():
            text = (TEMPLATE_ROOT / ".kilo" / "agents" / name).read_text(encoding="utf-8")
            self.assertIn(needle, text, name)
        prime = (TEMPLATE_ROOT / ".kilo" / "agents" / "prime.md").read_text(encoding="utf-8")
        self.assertNotIn("git worktree", prime)
        self.assertIn('"python3 .ai/tools/ctl.py*": allow', prime)

    def test_02_greenfield_bootstrap_creates_local_canonical_state_without_commit(self):
        td, root = cp_template()
        try:
            p = ctl(root, "bootstrap", "--init-git")
            self.assertIn("BOOTSTRAP_OK", p.stdout)
            self.assertTrue((root / ".git").exists())
            for rel in [".prime/state.yaml", ".prime/plan.yaml", ".prime/events.jsonl"]:
                self.assertTrue((root / rel).exists(), rel)
            self.assertEqual(git(root, "config", "--local", "user.name"), "Prime Agent")
            self.assertEqual(git(root, "config", "--local", "user.email"), "prime@localhost")
            phead = subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=root, env=clean_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertNotEqual(phead.returncode, 0)
        finally:
            td.cleanup()

    def test_03_governance_hash_is_narrow(self):
        td, root = cp_template()
        try:
            h1 = ctl(root, "hash").stdout.strip()
            (root / "README.md").write_text((root / "README.md").read_text(encoding="utf-8") + "\nnon-governance\n", encoding="utf-8")
            (root / ".ai" / "tools" / "ctl.py").write_text((root / ".ai" / "tools" / "ctl.py").read_text(encoding="utf-8") + "\n# tool-only\n", encoding="utf-8")
            h2 = ctl(root, "hash").stdout.strip()
            self.assertEqual(h1, h2)
            (root / "core.md").write_text((root / "core.md").read_text(encoding="utf-8") + "\nGovernance semantic change.\n", encoding="utf-8")
            h3 = ctl(root, "hash").stdout.strip()
            self.assertNotEqual(h1, h3)
        finally:
            td.cleanup()

    def test_04_human_event_generation_is_lossless_and_source_deduped(self):
        td, root = init_project()
        try:
            human = "Đổi yêu cầu M1: token rotate sau 15 phút"
            p = ctl(root, "human-change", "--text", human, "--source-ref", "human:2")
            self.assertIn("generation=2", p.stdout)
            events = [json.loads(x) for x in (root / ".prime" / "events.jsonl").read_text(encoding="utf-8").splitlines() if x]
            self.assertEqual(events[-1]["text"], human)
            self.assertEqual(events[-1]["generation"], 2)
            ctl(root, "human-change", "--text", human, "--source-ref", "human:2", expect=3)
        finally:
            td.cleanup()

    def test_05_plan_graph_missing_dependency_and_cycle_are_rejected(self):
        td, root = init_project()
        try:
            write_plan(root, {"M1": ["M404"]})
            p = ctl(root, "check", expect=2)
            self.assertIn("MILESTONE_DEP_MISSING", p.stdout)
            write_plan(root, {"M1": ["M2"], "M2": ["M1"]})
            p = ctl(root, "check", expect=2)
            self.assertIn("MILESTONE_CYCLE", p.stdout)
        finally:
            td.cleanup()

    def test_06_task_reference_policy_and_direct_dependency_gaps_are_rejected(self):
        td, root = init_project({"M1": [], "M2": ["M1"]})
        try:
            make_task(
                root,
                "T-BAD",
                milestone="M2",
                deps_m=["M404"],
                deps_t=["T-NOPE"],
                deps_d=["ADR-NOPE"],
                extra_policies=["nope.md"],
            )
            p = ctl(root, "check", expect=2)
            for code in ["TASK_DEP_INCOMPLETE", "TASK_DEP_MILESTONE", "TASK_DEP_TASK", "TASK_DEP_ADR", "TASK_POLICY_MISSING"]:
                self.assertIn(code, p.stdout)
        finally:
            td.cleanup()

    def test_07_single_mutation_lease_rejects_second_writer(self):
        td, root = init_project()
        try:
            make_task(root, "T-1")
            make_task(root, "T-2")
            ctl(root, "dispatch", "T-1")
            p = ctl(root, "dispatch", "T-2", expect=3)
            self.assertIn("single mutation lease", p.stderr)
        finally:
            td.cleanup()

    def test_08_dirty_scope_overlap_rejected_and_unrelated_dirty_drift_detected(self):
        td, root = init_project()
        try:
            (root / "src").mkdir(exist_ok=True)
            (root / "src" / "human.txt").write_text("human\n", encoding="utf-8")
            make_task(root, "T-OVERLAP")
            p = ctl(root, "dispatch", "T-OVERLAP", expect=3)
            self.assertIn("dirty task scope overlaps", p.stderr)
        finally:
            td.cleanup()

        td, root = init_project()
        try:
            (root / "notes.txt").write_text("human v1\n", encoding="utf-8")
            make_task(root, "T-PROTECT")
            ctl(root, "dispatch", "T-PROTECT")
            (root / "notes.txt").write_text("human v2\n", encoding="utf-8")
            p = ctl(root, "check", expect=2)
            self.assertIn("PROTECTED_DIRTY_DRIFT", p.stdout)
        finally:
            td.cleanup()

    def test_09_current_stale_governance_fails_but_historical_accepted_survives(self):
        td, root = init_project()
        try:
            make_task(root, "T-CURRENT")
            ctl(root, "dispatch", "T-CURRENT")
            (root / "core.md").write_text((root / "core.md").read_text(encoding="utf-8") + "\nchanged governance\n", encoding="utf-8")
            p = ctl(root, "check", expect=2)
            self.assertIn("STALE_GOVERNANCE", p.stdout)
        finally:
            td.cleanup()

        td, root = init_project()
        try:
            complete_task(root, "T-HIST")
            (root / "core.md").write_text((root / "core.md").read_text(encoding="utf-8") + "\nchanged governance\n", encoding="utf-8")
            p = ctl(root, "check")
            self.assertIn("CHECK_OK", p.stdout)
        finally:
            td.cleanup()

    def test_10_stale_dispatch_result_is_rejected(self):
        td, root = init_project()
        try:
            make_task(root, "T-ST")
            ctl(root, "dispatch", "T-ST")
            write_result(root, "T-ST", status="interrupted")
            ctl(root, "dispatch", "T-ST")
            self.assertEqual(field_int(task_path(root, "T-ST"), "dispatch_id"), 2)
            write_result(root, "T-ST", dispatch=1)
            p = ctl(root, "check", expect=2)
            self.assertIn("STALE_RESULT", p.stdout)
        finally:
            td.cleanup()

    def test_11_accepted_task_requires_exact_completed_result(self):
        td, root = init_project()
        try:
            make_task(root, "T-NORES")
            ctl(root, "dispatch", "T-NORES")
            set_task_status(root, "T-NORES", "accepted")
            set_active(root, None)
            p = ctl(root, "check", expect=2)
            self.assertIn("MISSING_RESULT", p.stdout)
        finally:
            td.cleanup()

        td, root = init_project()
        try:
            make_task(root, "T-FAIL")
            ctl(root, "dispatch", "T-FAIL")
            write_result(root, "T-FAIL", status="failed", failure_class="METHOD", failure_boundary="M1.A1")
            set_task_status(root, "T-FAIL", "accepted")
            set_active(root, None)
            p = ctl(root, "check", expect=2)
            self.assertIn("ACCEPTED_RESULT_STATUS", p.stdout)
        finally:
            td.cleanup()

    def test_12_result_scope_and_accept_actual_diff_claim_are_enforced(self):
        td, root = init_project()
        try:
            make_task(root, "T-SCOPE")
            ctl(root, "dispatch", "T-SCOPE")
            write_result(root, "T-SCOPE", changed=["outside.txt"])
            p = ctl(root, "check", expect=2)
            self.assertIn("RESULT_SCOPE", p.stdout)
        finally:
            td.cleanup()

        td, root = init_project()
        try:
            make_task(root, "T-DIFF")
            ctl(root, "dispatch", "T-DIFF")
            (root / "src").mkdir(exist_ok=True)
            (root / "src" / "a.txt").write_text("task\n", encoding="utf-8")
            write_result(root, "T-DIFF", changed=[])
            p = ctl(root, "accept", "T-DIFF", expect=3)
            self.assertIn("omits actual task paths", p.stderr)
        finally:
            td.cleanup()

    def test_13_accept_success_updates_current_milestone_realization(self):
        td, root = init_project()
        try:
            complete_task(root, "T-GOOD", changed_path="src/good.txt")
            self.assertIn('accepted_ref: "T-GOOD"', (root / ".prime" / "plan.yaml").read_text(encoding="utf-8"))
            self.assertIn('status: "accepted"', task_path(root, "T-GOOD").read_text(encoding="utf-8"))
            self.assertIn("active_task: null", (root / ".prime" / "state.yaml").read_text(encoding="utf-8"))
            p = ctl(root, "check")
            self.assertIn("CHECK_OK", p.stdout)
        finally:
            td.cleanup()

    def test_14_bounded_retry_survives_dispatch_replacement(self):
        td, root = init_project()
        try:
            make_task(root, "T-RETRY")
            ctl(root, "dispatch", "T-RETRY")
            write_result(root, "T-RETRY", status="failed", failure_class="METHOD", failure_boundary="M1.A1")
            ctl(root, "dispatch", "T-RETRY")
            text = task_path(root, "T-RETRY").read_text(encoding="utf-8")
            self.assertIn("same_boundary_failures: 1", text)
            self.assertIn('last_failure_class: "METHOD"', text)
            self.assertEqual(field_int(task_path(root, "T-RETRY"), "dispatch_id"), 2)
            write_result(root, "T-RETRY", status="failed", failure_class="METHOD", failure_boundary="M1.A1")
            p = ctl(root, "dispatch", "T-RETRY", expect=3)
            self.assertIn("same causal boundary failed twice", p.stderr)
            self.assertEqual(field_int(task_path(root, "T-RETRY"), "dispatch_id"), 2)
        finally:
            td.cleanup()

    def test_15_impact_closure_is_downstream_and_reconciliation_is_ordered(self):
        graph = {"M1": [], "M2": [], "M3": ["M1"], "M5": ["M3"], "M7": ["M3"], "M10": ["M5", "M7"]}
        td, root = init_project(graph)
        try:
            ctl(root, "human-change", "--text", "M1 changed", "--source-ref", "human:m1")
            p = ctl(root, "impact", "--roots", "M1", "--apply")
            self.assertIn("IMPACT M1,M3,M5,M7,M10", p.stdout)
            state = (root / ".prime" / "state.yaml").read_text(encoding="utf-8")
            self.assertIn('pending: ["M1","M3","M5","M7","M10"]', state)
            p = ctl(root, "reconcile", "--done", "M10", expect=3)
            self.assertIn("pending dependencies", p.stderr)
            ctl(root, "reconcile", "--done", "M1,M3,M5,M7,M10")
            ctl(root, "reconcile", "--clean")
        finally:
            td.cleanup()

    def test_16_root_spec_change_invalidates_old_realization_but_preserves_history(self):
        td, root = init_project()
        try:
            complete_task(root, "T-M1-OLD")
            git(root, "add", ".")
            git(root, "commit", "-m", "accept M1 old")
            ctl(root, "human-change", "--text", "Change M1 requirement", "--source-ref", "human:m1-v2")
            plan = (root / ".prime" / "plan.yaml").read_text(encoding="utf-8")
            plan = plan.replace("spec_rev: 1", "spec_rev: 2", 1)
            (root / ".prime" / "plan.yaml").write_text(plan, encoding="utf-8")
            ctl(root, "impact", "--roots", "M1", "--apply")
            plan = (root / ".prime" / "plan.yaml").read_text(encoding="utf-8")
            state = (root / ".prime" / "state.yaml").read_text(encoding="utf-8")
            self.assertIn("accepted_ref: null", plan)
            self.assertIn('invalidated: ["M1"]', state)
            self.assertIn('status: "accepted"', task_path(root, "T-M1-OLD").read_text(encoding="utf-8"))
            p = ctl(root, "reconcile", "--done", "M1", expect=3)
            self.assertIn("without current accepted realization", p.stderr)
            make_task(root, "T-M1-NEW", milestone="M1", spec_rev=2)
            ctl(root, "dispatch", "T-M1-NEW")
            write_result(root, "T-M1-NEW", milestone="M1", changed=[])
            ctl(root, "accept", "T-M1-NEW")
            ctl(root, "reconcile", "--done", "M1")
            ctl(root, "reconcile", "--clean")
            self.assertIn('accepted_ref: "T-M1-NEW"', (root / ".prime" / "plan.yaml").read_text(encoding="utf-8"))
            self.assertIn("CHECK_OK", ctl(root, "check").stdout)
        finally:
            td.cleanup()

    def test_17_impacted_upstream_blocks_downstream_dispatch(self):
        graph = {"M1": [], "M3": ["M1"], "M10": ["M3"]}
        td, root = init_project(graph)
        try:
            ctl(root, "human-change", "--text", "M1 changed", "--source-ref", "human:block")
            ctl(root, "impact", "--roots", "M1", "--apply")
            make_task(root, "T-M10", milestone="M10", deps_m=["M3"])
            p = ctl(root, "dispatch", "T-M10", expect=3)
            self.assertIn("impacted dependencies", p.stderr)
        finally:
            td.cleanup()

    def test_18_resume_capsule_is_compact_and_identity_bound(self):
        td, root = init_project()
        try:
            make_task(root, "T-RESUME")
            ctl(root, "dispatch", "T-RESUME")
            p = ctl(root, "resume")
            self.assertIn("PRIME_LEAN_V2_RESUME", p.stdout)
            self.assertIn("NOW T-RESUME r1 d1 active", p.stdout)
            self.assertIn("GEN 1 PHASE bootstrap", p.stdout)
            self.assertIn("RESULT none", p.stdout)
            self.assertIn("LIVE failures=0", p.stdout)
            self.assertLess(len(p.stdout.split()), 110, p.stdout)
        finally:
            td.cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=2)
