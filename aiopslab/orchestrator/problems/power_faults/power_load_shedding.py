# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Rolling load shedding (scheduled power cuts) in the HotelReservation app."""

from typing import Any

from aiopslab.orchestrator.tasks import *
from aiopslab.session import SessionItem

from aiopslab.generators.fault.inject_power import list_worker_nodes

from .power_base import PowerFaultBaseTask, POWER_LOCALIZATION_DESC


class PowerLoadSheddingBaseTask(PowerFaultBaseTask):
    def inject_fault(self):
        print("== Fault Injection ==")
        self.deploy_power_telemetry()
        nodes = list_worker_nodes(self.kubectl)
        self.faulty_nodes = nodes
        self.injector.inject_load_shedding(nodes=nodes)
        print(
            f"Fault: rolling load shedding | Nodes: {nodes} | "
            f"Namespace: {self.namespace}\n"
        )

    def recover_fault(self):
        print("== Fault Recovery ==")
        self.injector.recover_load_shedding()
        self.wait_for_power_recovery()
        self.teardown_power_telemetry()


################## Detection Problem ##################
class PowerLoadSheddingDetection(PowerLoadSheddingBaseTask, DetectionTask):
    def __init__(self):
        PowerLoadSheddingBaseTask.__init__(self)
        DetectionTask.__init__(self, self.app)

    def eval(self, soln: Any, trace: list[SessionItem], duration: float):
        self.eval_detection_solution(soln)
        return super().eval(soln, trace, duration)


################## Localization Problem ##################
class PowerLoadSheddingLocalization(PowerLoadSheddingBaseTask, LocalizationTask):
    def __init__(self):
        PowerLoadSheddingBaseTask.__init__(self)
        LocalizationTask.__init__(self, self.app)
        self.task_desc = POWER_LOCALIZATION_DESC.format(app_summary=self.app_summary)

    def eval(self, soln: Any, trace: list[SessionItem], duration: float):
        self.eval_node_localization(soln)
        return super().eval(soln, trace, duration)


################## Root cause analysis Problem ##################
class PowerLoadSheddingAnalysis(PowerLoadSheddingBaseTask, AnalysisTask):
    def __init__(self):
        PowerLoadSheddingBaseTask.__init__(self)
        AnalysisTask.__init__(self, self.app)

    def eval(self, soln: Any, trace: list[SessionItem], duration: float):
        self.eval_power_analysis(soln)
        return super().eval(soln, trace, duration)


################## Mitigation Problem ##################
class PowerLoadSheddingMitigation(PowerLoadSheddingBaseTask, MitigationTask):
    def __init__(self):
        PowerLoadSheddingBaseTask.__init__(self)
        MitigationTask.__init__(self, self.app)

    def eval(self, soln: Any, trace: list[SessionItem], duration: float) -> dict:
        print("== Evaluation ==")
        super().eval(soln, trace, duration)
        self.eval_power_mitigation()
        return self.results
