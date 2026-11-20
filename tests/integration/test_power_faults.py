# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Integration tests for power-aware faults (require a live kind cluster).

Run on a machine with kind + Docker:

    poetry run pytest tests/integration/test_power_faults.py -v -s -m integration

Recommended cluster (load shedding needs >= 2 workers):

    kind create cluster --config kind/kind-config-power.yaml --wait 120s

Layers:
  1. injector-level: inject -> assert measurable trace -> recover -> restored
  2. two-phase battery drain with live telemetry (ConfigMap channel)
  3. end-to-end runs of registered problems with a dummy agent
  4. detectability gate: a scripted agent must be able to observe the fault
     via get_power_status() and answer correctly

Set AIOPSLAB_POWER_E2E_ALL=1 to run all 20 power problems end to end
(each deploys HotelReservation; expect ~10-25 min per problem on CI).
"""

import asyncio
import os
import time

import pytest

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------- #
# Availability guards (skip, don't fail, when no cluster is present)
# --------------------------------------------------------------------------- #


def _cluster_available() -> bool:
    try:
        from aiopslab.service.kubectl import KubeCtl

        KubeCtl().list_namespaces()
        return True
    except Exception:
        return False


def _docker_available() -> bool:
    try:
        import docker

        docker.from_env().ping()
        return True
    except Exception:
        return False


CLUSTER_OK = _cluster_available()
DOCKER_OK = _docker_available()

requires_power_testbed = pytest.mark.skipif(
    not (CLUSTER_OK and DOCKER_OK),
    reason="power fault tests require a live kind cluster and Docker",
)


@pytest.fixture(autouse=True)
def cleanup_power_telemetry():
    """Remove the published UPS state after each test (best-effort)."""
    yield
    if not CLUSTER_OK:
        return
    try:
        from aiopslab.service.kubectl import KubeCtl
        from aiopslab.generators.fault.power.ups import (
            UPS_CONFIGMAP_NAME,
            UPS_CONFIGMAP_NAMESPACE,
        )

        KubeCtl().exec_command(
            f"kubectl delete configmap {UPS_CONFIGMAP_NAME} "
            f"-n {UPS_CONFIGMAP_NAMESPACE} --ignore-not-found=true"
        )
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def worker_nodes():
    from aiopslab.generators.fault.inject_power import list_worker_nodes
    from aiopslab.service.kubectl import KubeCtl

    return list_worker_nodes(KubeCtl())


def container_status(node: str) -> str:
    import docker

    return docker.from_env().containers.get(node).status


def node_is_ready(node: str) -> bool:
    from aiopslab.service.kubectl import KubeCtl

    kubectl = KubeCtl()
    try:
        node_info = kubectl.core_v1_api.read_node(node)
    except Exception:
        return False
    for condition in getattr(node_info.status, "conditions", None) or []:
        if condition.type == "Ready":
            return condition.status == "True"
    return False


def wait_until(predicate, timeout: float, interval: float = 2.0):
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            if predicate():
                return True
        except Exception as e:  # transient API hiccups during power events
            last_error = e
        time.sleep(interval)
    if last_error is not None:
        print(f"wait_until last error: {last_error}")
    return False


def make_injector(namespace: str = "default"):
    from aiopslab.generators.fault.inject_power import PowerFaultInjector

    return PowerFaultInjector(namespace=namespace)


# --------------------------------------------------------------------------- #
# 1. Injector-level tests
# --------------------------------------------------------------------------- #


@requires_power_testbed
def test_power_outage_inject_and_recover():
    node = worker_nodes()[0]
    injector = make_injector()

    injector.inject_power_outage(nodes=[node])
    try:
        assert container_status(node) == "exited", "node container should be stopped"
        assert wait_until(
            lambda: not node_is_ready(node), timeout=90
        ), f"node {node} should become NotReady after power loss"
    finally:
        injector.recover_power_outage()

    assert container_status(node) == "running"
    assert wait_until(
        lambda: node_is_ready(node), timeout=420
    ), f"node {node} should return Ready after power restoration"


@requires_power_testbed
def test_power_brownout_throttles_and_restores():
    node = worker_nodes()[0]
    injector = make_injector()

    injector.inject_brownout(nodes=[node], severity=0.35)
    try:
        import docker

        container = docker.from_env().containers.get(node)
        quota = container.attrs["HostConfig"]["CpuQuota"]
        assert quota and quota > 0, f"node CPU should be throttled, got quota={quota}"
    finally:
        injector.recover_brownout()

    import docker

    container = docker.from_env().containers.get(node)
    restored = container.attrs["HostConfig"]["CpuQuota"]
    assert restored != quota, "CPU quota should be restored after brownout recovery"


@requires_power_testbed
def test_generator_cutover_blips_node_and_recovers():
    nodes = worker_nodes()
    injector = make_injector()

    injector.inject_generator_cutover(nodes=nodes, blip_node=nodes[0], blip_secs=0.2)
    try:
        assert container_status(nodes[0]) == "exited", "blip node should be powered off"
        state = injector.controller.snapshot()
        assert state["ups_state"] == "ON_GENERATOR"
    finally:
        injector.recover_generator_cutover()

    assert container_status(nodes[0]) == "running"
    assert injector.controller.snapshot()["ups_state"] == "ONLINE"


@requires_power_testbed
def test_load_shedding_rotates_nodes():
    nodes = worker_nodes()
    if len(nodes) < 2:
        pytest.skip(
            "load shedding rotation needs >= 2 workers "
            "(use kind/kind-config-power.yaml)"
        )

    injector = make_injector()
    injector.inject_load_shedding(nodes=nodes, on_secs=8, off_secs=8, cycles=1)
    try:
        assert wait_until(
            lambda: all(
                any(
                    e["action"] == "node_power_off" and e["node"] == n
                    for e in injector.journal
                )
                for n in nodes
            ),
            timeout=90,
        ), f"every worker should be shed once; journal: {injector.journal}"
    finally:
        injector.recover_load_shedding()

    assert all(container_status(n) == "running" for n in nodes)


# --------------------------------------------------------------------------- #
# 2. Two-phase battery drain with live telemetry
# --------------------------------------------------------------------------- #


@requires_power_testbed
def test_ups_battery_drain_two_phases():
    from aiopslab.generators.fault.power.ups import read_power_state
    from aiopslab.observer.ups_exporter import UpsExporter

    exporter = UpsExporter()
    exporter.deploy()
    node = worker_nodes()[0]
    injector = make_injector()

    injector.inject_ups_battery_drain(nodes=[node], ride_through=25)
    try:
        # Phase A: on battery, countdown visible and decreasing.
        assert wait_until(
            lambda: read_power_state() is not None
            and read_power_state().get("ups_state") == "ON_BATTERY",
            timeout=30,
        ), "UPS state should show ON_BATTERY during ride-through"

        first = read_power_state()
        assert first["seconds_remaining"] <= 25

        time.sleep(6)
        second = read_power_state()
        assert (
            second["seconds_remaining"] < first["seconds_remaining"]
        ), "battery countdown should decrease"

        # Phase B: depletion hard-stops the node.
        assert wait_until(
            lambda: container_status(node) == "exited", timeout=60
        ), "node should be hard-stopped after battery depletion"
        assert read_power_state()["ups_state"] == "POWER_LOST"
    finally:
        injector.recover_ups_battery_drain()

    # Recovery checks BEFORE teardown removes the telemetry ConfigMap.
    assert container_status(node) == "running"
    assert wait_until(
        lambda: read_power_state() is not None
        and read_power_state()["ups_state"] == "ONLINE",
        timeout=30,
    ), "UPS state should return ONLINE after recovery"
    exporter.teardown()


# --------------------------------------------------------------------------- #
# 3 & 4. End-to-end problem runs (deploy -> inject -> workload -> agent -> eval)
# --------------------------------------------------------------------------- #


ALL_POWER_IDS = [
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

DEFAULT_E2E_IDS = [
    "power_outage_hotel_res-detection-1",
    "power_ups_drain_hotel_res-detection-1",
]

E2E_IDS = ALL_POWER_IDS if os.environ.get("AIOPSLAB_POWER_E2E_ALL") == "1" else DEFAULT_E2E_IDS


class DummyYesAgent:
    """Zero-cost agent: immediately submits the correct detection answer."""

    def init_context(self, problem_desc, instructions, apis):
        pass

    async def get_action(self, observation: str) -> str:
        return '```\nsubmit("Yes")\n```'


class PowerStatusAgent:
    """Scripted agent proving power faults are detectable from telemetry.

    First calls get_power_status() (the fault must be visible in the
    observation), then submits the correct answer.
    """

    def __init__(self):
        self.checked = False

    def init_context(self, problem_desc, instructions, apis):
        pass

    async def get_action(self, observation: str) -> str:
        if not self.checked:
            self.checked = True
            return "```\nget_power_status()\n```"
        return '```\nsubmit("Yes")\n```'


def run_problem_with_agent(problem_id: str, agent, max_steps: int):
    """Run one problem; eagerly recover the fault on ANY failure.

    The orchestrator registers fault recovery with ``atexit`` — which only
    fires at interpreter shutdown. Without eager recovery here, a failure in
    one test leaves the fault active (e.g. a node powered off) and poisons
    every following test on the same cluster.
    """
    from aiopslab.orchestrator import Orchestrator

    orchestrator = Orchestrator()
    orchestrator.register_agent(agent, name="power-test-agent")
    try:
        problem_desc, instructions, apis = orchestrator.init_problem(problem_id)
        agent.init_context(problem_desc, instructions, apis)
        return asyncio.run(orchestrator.start_problem(max_steps=max_steps))
    except Exception:
        try:
            if orchestrator.session is not None and orchestrator.session.problem is not None:
                print("Eagerly recovering injected fault after test failure...")
                orchestrator.session.problem.recover_fault()
        except Exception as cleanup_error:
            print(f"Eager recovery failed: {cleanup_error}")
        raise


@requires_power_testbed
@pytest.mark.parametrize("pid", E2E_IDS)
def test_power_problem_end_to_end(pid):
    """Full pipeline run for a power problem with a zero-cost agent."""
    output = run_problem_with_agent(pid, DummyYesAgent(), max_steps=1)

    assert output is not None
    results = output.get("results", {})
    assert results, f"results must be non-empty; got: {output}"

    if "-detection-" in pid:
        assert results.get("Detection Accuracy") == "Correct", results
    elif "-localization-" in pid:
        assert "Localization Accuracy" in results
    elif "-analysis-" in pid:
        assert "system_level_correct" in results
    elif "-mitigation-" in pid:
        assert "success" in results


@requires_power_testbed
def test_power_fault_detectable_via_telemetry():
    """Detectability gate: a scripted agent must be able to observe the
    fault through get_power_status() and answer correctly."""
    output = run_problem_with_agent(
        "power_ups_drain_hotel_res-detection-1", PowerStatusAgent(), max_steps=2
    )

    results = output.get("results", {})
    assert results.get("Detection Accuracy") == "Correct", results
