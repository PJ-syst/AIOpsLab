# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Unit tests for the power fault injector (no cluster or Docker required).

Docker and the UPS controller are faked/injected through the constructor,
so nothing here can touch a real cluster.
"""

import time
from types import SimpleNamespace as NS

import pytest

import aiopslab.generators.fault.base as fault_base
from aiopslab.generators.fault.inject_power import (
    CPU_PERIOD_US,
    PowerFaultInjector,
    _parse_duration,
    clear_active_injector,
    get_active_injector,
    list_worker_nodes,
    pick_node_hosting_most_pods,
    set_active_injector,
)
from aiopslab.generators.fault.power.ups import PowerController, UpsState
from aiopslab.orchestrator.actions.mitigation import MitigationActions


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class FakeContainer:
    def __init__(self, name, cpu_quota=-1, cpu_period=0):
        self.name = name
        self.attrs = {"HostConfig": {"CpuQuota": cpu_quota, "CpuPeriod": cpu_period}}
        self.stopped = False
        self.calls = []

    def stop(self, timeout=None):
        self.stopped = True
        self.calls.append(("stop", timeout))

    def start(self):
        self.stopped = False
        self.calls.append(("start",))

    def update(self, cpu_quota=None, cpu_period=None):
        self.calls.append(("update", cpu_quota, cpu_period))
        if cpu_quota is not None:
            self.attrs["HostConfig"]["CpuQuota"] = cpu_quota
        if cpu_period is not None:
            self.attrs["HostConfig"]["CpuPeriod"] = cpu_period


class FakeDocker:
    def __init__(self, names):
        self.containers = {name: FakeContainer(name) for name in names}

    def get_container(self, name):
        return self.containers[name]


class BoomKubeCtl:
    """Guards against accidental cluster access in unit tests."""

    def __getattr__(self, name):
        raise AssertionError(f"unexpected kubectl access in unit test: {name}")


def make_injector(names=("node-a", "node-b")):
    docker = FakeDocker(names)
    controller = PowerController(publish=False)
    injector = PowerFaultInjector(
        "test-ns", kubectl=BoomKubeCtl(), docker=docker, controller=controller
    )
    return injector, docker, controller


def wait_until(predicate, timeout=10.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


# --------------------------------------------------------------------------- #
# Primitives and dispatch
# --------------------------------------------------------------------------- #


class TestPrimitives:
    def test_power_off_and_on(self):
        injector, docker, _ = make_injector()
        injector.power_off_node("node-a")
        assert docker.containers["node-a"].stopped
        assert ("stop", 0) in docker.containers["node-a"].calls
        assert injector._stopped == {"node-a": True}
        assert injector.node_is_powered_off("node-a")

        injector.power_on_node("node-a")
        assert not docker.containers["node-a"].stopped
        assert ("start",) in docker.containers["node-a"].calls
        assert injector._stopped == {}
        assert not injector.node_is_powered_off("node-a")

    def test_base_dispatch_inject_and_recover(self, monkeypatch):
        # The base class sleeps 6 s after dispatch; keep the test fast.
        monkeypatch.setattr(fault_base.time, "sleep", lambda s: None)

        injector, docker, controller = make_injector()
        injector._inject("power_outage", ["node-a"])

        assert docker.containers["node-a"].stopped
        assert controller.state is UpsState.POWER_LOST

        injector._recover("power_outage")
        assert not docker.containers["node-a"].stopped
        assert controller.state is UpsState.ONLINE

    def test_throttle_quota_math(self, monkeypatch):
        monkeypatch.setattr("os.cpu_count", lambda: 8)
        injector, docker, _ = make_injector()

        injector.throttle_node_cpu("node-a", 0.35)
        container = docker.containers["node-a"]
        expected_quota = int(0.35 * 8 * CPU_PERIOD_US)
        assert ("update", expected_quota, CPU_PERIOD_US) in container.calls
        assert container.attrs["HostConfig"]["CpuQuota"] == expected_quota

    def test_unthrottle_restores_original_quota(self, monkeypatch):
        monkeypatch.setattr("os.cpu_count", lambda: 8)
        injector, docker, _ = make_injector()

        injector.throttle_node_cpu("node-a", 0.5)
        injector.unthrottle_node("node-a")

        # Original kind-node container has no quota limit (-1).
        assert docker.containers["node-a"].attrs["HostConfig"]["CpuQuota"] == -1

    def test_unthrottle_resets_zero_quota_to_unlimited(self, monkeypatch):
        # Real kind nodes report CpuQuota: 0 in inspect; the update API
        # treats 0 as a no-op, so recovery must translate it to -1.
        monkeypatch.setattr("os.cpu_count", lambda: 8)
        docker = FakeDocker(("node-a",))
        docker.containers["node-a"].attrs["HostConfig"]["CpuQuota"] = 0
        docker.containers["node-a"].attrs["HostConfig"]["CpuPeriod"] = 0
        injector = PowerFaultInjector(
            "test-ns",
            kubectl=BoomKubeCtl(),
            docker=docker,
            controller=PowerController(publish=False),
        )

        injector.throttle_node_cpu("node-a", 0.35)
        injector.unthrottle_node("node-a")

        container = docker.containers["node-a"]
        restored = container.attrs["HostConfig"]["CpuQuota"]
        assert restored == -1, f"quota must reset to -1 (unlimited), got {restored}"
        assert ("update", -1, 100000) in container.calls

    def test_power_on_clears_throttle(self, monkeypatch):
        # A power-on is a fresh node state: a brownout throttle must clear.
        monkeypatch.setattr("os.cpu_count", lambda: 8)
        injector, docker, _ = make_injector()

        injector.throttle_node_cpu("node-a", 0.35)
        assert injector.has_throttled_nodes

        injector.power_on_node("node-a")
        assert not injector.has_throttled_nodes
        assert docker.containers["node-a"].attrs["HostConfig"]["CpuQuota"] == -1

    def test_throttle_ratio_is_clamped(self, monkeypatch):
        monkeypatch.setattr("os.cpu_count", lambda: 4)
        injector, docker, _ = make_injector()
        injector.throttle_node_cpu("node-a", 99.0)  # clamped to 1.0
        expected = 4 * CPU_PERIOD_US
        assert docker.containers["node-a"].attrs["HostConfig"]["CpuQuota"] == expected


# --------------------------------------------------------------------------- #
# Individual faults
# --------------------------------------------------------------------------- #


class TestPowerOutage:
    def test_outage_stops_node_and_recover_is_idempotent(self):
        injector, docker, controller = make_injector()
        injector.inject_power_outage(nodes=["node-a"])

        assert docker.containers["node-a"].stopped
        assert not docker.containers["node-b"].stopped
        assert controller.state is UpsState.POWER_LOST
        assert controller.snapshot()["input_volts"] == 0.0

        injector.recover_power_outage()
        injector.recover_power_outage()  # idempotent
        assert not docker.containers["node-a"].stopped
        assert controller.state is UpsState.ONLINE
        assert injector.node_ever_down("node-a")

    def test_node_string_is_normalized_to_list(self):
        injector, docker, _ = make_injector()
        injector.inject_power_outage(nodes="node-b")
        assert docker.containers["node-b"].stopped


class TestBrownout:
    def test_brownout_throttles_and_recover_restores(self, monkeypatch):
        monkeypatch.setattr("os.cpu_count", lambda: 8)
        injector, docker, controller = make_injector()

        injector.inject_brownout(nodes=["node-a", "node-b"], severity=0.35)
        expected = int(0.35 * 8 * CPU_PERIOD_US)
        for name in ("node-a", "node-b"):
            assert docker.containers[name].attrs["HostConfig"]["CpuQuota"] == expected
            assert not docker.containers[name].stopped
        snap = controller.snapshot()
        assert snap["ups_state"] == "ONLINE"
        assert snap["input_volts"] < 120.0  # voltage sag

        injector.recover_brownout()
        for name in ("node-a", "node-b"):
            assert docker.containers[name].attrs["HostConfig"]["CpuQuota"] == -1
        assert controller.snapshot()["input_volts"] == 120.0


class TestLoadShedding:
    def test_schedule_generation(self):
        schedule = PowerFaultInjector._build_shedding_schedule(
            ["a", "b"], on_secs=60.0, off_secs=45.0, cycles=2
        )
        # 2 nodes * (off + on) * 2 cycles, minus the final restore event
        # (the last shed window is left as the mitigation target).
        assert len(schedule) == 7

        # Starts with a shed (off) event; the final window is left off.
        assert schedule[0][3] is False
        assert schedule[-1][3] is False

        # Every node except the last is left restored at the end of the
        # schedule; the final shed window is left off (mitigation target).
        final_state = {}
        for _, _, node, powered in schedule:
            final_state[node] = powered
        assert final_state == {"a": True, "b": False}

        # Per-node off windows never overlap.
        windows = {}
        for start, end, node, powered in schedule:
            if powered:
                continue
            windows.setdefault(node, []).append((start, end))
        for node, spans in windows.items():
            spans = sorted(spans)
            for (s1, e1), (s2, e2) in zip(spans, spans[1:]):
                assert e1 <= s2, f"overlapping shed windows for {node}"

        # One node is shed at a time: starts are strictly increasing and
        # events strictly alternate off -> on per node.
        assert [round(s, 6) for s, _, _, _ in schedule] == sorted(
            round(s, 6) for s, _, _, _ in schedule
        )

    def test_shedding_thread_rotates_and_recovers(self):
        injector, docker, _ = make_injector()
        injector.inject_load_shedding(
            nodes=["node-a", "node-b"], on_secs=0.2, off_secs=0.2, cycles=1
        )

        # Both nodes get shed at some point during the rotation.
        assert wait_until(
            lambda: any(
                e["action"] == "node_power_off" and e["node"] == "node-a"
                for e in injector.journal
            )
            and any(
                e["action"] == "node_power_off" and e["node"] == "node-b"
                for e in injector.journal
            ),
            timeout=10,
        ), f"shedding journal incomplete: {injector.journal}"

        injector.recover_load_shedding()
        assert not docker.containers["node-a"].stopped
        assert not docker.containers["node-b"].stopped
        assert not injector._threads


class TestGeneratorCutover:
    def test_cutover_blips_one_node_and_transfers_to_generator(self, monkeypatch):
        monkeypatch.setattr("os.cpu_count", lambda: 8)
        injector, docker, controller = make_injector()

        injector.inject_generator_cutover(
            nodes=["node-a", "node-b"], blip_node="node-a", blip_secs=0.05
        )

        # The single-PSU node lost power and stays off until mitigated.
        assert docker.containers["node-a"].stopped
        assert not docker.containers["node-b"].stopped
        # Transfer dip throttling was applied and reverted.
        assert len(docker.containers["node-b"].calls) >= 2
        assert controller.state is UpsState.ON_GENERATOR

        injector.recover_generator_cutover()
        assert not docker.containers["node-a"].stopped
        assert controller.state is UpsState.ONLINE


class TestUpsBatteryDrain:
    def test_drain_two_phases_then_recover(self):
        injector, docker, controller = make_injector()
        injector.inject_ups_battery_drain(nodes=["node-a"], ride_through=0.5)

        # Phase A: on battery, node still powered.
        assert controller.state is UpsState.ON_BATTERY
        assert not docker.containers["node-a"].stopped

        # Phase B: depletion hard-stops the node.
        assert wait_until(
            lambda: docker.containers["node-a"].stopped, timeout=15
        ), "node should be hard-stopped after battery depletion"
        assert controller.state is UpsState.POWER_LOST

        events = [entry[2] for entry in controller.journal()]
        assert "battery_depleted" in events
        assert injector.node_ever_down("node-a")

        injector.recover_ups_battery_drain()
        assert not docker.containers["node-a"].stopped
        assert controller.state is UpsState.ONLINE
        # Quota restored after power-on (updates persist across restarts).
        assert docker.containers["node-a"].attrs["HostConfig"]["CpuQuota"] == -1


class TestRecovery:
    def test_stale_pods_to_delete_selection(self):
        pods = [
            # Stuck Unknown after ungraceful node loss -> delete.
            {
                "metadata": {"name": "ndm-abc", "namespace": "openebs"},
                "status": {"phase": "Unknown"},
            },
            # Stuck Terminating -> delete.
            {
                "metadata": {
                    "name": "app-xyz",
                    "namespace": "test-hotel-reservation",
                    "deletionTimestamp": "2026-11-03T12:00:00Z",
                },
                "status": {"phase": "Running"},
            },
            # Healthy -> keep.
            {
                "metadata": {"name": "ok-pod", "namespace": "default"},
                "status": {"phase": "Running"},
            },
            # Completed job -> keep.
            {
                "metadata": {"name": "wrk2-job-1", "namespace": "default"},
                "status": {"phase": "Succeeded"},
            },
            # Scheduled but not started (kubelet will start it) -> keep.
            {
                "metadata": {"name": "pending-pod", "namespace": "default"},
                "status": {"phase": "Pending"},
            },
            # Crash-looped after the hard power loss -> delete.
            {
                "metadata": {"name": "node-exporter-1", "namespace": "openebs"},
                "status": {
                    "phase": "Running",
                    "containerStatuses": [
                        {
                            "state": {"waiting": {"reason": "CrashLoopBackOff"}},
                        }
                    ],
                },
            },
        ]
        stale = PowerFaultInjector._stale_pods_to_delete(pods)
        assert stale == [
            ("openebs", "ndm-abc"),
            ("test-hotel-reservation", "app-xyz"),
            ("openebs", "node-exporter-1"),
        ]

    def test_recover_all_is_idempotent(self):
        injector, docker, controller = make_injector()
        injector.inject_power_outage(nodes=["node-a"])
        injector.throttle_node_cpu("node-b", 0.5)

        injector.recover_all()
        injector.recover_all()

        assert not docker.containers["node-a"].stopped
        assert not docker.containers["node-b"].stopped
        assert docker.containers["node-b"].attrs["HostConfig"]["CpuQuota"] == -1
        assert controller.state is UpsState.ONLINE


# --------------------------------------------------------------------------- #
# Session registry and helpers
# --------------------------------------------------------------------------- #


class TestPowerOps:
    @staticmethod
    def _kubectl_with_worker():
        control = NS(
            metadata=NS(
                name="kind-control-plane",
                labels={"node-role.kubernetes.io/control-plane": ""},
            )
        )
        worker = NS(metadata=NS(name="kind-worker", labels={}))
        return NS(core_v1_api=NS(list_node=lambda: NS(items=[control, worker])))

    def test_active_injector_registry(self):
        injector, _, _ = make_injector()
        set_active_injector(injector)
        try:
            assert get_active_injector() is injector
        finally:
            clear_active_injector()
        assert get_active_injector() is None

    def test_power_action_rejects_control_plane(self):
        docker = FakeDocker(("kind-control-plane", "kind-worker"))
        injector = PowerFaultInjector(
            "test-ns",
            kubectl=self._kubectl_with_worker(),
            docker=docker,
            controller=PowerController(publish=False),
        )
        set_active_injector(injector)
        try:
            result = MitigationActions.power_node("kind-control-plane", "off")
        finally:
            clear_active_injector()

        assert "not an allow-listed worker" in result
        assert not docker.containers["kind-control-plane"].stopped

    def test_power_on_during_ride_through_restores_without_restart(self):
        docker = FakeDocker(("kind-worker",))
        controller = PowerController(publish=False)
        injector = PowerFaultInjector(
            "test-ns",
            kubectl=self._kubectl_with_worker(),
            docker=docker,
            controller=controller,
        )
        controller.utility_lost(ride_through=30)
        set_active_injector(injector)
        try:
            result = MitigationActions.power_node("kind-worker", "on")
        finally:
            clear_active_injector()

        assert result == "Power restored to node kind-worker."
        assert controller.state is UpsState.ON_BATTERY
        assert ("start",) not in docker.containers["kind-worker"].calls


class TestNodeHelpers:
    @staticmethod
    def _node(name, labels=None):
        return NS(metadata=NS(name=name, labels=labels or {}))

    def test_list_worker_nodes_excludes_control_plane(self):
        kubectl = NS(
            core_v1_api=NS(
                list_node=lambda: NS(
                    items=[
                        self._node(
                            "kind-control-plane",
                            {"node-role.kubernetes.io/control-plane": ""},
                        ),
                        self._node("kind-worker"),
                        self._node("old-master", {"node-role.kubernetes.io/master": ""}),
                        self._node("kind-worker2"),
                    ]
                )
            )
        )
        assert list_worker_nodes(kubectl) == ["kind-worker", "kind-worker2"]

    def test_pick_node_hosting_most_pods(self):
        pods = [
            NS(spec=NS(node_name="kind-worker")),
            NS(spec=NS(node_name="kind-worker")),
            NS(spec=NS(node_name="kind-worker2")),
            NS(spec=NS(node_name="kind-control-plane")),  # excluded
            NS(spec=NS(node_name=None)),  # unscheduled
        ]
        kubectl = NS(
            core_v1_api=NS(list_node=lambda: NS(items=[self._node("kind-worker"), self._node("kind-worker2")])),
            list_pods=lambda ns: NS(items=pods),
        )
        assert pick_node_hosting_most_pods(kubectl, "ns") == "kind-worker"

    def test_pick_node_falls_back_to_sorted_first_worker(self):
        kubectl = NS(
            core_v1_api=NS(list_node=lambda: NS(items=[self._node("kind-worker2"), self._node("kind-worker")])),
            list_pods=lambda ns: NS(items=[]),
        )
        assert pick_node_hosting_most_pods(kubectl, "ns") == "kind-worker"


class TestDurationParsing:
    @pytest.mark.parametrize(
        "value,expected",
        [
            (None, None),
            (90, 90.0),
            (90.0, 90.0),
            ("90s", 90.0),
            ("100S", 100.0),
            ("bogus", None),
        ],
    )
    def test_parse_duration(self, value, expected):
        assert _parse_duration(value) == expected
