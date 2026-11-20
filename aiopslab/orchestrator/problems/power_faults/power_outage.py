# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Utility power outage (hard node power-off) in the HotelReservation app."""

from typing import Any

from aiopslab.orchestrator.tasks import *
from aiopslab.session import SessionItem

from aiopslab.generators.fault.inject_power import pick_node_hosting_most_pods

from .power_base import PowerFaultBaseTask, POWER_LOCALIZATION_DESC


class PowerOutageBaseTask(PowerFaultBaseTask):
    def inject_fault(self):
        print("== Fault Injection ==")
        self.deploy_power_telemetry()
        node = pick_node_hosting_most_pods(self.kubectl, self.namespace)
        self.faulty_nodes = [node]
        self.injector.inject_power_outage(nodes=self.faulty_nodes)
        print(
            f"Fault: utility power outage | Node: {node} | "
            f"Namespace: {self.namespace}\n"
        )

    def recover_fault(self):
        print("== Fault Recovery ==")
        self.injector.recover_power_outage()
        self.wait_for_power_recovery()
        self.teardown_power_telemetry()


################## Detection Problem ##################
class PowerOutageDetection(PowerOutageBaseTask, DetectionTask):
    def __init__(self):
        PowerOutageBaseTask.__init__(self)
        DetectionTask.__init__(self, self.app)

    def eval(self, soln: Any, trace: list[SessionItem], duration: float):
        self.eval_detection_solution(soln)
        return super().eval(soln, trace, duration)


################## Localization Problem ##################
class PowerOutageLocalization(PowerOutageBaseTask, LocalizationTask):
    def __init__(self):
        PowerOutageBaseTask.__init__(self)
        LocalizationTask.__init__(self, self.app)
        self.task_desc = POWER_LOCALIZATION_DESC.format(app_summary=self.app_summary)

    def eval(self, soln: Any, trace: list[SessionItem], duration: float):
        self.eval_node_localization(soln)
        return super().eval(soln, trace, duration)


################## Root cause analysis Problem ##################
class PowerOutageAnalysis(PowerOutageBaseTask, AnalysisTask):
    def __init__(self):
        PowerOutageBaseTask.__init__(self)
        AnalysisTask.__init__(self, self.app)

    def eval(self, soln: Any, trace: list[SessionItem], duration: float):
        self.eval_power_analysis(soln)
        return super().eval(soln, trace, duration)


################## Mitigation Problem ##################
class PowerOutageMitigation(PowerOutageBaseTask, MitigationTask):
    def __init__(self):
        PowerOutageBaseTask.__init__(self)
        MitigationTask.__init__(self, self.app)

    def eval(self, soln: Any, trace: list[SessionItem], duration: float) -> dict:
        print("== Evaluation ==")
        super().eval(soln, trace, duration)
        self.eval_power_mitigation()
        return self.results
