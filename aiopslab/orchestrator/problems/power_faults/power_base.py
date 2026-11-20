# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Shared plumbing for power-aware fault problems (HotelReservation).

Each fault module (power_outage, power_brownout, power_load_shedding,
power_generator_cutover, power_ups_drain) subclasses
``PowerFaultBaseTask`` for the fault lifecycle and the four task variants
(detection / localization / analysis / mitigation) for evaluation, exactly
following the structure of the existing problems (e.g. network_delay).
"""

import os
import time
from typing import Any

from aiopslab.orchestrator.tasks import *
from aiopslab.orchestrator.evaluators.quantitative import *
from aiopslab.service.kubectl import KubeCtl
from aiopslab.service.apps.hotelres import HotelReservation
from aiopslab.generators.workload.wrk import Wrk
from aiopslab.generators.fault.inject_power import (
    PowerFaultInjector,
    clear_active_injector,
    set_active_injector,
)
from aiopslab.observer.ups_exporter import UpsExporter
from aiopslab.paths import TARGET_MICROSERVICES

from .helpers import get_frontend_url, node_is_ready

# Power faults unfold over tens of seconds (ride-through, shedding windows);
# the workload must span the event, unlike the default 10 s bursts.
POWER_WORKLOAD_DURATION = int(os.environ.get("POWER_WORKLOAD_DURATION", "180"))

# Localization for power faults targets NODES, not services, so these
# problems override the task description accordingly.
POWER_LOCALIZATION_DESC = """\
    You are an expert DevOps engineer assigned to localize faults in a deployed service.

    Service Details:
    {app_summary}

    A power-related fault has been injected into the underlying cluster
    infrastructure. You are requested to identify the Kubernetes node(s)
    where the root cause of the fault lies. You will begin by analyzing
    the service's state and telemetry (including power status), and then
    submit one of two possible solutions:
    1. list[str]: list of faulty nodes (node names, as shown by `kubectl get nodes`)
    2. list[]: an empty list if no faults were detected
    """


class PowerFaultBaseTask:
    """Base for all power-aware fault problems."""

    def __init__(self):
        self.app = HotelReservation()
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        self.faulty_service = None
        self.faulty_nodes: list[str] = []
        self.payload_script = (
            TARGET_MICROSERVICES
            / "hotelReservation/wrk2/scripts/hotel-reservation/mixed-workload_type_1.lua"
        )
        self.injector = PowerFaultInjector(namespace=self.namespace)
        self.ups_exporter = UpsExporter()

    def start_workload(self):
        print("== Start Workload ==")
        frontend_url = get_frontend_url(self.app)

        wrk = Wrk(
            rate=10,
            dist="exp",
            connections=2,
            duration=POWER_WORKLOAD_DURATION,
            threads=2,
        )
        wrk.start_workload(
            payload_script=self.payload_script,
            url=f"{frontend_url}",
        )

    # ------------------------------------------------------------------ #
    # Power telemetry lifecycle (called by inject/recover of each problem)
    # ------------------------------------------------------------------ #

    def deploy_power_telemetry(self):
        """Deploy the UPS exporter and open the power-control session."""
        try:
            self.ups_exporter.deploy()
        except Exception as e:
            print(f"UPS exporter deploy failed (continuing): {e}")
        set_active_injector(self.injector)
        self.injector.controller.publish(force=True)

    def teardown_power_telemetry(self):
        """Close the power-control session and remove the UPS exporter."""
        clear_active_injector()
        try:
            self.ups_exporter.teardown()
        except Exception as e:
            print(f"UPS exporter teardown failed (continuing): {e}")

    def wait_for_power_recovery(self, timeout: float = 420.0) -> None:
        """Block until every power-cycled node has fully converged.

        A recovered power fault is not "done" until the node is Ready again,
        its tmpfs-repaired state (/run/udev for OpenEBS NDM) is restored and
        pods stuck by the ungraceful loss are cleared — otherwise the next
        problem's init (openebs/app readiness waits) hangs. kubelet can take
        several minutes to re-register after a hard power cycle.
        """
        cycled = {
            entry["node"]
            for entry in self.injector.journal
            if entry["action"] == "node_power_off"
        }
        for node in sorted(cycled):
            print(f"== Waiting for node {node} to converge after power loss ==")
            self.injector.wait_node_power_recovered(node, timeout=timeout)

    def all_faulty_nodes_ready(self) -> bool:
        return all(node_is_ready(self.kubectl, n) for n in self.faulty_nodes)

    # ------------------------------------------------------------------ #
    # Shared evaluation helpers (used by the task classes below)
    # ------------------------------------------------------------------ #

    def eval_detection_solution(self, soln: Any) -> None:
        print("== Evaluation ==")
        expected_solution = "Yes"

        if isinstance(soln, str):
            if soln.strip().lower() == expected_solution.lower():
                print(f"Correct detection: {soln}")
                self.add_result("Detection Accuracy", "Correct")
            else:
                print(f"Incorrect detection: {soln}")
                self.add_result("Detection Accuracy", "Incorrect")
        else:
            print("Invalid solution format")
            self.add_result("Detection Accuracy", "Invalid Format")

    def eval_node_localization(self, soln: Any) -> None:
        print("== Evaluation ==")

        if soln is None:
            print("Solution is None")
            self.add_result("Localization Accuracy", 0.0)
            self.results["success"] = False
            self.results["is_subset"] = False
            return

        predicted = [soln] if isinstance(soln, str) else list(soln)
        expected = list(self.faulty_nodes)

        pred_set, exp_set = set(predicted), set(expected)
        if pred_set == exp_set:
            accuracy = 100.0
            print(f"Exact node match: {predicted} | Accuracy: {accuracy}%")
        elif exp_set and exp_set.issubset(pred_set):
            accuracy = (len(expected) / len(predicted)) * 100.0
            print(f"Subset node match: {predicted} | Accuracy: {accuracy:.2f}%")
        else:
            accuracy = 0.0
            print(f"No node match: {predicted} | Accuracy: {accuracy}%")

        self.add_result("Localization Accuracy", accuracy)
        self.results["success"] = pred_set == exp_set
        self.results["is_subset"] = bool(exp_set) and exp_set.issubset(pred_set)

    def eval_power_analysis(self, soln: Any) -> None:
        print("== Evaluation ==")

        if not isinstance(soln, dict):
            print("Solution is not a dictionary")
            self.results["system_level_correct"] = False
            self.results["fault_type_correct"] = False
            self.results["success"] = False
            return

        is_sys_level_correct = is_exact_match_lower(
            soln.get("system_level", ""), "Hardware"
        )
        is_fault_type_correct = is_exact_match_lower(
            soln.get("fault_type", ""), "Power Issue"
        )

        self.results["system_level_correct"] = is_sys_level_correct
        self.results["fault_type_correct"] = is_fault_type_correct
        self.results["success"] = is_sys_level_correct and is_fault_type_correct

    def eval_power_mitigation(self, node_timeout: float | None = None) -> None:
        """Success: faulty nodes Ready + pods healthy + UPS back ONLINE.

        Node readiness is polled (a mitigated power restore needs minutes for
        kubelet to boot) — a one-shot check would fail correct solutions.
        """
        if node_timeout is None:
            node_timeout = float(
                os.environ.get("AIOPSLAB_NODE_READY_TIMEOUT", "420")
            )
        deadline = time.monotonic() + node_timeout
        all_powered = False
        while time.monotonic() < deadline:
            if self.all_faulty_nodes_ready():
                all_powered = True
                break
            time.sleep(5.0)
        if not all_powered:
            print("Faulty nodes are not Ready after mitigation.")

        all_normal = False
        if all_powered:
            # Power-fault recovery is slower than the app-level default
            # settle window; give pods time to restart after node boot.
            all_normal = self.wait_until_pods_healthy(
                self.namespace, timeout=max(300.0, node_timeout)
            )

        ups_online = (
            self.injector.controller.snapshot().get("ups_state") == "ONLINE"
        )
        throttles_cleared = not self.injector.has_throttled_nodes
        self.results["success"] = (
            all_powered and all_normal and ups_online and throttles_cleared
        )
