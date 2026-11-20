# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Generator cutover (ATS transfer, blip node loses power) in the HotelReservation app."""

from typing import Any

from aiopslab.orchestrator.tasks import *
from aiopslab.session import SessionItem

from aiopslab.generators.fault.inject_power import list_worker_nodes

from .power_base import PowerFaultBaseTask, POWER_LOCALIZATION_DESC


class PowerGeneratorCutoverBaseTask(PowerFaultBaseTask):
    def inject_fault(self):
        print("== Fault Injection ==")
        self.deploy_power_telemetry()
        nodes = list_worker_nodes(self.kubectl)
        if not nodes:
            raise RuntimeError("No worker nodes found for generator cutover fault.")
        # The blip node models a single-PSU server that does not ride
        # through the transfer switch — the mitigation target.
        blip_node = nodes[0]
        self.faulty_nodes = [blip_node]
        self.injector.inject_generator_cutover(
            nodes=nodes, blip_node=blip_node
        )
        print(
            f"Fault: generator cutover | Blip node: {blip_node} | "
            f"Namespace: {self.namespace}\n"
        )

    def recover_fault(self):
        print("== Fault Recovery ==")
        self.injector.recover_generator_cutover()
        self.wait_for_power_recovery()
        self.teardown_power_telemetry()


################## Detection Problem ##################
class PowerGeneratorCutoverDetection(PowerGeneratorCutoverBaseTask, DetectionTask):
    def __init__(self):
        PowerGeneratorCutoverBaseTask.__init__(self)
        DetectionTask.__init__(self, self.app)

    def eval(self, soln: Any, trace: list[SessionItem], duration: float):
        self.eval_detection_solution(soln)
        return super().eval(soln, trace, duration)


################## Localization Problem ##################
class PowerGeneratorCutoverLocalization(
    PowerGeneratorCutoverBaseTask, LocalizationTask
):
    def __init__(self):
        PowerGeneratorCutoverBaseTask.__init__(self)
        LocalizationTask.__init__(self, self.app)
        self.task_desc = POWER_LOCALIZATION_DESC.format(app_summary=self.app_summary)

    def eval(self, soln: Any, trace: list[SessionItem], duration: float):
        self.eval_node_localization(soln)
        return super().eval(soln, trace, duration)


################## Root cause analysis Problem ##################
class PowerGeneratorCutoverAnalysis(PowerGeneratorCutoverBaseTask, AnalysisTask):
    def __init__(self):
        PowerGeneratorCutoverBaseTask.__init__(self)
        AnalysisTask.__init__(self, self.app)

    def eval(self, soln: Any, trace: list[SessionItem], duration: float):
        self.eval_power_analysis(soln)
        return super().eval(soln, trace, duration)


################## Mitigation Problem ##################
class PowerGeneratorCutoverMitigation(
    PowerGeneratorCutoverBaseTask, MitigationTask
):
    def __init__(self):
        PowerGeneratorCutoverBaseTask.__init__(self)
        MitigationTask.__init__(self, self.app)

    def eval(self, soln: Any, trace: list[SessionItem], duration: float) -> dict:
        print("== Evaluation ==")
        super().eval(soln, trace, duration)
        self.eval_power_mitigation()
        return self.results
