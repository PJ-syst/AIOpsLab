"""Regression tests for score inflation and evaluator shortcuts."""
from types import SimpleNamespace as NS

import pandas as pd

from clients.power_eval import extract_row, benchmark_scorecard
from tests.generators.test_power_problems import make_analysis_task, make_mitigation_task
from aiopslab.orchestrator.problems.power_faults.power_control import PowerHealthyDetection


def test_failed_run_stays_in_denominator():
    good = extract_row("power_outage_hotel_res-analysis-1", {"results": {"success": True}})
    bad = extract_row("power_outage_hotel_res-analysis-1", {})
    score = benchmark_scorecard(pd.DataFrame([good, bad])).iloc[0]
    assert score["analysis_score"] == 0.5
    assert score["attempted"] == 2
    assert score["completed"] == 1


def test_constant_yes_fails_healthy_control():
    task = PowerHealthyDetection.__new__(PowerHealthyDetection)
    task.results = {}
    task.eval_detection_solution("Yes")
    assert task.results["Detection Accuracy"] == "Incorrect"


def test_generic_power_answer_is_insufficient():
    task = make_analysis_task()
    task.eval_power_analysis({"system_level": "Hardware", "fault_type": "Power Issue"})
    assert task.results["success"] is False


def test_empty_application_is_unhealthy():
    task, _ = make_mitigation_task()
    task.kubectl = NS(list_pods=lambda namespace: NS(items=[]))
    assert task.pods_unhealthy_reasons(task.namespace)
