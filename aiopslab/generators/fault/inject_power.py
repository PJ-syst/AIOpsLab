# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Power-aware fault injection for kind clusters.

Faults at the facility/hardware layer: utility outages, brownouts, load
shedding, generator cutover and UPS battery drain — all in the presence of
battery backup. See POWER_FAULTS.md for the design.

The testbed is a kind cluster, where every Kubernetes node *is* a Docker
container on the host; node power maps to Docker container operations via
docker-py (never ``sudo`` shell calls, so this works on Windows hosts with
Docker Desktop too).

Method naming follows the base-class dispatch contract
(``inject_<fault_type>`` / ``recover_<fault_type>``), so both direct calls
(the style used by the power problems) and ``_inject("power_outage", [...])``
work.
"""

import os
import threading
import time
from typing import Callable

from aiopslab.generators.fault.base import FaultInjector
from aiopslab.generators.fault.power.ups import (
    PowerController,
    UpsState,
    read_power_state,
)

# Cgroup values used for CPU throttling (100 ms period is the kernel default).
CPU_PERIOD_US = 100_000


# --------------------------------------------------------------------------- #
# Tunables (environment), kept short so CI can run fast scenarios
# --------------------------------------------------------------------------- #


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def default_ride_through() -> float:
    return max(1.0, _env_float("POWER_RIDE_THROUGH_SECONDS", 90.0))


def default_brownout_severity() -> float:
    return min(1.0, max(0.05, _env_float("POWER_BROWNOUT_SEVERITY", 0.35)))


def default_shed_on_secs() -> float:
    return max(1.0, _env_float("POWER_SHED_ON_SECS", 60.0))


def default_shed_off_secs() -> float:
    return max(1.0, _env_float("POWER_SHED_OFF_SECS", 45.0))


def default_shed_cycles() -> int:
    return max(1, _env_int("POWER_SHED_CYCLES", 2))


def default_cutover_blip_secs() -> float:
    return max(0.1, _env_float("POWER_CUTOVER_BLIP_SECS", 1.0))


def _parse_duration(duration) -> float | None:
    """Accept None, a seconds number, or a Chaos-Mesh style string like '90s'."""
    if duration is None:
        return None
    if isinstance(duration, (int, float)):
        return float(duration)
    text = str(duration).strip().lower()
    if text.endswith("s"):
        text = text[:-1]
    try:
        return float(text)
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Node helpers (shared with the power problems)
# --------------------------------------------------------------------------- #


def list_worker_nodes(kubectl) -> list[str]:
    """Return the names of all worker (non-control-plane) nodes."""
    items = kubectl.core_v1_api.list_node().items
    workers = []
    for node in items:
        labels = node.metadata.labels or {}
        if any(
            key.startswith("node-role.kubernetes.io/control-plane")
            or key == "node-role.kubernetes.io/master"
            for key in labels
        ):
            continue
        workers.append(node.metadata.name)
    return workers


def pick_node_hosting_most_pods(kubectl, namespace: str) -> str:
    """Return the worker node hosting the most pods in ``namespace``.

    Power faults should hit the busiest node: that is what makes an outage
    hurt (and what makes detection non-trivial).
    """
    workers = set(list_worker_nodes(kubectl))
    if not workers:
        raise RuntimeError("No worker nodes found in the cluster.")

    counts: dict[str, int] = {}
    for pod in kubectl.list_pods(namespace).items:
        node = getattr(pod.spec, "node_name", None)
        if node and node in workers:
            counts[node] = counts.get(node, 0) + 1

    if not counts:
        # No scheduled pods (or none on workers): fall back to first worker.
        return sorted(workers)[0]
    return max(sorted(counts.items()), key=lambda kv: kv[1])[0]


# --------------------------------------------------------------------------- #
# Active power session (used by the agent actions)
# --------------------------------------------------------------------------- #

_active_lock = threading.Lock()
_active_injector: "PowerFaultInjector | None" = None


def set_active_injector(injector: "PowerFaultInjector") -> None:
    """Mark a power session as active so agent actions can use it."""
    global _active_injector
    with _active_lock:
        _active_injector = injector


def clear_active_injector() -> None:
    global _active_injector
    with _active_lock:
        _active_injector = None


def get_active_injector() -> "PowerFaultInjector | None":
    with _active_lock:
        return _active_injector


# --------------------------------------------------------------------------- #
# The injector
# --------------------------------------------------------------------------- #


class PowerFaultInjector(FaultInjector):
    """Inject power-aware faults by controlling kind node containers.

    Args:
        namespace: The application namespace (used for node selection when
            no explicit node list is given).
        kubectl: Injectable KubeCtl (tests pass fakes; lazily created when
            needed in production).
        docker: Injectable Docker service wrapper (same injection pattern).
        controller: Injectable PowerController; by default a publishing one
            is created.
    """

    def __init__(
        self,
        namespace: str,
        kubectl=None,
        docker=None,
        controller: PowerController | None = None,
    ):
        super().__init__(namespace)
        self.namespace = namespace
        self._kubectl = kubectl
        self._docker = docker
        self._controller = controller or PowerController(publish=True)

        self._stopped: dict[str, bool] = {}  # nodes currently powered off
        self._throttled: dict[str, dict] = {}  # node -> prior cgroup values
        self._threads: list[threading.Thread] = []
        self._stop_events: list[threading.Event] = []
        self._shed_schedule: list[tuple] = []  # load-shedding ground truth

        self._journal: list[dict] = []  # injector-side ground truth
        self._journal_lock = threading.Lock()

    # -- lazy service handles (unit tests never construct real ones) --------

    @property
    def kubectl(self):
        if self._kubectl is None:
            from aiopslab.service.kubectl import KubeCtl

            self._kubectl = KubeCtl()
        return self._kubectl

    @property
    def docker(self):
        if self._docker is None:
            from aiopslab.service.dock import Docker

            self._docker = Docker()
        return self._docker

    @property
    def controller(self) -> PowerController:
        return self._controller

    # ------------------------------------------------------------------ #
    # Journal (ground truth for evaluators)
    # ------------------------------------------------------------------ #

    @property
    def journal(self) -> list[dict]:
        with self._journal_lock:
            return list(self._journal)

    def _log(self, action: str, node: str | None = None, **details) -> None:
        entry = {"t": time.time(), "action": action, "node": node}
        entry.update(details)
        with self._journal_lock:
            self._journal.append(entry)

    def node_ever_down(self, node: str) -> bool:
        """True if this injector ever hard-stopped the node (proactive eval)."""
        return any(
            e["action"] == "node_power_off" and e["node"] == node
            for e in self.journal
        )

    def node_is_powered_off(self, node: str) -> bool:
        """Return whether this session currently owns a stopped node."""
        return bool(self._stopped.get(node))

    # ------------------------------------------------------------------ #
    # Node power primitives
    # ------------------------------------------------------------------ #

    def _container(self, node: str):
        return self.docker.get_container(node)

    def power_off_node(self, node: str, timeout: int = 0) -> None:
        """Hard power-off (yank the cord): no drain, no graceful shutdown."""
        self._container(node).stop(timeout=timeout)
        self._stopped[node] = True
        self._controller.node_off(node)
        self._log("node_power_off", node, timeout=timeout)
        print(f"Power OFF: node {node}")

    def power_on_node(self, node: str) -> None:
        """Restore power to a node (fast; convergence is a separate step).

        A power-on is a fresh node state: any brownout CPU throttle applied
        to the node is cleared (docker container updates persist across
        restarts, so the clear must be explicit).
        """
        self._container(node).start()
        if node in self._throttled:
            self.unthrottle_node(node)
        self._stopped.pop(node, None)
        self._controller.node_on(node)
        self._log("node_power_on", node)
        print(f"Power ON: node {node}")

    @property
    def has_throttled_nodes(self) -> bool:
        """True while any node still carries a brownout-style CPU throttle."""
        return bool(self._throttled)

    def wait_node_power_recovered(
        self, node: str, timeout: float = 420.0, poll: float = 3.0
    ) -> bool:
        """Synchronously converge a power-cycled node.

        After an ungraceful power loss and restore, a kind node needs:
        1. kubelet to re-register (node Ready) — can take minutes;
        2. /run/udev recreated (OpenEBS NDM hostPath target): it lives on
           the systemd tmpfs mounted on /run and is lost on every hard
           power cycle;
        3. pods stuck in Unknown / Terminating / CrashLoopBackOff cleared
           so their controllers recreate them.

        Call this after recover_* before handing the cluster to the next
        consumer (next problem's init, evaluation, teardown).
        """
        stop = threading.Event()
        if not self._wait_node_ready(node, stop, timeout, poll):
            print(f"Warning: node {node} did not report Ready within {timeout}s")
            return False
        try:
            self._container(node).exec_run(["sh", "-c", "mkdir -p /run/udev"])
        except Exception as e:
            print(f"Warning: /run/udev repair failed on {node}: {e}")
        # Give controllers a moment to reconcile post-boot pod statuses,
        # then clear anything stuck from the ungraceful loss.
        if stop.wait(10.0):
            return False
        self._cleanup_stale_pods(node)
        return True

    def _wait_node_ready(
        self, node: str, stop: threading.Event, timeout: float, poll: float
    ) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if stop.is_set():
                return False
            try:
                info = self.kubectl.core_v1_api.read_node(node)
                for condition in getattr(info.status, "conditions", None) or []:
                    if condition.type == "Ready" and condition.status == "True":
                        return True
            except Exception:
                pass
            if stop.wait(poll):
                return False
        return False

    def _cleanup_stale_pods(self, node: str) -> None:
        """Force-delete pods on a node stuck by an ungraceful power loss.

        Pods the kubelet never re-adopted (phase Unknown) or stuck Terminating
        must be cleared so their controllers recreate them cleanly.
        """
        try:
            import json as _json

            raw = self.kubectl.exec_command(
                f"kubectl get pods -A --field-selector spec.nodeName={node} -o json"
            )
            pods = _json.loads(raw or "{}").get("items", [])
            for ns, name in self._stale_pods_to_delete(pods):
                self.kubectl.exec_command(
                    f"kubectl delete pod {name} -n {ns} --force --grace-period=0"
                )
                print(f"Cleaned stale pod {name} in {ns} after node power-on")
        except Exception as e:
            print(f"Stale pod cleanup skipped for {node}: {e}")

    @staticmethod
    def _stale_pods_to_delete(pods: list[dict]) -> list[tuple[str, str]]:
        """Return (namespace, name) of pods stuck after ungraceful node loss.

        Covers pods in Unknown phase, pods stuck Terminating, and pods whose
        containers crash-looped after the hard power loss (controllers
        recreate all of them cleanly).
        """
        stale: list[tuple[str, str]] = []
        for pod in pods:
            meta = pod.get("metadata", {}) or {}
            status = pod.get("status", {}) or {}
            phase = str(status.get("phase") or "").lower()
            terminating = meta.get("deletionTimestamp") is not None
            crashlooping = any(
                str(
                    ((c.get("state") or {}).get("waiting") or {}).get("reason") or ""
                ).lower()
                == "crashloopbackoff"
                for c in status.get("containerStatuses", []) or []
            )
            if (
                phase == "unknown"
                or (terminating and phase != "succeeded")
                or crashlooping
            ):
                stale.append((meta.get("namespace", "default"), meta.get("name", "?")))
        return stale

    def throttle_node_cpu(self, node: str, ratio: float) -> None:
        """Throttle a node's CPUs to ``ratio`` fraction (brownout/degradation).

        The original cgroup quota is journaled so it can be restored exactly
        (note: docker container updates persist across container restarts,
        so recovery must always unthrottle after power-on).
        """
        container = self._container(node)
        if node not in self._throttled:
            host_config = container.attrs.get("HostConfig", {}) or {}
            self._throttled[node] = {
                "quota": host_config.get("CpuQuota", -1),
                "period": host_config.get("CpuPeriod", 0) or CPU_PERIOD_US,
            }
        ratio = min(1.0, max(0.01, float(ratio)))
        ncpu = os.cpu_count() or 1
        quota = max(1000, int(ratio * ncpu * CPU_PERIOD_US))
        container.update(cpu_quota=quota, cpu_period=CPU_PERIOD_US)
        self._log("node_throttle", node, ratio=ratio, quota=quota)

    def unthrottle_node(self, node: str) -> None:
        """Restore a node's original CPU quota.

        Note: an untouched container reports ``CpuQuota: 0`` in inspect, but
        the Docker update API treats ``0`` as a no-op; ``-1`` is the value
        that actually resets the quota to unlimited.
        """
        container = self._container(node)
        prior = self._throttled.pop(node, None)
        if prior:
            restore_quota = prior["quota"] if prior["quota"] else -1
            container.update(
                cpu_quota=restore_quota, cpu_period=prior["period"]
            )
        else:
            container.update(cpu_quota=-1, cpu_period=CPU_PERIOD_US)
        self._log("node_unthrottle", node)

    # ------------------------------------------------------------------ #
    # Fault library
    # ------------------------------------------------------------------ #

    # P.1 - power_outage: utility loss without ride-through (hard node loss)
    def inject_power_outage(self, nodes=None, duration=None) -> None:
        """Full utility loss: hard power-off of the target node(s)."""
        nodes = self._resolve_nodes(nodes)
        self._controller.utility_lost(ride_through=0)
        for node in nodes:
            self.power_off_node(node)
        self._controller.publish(force=True)
        print(
            f"Injected power outage | nodes: {nodes} | namespace: {self.namespace}"
        )

    def recover_power_outage(self, nodes=None) -> None:
        self._controller.utility_restored()
        self._restore_powered_off_nodes(nodes)
        print("Recovered from power outage")

    # P.2 - power_brownout: sustained undervoltage, node survives degraded
    def inject_brownout(
        self, nodes=None, severity: float | None = None, duration=None
    ) -> None:
        """Voltage sag: nodes stay up but are CPU-throttled to ``severity``."""
        nodes = self._resolve_nodes(nodes)
        severity = default_brownout_severity() if severity is None else severity
        self._controller.brownout()
        for node in nodes:
            self.throttle_node_cpu(node, severity)
        self._controller.publish(force=True)
        print(
            f"Injected brownout | nodes: {nodes} | severity: {severity:.2f}"
        )

        auto_recover = _parse_duration(duration)
        if auto_recover:
            stop = threading.Event()
            self._stop_events.append(stop)
            thread = threading.Thread(
                target=self._timed_recover_worker,
                args=(auto_recover, self.recover_brownout, stop),
                daemon=True,
            )
            self._threads.append(thread)
            thread.start()

    def recover_brownout(self, nodes=None) -> None:
        for node in list(self._throttled):
            if nodes is None or node in nodes:
                self.unthrottle_node(node)
        self._controller.utility_restored()
        print("Recovered from brownout")

    # P.3 - power_load_shedding: rolling scheduled outages across nodes
    def inject_load_shedding(
        self,
        nodes=None,
        on_secs: float | None = None,
        off_secs: float | None = None,
        cycles: int | None = None,
    ) -> None:
        """Rolling power cuts: one node is shed at a time, round-robin."""
        nodes = self._resolve_nodes(nodes)
        on_secs = default_shed_on_secs() if on_secs is None else float(on_secs)
        off_secs = default_shed_off_secs() if off_secs is None else float(off_secs)
        cycles = default_shed_cycles() if cycles is None else int(cycles)

        schedule = self._build_shedding_schedule(nodes, on_secs, off_secs, cycles)
        self._shed_schedule = schedule

        stop = threading.Event()
        self._stop_events.append(stop)
        thread = threading.Thread(
            target=self._shedding_worker, args=(schedule, stop), daemon=True
        )
        self._threads.append(thread)
        thread.start()

        self._controller.publish(force=True)
        print(
            f"Injected load shedding | nodes: {nodes} | on: {on_secs}s | "
            f"off: {off_secs}s | cycles: {cycles}"
        )

    def recover_load_shedding(self, nodes=None) -> None:
        self._stop_workers()
        self._restore_powered_off_nodes(nodes)
        self._controller.utility_restored()
        print("Recovered from load shedding")

    @staticmethod
    def _build_shedding_schedule(
        nodes: list[str], on_secs: float, off_secs: float, cycles: int
    ) -> list[tuple[float, float, str, bool]]:
        """Build the round-robin (start, end, node, powered) event list.

        One node is shed at a time; per-node off windows never overlap.
        The final shed window is NOT followed by a restore event: the
        utility "extends the last outage", leaving the last node powered
        off as the mitigation target for the agent to restore.
        """
        events: list[tuple[float, float, str, bool]] = []
        t = 0.0
        for cycle in range(cycles):
            for i, node in enumerate(nodes):
                events.append((t, t + off_secs, node, False))
                t += off_secs
                is_final_window = cycle == cycles - 1 and i == len(nodes) - 1
                if not is_final_window:
                    events.append((t, t + on_secs, node, True))
                    t += on_secs
        return events

    def _shedding_worker(self, schedule, stop: threading.Event) -> None:
        t0 = time.monotonic()
        for start, _end, node, powered in schedule:
            delay = start - (time.monotonic() - t0)
            if stop.wait(max(0.0, delay)):
                break
            try:
                if powered:
                    self.power_on_node(node)
                else:
                    self.power_off_node(node)
                self._controller.publish(force=True)
            except Exception as e:
                print(f"Load shedding step failed for {node}: {e}")
                break

    # P.4 - power_generator_cutover: ATS transfer, brief dip, blip node dies
    def inject_generator_cutover(
        self,
        nodes=None,
        blip_node: str | None = None,
        blip_secs: float | None = None,
        dip_severity: float = 0.6,
    ) -> None:
        """Utility loss caught by the UPS; ATS transfers to the generator.

        Models the transfer event: the UPS catches the loss (ON_BATTERY),
        all nodes briefly sag (transfer dip throttling), the ATS transfers
        the load (ON_GENERATOR with a frequency transient). ``blip_node``
        optionally models a single-PSU server that does not ride through
        and stays off until mitigated.
        """
        nodes = self._resolve_nodes(nodes)
        blip_secs = default_cutover_blip_secs() if blip_secs is None else blip_secs

        self._controller.utility_lost(ride_through=30)
        for node in nodes:
            self.throttle_node_cpu(node, dip_severity)
        self._controller.publish(force=True)

        if blip_node is not None:
            self.power_off_node(blip_node)

        # The transfer itself is fast — that is the point of an ATS.
        time.sleep(min(float(blip_secs), 5.0))

        self._controller.ats_transfer()
        for node in nodes:
            self.unthrottle_node(node)
        self._controller.publish(force=True)
        print(
            f"Injected generator cutover | nodes: {nodes} | "
            f"blip node: {blip_node}"
        )

    def recover_generator_cutover(self, nodes=None) -> None:
        self._controller.utility_restored()
        self._restore_powered_off_nodes(nodes)
        for node in list(self._throttled):
            self.unthrottle_node(node)
        print("Recovered from generator cutover")

    # P.5 - power_ups_battery_drain: two-phase composite power-aware fault
    def inject_ups_battery_drain(
        self,
        nodes=None,
        ride_through: float | None = None,
        severity_floor: float = 0.25,
    ) -> None:
        """Utility lost with battery backup: ride-through, then hard loss.

        Phase A (on battery): UPS state ON_BATTERY with a visible countdown;
        the node's CPU is progressively throttled as the battery drains.
        Phase B (depletion): the controller's deadline fires and the node
        is hard-stopped (UPS -> POWER_LOST).
        """
        nodes = self._resolve_nodes(nodes)
        ride_through = default_ride_through() if ride_through is None else ride_through

        self._controller.utility_lost(ride_through=ride_through)
        for node in nodes:
            self._controller.node_on(node)
        # Depletion performs the destructive phase once, exactly.
        self._controller.set_on_depleted(
            lambda: [self.power_off_node(n) for n in nodes]
        )
        self._controller.publish(force=True)

        stop = threading.Event()
        self._stop_events.append(stop)
        thread = threading.Thread(
            target=self._drain_worker,
            args=(nodes, float(ride_through), severity_floor, stop),
            daemon=True,
        )
        self._threads.append(thread)
        thread.start()
        print(
            f"Injected UPS battery drain | nodes: {nodes} | "
            f"ride-through: {ride_through}s"
        )

    def recover_ups_battery_drain(self, nodes=None) -> None:
        self._stop_workers()
        self._controller.set_on_depleted(None)
        self._restore_powered_off_nodes(nodes)
        for node in list(self._throttled):
            self.unthrottle_node(node)
        self._controller.utility_restored()
        print("Recovered from UPS battery drain")

    def _drain_worker(
        self,
        nodes: list[str],
        ride_through: float,
        severity_floor: float,
        stop: threading.Event,
    ) -> None:
        while not stop.is_set():
            # poll() journals depletion and fires the hard power-off exactly
            # once at the deadline.
            self._controller.poll()
            if all(self._stopped.get(n) for n in nodes):
                break
            state = self._controller.snapshot()
            remaining = state.get("seconds_remaining")
            if remaining is not None and ride_through > 0:
                ratio = max(
                    severity_floor,
                    (remaining / ride_through),
                )
                for node in nodes:
                    try:
                        self.throttle_node_cpu(node, ratio)
                    except Exception as e:
                        print(f"Drain throttling failed for {node}: {e}")
            self._controller.publish()
            if stop.wait(2.0):
                break

    # ------------------------------------------------------------------ #
    # Recovery plumbing
    # ------------------------------------------------------------------ #

    def recover_all(self) -> None:
        """Idempotent full recovery: threads stopped, nodes on, UPS ONLINE."""
        self._stop_workers()
        self._controller.set_on_depleted(None)
        self._restore_powered_off_nodes(None)
        for node in list(self._throttled):
            self.unthrottle_node(node)
        if self._controller.state != UpsState.ONLINE:
            self._controller.utility_restored()
        self._controller.publish(force=True)
        print("Power injector fully recovered")

    def _restore_powered_off_nodes(self, nodes) -> None:
        targets = list(self._stopped) if nodes is None else list(nodes)
        for node in targets:
            if self._stopped.get(node):
                try:
                    self.power_on_node(node)
                except Exception as e:
                    print(f"Power-on failed for {node}: {e}")

    def _timed_recover_worker(
        self, delay: float, recover_fn, stop: threading.Event
    ) -> None:
        if stop.wait(delay):
            return
        try:
            recover_fn()
        except Exception as e:
            print(f"Auto-recover failed: {e}")

    def _stop_workers(self) -> None:
        for stop in self._stop_events:
            stop.set()
        for thread in self._threads:
            thread.join(timeout=10)
        self._stop_events = []
        self._threads = []

    def _resolve_nodes(self, nodes) -> list[str]:
        if nodes is None:
            return list_worker_nodes(self.kubectl)
        if isinstance(nodes, str):
            return [nodes]
        return list(nodes)


# Convenience re-export for the agent actions (avoids import cycles).
__all__ = [
    "PowerFaultInjector",
    "list_worker_nodes",
    "pick_node_hosting_most_pods",
    "set_active_injector",
    "clear_active_injector",
    "get_active_injector",
    "read_power_state",
    "default_ride_through",
    "default_brownout_severity",
    "default_shed_on_secs",
    "default_shed_off_secs",
    "default_shed_cycles",
    "default_cutover_blip_secs",
]
