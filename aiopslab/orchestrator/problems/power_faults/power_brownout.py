# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Sustained brownout (undervoltage, degraded nodes) in the HotelReservation app."""

from typing import Any

from aiopslab.orchestrator.tasks import *
from aiopslab.session import SessionItem

from aiopslab.generators.fault.inject_power import list_worker_nodes

from .power_base import PowerFaultBaseTask, POWER_LOCALIZATION_DESC


class PowerBrownoutBaseTask(PowerFaultBaseTask):
    def inject_fault(self):
        print("== Fault Injection ==")
        self.deploy_power_telemetry()
        nodes = list_worker_nodes(self.kubectl)
        self.faulty_nodes = nodes
        self.injector.inject_brownout(nodes=nodes)
        print(
            f"Fault: utility brownout | Nodes: {nodes} | "
            f"Namespace: {self.namespace}\n"
        )

    def recover_fault(self):
        print("== Fault Recovery ==")
        self.injector.recover_brownout()
        self.teardown_power_telemetry()


################## Detection Problem ##################
class PowerBrownoutDetection(PowerBrownoutBaseTask, DetectionTask):
    def __init__(self):
        PowerBrownoutBaseTask.__init__(self)
        DetectionTask.__init__(self, self.app)

    def eval(self, soln: Any, trace: list[SessionItem], duration: float):
        self.eval_detection_solution(soln)
        return super().eval(soln, trace, duration)


################## Localization Problem ##################
class PowerBrownoutLocalization(PowerBrownoutBaseTask, LocalizationTask):
    def __init__(self):
        PowerBrownoutBaseTask.__init__(self)
        LocalizationTask.__init__(self, self.app)
        self.task_desc = POWER_LOCALIZATION_DESC.format(app_summary=self.app_summary)

    def eval(self, soln: Any, trace: list[SessionItem], duration: float):
        self.eval_node_localization(soln)
        return super().eval(soln, trace, duration)


################## Root cause analysis Problem ##################
class PowerBrownoutAnalysis(PowerBrownoutBaseTask, AnalysisTask):
    def __init__(self):
        PowerBrownoutBaseTask.__init__(self)
        AnalysisTask.__init__(self, self.app)

    def eval(self, soln: Any, trace: list[SessionItem], duration: float):
        self.eval_power_analysis(soln)
        return super().eval(soln, trace, duration)


################## Mitigation Problem ##################
class PowerBrownoutMitigation(PowerBrownoutBaseTask, MitigationTask):
    def __init__(self):
        PowerBrownoutBaseTask.__init__(self)
        MitigationTask.__init__(self, self.app)

    def eval(self, soln: Any, trace: list[SessionItem], duration: float) -> dict:
        print("== Evaluation ==")
        super().eval(soln, trace, duration)
        self.eval_power_mitigation()
        return self.results
