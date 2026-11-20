# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Actions for the mitigation task."""

from aiopslab.orchestrator.actions.base import TaskActions
from aiopslab.utils.actions import action, write
from aiopslab.utils.status import SubmissionStatus


class MitigationActions(TaskActions):
    """
    Class for mitigation task's actions.
    """

    @staticmethod
    @action
    def submit() -> SubmissionStatus:
        """
        Submit once your mitigation solution is complete and ready to be evaluated.

        Args:
            None

        Returns:
            SubmissionStatus: The status of the submission.
        """
        # for mitigation task, the submission is valid if the solution is submitted
        # NOTE: this does not mean the solution is correct!
        return SubmissionStatus.VALID_SUBMISSION

    @staticmethod
    @write
    def power_node(node: str, action: str) -> str:
        """
        Power a node on or off through out-of-band power management (IPMI-style).

        Use this to restore power to a node that lost it (e.g. after a
        utility outage or UPS battery depletion), or to cut power to a node.

        Args:
            node (str): Name of the target Kubernetes node (as in `kubectl get nodes`).
            action (str): "on" to restore power, "off" to cut power.

        Returns:
            str: Result of the power operation.
        """
        from aiopslab.generators.fault.inject_power import (
            get_active_injector,
            list_worker_nodes,
        )

        injector = get_active_injector()
        if injector is None:
            return (
                "Error: no active power control session. Power management is "
                "only available for power-aware fault problems; use "
                "exec_shell instead."
            )

        action = (action or "").strip().lower()
        if action not in ("on", "off"):
            return "Error: action must be 'on' or 'off'."

        try:
            manageable_nodes = set(list_worker_nodes(injector.kubectl))
            if node not in manageable_nodes:
                return (
                    f"Error: node {node} is not an allow-listed worker node. "
                    "Control-plane and unknown nodes cannot be power-cycled."
                )
            if action == "on":
                # Restoring a node's power is, from the facility model's
                # perspective, restoring utility power: the UPS returns
                # ONLINE and any battery countdown is cancelled. This is
                # what makes *proactive* mitigation possible — an agent
                # that acts during the ride-through window prevents the
                # hard power-off entirely.
                # Facility restoration is now a separate explicit action.
                if injector.node_is_powered_off(node):
                    injector.power_on_node(node)
                else:
                    injector.controller.node_on(node)
                return f"Power restored to node {node}."
            injector.power_off_node(node)
            return f"Power cut to node {node}."
        except Exception as e:
            return f"Error: power operation failed for node {node}: {e}"

    @staticmethod
    @write
    def restore_simulated_utility() -> str:
        """Restore local simulated utility with a one-second operation delay.

        Models a facility operator, separate from node IPMI. Verify node and
        application recovery afterwards. This does not control real hardware.
        """
        import time
        from aiopslab.generators.fault.inject_power import get_active_injector
        injector = get_active_injector()
        if injector is None:
            return "Error: no active simulated facility session."
        time.sleep(1.0)
        injector.controller.utility_restored()
        injector.facility_restored_by_agent = True
        injector._log("agent_utility_restored")
        return "Simulated supply restored; verify nodes and service health."
