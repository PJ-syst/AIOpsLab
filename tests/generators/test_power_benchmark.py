# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Unit tests for benchmark selection and scoring (no cluster required)."""

import pandas as pd

from clients.power_eval import (
    CURRENT_SMOKE_IDS,
    POWER_SMOKE_IDS,
    benchmark_problem_ids,
    benchmark_scorecard,
    fault_family_of,
    suite_of,
    task_type_of,
)


def test_named_suites_are_stable_and_disjoint():
    assert benchmark_problem_ids("power-smoke") == POWER_SMOKE_IDS
    assert benchmark_problem_ids("current-smoke") == CURRENT_SMOKE_IDS
    assert len(benchmark_problem_ids("power")) == 21
    assert all(pid.startswith("power_") for pid in benchmark_problem_ids("power"))
    assert all(
        not pid.startswith("power_") for pid in benchmark_problem_ids("current")
    )


def test_explicit_problem_ids_override_suite_filters():
    ids = ["noop_detection_hotel_reservation-1"]
    assert benchmark_problem_ids("power", problem_ids=ids) == ids


def test_problem_classification_handles_upstream_id_shapes():
    assert task_type_of("power_outage_hotel_res-mitigation-1") == "mitigation"
    assert task_type_of("container_kill-detection") == "detection"
    assert task_type_of("noop_detection_hotel_reservation-1") == "detection"
    assert suite_of("power_brownout_hotel_res-detection-1") == "power"
    assert suite_of("network_delay_hotel_res-detection-1") == "current"
    assert fault_family_of("power_generator_cutover_hotel_res-analysis-1") == (
        "generator_cutover"
    )


def test_scorecard_keeps_task_scores_and_safety_metrics_separate():
    df = pd.DataFrame(
        [
            {
                "suite": "power",
                "task_type": "detection",
                "success": True,
                "accuracy": None,
                "is_no_fault_control": False,
                "proactive": None,
            },
            {
                "suite": "power",
                "task_type": "localization",
                "success": False,
                "accuracy": 50.0,
                "is_no_fault_control": False,
                "proactive": None,
            },
            {
                "suite": "power",
                "task_type": "analysis",
                "success": False,
                "accuracy": None,
                "is_no_fault_control": False,
                "proactive": None,
            },
            {
                "suite": "power",
                "task_type": "mitigation",
                "success": True,
                "accuracy": None,
                "is_no_fault_control": False,
                "proactive": True,
            },
            {
                "suite": "current",
                "task_type": "detection",
                "success": False,
                "accuracy": None,
                "is_no_fault_control": True,
                "proactive": None,
            },
        ]
    )

    scorecard = benchmark_scorecard(df).set_index("suite")
    assert scorecard.loc["power", "detection_score"] == 1.0
    assert scorecard.loc["power", "localization_score"] == 0.5
    assert scorecard.loc["power", "analysis_score"] == 0.0
    assert scorecard.loc["power", "mitigation_score"] == 1.0
    assert scorecard.loc["power", "overall_score"] == 0.625
    assert scorecard.loc["power", "proactive_rate"] == 1.0
    assert scorecard.loc["current", "false_positive_rate"] == 1.0
