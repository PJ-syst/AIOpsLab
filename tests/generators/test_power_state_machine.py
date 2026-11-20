# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Unit tests for the UPS/power state machine (no cluster required)."""

import json

import pytest

from aiopslab.generators.fault.power.ups import (
    NOMINAL_FREQ,
    NOMINAL_VOLTS,
    TRANSFER_FREQ,
    PowerController,
    UpsState,
)


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, delta):
        self.t += delta


def make_controller(clock, publish=False, publisher=None, **kw):
    return PowerController(
        publish=publish, clock=clock, publisher=publisher, **kw
    )


class TestInitialAndSnapshot:
    def test_initial_state_is_online(self, ):
        controller = make_controller(FakeClock())
        snap = controller.snapshot()
        assert snap["ups_state"] == "ONLINE"
        assert snap["input_volts"] == NOMINAL_VOLTS
        assert snap["frequency_hz"] == NOMINAL_FREQ
        assert snap["battery_charge_ratio"] == 1.0
        assert snap["seconds_remaining"] is None
        assert controller.state is UpsState.ONLINE

    def test_node_states_in_snapshot(self):
        controller = make_controller(FakeClock())
        controller.set_node("kind-worker", powered=True)
        controller.set_node("kind-worker2", powered=False)
        snap = controller.snapshot()
        assert snap["nodes"] == {"kind-worker": "on", "kind-worker2": "off"}


class TestUtilityLoss:
    def test_utility_lost_with_ride_through(self):
        clock = FakeClock()
        controller = make_controller(clock)
        controller.utility_lost(ride_through=90)

        assert controller.state is UpsState.ON_BATTERY
        snap = controller.snapshot()
        assert snap["input_volts"] == 0.0
        assert snap["seconds_remaining"] == 90
        assert snap["battery_charge_ratio"] == 1.0

    def test_countdown_and_charge_decay(self):
        clock = FakeClock()
        controller = make_controller(clock)
        controller.utility_lost(ride_through=90)

        clock.advance(30)
        controller.poll()
        snap = controller.snapshot()
        assert snap["seconds_remaining"] == 60
        assert abs(snap["battery_charge_ratio"] - 60.0 / 90.0) < 0.01

    def test_utility_lost_without_ride_through_is_immediate(self):
        controller = make_controller(FakeClock())
        controller.utility_lost(ride_through=0)
        assert controller.state is UpsState.POWER_LOST
        assert controller.snapshot()["battery_charge_ratio"] == 0.0


class TestDepletion:
    def test_depletion_fires_callback_exactly_once(self):
        clock = FakeClock()
        controller = make_controller(clock)
        fired = []
        controller.set_on_depleted(lambda: fired.append("boom"))
        controller.utility_lost(ride_through=10)

        clock.advance(11)
        controller.poll()
        assert controller.state is UpsState.POWER_LOST
        assert len(fired) == 1

        controller.poll()  # second poll must not re-fire
        controller.poll()
        assert len(fired) == 1
        assert controller.snapshot()["seconds_remaining"] is None

    def test_journal_records_depletion(self):
        clock = FakeClock()
        controller = make_controller(clock)
        controller.utility_lost(ride_through=5)
        clock.advance(6)
        controller.poll()

        events = [entry[2] for entry in controller.journal()]
        assert "utility_lost" in events
        assert "battery_depleted" in events
        assert events.index("utility_lost") < events.index("battery_depleted")


class TestAtsTransfer:
    def test_transfer_cancels_countdown(self):
        clock = FakeClock()
        controller = make_controller(clock)
        controller.utility_lost(ride_through=10)
        controller.ats_transfer()

        assert controller.state is UpsState.ON_GENERATOR
        assert controller.seconds_remaining() is None

        clock.advance(100)  # long past the original deadline
        controller.poll()
        assert controller.state is UpsState.ON_GENERATOR  # no depletion

    def test_frequency_transient_settles(self):
        clock = FakeClock()
        controller = make_controller(clock, transfer_settle=3.0)
        controller.utility_lost(ride_through=30)
        controller.ats_transfer()

        assert controller.snapshot()["frequency_hz"] == TRANSFER_FREQ
        clock.advance(5)
        controller.poll()
        assert controller.snapshot()["frequency_hz"] == NOMINAL_FREQ


class TestRestoreAndBrownout:
    def test_utility_restored_resets_everything(self):
        clock = FakeClock()
        controller = make_controller(clock)
        controller.utility_lost(ride_through=90)
        clock.advance(10)
        controller.utility_restored()

        snap = controller.snapshot()
        assert snap["ups_state"] == "ONLINE"
        assert snap["input_volts"] == NOMINAL_VOLTS
        assert snap["battery_charge_ratio"] == 1.0
        assert snap["seconds_remaining"] is None

    def test_brownout_sags_voltage_but_stays_online(self):
        controller = make_controller(FakeClock())
        controller.brownout(volts=98.0)
        snap = controller.snapshot()
        assert snap["ups_state"] == "ONLINE"
        assert snap["input_volts"] == 98.0


class TestPublishing:
    def test_publish_writes_json_snapshot(self):
        clock = FakeClock()
        payloads = []
        controller = make_controller(clock, publish=True, publisher=payloads.append)
        controller.utility_lost(ride_through=90)

        assert len(payloads) >= 1
        state = json.loads(payloads[-1])
        assert state["ups_state"] == "ON_BATTERY"
        assert state["seconds_remaining"] == 90

    def test_publish_interval_throttles(self):
        clock = FakeClock()
        payloads = []
        controller = make_controller(
            clock, publish=True, publisher=payloads.append, publish_interval=2.0
        )

        controller.publish(force=True)
        assert len(payloads) == 1

        clock.advance(1.0)
        controller.publish()  # within interval -> suppressed
        assert len(payloads) == 1

        clock.advance(2.0)
        controller.publish()  # past interval -> published
        assert len(payloads) == 2

    def test_publish_disabled(self):
        controller = make_controller(FakeClock(), publish=False)
        controller.publish(force=True)  # must be a no-op, not an error


class TestJournal:
    def test_journal_is_ordered_and_copied(self):
        clock = FakeClock()
        controller = make_controller(clock)
        controller.utility_lost(ride_through=10)
        controller.set_node("n1", powered=False)
        controller.utility_restored()

        events = [entry[2] for entry in controller.journal()]
        assert events == ["utility_lost", "node_power", "utility_restored"]

        journal_copy = controller.journal()
        journal_copy.clear()
        assert len(controller.journal()) == 3  # copy semantics
