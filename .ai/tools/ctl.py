#!/usr/bin/env python3
"""Prime Lean v2 deterministic control plane.

Standard-library only. The canonical project YAML intentionally uses a small mapping
plus inline JSON-style scalar/list subset so recovery never depends on a package install.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid

TERMINAL = {"accepted", "superseded", "cancelled"}
ACTIVEISH = {"active", "returned", "blocked"}
ALL_STATUSES = {"draft", "active", "returned", "accepted", "blocked", "superseded", "cancelled"}
RESULT_STATUSES = {"completed", "blocked", "needs_recontract", "interrupted", "failed"}
RECOVERY_MODES = {"redo", "resume", "inspect"}
RECON_STATUSES = {"clean", "needs_intent", "needs_reconcile", "blocked"}
REQUIRED_POLICIES = ["execution.md", "evidence.md", "reconciliation.md", "safety.md"]
REQUIRED_AGENTS = [
    "prime.md",
    "worker-fast.md",
    "worker-fast-qwen.md",
    "worker-deep.md",
    "worker-deep-gemini.md",
    "inspector.md",
]


class ControlError(RuntimeError):
    pass


class CheckReport:
    def __init__(self) -> None:
        self.errors: list[tuple[str, str]] = []
        self.warnings: list[tuple[str, str]] = []

    def error(self, code: str, message: str) -> None:
        self.errors.append((code, message))

    def warn(self, code: str, message: str) -> None:
        self.warnings.append((code, message))

    @property
    def ok(self) -> bool:
        return not self.errors


def parse_scalar(raw: str):
    s = raw.strip()
    if s == "":
        return None
    if s in {"null", "~"}:
        return None
    if s == "true":
        return True
    if s == "false":
        return False
    if s.startswith('"') or s.startswith("[") or s.startswith("{"):
        try:
            return json.loads(s)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON-style YAML scalar: {s}: {exc}") from exc
    if s.startswith("'") and s.endswith("'") and len(s) >= 2:
        return s[1:-1].replace("''", "'")
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    return s


def load_simple_yaml(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(path)
    root: dict = {}
    stack: list[tuple[int, dict]] = [(-1, root)]
    for lineno, original in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not original.strip() or original.lstrip().startswith("#"):
            continue
        if "\t" in original[: len(original) - len(original.lstrip())]:
            raise ValueError(f"{path}:{lineno}: tabs are not allowed for indentation")
        indent = len(original) - len(original.lstrip(" "))
        text = original.strip()
        if text.startswith("-"):
            raise ValueError(f"{path}:{lineno}: block lists are unsupported; use inline [..]")
        if ":" not in text:
            raise ValueError(f"{path}:{lineno}: expected key: value")
        key, raw = text.split(":", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"{path}:{lineno}: empty key")
        while stack and indent <= stack[-1][0]:
            stack.pop()
        if not stack:
            raise ValueError(f"{path}:{lineno}: invalid indentation")
        parent = stack[-1][1]
        if key in parent:
            raise ValueError(f"{path}:{lineno}: duplicate key {key}")
        if raw.strip() == "":
            child: dict = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = parse_scalar(raw)
    return root


def scalar_text(value) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    raise TypeError(f"unsupported scalar type: {type(value).__name__}")


def dump_simple_yaml(data: dict, indent: int = 0) -> str:
    lines: list[str] = []
    prefix = " " * indent
    for key, value in data.items():
        if not isinstance(key, str) or not key:
            raise ValueError("YAML keys must be non-empty strings")
        if isinstance(value, dict):
            lines.append(f"{prefix}{key}:")
            lines.append(dump_simple_yaml(value, indent + 2).rstrip("\n"))
        else:
            lines.append(f"{prefix}{key}: {scalar_text(value)}")
    return "\n".join(lines) + "\n"


def atomic_write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(dump_simple_yaml(data), encoding="utf-8")
    os.replace(tmp, path)


def run(root: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=root, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def run_bytes(root: Path, args: list[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(args, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def git_ok(root: Path) -> bool:
    return run(root, ["git", "rev-parse", "--show-toplevel"]).returncode == 0


def git_value(root: Path, args: list[str]) -> str | None:
    p = run(root, ["git", *args])
    if p.returncode != 0:
        return None
    value = p.stdout.strip()
    return value or None


def git_head(root: Path) -> str | None:
    return git_value(root, ["rev-parse", "--verify", "HEAD"])


def nul_paths(p: subprocess.CompletedProcess[str]) -> set[str]:
    if p.returncode != 0:
        return set()
    return {x for x in p.stdout.split("\0") if x}


def git_dirty_paths(root: Path) -> set[str]:
    if not git_ok(root):
        return set()
    paths: set[str] = set()
    paths |= nul_paths(run(root, ["git", "diff", "--name-only", "-z"]))
    paths |= nul_paths(run(root, ["git", "diff", "--cached", "--name-only", "-z"]))
    paths |= nul_paths(run(root, ["git", "ls-files", "--others", "--exclude-standard", "-z"]))
    return {Path(p).as_posix() for p in paths}


def git_changed_since(root: Path, base: str) -> set[str]:
    paths = git_dirty_paths(root)
    head = git_head(root)
    if head and head != base:
        p = run(root, ["git", "diff", "--name-only", "-z", f"{base}..{head}"])
        if p.returncode == 0:
            paths |= nul_paths(p)
    return {Path(p).as_posix() for p in paths}


def path_blob(root: Path, rel: str) -> bytes:
    h = hashlib.sha256()
    h.update(rel.encode("utf-8"))
    h.update(b"\0")
    idx = run_bytes(root, ["git", "show", f":{rel}"])
    if idx.returncode == 0:
        h.update(b"INDEX\0")
        h.update(idx.stdout)
    else:
        h.update(b"NO_INDEX\0")
    path = root / rel
    if path.is_symlink():
        h.update(b"SYMLINK\0")
        h.update(os.readlink(path).encode("utf-8", errors="surrogateescape"))
    elif path.is_file():
        h.update(b"WORK\0")
        h.update(path.read_bytes())
    elif path.exists():
        h.update(b"OTHER\0")
    else:
        h.update(b"MISSING\0")
    return h.digest()


def protected_dirty_hash(root: Path, paths: list[str]) -> str:
    h = hashlib.sha256()
    for rel in sorted(paths):
        h.update(path_blob(root, rel))
    return h.hexdigest()[:16]


def is_canonical_path(rel: str) -> bool:
    p = Path(rel).as_posix()
    return p == ".prime" or p.startswith(".prime/")


def valid_rel_pattern(value: str) -> bool:
    if not value or value.startswith("/") or "\\" in value:
        return False
    parts = value.split("/")
    return ".." not in parts


def path_in_scope(rel: str, scope: dict) -> bool:
    rel = Path(rel).as_posix()
    includes = scope.get("include") if isinstance(scope, dict) else None
    excludes = scope.get("exclude") if isinstance(scope, dict) else None
    if not isinstance(includes, list) or not includes:
        return False
    if not isinstance(excludes, list):
        excludes = []
    inside = any(isinstance(pat, str) and fnmatch.fnmatchcase(rel, pat) for pat in includes)
    blocked = any(isinstance(pat, str) and fnmatch.fnmatchcase(rel, pat) for pat in excludes)
    return inside and not blocked


def governance_files(root: Path) -> list[Path]:
    files = [root / "core.md"]
    files.extend(sorted((root / ".ai" / "policies").glob("*.md")))
    return files


def governance_hash(root: Path) -> str:
    h = hashlib.sha256()
    for path in governance_files(root):
        if not path.is_file():
            raise FileNotFoundError(f"missing governance file: {path}")
        rel = path.relative_to(root).as_posix().encode("utf-8")
        h.update(len(rel).to_bytes(4, "big"))
        h.update(rel)
        data = path.read_bytes()
        h.update(len(data).to_bytes(8, "big"))
        h.update(data)
    return h.hexdigest()[:16]


def bootstrap(root: Path, init_git: bool) -> None:
    root.mkdir(parents=True, exist_ok=True)
    if init_git and not git_ok(root):
        p = run(root, ["git", "init", "-b", "main"])
        if p.returncode != 0:
            p = run(root, ["git", "init"])
        if p.returncode != 0:
            raise ControlError(f"git init failed: {p.stderr.strip()}")
    if init_git and git_ok(root):
        if not git_value(root, ["config", "user.name"]):
            p = run(root, ["git", "config", "--local", "user.name", "Prime Agent"])
            if p.returncode != 0:
                raise ControlError(f"git local user.name failed: {p.stderr.strip()}")
        if not git_value(root, ["config", "user.email"]):
            p = run(root, ["git", "config", "--local", "user.email", "prime@localhost"])
            if p.returncode != 0:
                raise ControlError(f"git local user.email failed: {p.stderr.strip()}")

    prime = root / ".prime"
    (prime / "tasks").mkdir(parents=True, exist_ok=True)
    (prime / "decisions").mkdir(parents=True, exist_ok=True)
    template_root = root / ".ai" / "templates" / "prime-memory"
    for name in ["state.yaml", "plan.yaml"]:
        dest = prime / name
        if not dest.exists():
            src = template_root / name
            if not src.is_file():
                raise FileNotFoundError(f"missing template: {src}")
            shutil.copyfile(src, dest)
    events = prime / "events.jsonl"
    if not events.exists():
        events.write_text("", encoding="utf-8")
    print(f"BOOTSTRAP_OK root={root} git={'yes' if git_ok(root) else 'no'}")


def load_state(root: Path) -> dict:
    return load_simple_yaml(root / ".prime" / "state.yaml")


def load_plan(root: Path) -> dict:
    return load_simple_yaml(root / ".prime" / "plan.yaml")


def load_task(root: Path, task_id: str) -> dict:
    return load_simple_yaml(root / ".prime" / "tasks" / task_id / "task.yaml")


def milestone_graph(plan: dict) -> dict[str, list[str]]:
    milestones = plan.get("milestones")
    if not isinstance(milestones, dict):
        return {}
    graph: dict[str, list[str]] = {}
    for mid, data in milestones.items():
        deps = data.get("depends_on") if isinstance(data, dict) else []
        graph[str(mid)] = [str(x) for x in deps] if isinstance(deps, list) else []
    return graph


def topo_order(graph: dict[str, list[str]]) -> list[str]:
    indegree = {n: 0 for n in graph}
    reverse = {n: [] for n in graph}
    for node, deps in graph.items():
        for dep in deps:
            if dep not in graph:
                continue
            indegree[node] += 1
            reverse[dep].append(node)
    queue = [n for n in graph if indegree[n] == 0]
    out: list[str] = []
    while queue:
        node = queue.pop(0)
        out.append(node)
        for child in reverse[node]:
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    if len(out) != len(graph):
        raise ControlError("milestone dependency graph contains a cycle")
    return out


def downstream_closure(plan: dict, roots: list[str]) -> list[str]:
    graph = milestone_graph(plan)
    missing = [x for x in roots if x not in graph]
    if missing:
        raise ControlError(f"unknown milestone roots: {missing}")
    reverse = {n: [] for n in graph}
    for node, deps in graph.items():
        for dep in deps:
            if dep in reverse:
                reverse[dep].append(node)
    seen = set(roots)
    queue = list(roots)
    while queue:
        node = queue.pop(0)
        for child in reverse[node]:
            if child not in seen:
                seen.add(child)
                queue.append(child)
    order = topo_order(graph)
    return [m for m in order if m in seen]


def transitive_dependencies(plan: dict, milestone: str) -> set[str]:
    graph = milestone_graph(plan)
    seen: set[str] = set()
    stack = list(graph.get(milestone, []))
    while stack:
        dep = stack.pop()
        if dep in seen:
            continue
        seen.add(dep)
        stack.extend(graph.get(dep, []))
    return seen


def acceptance_refs(plan: dict, milestone: str) -> list[str]:
    milestones = plan.get("milestones") or {}
    data = milestones.get(milestone) if isinstance(milestones, dict) else None
    acceptance = data.get("acceptance") if isinstance(data, dict) else None
    if not isinstance(acceptance, dict):
        return []
    return [f"{milestone}.{aid}" for aid in acceptance]


def accepted_ref_valid(plan: dict, tasks: dict[str, dict], milestone: str, root: Path | None = None) -> tuple[bool, str]:
    milestones = plan.get("milestones")
    if not isinstance(milestones, dict):
        return False, "milestones mapping missing"
    data = milestones.get(milestone)
    if not isinstance(data, dict):
        return False, "milestone missing"
    ref = data.get("accepted_ref")
    if ref is None:
        return False, "accepted_ref is null"
    if not isinstance(ref, str) or ref not in tasks:
        return False, f"accepted_ref {ref!r} not found"
    task = tasks[ref]
    if task.get("status") != "accepted":
        return False, f"task {ref} is not accepted"
    if task.get("objective_ref") != milestone:
        return False, f"task {ref} objective_ref is {task.get('objective_ref')!r}"
    if task.get("objective_spec_rev") != data.get("spec_rev"):
        return False, f"task {ref} binds spec_rev {task.get('objective_spec_rev')} not {data.get('spec_rev')}"
    required = set(acceptance_refs(plan, milestone))
    task_refs_raw = task.get("acceptance_refs")
    if not isinstance(task_refs_raw, list) or any(not isinstance(x, str) for x in task_refs_raw):
        return False, f"task {ref} has invalid acceptance_refs"
    task_refs = set(task_refs_raw)
    if not required.issubset(task_refs):
        return False, f"task {ref} does not cover current milestone acceptance"
    if root is not None:
        result_path = root / ".prime" / "tasks" / ref / "result.yaml"
        if not result_path.is_file():
            return False, f"task {ref} has no result.yaml"
        try:
            result = load_simple_yaml(result_path)
        except (OSError, ValueError) as exc:
            return False, f"task {ref} result invalid: {exc}"
        if result.get("task") != ref or result.get("contract_rev") != task.get("contract_rev") or result.get("dispatch_id") != task.get("dispatch_id"):
            return False, f"task {ref} result identity is stale"
        if result.get("status") != "completed":
            return False, f"task {ref} result is not completed"
        proved = result.get("proved")
        if not isinstance(proved, list) or not set(task_refs).issubset(set(x for x in proved if isinstance(x, str))):
            return False, f"task {ref} result does not prove its acceptance refs"
    return True, "ok"


def validate_structure(root: Path, report: CheckReport) -> None:
    if not (root / "core.md").is_file():
        report.error("MISSING_CORE", "core.md is missing")
    for name in REQUIRED_POLICIES:
        if not (root / ".ai" / "policies" / name).is_file():
            report.error("MISSING_POLICY", f".ai/policies/{name} is missing")
    for name in REQUIRED_AGENTS:
        if not (root / ".kilo" / "agents" / name).is_file():
            report.error("MISSING_AGENT", f".kilo/agents/{name} is missing")
    if not (root / ".ai" / "tools" / "ctl.py").is_file():
        report.error("MISSING_CTL", ".ai/tools/ctl.py is missing")
    for obsolete in [
        root / ".ai" / "tools" / "statecheck.py",
        root / ".ai" / "templates" / "prime-memory" / "roadmap.yaml",
        root / ".ai" / "templates" / "prime-memory" / "BOOTSTRAP-TEMPLATE.md",
    ]:
        if obsolete.exists():
            report.error("OBSOLETE_SURFACE", f"v2 obsolete surface exists: {obsolete.relative_to(root)}")


def validate_events(root: Path, state: dict, report: CheckReport) -> None:
    path = root / ".prime" / "events.jsonl"
    if not path.exists():
        report.error("MISSING_EVENTS", ".prime/events.jsonl is missing")
        return
    seen_ids: set[str] = set()
    seen_sources: set[str] = set()
    human_generations: list[int] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            report.error("EVENT_JSON", f"events line {lineno} invalid JSON: {exc}")
            continue
        eid = obj.get("event_id")
        if not isinstance(eid, str) or not eid:
            report.error("EVENT_ID", f"events line {lineno} missing event_id")
        elif eid in seen_ids:
            report.error("EVENT_DUP", f"duplicate event_id {eid}")
        else:
            seen_ids.add(eid)
        kind = obj.get("kind")
        if not isinstance(kind, str) or not isinstance(obj.get("text"), str):
            report.error("EVENT_SCHEMA", f"events line {lineno} requires string kind/text")
        source = obj.get("source_ref")
        if source is not None:
            if not isinstance(source, str) or not source:
                report.error("EVENT_SOURCE", f"events line {lineno} invalid source_ref")
            elif source in seen_sources:
                report.error("EVENT_SOURCE_DUP", f"duplicate source_ref {source}")
            else:
                seen_sources.add(source)
        if kind == "human_change":
            generation = obj.get("generation")
            if not isinstance(generation, int) or generation < 1:
                report.error("EVENT_GENERATION", f"events line {lineno} human_change requires positive generation")
            else:
                human_generations.append(generation)
    expected = list(range(1, len(human_generations) + 1))
    if human_generations != expected:
        report.error("EVENT_GENERATION", f"human_change generations must be contiguous {expected}, got {human_generations}")
    state_gen = state.get("generation")
    if isinstance(state_gen, int) and state_gen != len(human_generations):
        report.error("GENERATION_EVENT_DRIFT", f"state.generation={state_gen} but human_change events={len(human_generations)}")


def validate_plan(plan: dict, report: CheckReport) -> None:
    objective = plan.get("objective")
    if not isinstance(objective, str) or not objective.strip() or objective == "UNSET":
        report.error("PLAN_OBJECTIVE", ".prime/plan.yaml objective is not reconciled")
    if not isinstance(plan.get("non_goals"), list):
        report.error("PLAN_NON_GOALS", "plan.non_goals must be an inline list")
    milestones = plan.get("milestones")
    if not isinstance(milestones, dict) or not milestones:
        report.error("PLAN_MILESTONES", "plan.milestones must be a non-empty mapping")
        return
    graph: dict[str, list[str]] = {}
    for mid, data in milestones.items():
        if not isinstance(mid, str) or not re.fullmatch(r"M[A-Za-z0-9._-]+", mid):
            report.error("MILESTONE_ID", f"invalid milestone id {mid!r}")
            continue
        if not isinstance(data, dict):
            report.error("MILESTONE_SCHEMA", f"{mid}: milestone must be mapping")
            continue
        if not isinstance(data.get("spec_rev"), int) or data.get("spec_rev") < 1:
            report.error("MILESTONE_REV", f"{mid}: spec_rev must be positive integer")
        if not isinstance(data.get("outcome"), str) or not data.get("outcome").strip():
            report.error("MILESTONE_OUTCOME", f"{mid}: outcome is required")
        acceptance = data.get("acceptance")
        if not isinstance(acceptance, dict) or not acceptance:
            report.error("MILESTONE_ACCEPTANCE", f"{mid}: non-empty acceptance mapping required")
        else:
            for aid, text in acceptance.items():
                if not isinstance(aid, str) or not re.fullmatch(r"A[A-Za-z0-9._-]+", aid):
                    report.error("ACCEPTANCE_ID", f"{mid}: invalid acceptance id {aid!r}")
                if not isinstance(text, str) or not text.strip():
                    report.error("ACCEPTANCE_TEXT", f"{mid}.{aid}: acceptance text required")
        deps = data.get("depends_on")
        if not isinstance(deps, list):
            report.error("MILESTONE_DEPS", f"{mid}: depends_on must be inline list")
            deps = []
        graph[mid] = [x for x in deps if isinstance(x, str)]
        if any(not isinstance(x, str) for x in deps):
            report.error("MILESTONE_DEPS", f"{mid}: dependency ids must be strings")
        accepted = data.get("accepted_ref")
        if accepted is not None and not isinstance(accepted, str):
            report.error("MILESTONE_ACCEPTED_REF", f"{mid}: accepted_ref must be null or task id")
    for mid, deps in graph.items():
        for dep in deps:
            if dep == mid:
                report.error("MILESTONE_SELF_DEP", f"{mid}: cannot depend on itself")
            elif dep not in graph:
                report.error("MILESTONE_DEP_MISSING", f"{mid}: unknown dependency {dep}")
    try:
        topo_order(graph)
    except ControlError:
        report.error("MILESTONE_CYCLE", "milestone dependency graph contains a cycle")


def validate_task_schema(root: Path, plan: dict, task: dict, task_id: str, report: CheckReport) -> None:
    if task.get("id") != task_id:
        report.error("TASK_ID", f"{task_id}: id must equal directory name")
    status = task.get("status")
    if status not in ALL_STATUSES:
        report.error("TASK_STATUS", f"{task_id}: invalid status {status!r}")
    if not isinstance(task.get("contract_rev"), int) or task.get("contract_rev") < 1:
        report.error("TASK_VERSION", f"{task_id}: contract_rev must be positive integer")
    dispatch = task.get("dispatch_id")
    if not isinstance(dispatch, int) or dispatch < 0 or (status != "draft" and dispatch < 1):
        report.error("TASK_VERSION", f"{task_id}: dispatch_id invalid for status {status!r}")
    if not isinstance(task.get("validated_generation"), int) or task.get("validated_generation") < 0:
        report.error("TASK_VERSION", f"{task_id}: validated_generation must be non-negative integer")
    if not isinstance(task.get("governance_hash"), str) or (status != "draft" and not task.get("governance_hash")):
        report.error("TASK_GOV", f"{task_id}: governance_hash must be non-empty after draft")
    objective_ref = task.get("objective_ref")
    if not isinstance(objective_ref, str) or not objective_ref:
        report.error("TASK_OBJECTIVE_REF", f"{task_id}: objective_ref is required")
    if not isinstance(task.get("objective_spec_rev"), int) or task.get("objective_spec_rev") < 1:
        report.error("TASK_SPEC_REV", f"{task_id}: objective_spec_rev must be positive integer")
    refs = task.get("acceptance_refs")
    if not isinstance(refs, list) or not refs or any(not isinstance(x, str) for x in refs):
        report.error("TASK_ACCEPTANCE", f"{task_id}: non-empty acceptance_refs string list required")
    scope = task.get("scope")
    if not isinstance(scope, dict):
        report.error("TASK_SCOPE", f"{task_id}: scope mapping required")
    else:
        includes = scope.get("include")
        excludes = scope.get("exclude")
        if not isinstance(includes, list) or not includes or any(not isinstance(x, str) or not valid_rel_pattern(x) for x in includes):
            report.error("TASK_SCOPE", f"{task_id}: scope.include requires safe relative patterns")
        if not isinstance(excludes, list) or any(not isinstance(x, str) or not valid_rel_pattern(x) for x in excludes):
            report.error("TASK_SCOPE", f"{task_id}: scope.exclude requires safe relative patterns")
    deps = task.get("depends_on")
    if not isinstance(deps, dict):
        report.error("TASK_DEPS", f"{task_id}: depends_on mapping required")
    else:
        for key in ["decisions", "tasks", "milestones"]:
            vals = deps.get(key)
            if not isinstance(vals, list) or any(not isinstance(x, str) for x in vals):
                report.error("TASK_DEPS", f"{task_id}: depends_on.{key} must be string list")
    if task.get("recovery") not in RECOVERY_MODES:
        report.error("TASK_RECOVERY", f"{task_id}: recovery must be one of {sorted(RECOVERY_MODES)}")
    policies = task.get("extra_policies")
    if not isinstance(policies, list) or any(not isinstance(x, str) for x in policies):
        report.error("TASK_POLICIES", f"{task_id}: extra_policies must be string list")
    else:
        for name in policies:
            if "/" in name or not (root / ".ai" / "policies" / name).is_file():
                report.error("TASK_POLICY_MISSING", f"{task_id}: extra policy missing: {name}")
    forbidden = task.get("forbidden")
    if not isinstance(forbidden, list) or any(not isinstance(x, str) for x in forbidden):
        report.error("TASK_FORBIDDEN", f"{task_id}: forbidden must be string list")
    workspace = task.get("workspace")
    if not isinstance(workspace, dict):
        report.error("TASK_WORKSPACE", f"{task_id}: workspace mapping required")
    else:
        base = workspace.get("base_commit")
        if status == "draft":
            if base is not None and not isinstance(base, str):
                report.error("BASE_COMMIT", f"{task_id}: draft base_commit must be null/string")
        elif not isinstance(base, str) or not base:
            report.error("BASE_COMMIT", f"{task_id}: non-draft task requires base_commit")
        protected = workspace.get("protected_dirty_paths")
        if not isinstance(protected, list) or any(not isinstance(x, str) or not valid_rel_pattern(x) for x in protected):
            report.error("PROTECTED_DIRTY", f"{task_id}: protected_dirty_paths must be safe path list")
        digest = workspace.get("protected_dirty_hash")
        if digest is not None and not isinstance(digest, str):
            report.error("PROTECTED_DIRTY", f"{task_id}: protected_dirty_hash must be null/string")
    live = task.get("liveness")
    if not isinstance(live, dict):
        report.error("TASK_LIVENESS", f"{task_id}: liveness mapping required")
    else:
        if not isinstance(live.get("contract_rev"), int) or live.get("contract_rev") < 1:
            report.error("TASK_LIVENESS", f"{task_id}: liveness.contract_rev invalid")
        failures = live.get("same_boundary_failures")
        if not isinstance(failures, int) or failures < 0:
            report.error("TASK_LIVENESS", f"{task_id}: same_boundary_failures must be non-negative")
        for key in ["last_failure_class", "last_failure_boundary"]:
            if live.get(key) is not None and not isinstance(live.get(key), str):
                report.error("TASK_LIVENESS", f"{task_id}: {key} must be null/string")

    # Current-plan semantic references are required for non-terminal work. Historical tasks remain immutable.
    if status not in TERMINAL and isinstance(objective_ref, str):
        milestones = plan.get("milestones") or {}
        if objective_ref not in milestones:
            report.error("TASK_MILESTONE_MISSING", f"{task_id}: objective milestone missing: {objective_ref}")
        else:
            current_spec = milestones[objective_ref].get("spec_rev")
            if task.get("objective_spec_rev") != current_spec:
                report.error("TASK_SPEC_STALE", f"{task_id}: objective_spec_rev {task.get('objective_spec_rev')} != {current_spec}")
            valid_refs = set(acceptance_refs(plan, objective_ref))
            for ref in refs or []:
                if ref not in valid_refs:
                    report.error("TASK_ACCEPTANCE_REF", f"{task_id}: unknown current acceptance ref {ref}")


def validate_checkpoint(task: dict, task_id: str, task_dir: Path, report: CheckReport) -> None:
    path = task_dir / "checkpoint.yaml"
    if not path.exists():
        return
    try:
        cp = load_simple_yaml(path)
    except (OSError, ValueError) as exc:
        report.error("CHECKPOINT_PARSE", str(exc))
        return
    if cp.get("task") != task_id or cp.get("contract_rev") != task.get("contract_rev"):
        report.error("STALE_CHECKPOINT", f"{task_id}: checkpoint task/rev is stale")
    source = cp.get("source_dispatch")
    current_dispatch = task.get("dispatch_id") if isinstance(task.get("dispatch_id"), int) else 0
    if not isinstance(source, int) or source < 1 or source > current_dispatch:
        report.error("CHECKPOINT_DISPATCH", f"{task_id}: checkpoint source_dispatch invalid")
    if not isinstance(cp.get("stage"), str) or not isinstance(cp.get("next"), list) or not isinstance(cp.get("proved"), list):
        report.error("CHECKPOINT_SCHEMA", f"{task_id}: checkpoint requires stage, next[], proved[]")


def validate_result(task: dict, task_id: str, task_dir: Path, report: CheckReport, current: bool) -> tuple[dict | None, bool]:
    path = task_dir / "result.yaml"
    if not path.exists():
        if task.get("status") in {"returned", "accepted"}:
            report.error("MISSING_RESULT", f"{task_id}: {task.get('status')} task requires result.yaml")
        return None, False
    try:
        result = load_simple_yaml(path)
    except (OSError, ValueError) as exc:
        report.error("RESULT_PARSE", str(exc))
        return None, False
    status = result.get("status")
    if status not in RESULT_STATUSES:
        report.error("RESULT_STATUS", f"{task_id}: invalid result status {status!r}")
    exact = (
        result.get("task") == task_id
        and result.get("contract_rev") == task.get("contract_rev")
        and result.get("dispatch_id") == task.get("dispatch_id")
    )
    if current and not exact:
        report.error("STALE_RESULT", f"{task_id}: result identity does not match current r{task.get('contract_rev')} d{task.get('dispatch_id')}")
    if task.get("status") == "accepted" and not exact:
        report.error("HISTORY_RESULT_ID", f"{task_id}: accepted result must match final task rev/dispatch")
    proved = result.get("proved")
    changed = result.get("changed")
    evidence = result.get("evidence")
    if not isinstance(proved, list) or any(not isinstance(x, str) for x in proved):
        report.error("RESULT_PROVED", f"{task_id}: proved must be string list")
    if not isinstance(changed, list) or any(not isinstance(x, str) or not valid_rel_pattern(x) for x in changed):
        report.error("RESULT_CHANGED", f"{task_id}: changed must be safe relative path list")
    else:
        for rel in changed:
            if not path_in_scope(rel, task.get("scope") or {}):
                report.error("RESULT_SCOPE", f"{task_id}: result.changed outside scope: {rel}")
    if not isinstance(evidence, list) or any(not isinstance(x, str) for x in evidence):
        report.error("RESULT_EVIDENCE", f"{task_id}: evidence must be string list")
    if exact and status == "completed":
        missing = [x for x in task.get("acceptance_refs") or [] if x not in (proved or [])]
        if missing:
            report.error("RESULT_ACCEPTANCE", f"{task_id}: completed result missing acceptance refs {missing}")
        if current and task.get("status") == "active":
            report.warn("PENDING_REVIEW", f"{task_id}: completed current result awaits Prime acceptance")
    if task.get("status") == "accepted" and status != "completed":
        report.error("ACCEPTED_RESULT_STATUS", f"{task_id}: accepted task requires completed result, got {status!r}")
    return result, exact




def validate_task_refs(root: Path, plan: dict, tasks: dict[str, dict], task_id: str, task: dict, report: CheckReport) -> None:
    deps = task.get("depends_on") if isinstance(task.get("depends_on"), dict) else {}
    milestones = plan.get("milestones") if isinstance(plan.get("milestones"), dict) else {}
    objective = task.get("objective_ref")
    declared_m = deps.get("milestones") if isinstance(deps.get("milestones"), list) else []
    if isinstance(objective, str) and objective in milestones:
        required_direct = {x for x in (milestones[objective].get("depends_on") or []) if isinstance(x, str)}
        declared_set = {x for x in declared_m if isinstance(x, str)}
        missing_direct = sorted(required_direct - declared_set)
        if missing_direct and task.get("status") not in TERMINAL:
            report.error("TASK_DEP_INCOMPLETE", f"{task_id}: missing direct milestone deps {missing_direct}")
    for mid in declared_m:
        if not isinstance(mid, str):
            continue
        if task.get("status") not in TERMINAL and mid not in milestones:
            report.error("TASK_DEP_MILESTONE", f"{task_id}: missing milestone dependency {mid}")
    for dep in deps.get("tasks") or []:
        if not isinstance(dep, str):
            continue
        if dep == task_id:
            report.error("TASK_SELF_DEP", f"{task_id}: cannot depend on itself")
        elif dep not in tasks:
            report.error("TASK_DEP_TASK", f"{task_id}: missing task dependency {dep}")
    for adr in deps.get("decisions") or []:
        if not isinstance(adr, str):
            continue
        candidate = root / ".prime" / "decisions" / (adr if adr.endswith(".md") else f"{adr}.md")
        if not candidate.is_file():
            report.error("TASK_DEP_ADR", f"{task_id}: missing decision dependency {adr}")

def task_dependencies_ready(plan: dict, tasks: dict[str, dict], task_id: str, task: dict, report: CheckReport | None = None, root: Path | None = None) -> bool:
    ok = True
    deps = task.get("depends_on") or {}
    milestones = plan.get("milestones") if isinstance(plan.get("milestones"), dict) else {}
    declared_m = deps.get("milestones") if isinstance(deps, dict) else []
    for mid in declared_m or []:
        if not isinstance(mid, str):
            ok = False
            continue
        if mid not in milestones:
            ok = False
            if report:
                report.error("TASK_DEP_MILESTONE", f"{task_id}: missing milestone dependency {mid}")
            continue
        valid, why = accepted_ref_valid(plan, tasks, mid, root)
        if not valid:
            ok = False
            if report:
                report.error("TASK_DEP_NOT_ACCEPTED", f"{task_id}: milestone dependency {mid} not currently accepted: {why}")
    for dep in (deps.get("tasks") or []) if isinstance(deps, dict) else []:
        if not isinstance(dep, str):
            ok = False
            continue
        if dep == task_id:
            ok = False
            if report:
                report.error("TASK_SELF_DEP", f"{task_id}: cannot depend on itself")
        elif dep not in tasks:
            ok = False
            if report:
                report.error("TASK_DEP_TASK", f"{task_id}: missing task dependency {dep}")
        elif tasks[dep].get("status") != "accepted":
            ok = False
            if report:
                report.error("TASK_DEP_TASK_STATUS", f"{task_id}: task dependency {dep} is not accepted")
    return ok


def validate_task_graph(tasks: dict[str, dict], report: CheckReport) -> None:
    graph: dict[str, list[str]] = {}
    for tid, task in tasks.items():
        deps = task.get("depends_on")
        vals = deps.get("tasks") if isinstance(deps, dict) else []
        graph[tid] = [x for x in vals if isinstance(x, str) and x in tasks]
    visiting: set[str] = set()
    done: set[str] = set()

    def visit(node: str) -> bool:
        if node in done:
            return False
        if node in visiting:
            return True
        visiting.add(node)
        for dep in graph.get(node, []):
            if visit(dep):
                return True
        visiting.remove(node)
        done.add(node)
        return False

    if any(visit(node) for node in graph):
        report.error("TASK_DEP_CYCLE", "task dependency graph contains a cycle")


def git_snapshot(root: Path) -> dict:
    if not git_ok(root):
        return {"ok": False, "branch": "NO_GIT", "head": None, "staged": 0, "unstaged": 0, "untracked": 0}
    branch = git_value(root, ["branch", "--show-current"]) or "DETACHED/UNBORN"
    head = git_head(root)
    p = run(root, ["git", "status", "--porcelain=v1"])
    staged = unstaged = untracked = 0
    if p.returncode == 0:
        for line in p.stdout.splitlines():
            if line.startswith("??"):
                untracked += 1
            elif len(line) >= 2:
                if line[0] != " ":
                    staged += 1
                if line[1] != " ":
                    unstaged += 1
    return {"ok": True, "branch": branch, "head": head, "staged": staged, "unstaged": unstaged, "untracked": untracked}


def runtime_check(root: Path) -> tuple[CheckReport, dict]:
    report = CheckReport()
    validate_structure(root, report)
    try:
        gov = governance_hash(root)
    except (OSError, ValueError) as exc:
        report.error("GOV_HASH", str(exc))
        gov = None
    try:
        state = load_state(root)
    except (OSError, ValueError) as exc:
        report.error("STATE_PARSE", str(exc))
        state = {}
    try:
        plan = load_plan(root)
    except (OSError, ValueError) as exc:
        report.error("PLAN_PARSE", str(exc))
        plan = {}

    validate_plan(plan, report)
    generation = state.get("generation")
    if not isinstance(generation, int) or generation < 0:
        report.error("STATE_GENERATION", "state.generation must be non-negative integer")
    if not isinstance(state.get("phase"), str) or not state.get("phase"):
        report.error("STATE_PHASE", "state.phase must be string")
    if not isinstance(state.get("next"), list):
        report.error("STATE_NEXT", "state.next must be inline list")
    if not isinstance(state.get("blockers"), list):
        report.error("STATE_BLOCKERS", "state.blockers must be inline list")
    recon = state.get("reconciliation")
    if not isinstance(recon, dict):
        report.error("STATE_RECON", "state.reconciliation must be mapping")
        recon = {}
    status = recon.get("status")
    if status not in RECON_STATUSES:
        report.error("STATE_RECON", f"invalid reconciliation.status {status!r}")
    if status == "needs_intent":
        report.error("STATE_RECON", "reconciliation still needs initial Human intent")
    for key in ["roots", "pending", "invalidated"]:
        if not isinstance(recon.get(key), list) or any(not isinstance(x, str) for x in recon.get(key) or []):
            report.error("STATE_RECON", f"reconciliation.{key} must be string list")
    if recon.get("event") is not None and not isinstance(recon.get("event"), str):
        report.error("STATE_RECON", "reconciliation.event must be null/string")
    milestones = plan.get("milestones") or {}
    for key in ["roots", "pending", "invalidated"]:
        for mid in recon.get(key) or []:
            if isinstance(milestones, dict) and mid not in milestones:
                report.error("RECON_MILESTONE", f"reconciliation.{key} contains unknown milestone {mid}")
    if status == "clean" and ((recon.get("pending") or []) or (recon.get("invalidated") or [])):
        report.error("RECON_NOT_EMPTY", "clean reconciliation cannot have pending/invalidated milestones")

    validate_events(root, state, report)
    git = git_snapshot(root)
    if not git["ok"]:
        report.error("NO_GIT", "workspace is not a Git repository; run bootstrap --init-git")

    tasks_root = root / ".prime" / "tasks"
    tasks: dict[str, dict] = {}
    if tasks_root.exists():
        for task_dir in sorted(p for p in tasks_root.iterdir() if p.is_dir()):
            task_path = task_dir / "task.yaml"
            if not task_path.exists():
                report.error("TASK_FILE", f"{task_dir.relative_to(root)} missing task.yaml")
                continue
            try:
                task = load_simple_yaml(task_path)
            except (OSError, ValueError) as exc:
                report.error("TASK_PARSE", str(exc))
                continue
            tid = task_dir.name
            tasks[tid] = task
            validate_task_schema(root, plan, task, tid, report)
    validate_task_graph(tasks, report)
    for tid, task in tasks.items():
        validate_task_refs(root, plan, tasks, tid, task, report)

    # Validate accepted milestone pointers only when present. Null means not currently accepted.
    if isinstance(milestones, dict):
        for mid, data in milestones.items():
            if isinstance(data, dict) and data.get("accepted_ref") is not None:
                valid, why = accepted_ref_valid(plan, tasks, mid, root)
                if not valid:
                    report.error("MILESTONE_ACCEPTED_INVALID", f"{mid}: {why}")

    active_id = state.get("active_task")
    if active_id is not None and not isinstance(active_id, str):
        report.error("ACTIVE_TASK", "state.active_task must be null or task id")
        active_id = None
    if isinstance(active_id, str) and active_id not in tasks:
        report.error("ACTIVE_TASK", f"state.active_task {active_id} does not exist")

    for tid, task in tasks.items():
        current = tid == active_id
        status_t = task.get("status")
        if status_t in ACTIVEISH and not current:
            report.error("ORPHAN_TASK", f"{tid}: status {status_t} but active_task is {active_id!r}")
        if current:
            if status_t not in ACTIVEISH:
                report.error("ACTIVE_STATUS", f"{tid}: active pointer cannot target {status_t!r}")
            if gov and task.get("governance_hash") != gov:
                report.error("STALE_GOVERNANCE", f"{tid}: governance hash differs from current {gov}")
            if isinstance(generation, int) and task.get("validated_generation") != generation:
                report.error("STALE_GENERATION", f"{tid}: validated_generation {task.get('validated_generation')} != {generation}")
            workspace = task.get("workspace") or {}
            base = workspace.get("base_commit") if isinstance(workspace, dict) else None
            if git["ok"] and git["head"]:
                if not isinstance(base, str) or not base:
                    report.error("BASE_COMMIT", f"{tid}: active task requires base_commit")
                elif run(root, ["git", "cat-file", "-e", f"{base}^{{commit}}"]).returncode != 0:
                    report.error("BASE_COMMIT", f"{tid}: unknown base_commit {base}")
            elif git["ok"]:
                report.error("NO_BASELINE", f"{tid}: active task requires committed baseline")
            protected = workspace.get("protected_dirty_paths") if isinstance(workspace, dict) else []
            digest = workspace.get("protected_dirty_hash") if isinstance(workspace, dict) else None
            if isinstance(protected, list) and all(isinstance(x, str) for x in protected) and isinstance(digest, str):
                current_digest = protected_dirty_hash(root, protected)
                if current_digest != digest:
                    report.error("PROTECTED_DIRTY_DRIFT", f"{tid}: protected Human/unrelated dirty state changed during dispatch")
            if isinstance(base, str) and base and run(root, ["git", "cat-file", "-e", f"{base}^{{commit}}"]).returncode == 0:
                delta = current_task_delta(root, task)
                outside = sorted(p for p in delta if not path_in_scope(p, task.get("scope") or {}))
                if outside:
                    report.error("TASK_SCOPE_DRIFT", f"{tid}: non-canonical delta outside task scope: {outside}")
            live = task.get("liveness") if isinstance(task.get("liveness"), dict) else {}
            if isinstance(live.get("same_boundary_failures"), int) and live.get("same_boundary_failures") >= 2:
                report.error("RETRY_EXHAUSTED", f"{tid}: same causal boundary has failed twice")
            if recon.get("status") == "needs_reconcile":
                objective = task.get("objective_ref")
                pending = set(recon.get("pending") or [])
                invalidated = set(recon.get("invalidated") or [])
                if isinstance(objective, str):
                    blocked = transitive_dependencies(plan, objective) & (pending | invalidated)
                    if blocked:
                        report.error("RECON_ACTIVE_BLOCKED", f"{tid}: impacted dependencies still unresolved: {sorted(blocked)}")
            task_dependencies_ready(plan, tasks, tid, task, report, root=root)
        validate_checkpoint(task, tid, tasks_root / tid, report)
        validate_result(task, tid, tasks_root / tid, report, current=current)

    return report, {"governance_hash": gov, "state": state, "plan": plan, "tasks": tasks, "git": git, "active_id": active_id}


def format_report(report: CheckReport) -> None:
    for code, msg in report.errors:
        print(f"ERROR {code}: {msg}")
    for code, msg in report.warnings:
        print(f"WARN {code}: {msg}")


def print_check(root: Path) -> int:
    report, meta = runtime_check(root)
    format_report(report)
    state = meta["state"]
    git = meta["git"]
    recon = state.get("reconciliation") if isinstance(state.get("reconciliation"), dict) else {}
    if report.ok:
        print(
            "CHECK_OK "
            f"governance={meta['governance_hash']} generation={state.get('generation')} "
            f"active={meta['active_id'] or 'none'} recon={recon.get('status')} "
            f"branch={git.get('branch')} dirty={git.get('staged',0)+git.get('unstaged',0)+git.get('untracked',0)}"
        )
        return 0
    print(f"CHECK_FAILED errors={len(report.errors)} warnings={len(report.warnings)}")
    return 2


def result_state(root: Path, meta: dict) -> str:
    tid = meta.get("active_id")
    if not tid:
        return "none"
    task = meta["tasks"].get(tid) or {}
    path = root / ".prime" / "tasks" / tid / "result.yaml"
    if not path.exists():
        return "none"
    try:
        result = load_simple_yaml(path)
    except Exception:
        return "invalid"
    exact = result.get("task") == tid and result.get("contract_rev") == task.get("contract_rev") and result.get("dispatch_id") == task.get("dispatch_id")
    return f"{result.get('status','unknown')}/{'current' if exact else 'stale'}"


def print_resume(root: Path) -> int:
    report, meta = runtime_check(root)
    state = meta["state"]
    plan = meta["plan"]
    git = meta["git"]
    tid = meta["active_id"]
    task = meta["tasks"].get(tid, {}) if tid else {}
    recon = state.get("reconciliation") if isinstance(state.get("reconciliation"), dict) else {}
    issues = [code for code, _ in report.errors] or [code for code, _ in report.warnings]
    now = "none"
    if tid:
        now = f"{tid} r{task.get('contract_rev','?')} d{task.get('dispatch_id','?')} {task.get('status','?')}"
    print("PRIME_LEAN_V2_RESUME")
    print(f"GOV {meta.get('governance_hash') or 'unknown'}")
    print(f"OBJ {plan.get('objective','unknown')}")
    print(f"GEN {state.get('generation','?')} PHASE {state.get('phase','?')}")
    print(f"NOW {now}")
    print(f"RESULT {result_state(root, meta)}")
    print(f"GIT {git.get('branch')} staged={git.get('staged',0)} unstaged={git.get('unstaged',0)} untracked={git.get('untracked',0)}")
    print(f"RECON {recon.get('status','unknown')} event={recon.get('event') or 'none'}")
    print("PENDING " + (",".join((recon.get("pending") or [])[:8]) if recon.get("pending") else "none"))
    print("INVALID " + (",".join((recon.get("invalidated") or [])[:8]) if recon.get("invalidated") else "none"))
    print("NEXT " + (" | ".join(str(x) for x in (state.get("next") or [])[:4]) if state.get("next") else "none"))
    if tid:
        live = task.get("liveness") if isinstance(task.get("liveness"), dict) else {}
        print(f"LIVE failures={live.get('same_boundary_failures',0)} class={live.get('last_failure_class') or 'none'} boundary={live.get('last_failure_boundary') or 'none'}")
    print("ISSUES " + (",".join(dict.fromkeys(issues)) if issues else "none"))
    return 0


def read_events(root: Path) -> list[dict]:
    path = root / ".prime" / "events.jsonl"
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def append_event(root: Path, event: dict) -> None:
    path = root / ".prime" / "events.jsonl"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(existing + json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def parse_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [x.strip() for x in value.split(",") if x.strip()]


def resolve_roots(plan: dict, raw: str | None) -> list[str]:
    roots = parse_csv(raw)
    if roots == ["ALL"]:
        return topo_order(milestone_graph(plan))
    return roots


def human_change(root: Path, text: str, source_ref: str | None, roots_raw: str | None) -> None:
    state = load_state(root)
    plan = load_plan(root)
    if source_ref:
        for event in read_events(root):
            if event.get("source_ref") == source_ref:
                raise ControlError(f"source_ref already recorded: {source_ref}")
    generation = state.get("generation")
    if not isinstance(generation, int) or generation < 0:
        raise ControlError("invalid state.generation")
    new_generation = generation + 1
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    event_id = f"E-{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    roots = resolve_roots(plan, roots_raw)
    closure = downstream_closure(plan, roots) if roots else []
    event = {
        "event_id": event_id,
        "at": now.isoformat().replace("+00:00", "Z"),
        "kind": "human_change",
        "generation": new_generation,
        "text": text,
    }
    if source_ref:
        event["source_ref"] = source_ref
    if roots:
        event["roots"] = roots
    append_event(root, event)

    recon = state.get("reconciliation")
    if not isinstance(recon, dict):
        recon = {"status": "needs_reconcile", "event": event_id, "roots": [], "pending": [], "invalidated": []}
    prior_pending = recon.get("pending") if isinstance(recon.get("pending"), list) else []
    prior_invalid = recon.get("invalidated") if isinstance(recon.get("invalidated"), list) else []
    prior_roots = recon.get("roots") if isinstance(recon.get("roots"), list) else []
    state["generation"] = new_generation
    state["reconciliation"] = {
        "status": "needs_reconcile",
        "event": event_id,
        "roots": list(dict.fromkeys(prior_roots + roots)),
        "pending": list(dict.fromkeys(prior_pending + closure)),
        "invalidated": prior_invalid,
    }
    atomic_write_yaml(root / ".prime" / "state.yaml", state)
    print(f"HUMAN_CHANGE {event_id} generation={new_generation}")


def apply_impact(root: Path, roots_raw: str, apply: bool) -> None:
    plan = load_plan(root)
    roots = resolve_roots(plan, roots_raw)
    if not roots:
        raise ControlError("impact requires at least one root milestone or ALL")
    closure = downstream_closure(plan, roots)
    print("IMPACT " + ",".join(closure))
    if not apply:
        return
    state = load_state(root)
    recon = state.get("reconciliation")
    if not isinstance(recon, dict):
        recon = {"status": "needs_reconcile", "event": None, "roots": [], "pending": [], "invalidated": []}
    milestones = plan.get("milestones") or {}
    invalidated = list(recon.get("invalidated") or [])
    for mid in roots:
        data = milestones.get(mid)
        if isinstance(data, dict) and data.get("accepted_ref") is not None:
            if mid not in invalidated:
                invalidated.append(mid)
            data["accepted_ref"] = None
    recon["status"] = "needs_reconcile"
    recon["roots"] = list(dict.fromkeys(list(recon.get("roots") or []) + roots))
    recon["pending"] = list(dict.fromkeys(list(recon.get("pending") or []) + closure))
    recon["invalidated"] = invalidated
    state["reconciliation"] = recon
    atomic_write_yaml(root / ".prime" / "plan.yaml", plan)
    atomic_write_yaml(root / ".prime" / "state.yaml", state)
    print("IMPACT_APPLIED")


def load_all_tasks(root: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    tasks_root = root / ".prime" / "tasks"
    if not tasks_root.exists():
        return out
    for d in tasks_root.iterdir():
        if d.is_dir() and (d / "task.yaml").is_file():
            out[d.name] = load_simple_yaml(d / "task.yaml")
    return out


def reconcile_state(root: Path, done_raw: str | None, invalidate_raw: str | None, clean: bool) -> None:
    state = load_state(root)
    plan = load_plan(root)
    tasks = load_all_tasks(root)
    recon = state.get("reconciliation")
    if not isinstance(recon, dict):
        raise ControlError("state.reconciliation is invalid")
    pending = list(recon.get("pending") or [])
    invalidated = list(recon.get("invalidated") or [])
    milestones = plan.get("milestones") or {}

    for mid in parse_csv(invalidate_raw):
        if mid not in milestones:
            raise ControlError(f"unknown milestone to invalidate: {mid}")
        data = milestones[mid]
        if data.get("accepted_ref") is not None:
            data["accepted_ref"] = None
            if mid not in invalidated:
                invalidated.append(mid)
        if mid not in pending:
            pending.append(mid)

    done = parse_csv(done_raw)
    for mid in done:
        if mid not in milestones:
            raise ControlError(f"unknown milestone to reconcile: {mid}")
        deps_pending = transitive_dependencies(plan, mid) & set(pending)
        if deps_pending:
            raise ControlError(f"cannot reconcile {mid} before pending dependencies {sorted(deps_pending)}")
        if mid in invalidated:
            valid, why = accepted_ref_valid(plan, tasks, mid, root)
            if not valid:
                raise ControlError(f"cannot reconcile invalidated {mid} without current accepted realization: {why}")
            invalidated.remove(mid)
        if mid in pending:
            pending.remove(mid)

    recon["pending"] = pending
    recon["invalidated"] = invalidated
    if clean:
        if pending or invalidated:
            raise ControlError(f"cannot mark clean with pending={pending} invalidated={invalidated}")
        if not isinstance(plan.get("objective"), str) or not plan.get("objective").strip() or plan.get("objective") == "UNSET":
            raise ControlError("cannot mark clean before canonical project objective is reconciled")
        if not isinstance(state.get("generation"), int) or state.get("generation") < 1:
            raise ControlError("cannot mark clean before at least one Human intent generation")
        active = state.get("active_task")
        if active:
            task = tasks.get(active)
            if not task or task.get("validated_generation") != state.get("generation"):
                raise ControlError("cannot mark clean while active task generation is stale")
        recon = {"status": "clean", "event": None, "roots": [], "pending": [], "invalidated": []}
    else:
        recon["status"] = "needs_reconcile" if pending or invalidated or recon.get("event") else "clean"
    state["reconciliation"] = recon
    atomic_write_yaml(root / ".prime" / "plan.yaml", plan)
    atomic_write_yaml(root / ".prime" / "state.yaml", state)
    print(f"RECONCILE status={recon.get('status')} pending={len(recon.get('pending') or [])} invalidated={len(recon.get('invalidated') or [])}")


def dispatch_task(root: Path, task_id: str) -> None:
    if not git_ok(root) or not git_head(root):
        raise ControlError("dispatch requires a committed Git baseline")
    state = load_state(root)
    plan = load_plan(root)
    tasks = load_all_tasks(root)
    if task_id not in tasks:
        raise ControlError(f"task not found: {task_id}")
    task = tasks[task_id]
    status = task.get("status")
    if status not in {"draft", "active", "blocked"}:
        raise ControlError(f"cannot dispatch task in status {status!r}")
    active = state.get("active_task")
    if active not in {None, task_id}:
        raise ControlError(f"single mutation lease already owned by {active}")
    objective = task.get("objective_ref")
    milestones = plan.get("milestones") or {}
    if objective not in milestones:
        raise ControlError(f"task objective milestone missing: {objective}")
    if task.get("objective_spec_rev") != milestones[objective].get("spec_rev"):
        raise ControlError("task objective_spec_rev is stale; increment contract_rev and recontract explicitly")

    recon = state.get("reconciliation") or {}
    recon_status = recon.get("status")
    if recon_status == "needs_intent":
        raise ControlError("cannot dispatch before initial Human intent is reconciled")
    if recon_status == "blocked":
        raise ControlError("reconciliation is blocked")
    if recon_status == "needs_reconcile":
        pending = set(recon.get("pending") or [])
        invalidated = set(recon.get("invalidated") or [])
        deps = transitive_dependencies(plan, objective)
        blocked = deps & (pending | invalidated)
        if blocked:
            raise ControlError(f"cannot dispatch {objective} before impacted dependencies {sorted(blocked)}")

    # Structural task references and dependency readiness.
    local_report = CheckReport()
    validate_task_schema(root, plan, task, task_id, local_report)
    validate_task_refs(root, plan, tasks, task_id, task, local_report)
    task_dependencies_ready(plan, tasks, task_id, task, local_report, root=root)
    for adr in ((task.get("depends_on") or {}).get("decisions") or []):
        candidate = root / ".prime" / "decisions" / (adr if adr.endswith(".md") else f"{adr}.md")
        if not candidate.is_file():
            local_report.error("TASK_DEP_ADR", f"{task_id}: missing decision dependency {adr}")
    if local_report.errors:
        raise ControlError("; ".join(f"{c}: {m}" for c, m in local_report.errors))

    task_dir = root / ".prime" / "tasks" / task_id
    live = task.get("liveness") if isinstance(task.get("liveness"), dict) else {}
    if live.get("contract_rev") != task.get("contract_rev"):
        live = {"contract_rev": task.get("contract_rev"), "same_boundary_failures": 0, "last_failure_class": None, "last_failure_boundary": None}
        cp = task_dir / "checkpoint.yaml"
        if cp.exists():
            cp.unlink()

    workspace = task.get("workspace") if isinstance(task.get("workspace"), dict) else {}
    first_dispatch = task.get("dispatch_id") == 0 or not workspace.get("base_commit")
    if first_dispatch:
        dirty = sorted(p for p in git_dirty_paths(root) if not is_canonical_path(p))
        overlap = [p for p in dirty if path_in_scope(p, task.get("scope") or {})]
        if overlap:
            raise ControlError(f"dirty task scope overlaps existing Human/unreconciled work: {overlap}")
        protected = dirty
        workspace = {
            "base_commit": git_head(root),
            "protected_dirty_paths": protected,
            "protected_dirty_hash": protected_dirty_hash(root, protected),
        }
    else:
        protected = workspace.get("protected_dirty_paths") or []
        digest = workspace.get("protected_dirty_hash")
        if isinstance(digest, str) and protected_dirty_hash(root, protected) != digest:
            raise ControlError("protected dirty state drifted; reconcile Human/filesystem changes before redispatch")

    result_path = task_dir / "result.yaml"
    if result_path.exists():
        result = load_simple_yaml(result_path)
        exact_old = (
            result.get("task") == task_id
            and result.get("contract_rev") == task.get("contract_rev")
            and result.get("dispatch_id") == task.get("dispatch_id")
        )
        if exact_old and result.get("status") == "completed":
            raise ControlError("current dispatch already returned completed; accept or recontract instead of retry")
        if exact_old and result.get("status") in {"failed", "blocked", "needs_recontract"}:
            failure_class = result.get("failure_class") or str(result.get("status")).upper()
            boundary = result.get("failure_boundary") or "unknown"
            prev_class = live.get("last_failure_class")
            prev_boundary = live.get("last_failure_boundary")
            failures = live.get("same_boundary_failures") or 0
            failures = failures + 1 if prev_class == failure_class and prev_boundary == boundary else 1
            if failures >= 2:
                raise ControlError(f"same causal boundary failed twice ({failure_class}/{boundary}); split, recontract, or block")
            live["same_boundary_failures"] = failures
            live["last_failure_class"] = failure_class
            live["last_failure_boundary"] = boundary
        elif exact_old and result.get("status") == "interrupted":
            live["last_failure_class"] = "INTERRUPTED"
            live["last_failure_boundary"] = result.get("failure_boundary")
        result_path.unlink()

    task["governance_hash"] = governance_hash(root)
    task["validated_generation"] = state.get("generation")
    task["dispatch_id"] = int(task.get("dispatch_id") or 0) + 1
    task["status"] = "active"
    task["workspace"] = workspace
    task["liveness"] = live
    state["active_task"] = task_id
    atomic_write_yaml(task_dir / "task.yaml", task)
    atomic_write_yaml(root / ".prime" / "state.yaml", state)
    print(f"DISPATCH {task_id} r{task.get('contract_rev')} d{task.get('dispatch_id')} generation={state.get('generation')}")


def current_task_delta(root: Path, task: dict) -> set[str]:
    workspace = task.get("workspace") or {}
    base = workspace.get("base_commit")
    if not isinstance(base, str) or not base:
        raise ControlError("task has no base_commit")
    all_paths = git_changed_since(root, base)
    protected = set(workspace.get("protected_dirty_paths") or [])
    return {p for p in all_paths if not is_canonical_path(p) and p not in protected}


def accept_task(root: Path, task_id: str) -> None:
    state = load_state(root)
    plan = load_plan(root)
    tasks = load_all_tasks(root)
    if state.get("active_task") != task_id:
        raise ControlError(f"task {task_id} does not own the active mutation lease")
    if task_id not in tasks:
        raise ControlError(f"task not found: {task_id}")
    task = tasks[task_id]
    if task.get("status") not in {"active", "returned"}:
        raise ControlError(f"task status {task.get('status')!r} is not reviewable")
    if task.get("validated_generation") != state.get("generation"):
        raise ControlError("task generation is stale")
    if task.get("governance_hash") != governance_hash(root):
        raise ControlError("task governance is stale")
    result_path = root / ".prime" / "tasks" / task_id / "result.yaml"
    if not result_path.exists():
        raise ControlError("accept requires result.yaml")
    local_report = CheckReport()
    validate_task_schema(root, plan, task, task_id, local_report)
    validate_task_refs(root, plan, tasks, task_id, task, local_report)
    task_dependencies_ready(plan, tasks, task_id, task, local_report, root=root)
    validate_result(task, task_id, result_path.parent, local_report, current=True)
    if local_report.errors:
        raise ControlError("; ".join(f"{c}: {m}" for c, m in local_report.errors))
    result = load_simple_yaml(result_path)
    exact = (
        result.get("task") == task_id
        and result.get("contract_rev") == task.get("contract_rev")
        and result.get("dispatch_id") == task.get("dispatch_id")
    )
    if not exact or result.get("status") != "completed":
        raise ControlError("accept requires exact current completed result")
    missing = [x for x in task.get("acceptance_refs") or [] if x not in (result.get("proved") or [])]
    if missing:
        raise ControlError(f"result missing acceptance refs: {missing}")
    changed = result.get("changed")
    if not isinstance(changed, list):
        raise ControlError("result.changed must be list")
    outside_claim = [p for p in changed if not isinstance(p, str) or not path_in_scope(p, task.get("scope") or {})]
    if outside_claim:
        raise ControlError(f"result claims out-of-scope paths: {outside_claim}")
    workspace = task.get("workspace") or {}
    protected = workspace.get("protected_dirty_paths") or []
    digest = workspace.get("protected_dirty_hash")
    if isinstance(digest, str) and protected_dirty_hash(root, protected) != digest:
        raise ControlError("protected dirty state changed; reconcile before acceptance")
    actual = current_task_delta(root, task)
    outside_actual = sorted(p for p in actual if not path_in_scope(p, task.get("scope") or {}))
    if outside_actual:
        raise ControlError(f"actual task delta outside scope: {outside_actual}")
    missing_claim = sorted(actual - set(changed))
    phantom_claim = sorted(set(changed) - actual)
    if missing_claim:
        raise ControlError(f"result.changed omits actual task paths: {missing_claim}")
    if phantom_claim:
        raise ControlError(f"result.changed claims paths absent from actual task delta: {phantom_claim}")
    task["status"] = "accepted"
    atomic_write_yaml(root / ".prime" / "tasks" / task_id / "task.yaml", task)
    state["active_task"] = None
    objective = task.get("objective_ref")
    required = set(acceptance_refs(plan, objective))
    if required and required.issubset(set(task.get("acceptance_refs") or [])):
        plan["milestones"][objective]["accepted_ref"] = task_id
    cp = root / ".prime" / "tasks" / task_id / "checkpoint.yaml"
    if cp.exists():
        cp.unlink()
    atomic_write_yaml(root / ".prime" / "plan.yaml", plan)
    atomic_write_yaml(root / ".prime" / "state.yaml", state)
    print(f"ACCEPTED {task_id} objective={objective}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prime Lean v2 deterministic control utility")
    parser.add_argument("--root", default=".", help="project root")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("hash", help="print semantic governance hash")
    p_boot = sub.add_parser("bootstrap", help="create canonical state and optionally initialize Git")
    p_boot.add_argument("--init-git", action="store_true")
    sub.add_parser("check", help="validate canonical state, graph, task/result contracts, and Git guards")
    sub.add_parser("resume", help="print compact ephemeral recovery capsule")

    p_human = sub.add_parser("human-change", help="persist one Human intent event and increment generation exactly once")
    p_human.add_argument("--text", required=True)
    p_human.add_argument("--source-ref")
    p_human.add_argument("--roots", help="optional comma-separated affected milestone roots, or ALL")

    p_impact = sub.add_parser("impact", help="compute downstream milestone closure")
    p_impact.add_argument("--roots", required=True, help="comma-separated milestone roots, or ALL")
    p_impact.add_argument("--apply", action="store_true", help="persist closure and invalidate root accepted refs")

    p_recon = sub.add_parser("reconcile", help="advance deterministic reconciliation frontier")
    p_recon.add_argument("--done", help="comma-separated milestones revalidated against current intent")
    p_recon.add_argument("--invalidate", help="comma-separated milestones whose accepted realization proved stale")
    p_recon.add_argument("--clean", action="store_true")

    p_dispatch = sub.add_parser("dispatch", help="acquire the single mutation lease and issue next dispatch id")
    p_dispatch.add_argument("task_id")
    p_accept = sub.add_parser("accept", help="structurally validate exact result/diff and accept current task")
    p_accept.add_argument("task_id")

    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    try:
        if args.command == "hash":
            print(governance_hash(root))
            return 0
        if args.command == "bootstrap":
            bootstrap(root, args.init_git)
            return 0
        if args.command == "check":
            return print_check(root)
        if args.command == "resume":
            return print_resume(root)
        if args.command == "human-change":
            human_change(root, args.text, args.source_ref, args.roots)
            return 0
        if args.command == "impact":
            apply_impact(root, args.roots, args.apply)
            return 0
        if args.command == "reconcile":
            reconcile_state(root, args.done, args.invalidate, args.clean)
            return 0
        if args.command == "dispatch":
            dispatch_task(root, args.task_id)
            return 0
        if args.command == "accept":
            accept_task(root, args.task_id)
            return 0
    except (OSError, ValueError, json.JSONDecodeError, ControlError) as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 3
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
