# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""UPS battery drain (ride-through, then hard power loss) in the HotelReservation app."""

from typing import Any

from aiopslab.orchestrator.tasks import *
from aiopslab.session import SessionItem

from aiopslab.generators.fault.inject_power import pick_node_hosting_most_pods

from .power_base import PowerFaultBaseTask, POWER_LOCALIZATION_DESC


class PowerUpsDrainBaseTask(PowerFaultBaseTask):
    def inject_fault(self):
        print("== Fault Injection ==")
        self.deploy_power_telemetry()
        node = pick_node_hosting_most_pods(self.kubectl, self.namespace)
        self.faulty_nodes = [node]
        self.injector.inject_ups_battery_drain(nodes=[node])
        print(
            f"Fault: UPS battery drain | Node: {node} | "
            f"Namespace: {self.namespace}\n"
        )

    def recover_fault(self):
        print("== Fault Recovery ==")
        self.injector.recover_ups_battery_drain()
        self.wait_for_power_recovery()
        self.teardown_power_telemetry()


################## Detection Problem ##################
class PowerUpsDrainDetection(PowerUpsDrainBaseTask, DetectionTask):
    def __init__(self):
        PowerUpsDrainBaseTask.__init__(self)
        DetectionTask.__init__(self, self.app)

    def eval(self, soln: Any, trace: list[SessionItem], duration: float):
        self.eval_detection_solution(soln)
        return super().eval(soln, trace, duration)


################## Localization Problem ##################
class PowerUpsDrainLocalization(PowerUpsDrainBaseTask, LocalizationTask):
    def __init__(self):
        PowerUpsDrainBaseTask.__init__(self)
        LocalizationTask.__init__(self, self.app)
        self.task_desc = POWER_LOCALIZATION_DESC.format(app_summary=self.app_summary)

    def eval(self, soln: Any, trace: list[SessionItem], duration: float):
        self.eval_node_localization(soln)
        return super().eval(soln, trace, duration)


################## Root cause analysis Problem ##################
class PowerUpsDrainAnalysis(PowerUpsDrainBaseTask, AnalysisTask):
    def __init__(self):
        PowerUpsDrainBaseTask.__init__(self)
        AnalysisTask.__init__(self, self.app)

    def eval(self, soln: Any, trace: list[SessionItem], duration: float):
        self.eval_power_analysis(soln)
        return super().eval(soln, trace, duration)


################## Mitigation Problem ##################
class PowerUpsDrainMitigation(PowerUpsDrainBaseTask, MitigationTask):
    def __init__(self):
        PowerUpsDrainBaseTask.__init__(self)
        MitigationTask.__init__(self, self.app)

    def eval(self, soln: Any, trace: list[SessionItem], duration: float) -> dict:
        print("== Evaluation ==")
        super().eval(soln, trace, duration)
        self.eval_power_mitigation()

        # Proactive mitigation: the node never actually lost power because
        # the agent acted during the battery ride-through window.
        node = self.faulty_nodes[0] if self.faulty_nodes else None
        self.results["proactive"] = self.results["success"] and bool(getattr(self.injector, "facility_restored_by_agent", False)) and bool(node) and not self.injector.node_ever_down(
            node
        )
        return self.results
