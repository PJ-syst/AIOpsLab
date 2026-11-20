# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Deploy/teardown the in-cluster UPS telemetry exporter.

The manifest (aiopslab/observer/ups/ups-exporter.yaml) runs a stdlib-only
exporter pod in the ``observe`` namespace that reads the ``ups-power-state``
ConfigMap (written host-side by the PowerController) and serves it as
Prometheus gauges on :9801. Prometheus scrapes it via the annotation-based
``extraScrapeConfigs`` job added to the vendored prometheus chart.
"""

from aiopslab.paths import BASE_DIR


class UpsExporter:
    """Manages the ups-exporter deployment inside the cluster."""

    manifest_path = BASE_DIR / "observer" / "ups" / "ups-exporter.yaml"
    namespace = "observe"

    def __init__(self, kubectl=None):
        self._kubectl = kubectl

    @property
    def kubectl(self):
        if self._kubectl is None:
            from aiopslab.service.kubectl import KubeCtl

            self._kubectl = KubeCtl()
        return self._kubectl

    def deploy(self) -> None:
        """Idempotently apply the exporter manifest (kubectl apply)."""
        self.kubectl.create_namespace_if_not_exist(self.namespace)
        result = self.kubectl.exec_command(
            f'kubectl apply -f "{self.manifest_path}"'
        )
        print(f"UPS exporter deployed: {result.strip()}")

    def teardown(self) -> None:
        """Remove the exporter manifest and the published state (safe if absent)."""
        result = self.kubectl.exec_command(
            f'kubectl delete -f "{self.manifest_path}" --ignore-not-found=true --wait=false'
        )
        # The host-side PowerController created the state ConfigMap; remove it
        # so the cluster is clean for the next problem.
        from aiopslab.generators.fault.power.ups import UPS_CONFIGMAP_NAME

        self.kubectl.exec_command(
            f"kubectl delete configmap {UPS_CONFIGMAP_NAME} "
            f"-n {self.namespace} --ignore-not-found=true"
        )
        print(f"UPS exporter removed: {result.strip()}")
