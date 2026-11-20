"""Healthy power control with the same telemetry as fault scenarios."""

from .power_outage import PowerOutageDetection


class PowerHealthyDetection(PowerOutageDetection):
    expected_detection = "No"

    def inject_fault(self):
        self.deploy_power_telemetry()
        self.faulty_nodes = []

    def recover_fault(self):
        self.teardown_power_telemetry()
