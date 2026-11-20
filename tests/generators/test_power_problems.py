# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Unit tests for the power fault problems: registry + evaluation logic.

Problem constructors deploy real applications, so task instances here are
built with ``__new__`` (the same pattern as tests/orchestrator/
test_mitigation_settle.py) and given explicit attributes. Nothing in this
file touches a cluster.
"""

from types import SimpleNamespace as NS

import pytest

import aiopslab.orchestrator.tasks.base as tasks_base
from aiopslab.orchestrator.problems.registry import ProblemRegistry
from aiopslab.orchestrator.problems.power_faults import (
    PowerOutageAnalysis,
    PowerOutageDetection,
    PowerOutageLocalization,
    PowerUpsDrainMitigation,
)
from aiopslab.generators.fault.inject_power import PowerFaultInjector
from aiopslab.generators.fault.power.ups import PowerController

from tests.generators.test_power_injector import FakeDocker


POWER_PROBLEM_IDS = [
    f"power_{fault}_hotel_res-{task}-1"
    for fault in [
        "outage",
        "brownout",
        "load_shedding",
        "generator_cutover",
        "ups_drain",
    ]
    for task in ["detection", "localization", "analysis", "mitigation"]
]


class StubConfig:
    """ qualitative_eval always off: never let eval make LLM calls. """

    def get(self, key, default=None):
        if key == "qualitative_eval":
            return False
        return default


@pytest.fixture(autouse=True)
def no_qualitative_eval(monkeypatch):
    monkeypatch.setattr(tasks_base, "config", StubConfig())
    # Keep the polled node-readiness window tiny for unit tests.
    monkeypatch.setenv("AIOPSLAB_NODE_READY_TIMEOUT", "1")


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #


class TestRegistry:
    def test_all_power_problems_registered(self):
        registry = ProblemRegistry()
        for pid in POWER_PROBLEM_IDS:
            assert pid in registry.PROBLEM_REGISTRY, f"missing problem id: {pid}"

    def test_power_problems_deploy_on_k8s(self):
        registry = ProblemRegistry()
        for pid in POWER_PROBLEM_IDS:
            assert registry.get_problem_deployment(pid) == "k8s"

    def test_get_problem_instance_returns_factory(self):
        registry = ProblemRegistry()
        factory = registry.get_problem("power_outage_hotel_res-detection-1")
        assert callable(factory)


# --------------------------------------------------------------------------- #
# Eval logic (instances built via __new__, no deployment)
# --------------------------------------------------------------------------- #


def make_detection_task():
    task = PowerOutageDetection.__new__(PowerOutageDetection)
    task.results = {}
    return task


def make_localization_task(nodes):
    task = PowerOutageLocalization.__new__(PowerOutageLocalization)
    task.results = {}
    task.faulty_nodes = list(nodes)
    return task


def make_analysis_task():
    task = PowerOutageAnalysis.__new__(PowerOutageAnalysis)
    task.results = {}
    task.faulty_nodes = ["kind-worker"]
    return task


def make_mitigation_task(node="kind-worker", ever_down=False):
    task = PowerUpsDrainMitigation.__new__(PowerUpsDrainMitigation)
    task.results = {}
    task.namespace = "test-hotel-reservation"
    task.faulty_nodes = [node]
    task.kubectl = FakeHealthyKubeCtl()
    docker = FakeDocker([node])
    injector = PowerFaultInjector(
        "test-hotel-reservation",
        kubectl=None,
        docker=docker,
        controller=PowerController(publish=False),
    )
    if ever_down:
        injector.power_off_node(node)
        injector.power_on_node(node)
    task.injector = injector
    return task, injector


class FakeHealthyKubeCtl:
    def list_pods(self, namespace):
        return NS(items=[NS(status=NS(container_statuses=[NS(name="c", ready=True, state=NS(waiting=None, terminated=None))]))])


class TestDetectionEval:
    def test_yes_is_correct(self):
        task = make_detection_task()
        results = task.eval("Yes", [], 1.0)
        assert results["Detection Accuracy"] == "Correct"
        assert results["TTD"] == 1.0

    def test_no_is_incorrect(self):
        task = make_detection_task()
        results = task.eval("No", [], 1.0)
        assert results["Detection Accuracy"] == "Incorrect"

    def test_non_string_is_invalid_format(self):
        task = make_detection_task()
        results = task.eval(["Yes"], [], 1.0)
        assert results["Detection Accuracy"] == "Invalid Format"


class TestLocalizationEval:
    def test_exact_node_match(self):
        task = make_localization_task(["kind-worker"])
        results = task.eval(["kind-worker"], [], 1.0)
        assert results["Localization Accuracy"] == 100.0
        assert results["success"] is True
        assert results["TTL"] == 1.0

    def test_single_node_string_counts_as_exact(self):
        task = make_localization_task(["kind-worker"])
        results = task.eval("kind-worker", [], 1.0)
        assert results["Localization Accuracy"] == 100.0

    def test_superset_gets_partial_credit(self):
        task = make_localization_task(["kind-worker"])
        results = task.eval(["kind-worker", "kind-worker2"], [], 1.0)
        assert results["Localization Accuracy"] == pytest.approx(200 / 3)
        assert results["success"] is False
        assert results["is_subset"] is True

    def test_wrong_node_is_zero(self):
        task = make_localization_task(["kind-worker"])
        results = task.eval(["some-other-node"], [], 1.0)
        assert results["Localization Accuracy"] == 0.0
        assert results["success"] is False

    def test_none_solution_is_zero(self):
        task = make_localization_task(["kind-worker"])
        results = task.eval(None, [], 1.0)
        assert results["Localization Accuracy"] == 0.0
        assert results["success"] is False


class TestAnalysisEval:
    def test_correct_power_analysis(self):
        task = make_analysis_task()
        results = task.eval(
            {"system_level": "Hardware", "fault_type": "Power Issue", "fault_family": "outage", "affected_nodes": ["kind-worker"], "evidence": "Input voltage zero"}, [], 1.0
        )
        assert results["success"] is True
        assert results["system_level_correct"] is True
        assert results["fault_type_correct"] is True
        assert results["TTA"] == 1.0

    def test_wrong_fault_type(self):
        task = make_analysis_task()
        results = task.eval(
            {"system_level": "Hardware", "fault_type": "Misconfiguration"},
            [],
            1.0,
        )
        assert results["success"] is False

    def test_non_dict_solution(self):
        task = make_analysis_task()
        results = task.eval("Hardware", [], 1.0)
        assert results["success"] is False


class TestMitigationEval:
    @pytest.fixture
    def patch_node_ready(self, monkeypatch):
        import aiopslab.orchestrator.problems.power_faults.power_base as pb

        monkeypatch.setattr(pb, "node_is_ready", lambda kubectl, node: True)

    def test_success_when_cluster_recovered(self, patch_node_ready):
        task, _ = make_mitigation_task()
        results = task.eval(None, [], 1.0)
        assert results["success"] is True
        assert results["TTM"] >= 1.0

    def test_proactive_when_node_never_went_down(self, patch_node_ready):
        task, injector = make_mitigation_task(ever_down=False)
        injector.facility_restored_by_agent = True
        results = task.eval(None, [], 1.0)
        assert results["proactive"] is True

    def test_reactive_when_node_had_died(self, patch_node_ready):
        task, _ = make_mitigation_task(ever_down=True)
        results = task.eval(None, [], 1.0)
        assert results["proactive"] is False

    def test_failure_when_node_not_ready(self, patch_node_ready, monkeypatch):
        import aiopslab.orchestrator.problems.power_faults.power_base as pb

        monkeypatch.setattr(pb, "node_is_ready", lambda kubectl, node: False)
        task, _ = make_mitigation_task()
        results = task.eval(None, [], 1.0)
        assert results["success"] is False
