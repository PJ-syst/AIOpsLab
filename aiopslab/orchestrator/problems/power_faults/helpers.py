# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Helpers for the power-aware fault problems (HotelReservation)."""

from aiopslab.service.kubectl import KubeCtl
from aiopslab.service.apps.base import Application


def get_frontend_url(app: Application) -> str:
    kubectl = KubeCtl()
    endpoint = kubectl.get_cluster_ip(app.frontend_service, app.namespace)
    return f"http://{endpoint}:{app.frontend_port}"


def node_is_ready(kubectl: KubeCtl, node: str) -> bool:
    """True when the node object reports Ready (kubelet back online)."""
    try:
        node_info = kubectl.core_v1_api.read_node(node)
    except Exception:
        return False
    conditions = getattr(node_info.status, "conditions", None) or []
    for condition in conditions:
        if condition.type == "Ready":
            return condition.status == "True"
    return False
