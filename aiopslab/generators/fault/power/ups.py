# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Host-side power/facility model: UPS state machine, journal and telemetry channel.

This module models the *electrical* side of a datacenter row hosting the
cluster under test. It is deliberately deterministic and journaled, so that
problem evaluators have exact ground truth for what happened and when.

The controller runs on the machine driving AIOpsLab (next to the fault
injector). The latest state snapshot can be published to a ConfigMap inside
the cluster, where the ups-exporter exposes it to Prometheus and the
``get_power_status()`` agent action reads it directly.
"""

import json
import threading
import time
from enum import Enum
from typing import Callable

# Names/locations of the telemetry channel inside the cluster.
UPS_CONFIGMAP_NAME = "ups-power-state"
UPS_CONFIGMAP_NAMESPACE = "observe"

# Electrical model constants (North-American style single-phase values).
NOMINAL_VOLTS = 120.0
NOMINAL_FREQ = 60.0
BROWNOUT_VOLTS = 98.0
BROWNOUT_FREQ = 59.2
TRANSFER_FREQ = 58.5  # frequency dip while the ATS transfers to generator


class UpsState(str, Enum):
    ONLINE = "ONLINE"            # utility power, everything nominal
    ON_BATTERY = "ON_BATTERY"    # utility lost, riding on UPS battery
    ON_GENERATOR = "ON_GENERATOR"  # ATS transferred load to generator
    POWER_LOST = "POWER_LOST"    # no power at all (battery depleted or no UPS)


# Gauge value used for the ups_state metric (order matters).
STATE_VALUE = {
    UpsState.ONLINE: 0,
    UpsState.ON_BATTERY: 1,
    UpsState.ON_GENERATOR: 2,
    UpsState.POWER_LOST: 3,
}


class PowerController:
    """Stateful model of site power + UPS for a cluster under test.

    Thread-safe: all state transitions take an internal lock, so the
    controller can be shared between the injector's worker threads (e.g. the
    battery-drain loop) and the fault lifecycle.

    Args:
        publish: When True, snapshots are published to the telemetry
            channel on transitions and (roughly) every ``publish_interval``.
        clock: Injectable monotonic clock (seconds). Defaults to
            ``time.monotonic``; unit tests pass a fake.
        publisher: Injectable sink for published snapshots (receives the
            JSON string). Defaults to writing the ``ups-power-state``
            ConfigMap via kubectl, best-effort.
        publish_interval: Minimum seconds between automatic publishes.
        transfer_settle: Seconds the frequency transient lasts after an ATS
            transfer.
    """

    def __init__(
        self,
        publish: bool = True,
        clock: Callable[[], float] = time.monotonic,
        publisher: Callable[[str], None] | None = None,
        publish_interval: float = 2.0,
        transfer_settle: float = 3.0,
    ):
        self._lock = threading.RLock()
        self._clock = clock
        self._publish = publish
        self._publisher = publisher or self._default_publisher
        self._publish_interval = publish_interval
        self._transfer_settle = transfer_settle

        self._state = UpsState.ONLINE
        self._input_volts = NOMINAL_VOLTS
        self._frequency = NOMINAL_FREQ
        self._battery = 1.0
        self._deadline = None  # monotonic time when the battery depletes
        self._ride_through = None  # total ride-through seconds (for charge %)
        self._transfer_until = None  # monotonic time until freq transient ends
        self._node_states: dict[str, str] = {}  # node -> "on" / "off"

        # (monotonic, wall, event, details) tuples; ground truth for evals.
        self._journal: list[tuple] = []
        self._depleted_fired = False
        self._on_depleted: Callable[[], None] | None = None
        self._last_publish = None

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def state(self) -> UpsState:
        with self._lock:
            return self._state

    def journal(self) -> list[tuple]:
        """Return a copy of the event journal: (monotonic, wall, event, details)."""
        with self._lock:
            return list(self._journal)

    def seconds_remaining(self) -> int | None:
        """Seconds of battery left while ON_BATTERY, else None."""
        with self._lock:
            return self._seconds_remaining_locked()

    def _seconds_remaining_locked(self) -> int | None:
        if self._state != UpsState.ON_BATTERY or self._deadline is None:
            return None
        return max(0, int(self._deadline - self._clock()))

    def snapshot(self) -> dict:
        """Build the current state snapshot (also the published payload)."""
        with self._lock:
            remaining = self._seconds_remaining_locked()
            charge = self._battery
            if self._state == UpsState.ON_BATTERY and self._ride_through:
                charge = 0.0 if remaining is None else min(
                    1.0, remaining / self._ride_through
                )
            elif self._state == UpsState.POWER_LOST:
                charge = 0.0
            freq = self._frequency
            if self._transfer_until is not None and self._clock() >= self._transfer_until:
                freq = NOMINAL_FREQ
            return {
                "ups_state": self._state.value,
                "input_volts": round(self._input_volts, 1),
                "frequency_hz": round(freq, 2),
                "battery_charge_ratio": round(charge, 3),
                "seconds_remaining": remaining,
                "nodes": dict(self._node_states),
                "timestamp": time.time(),
            }

    # ------------------------------------------------------------------
    # Power events
    # ------------------------------------------------------------------

    def utility_lost(self, ride_through: float | None = None) -> None:
        """Utility power was lost.

        With ``ride_through > 0`` the UPS carries the load (ON_BATTERY) and a
        depletion deadline is armed; ``on_depleted`` callbacks fire from
        ``poll()`` when it expires. With no ride-through the site goes
        POWER_LOST immediately.
        """
        with self._lock:
            self._input_volts = 0.0
            self._record("utility_lost", {"ride_through": ride_through})
            if ride_through and ride_through > 0:
                self._state = UpsState.ON_BATTERY
                self._battery = 1.0
                self._ride_through = float(ride_through)
                self._deadline = self._clock() + float(ride_through)
                self._depleted_fired = False
            else:
                self._state = UpsState.POWER_LOST
                self._battery = 0.0
                self._deadline = None
                self._ride_through = None
            self.publish(force=True)

    def brownout(self, volts: float = BROWNOUT_VOLTS) -> None:
        """Sustained undervoltage: the UPS corrects it and stays ONLINE."""
        with self._lock:
            self._input_volts = float(volts)
            self._frequency = BROWNOUT_FREQ
            self._record("brownout", {"volts": self._input_volts})
            self.publish(force=True)

    def ats_transfer(self) -> None:
        """The automatic transfer switch moved the load to the generator."""
        with self._lock:
            self._record("ats_transfer", {})
            if self._state in (UpsState.ON_BATTERY, UpsState.ONLINE):
                self._state = UpsState.ON_GENERATOR
            # Countdown is cancelled the moment the generator takes the load.
            self._deadline = None
            self._frequency = TRANSFER_FREQ
            self._transfer_until = self._clock() + self._transfer_settle
            self._input_volts = NOMINAL_VOLTS
            self._battery = 1.0
            self.publish(force=True)

    def utility_restored(self) -> None:
        """Utility power is back (or power was otherwise fully restored)."""
        with self._lock:
            self._record("utility_restored", {})
            self._state = UpsState.ONLINE
            self._input_volts = NOMINAL_VOLTS
            self._frequency = NOMINAL_FREQ
            self._battery = 1.0
            self._deadline = None
            self._ride_through = None
            self._transfer_until = None
            self._depleted_fired = False
            self._on_depleted = None
            self.publish(force=True)

    def set_on_depleted(self, callback: Callable[[], None] | None) -> None:
        """Register the callback fired (once) when the battery depletes."""
        with self._lock:
            self._on_depleted = callback

    # ------------------------------------------------------------------
    # Node power bookkeeping (actual container power ops live in the injector)
    # ------------------------------------------------------------------

    def set_node(self, node: str, powered: bool) -> None:
        with self._lock:
            self._node_states[node] = "on" if powered else "off"
            self._record("node_power", {"node": node, "powered": powered})

    def node_off(self, node: str) -> None:
        self.set_node(node, powered=False)

    def node_on(self, node: str) -> None:
        self.set_node(node, powered=True)

    # ------------------------------------------------------------------
    # Clock-driven behaviour
    # ------------------------------------------------------------------

    def poll(self) -> None:
        """Non-blocking step: check battery depletion, publish if due.

        Call this regularly from the injector's worker loops. Depletion is
        journaled exactly once and the ``on_depleted`` callback fires
        exactly once per utility-loss episode.
        """
        with self._lock:
            if (
                self._state == UpsState.ON_BATTERY
                and self._deadline is not None
                and not self._depleted_fired
                and self._clock() >= self._deadline
            ):
                self._state = UpsState.POWER_LOST
                self._battery = 0.0
                self._deadline = None
                self._depleted_fired = True
                self._record("battery_depleted", {})
                callback = self._on_depleted
            else:
                callback = None
            self.publish()

        if callback is not None:
            # Fire outside the lock: callbacks perform (slow) docker ops.
            try:
                callback()
            except Exception as e:  # never let a callback kill the loop
                print(f"on_depleted callback failed: {e}")

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    def publish(self, force: bool = False) -> None:
        """Publish the current snapshot to the telemetry channel.

        Best-effort: failures to reach the cluster are logged and swallowed
        (the controller must keep modelling even if telemetry is down).
        """
        if not self._publish:
            return
        now = self._clock()
        if not force:
            if (
                self._last_publish is not None
                and now - self._last_publish < self._publish_interval
            ):
                return
        self._last_publish = now
        payload = json.dumps(self.snapshot())
        try:
            self._publisher(payload)
        except Exception as e:
            print(f"UPS state publish failed: {e}")

    @staticmethod
    def _default_publisher(payload: str) -> None:
        """Write the snapshot into the in-cluster ConfigMap."""
        from aiopslab.service.kubectl import KubeCtl

        kubectl = KubeCtl()
        kubectl.create_or_update_configmap(
            UPS_CONFIGMAP_NAME,
            UPS_CONFIGMAP_NAMESPACE,
            {"state.json": payload},
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _record(self, event: str, details: dict) -> None:
        self._journal.append((self._clock(), time.time(), event, details))


# Re-exported for the exporter/action layer: read the published snapshot.
def read_power_state() -> dict | None:
    """Read the latest UPS state snapshot from the cluster, or None.

    Returns None when the telemetry channel is absent (no cluster, or power
    telemetry not deployed for this problem).
    """
    import json as _json

    from aiopslab.service.kubectl import KubeCtl

    try:
        kubectl = KubeCtl()
        cm = kubectl.core_v1_api.read_namespaced_config_map(
            UPS_CONFIGMAP_NAME, UPS_CONFIGMAP_NAMESPACE
        )
        return _json.loads(cm.data["state.json"])
    except Exception:
        return None
