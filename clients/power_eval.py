# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Batch evaluation of current AIOpsLab and power-aware problems.

Runs any registered agent over current and/or ``power_*`` problems, collects
the per-problem evaluation metrics, and produces an aggregate summary on the
console plus a per-problem CSV.

Examples:
    # Zero-cost pipeline validation (no LLM calls, no API keys)
    python clients/power_eval.py --agent dummy --suite power-smoke

    # Complete power benchmark or representative upstream controls
    python clients/power_eval.py --agent generic --suite power
    python clients/power_eval.py --agent generic --suite current-smoke

    # Scoped power runs
    python clients/power_eval.py --agent generic --faults ups_drain outage
    python clients/power_eval.py --agent generic --task-types detection mitigation
    python clients/power_eval.py --agent generic --max-steps 15 --output my_eval.csv

Metrics collected per problem (see POWER_FAULTS.md for the full glossary):
    detection    -> Detection Accuracy ("Correct"/"Incorrect"/"Invalid Format"), TTD
    localization -> Localization Accuracy (0-100), TTL, success
    analysis     -> system_level_correct, fault_type_correct, success, TTA
    mitigation   -> success, TTM, proactive (UPS-drain: acted before depletion)
    all          -> steps, in_tokens, out_tokens, framework_overhead

Each run also persists the full session JSON (trace + results) via the
orchestrator to aiopslab/data/results/, so nothing is lost even if the
batch is interrupted. The CSV is appended after every problem.
"""

import argparse
import asyncio
import os
import sys
import time
from datetime import datetime

import pandas as pd

# Allow running this file directly from a source checkout
# (no-op when aiopslab is pip/poetry-installed).
if __package__ in (None, ""):
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

# Windows consoles default to a legacy code page (cp1252); traces and logs
# contain non-ASCII (e.g. 'us' durations from Jaeger) which crash print().
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from aiopslab.orchestrator import Orchestrator
from aiopslab.orchestrator.problems.registry import ProblemRegistry
from aiopslab.paths import RESULTS_DIR

POWER_FAULTS = [
    "outage",
    "brownout",
    "load_shedding",
    "generator_cutover",
    "ups_drain",
]
TASK_TYPES = ["detection", "localization", "analysis", "mitigation"]

# Small, stable control set drawn only from Microsoft AIOpsLab's existing
# registry. It covers a no-fault control plus virtualization, application,
# dependency, and network failures without pretending that a short suite is
# the whole upstream benchmark.
CURRENT_SMOKE_IDS = [
    "noop_detection_hotel_reservation-1",
    "k8s_target_port-misconfig-detection-1",
    "k8s_target_port-misconfig-localization-1",
    "k8s_target_port-misconfig-analysis-1",
    "k8s_target_port-misconfig-mitigation-1",
    "misconfig_app_hotel_res-detection-1",
    "misconfig_app_hotel_res-localization-1",
    "misconfig_app_hotel_res-analysis-1",
    "misconfig_app_hotel_res-mitigation-1",
    "network_delay_hotel_res-detection-1",
    "network_delay_hotel_res-localization-1",
]

POWER_SMOKE_IDS = [
    "power_outage_hotel_res-detection-1",
    "power_brownout_hotel_res-detection-1",
    "power_load_shedding_hotel_res-detection-1",
    "power_generator_cutover_hotel_res-detection-1",
    "power_ups_drain_hotel_res-detection-1",
    "power_ups_drain_hotel_res-localization-1",
    "power_ups_drain_hotel_res-analysis-1",
    "power_ups_drain_hotel_res-mitigation-1",
]

DETECTION_METRIC_KEY = "Detection Accuracy"
LOCALIZATION_METRIC_KEY = "Localization Accuracy"
TIME_KEYS = {
    "detection": "TTD",
    "localization": "TTL",
    "analysis": "TTA",
    "mitigation": "TTM",
}


class DummyYesAgent:
    """Zero-cost agent for pipeline validation: immediately submits "Yes".

    Correct for every detection problem (a fault is always injected), wrong
    for the other task types — the point is to prove the full lifecycle
    (deploy -> inject -> workload -> submit -> eval -> recover) and produce
    baseline TTD/steps/tokens without any API keys.
    """

    async def get_action(self, observation: str) -> str:
        return '```\nsubmit("Yes")\n```'

    def init_context(self, problem_desc, instructions, apis):
        pass


def power_problem_ids(
    faults: list[str] | None = None,
    task_types: list[str] | None = None,
    problem_ids: list[str] | None = None,
) -> list[str]:
    """Return matching power problem IDs in a stable (fault, task) order.

    ``problem_ids`` (optional) overrides fault/task filtering with an
    explicit list, validated against the registry — used to include
    non-power control problems (e.g. the no-op detection problems) in a
    batch.
    """
    registry = ProblemRegistry()
    if problem_ids:
        for pid in problem_ids:
            if pid not in registry.PROBLEM_REGISTRY:
                raise ValueError(f"Unknown problem id: {pid}")
        return list(problem_ids)

    faults = faults or POWER_FAULTS
    task_types = task_types or TASK_TYPES

    ids = []
    for fault in faults:
        for task in task_types:
            pid = f"power_{fault}_hotel_res-{task}-1"
            if pid in registry.PROBLEM_REGISTRY:
                ids.append(pid)
            else:
                raise ValueError(f"Unknown power problem id: {pid}")
    return ids


def benchmark_problem_ids(
    suite: str = "power",
    faults: list[str] | None = None,
    task_types: list[str] | None = None,
    problem_ids: list[str] | None = None,
) -> list[str]:
    """Resolve a named benchmark suite to registered problem IDs."""
    registry = ProblemRegistry()
    if problem_ids:
        return power_problem_ids(problem_ids=problem_ids)

    current_all = [
        pid for pid in registry.get_problem_ids() if not pid.startswith("power_")
    ]
    power_all = power_problem_ids(faults=faults, task_types=task_types)
    suites = {
        "power-smoke": POWER_SMOKE_IDS,
        "power": power_all,
        "current-smoke": CURRENT_SMOKE_IDS,
        "current": current_all,
        "all-smoke": CURRENT_SMOKE_IDS + POWER_SMOKE_IDS,
        "all": current_all + power_all,
    }
    if suite not in suites:
        raise ValueError(f"Unknown benchmark suite: {suite}")
    return list(suites[suite])


def task_type_of(problem_id: str) -> str:
    for task in TASK_TYPES:
        if (
            f"-{task}-" in problem_id
            or problem_id.endswith(f"-{task}")
            or f"_{task}_" in problem_id
        ):
            return task
    raise ValueError(f"Cannot infer task type from {problem_id}")


def suite_of(problem_id: str) -> str:
    return "power" if problem_id.startswith("power_") else "current"


def fault_family_of(problem_id: str) -> str:
    if problem_id.startswith("power_"):
        return problem_id.removeprefix("power_").split("_hotel_res-")[0]
    return problem_id.split("-", 1)[0]


def extract_row(problem_id: str, output: dict) -> dict:
    """Flatten one orchestrator run's results into a CSV row."""
    task = task_type_of(problem_id)
    results = output.get("results", {}) or {}

    row = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "problem_id": problem_id,
        "task_type": task,
        "suite": suite_of(problem_id),
        "fault_family": fault_family_of(problem_id),
        "is_no_fault_control": problem_id.startswith("noop_"),
        "steps": results.get("steps"),
        "in_tokens": results.get("in_tokens"),
        "out_tokens": results.get("out_tokens"),
        "framework_overhead": output.get("framework_overhead"),
    }

    time_key = TIME_KEYS[task]
    if time_key in results:
        row["time_metric_name"] = time_key
        row["time_metric_value"] = results[time_key]

    if task == "detection":
        accuracy = results.get(DETECTION_METRIC_KEY)
        row["accuracy"] = accuracy
        row["success"] = str(accuracy) == "Correct"
    elif task == "localization":
        row["accuracy"] = results.get(LOCALIZATION_METRIC_KEY)
        row["success"] = results.get("success")
        row["is_subset"] = results.get("is_subset")
    elif task == "analysis":
        row["success"] = results.get("success")
        row["system_level_correct"] = results.get("system_level_correct")
        row["fault_type_correct"] = results.get("fault_type_correct")
    elif task == "mitigation":
        row["success"] = results.get("success")
        if "proactive" in results:
            row["proactive"] = results["proactive"]

    return row


def run_problem(problem_id: str, agent, agent_name: str, max_steps: int) -> dict:
    """Run one problem end to end and return the orchestrator output dict.

    Faults are eagerly recovered on failure (the orchestrator's own atexit
    hook only fires at interpreter shutdown, far too late for a batch run:
    a node left powered off would poison every following problem).
    """
    orchestrator = Orchestrator()
    orchestrator.register_agent(agent, name=agent_name)

    try:
        problem_desc, instructions, apis = orchestrator.init_problem(problem_id)
        init_context = getattr(agent, "init_context", None)
        if callable(init_context):
            init_context(problem_desc, instructions, apis)
        return asyncio.run(orchestrator.start_problem(max_steps=max_steps))
    except Exception as e:
        print(f"[power_eval] problem {problem_id} failed: {e}")
        # Ensure the fault is recovered even when the run blows up mid-way.
        try:
            if orchestrator.session is not None and orchestrator.session.problem is not None:
                orchestrator.session.problem.recover_fault()
        except Exception:
            pass
        return {}


def summarize(df: pd.DataFrame) -> pd.DataFrame:
    """Aggregate metrics per suite and task type."""
    summary_rows = []
    for (suite, task), group in df.groupby(["suite", "task_type"]):
        entry = {
            "suite": suite,
            "task_type": task,
            "problems": len(group),
        }
        if task == "detection":
            entry["accuracy_rate"] = group["success"].mean()
        elif task == "localization":
            entry["mean_accuracy"] = group["accuracy"].mean()
            entry["success_rate"] = group["success"].mean()
        elif task == "analysis":
            entry["success_rate"] = group["success"].mean()
        elif task == "mitigation":
            entry["success_rate"] = group["success"].mean()
            if "proactive" in group.columns:
                subset = group["proactive"].dropna()
                entry["proactive_rate"] = subset.mean() if len(subset) else None
        time_values = group.get("time_metric_value", pd.Series(dtype=float)).dropna()
        if len(time_values):
            entry["mean_time_metric"] = time_values.mean()
        entry["mean_steps"] = group.get("steps", pd.Series(dtype=float)).mean()
        entry["total_in_tokens"] = group.get(
            "in_tokens", pd.Series(dtype=float)
        ).sum()
        entry["total_out_tokens"] = group.get(
            "out_tokens", pd.Series(dtype=float)
        ).sum()
        summary_rows.append(entry)
    return pd.DataFrame(summary_rows)


def benchmark_scorecard(df: pd.DataFrame) -> pd.DataFrame:
    """Return normalized task and overall scores without hiding hard gates.

    Detection, analysis, and mitigation are binary per scenario.
    Localization uses the framework's partial-credit percentage. Proactive
    mitigation and no-fault false positives remain separate diagnostics.
    """
    scored = df.copy()
    scored["normalized_score"] = None
    detection = scored["task_type"] == "detection"
    localization = scored["task_type"] == "localization"
    analysis = scored["task_type"] == "analysis"
    mitigation = scored["task_type"] == "mitigation"
    scored.loc[detection | analysis | mitigation, "normalized_score"] = scored.loc[
        detection | analysis | mitigation, "success"
    ].map({True: 1.0, False: 0.0})
    scored.loc[localization, "normalized_score"] = (
        scored.loc[localization, "accuracy"].astype(float) / 100.0
    )

    rows = []
    for suite, group in scored.groupby("suite"):
        task_scores = {
            task: values["normalized_score"].dropna().astype(float).mean()
            for task, values in group.groupby("task_type")
        }
        controls = group[group["is_no_fault_control"] == True]  # noqa: E712
        proactive = group["proactive"].dropna() if "proactive" in group else []
        row = {
            "suite": suite,
            **{f"{task}_score": task_scores.get(task) for task in TASK_TYPES},
            "overall_score": (
                sum(task_scores.values()) / len(task_scores) if task_scores else None
            ),
            "false_positive_rate": (
                1.0 - controls["success"].astype(float).mean()
                if len(controls)
                else None
            ),
            "proactive_rate": (
                pd.Series(proactive).astype(float).mean() if len(proactive) else None
            ),
        }
        rows.append(row)
    return pd.DataFrame(rows)


def run_power_eval(
    agent_name: str = "dummy",
    suite: str = "power",
    faults: list[str] | None = None,
    task_types: list[str] | None = None,
    max_steps: int = 20,
    output: str | None = None,
    problem_ids: list[str] | None = None,
) -> pd.DataFrame:
    """Run a batch evaluation over a named current/power problem suite.

    Args:
        agent_name: Registry agent name, or "dummy" for the zero-cost agent.
        suite: Named suite resolved by :func:`benchmark_problem_ids`.
        faults: Subset of POWER_FAULTS to evaluate (default: all).
        task_types: Subset of task types (default: all).
        max_steps: Max agent steps per problem.
        output: CSV path for per-problem results
            (default: aiopslab/data/results/power_eval_<timestamp>.csv).
        problem_ids: Explicit problem IDs to run (overrides faults/task_types).

    Returns:
        DataFrame with one row per problem.
    """
    if agent_name == "dummy":
        make_agent = DummyYesAgent
        agent_name = "dummy-yes"
    else:
        # Lazy import: clients.registry imports every client module.
        from clients.registry import AgentRegistry

        agent_cls = AgentRegistry().get_agent(agent_name)
        if agent_cls is None:
            raise ValueError(
                f"Unknown agent '{agent_name}'. Registered agents: "
                f"{AgentRegistry().get_agent_ids()} + 'dummy'"
            )
        make_agent = agent_cls

    problem_ids = benchmark_problem_ids(
        suite=suite,
        faults=faults,
        task_types=task_types,
        problem_ids=problem_ids,
    )
    print(
        f"[power_eval] agent={agent_name} | suite={suite} | "
        f"problems={len(problem_ids)} | "
        f"max_steps={max_steps}"
    )

    if output is None:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output = str(RESULTS_DIR / f"power_eval_{agent_name}_{stamp}.csv")

    rows = []
    for pid in problem_ids:
        print(f"\n[power_eval] ===== {pid} =====")
        start = time.time()
        try:
            output_dict = run_problem(pid, make_agent(), agent_name, max_steps)
            row = extract_row(pid, output_dict or {})
        except Exception as e:
            print(f"[power_eval] crashed on {pid}: {e}")
            row = {
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "problem_id": pid,
                "task_type": task_type_of(pid),
                "suite": suite_of(pid),
                "fault_family": fault_family_of(pid),
                "is_no_fault_control": pid.startswith("noop_"),
                "success": False,
                "crashed": True,
                "steps": None,
                "in_tokens": None,
                "out_tokens": None,
            }
        row["wall_time"] = round(time.time() - start, 1)
        rows.append(row)

        # Persist incrementally so partial results survive interruptions.
        df = pd.DataFrame(rows)
        df.to_csv(output, index=False)

    df = pd.DataFrame(rows)
    print(f"\n[power_eval] per-problem results saved to: {output}")
    print("\n[power_eval] ===== Aggregate summary =====")
    print(summarize(df).to_string(index=False))
    print("\n[power_eval] ===== Benchmark scorecard =====")
    print(benchmark_scorecard(df).to_string(index=False))
    return df


def main():
    parser = argparse.ArgumentParser(
        description="Batch evaluation of current AIOpsLab and power faults."
    )
    parser.add_argument(
        "--agent",
        default="dummy",
        help="Agent registry name (e.g. generic, gpt) or 'dummy' "
        "(default: dummy — zero-cost pipeline validation)",
    )
    parser.add_argument(
        "--suite",
        choices=[
            "power-smoke",
            "power",
            "current-smoke",
            "current",
            "all-smoke",
            "all",
        ],
        default="power",
        help="Named benchmark suite (default: power).",
    )
    parser.add_argument(
        "--faults",
        nargs="*",
        choices=POWER_FAULTS,
        help="Subset of power faults to evaluate (default: all)",
    )
    parser.add_argument(
        "--task-types",
        nargs="*",
        choices=TASK_TYPES,
        help="Subset of task types to evaluate (default: all)",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=20,
        help="Max agent steps per problem (default: 20)",
    )
    parser.add_argument(
        "--problem-ids",
        nargs="*",
        help="Explicit problem IDs to run (overrides --faults/--task-types; "
        "e.g. add the no-op detection problems as false-positive controls)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="CSV path for per-problem results (default: results dir)",
    )
    args = parser.parse_args()

    run_power_eval(
        agent_name=args.agent,
        suite=args.suite,
        faults=args.faults,
        task_types=args.task_types,
        max_steps=args.max_steps,
        output=args.output,
        problem_ids=args.problem_ids,
    )


if __name__ == "__main__":
    main()
