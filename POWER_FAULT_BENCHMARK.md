# AIOpsLab power-fault simulation and agent benchmark

## Evaluation version 2

Version 2 changes score semantics; do not compare its numbers directly with
earlier runs. The power suite now includes a healthy telemetry control (21
cases total). Analysis requires `fault_family`, `affected_nodes`, and a
nonempty `evidence` summary in addition to the existing fields. Evidence
presence is checked, but its factual grounding is not automatically graded.
Localization uses node-set F1, with precision and recall retained separately.

Failed executions remain in score denominators as zero; infrastructure failures
are marked explicitly. This is a conservative attempted-run score, not a
claim that every execution error is the agent's fault. Cleanup failures stop
the batch. Task coverage and completed/attempted counts accompany scores.

Mitigation rejects empty namespaces, missing container statuses, removed
deployments, and reduced replica targets. TTM now includes post-submission
recovery checking; `submission_time` and `recovery_wait` remain separate.
This is measured from session start, not fault onset. Failed recovery has no
finite TTM and remains a failed attempt.

`power_node(..., "on")` no longer restores facility supply. The explicit
`restore_simulated_utility()` action models a facility operator with a one
second delay. This action is available only during an active simulator session.
Proactive credit additionally requires successful mitigation and this explicit
facility action, with no recorded node loss.

Use `--repeats 3` for repeated runs. CSV rows include run/repeat IDs; a sibling
`.manifest.json` records suite version, selected cases, commit, dirty state,
Python version, step budget and power-related environment overrides.

Still required before publishing a full operational benchmark: application
request/SLO probes and sustained recovery windows; fault-onset timing; separate
agent/infrastructure error attribution; image digests/topology/model/prompt
provenance; confidence intervals; audited safety gates; noisy/delayed telemetry
tracks; additional workloads and parameterized scenarios. The current manifest
is not sufficient to reproduce all infrastructure or model settings.

## Purpose and scope

This extension evaluates an AIOps agent that manages a battery-backed hybrid
compute estate. The local testbed is a multi-node `kind` Kubernetes cluster;
kind worker containers represent on-premises compute nodes. The benchmark
adds facility-power signals and node-level faults while preserving AIOpsLab's
existing problem contract:

1. deploy an application;
2. start workload;
3. inject a fault;
4. let the agent observe and act through registered APIs;
5. score detection, localization, analysis, or mitigation;
6. recover and clean up the environment.

The four required power events are outage, brownout, load shedding, and
generator cutover. UPS battery depletion is included as a fifth composite
case because it tests the core BMS requirement: act during a finite battery
window before a node is lost.

This is a software-in-the-loop benchmark. Docker stop/start and CPU quota
changes are controlled proxies for power loss and degradation; they do not
electrically stress hardware. Real BMS, UPS, generator, thermal, and cloud
control should be attached later through adapters with the same fault and
action contracts.

## Relationship to upstream AIOpsLab

The repository is based on Microsoft AIOpsLab `main`. The standard agent
onboarding contract remains unchanged: an agent is a Python object with an
async `get_action(state: str) -> str` method, registered with
`Orchestrator.register_agent`. See the
[upstream onboarding instructions](https://github.com/microsoft/AIOpsLab#how-to-onboard-your-agent-to-aiopslab).

The extension follows the same layout as existing problems such as
`network_delay`:

| AIOpsLab concept | Power implementation |
|---|---|
| Fault injector | `aiopslab/generators/fault/inject_power.py` |
| Fault state/ground truth | `aiopslab/generators/fault/power/ups.py` |
| Problem definitions | `aiopslab/orchestrator/problems/power_faults/` |
| Problem registry | `aiopslab/orchestrator/problems/registry.py` |
| Read action | `get_power_status()` |
| Mitigation action | `power_node(node, "on" | "off")` |
| Unit tests | `tests/generators/test_power_*.py` |
| Live-cluster tests | `tests/integration/test_power_faults.py` |
| Batch benchmark | `clients/power_eval.py` |

## Simulation model

### State model

The deterministic `PowerController` records transitions and publishes the
current state to the `ups-power-state` ConfigMap:

```text
ONLINE --utility lost--> ON_BATTERY --battery depleted--> POWER_LOST
   ^                          |
   |                          +--ATS transfer--> ON_GENERATOR
   +---------------- utility restored ------------------+
```

Published state includes:

- UPS state;
- input voltage and frequency;
- battery charge ratio and seconds remaining;
- per-node power state;
- timestamp.

The in-cluster UPS exporter converts that state into Prometheus gauges:
`power_input_volts`, `power_frequency_hz`, `ups_state`,
`ups_battery_charge_ratio`, `ups_seconds_remaining`, and
`node_power_state{node}`. It runs on the control-plane node so worker loss
does not remove the benchmark's monitoring channel.

### Physical-to-software mapping

| Physical event | Testbed operation | Expected evidence |
|---|---|---|
| Hard power loss | Stop kind worker container with timeout 0 | Container exited; node becomes `NotReady`; pods become unavailable/stale |
| Power restoration | Start kind worker container | Container running; node returns `Ready`; pods converge |
| Brownout/thermal derating | Apply Docker CPU cgroup quota | Node remains `Ready`; throttling and latency increase |
| UPS ride-through | Countdown in power controller | `ON_BATTERY`; charge and seconds remaining decrease |
| ATS transfer | State transition plus frequency transient | `ON_BATTERY` then `ON_GENERATOR`; optional blip node is lost |
| Load shedding | Deterministic rolling stop/start schedule | Different workers are off in non-overlapping windows |

## Fault scenarios

### Outage

`inject_power_outage` sets utility voltage to zero and hard-stops the target
worker. The target is normally the worker hosting the most application pods.
Recovery starts every stopped worker and returns the UPS model to `ONLINE`.

Agent expectations:

- detect a facility/hardware incident;
- localize it to the affected node;
- distinguish it from a pod-only failure;
- restore the node or move critical work before service recovery deadlines.

### Brownout

`inject_brownout` lowers reported voltage and applies a CPU quota to the
workers. Nodes stay `Ready`, making Kubernetes availability alone an
insufficient signal. Recovery restores the exact pre-fault quota.

This scenario also acts as the present thermal-throttling proxy: both produce
reduced compute capacity without immediate node loss. A future thermal
adapter should publish temperature and thermal-throttle counters rather than
reusing the electrical voltage signal.

Agent expectations:

- correlate low voltage/CPU throttling with latency or throughput change;
- avoid unnecessary hard restarts;
- reduce or migrate non-critical load;
- confirm capacity and SLO recovery.

### Load shedding

`inject_load_shedding` creates a round-robin schedule over worker nodes. Only
one worker is intentionally off at a time. The final window remains off so a
mitigation task has a concrete target.

Agent expectations:

- recognize a planned rotating pattern rather than independent random node
  failures;
- protect critical workloads and shed lower-priority work;
- avoid placing migrated work on the next scheduled node;
- restore the final node and verify cluster health.

### Generator cutover

`inject_generator_cutover` transitions from utility to battery and then to
generator, applies a short CPU dip, and optionally leaves one single-PSU
worker off as the transfer casualty.

Agent expectations:

- detect the ATS sequence and frequency transient;
- localize the worker that failed to ride through;
- keep critical service available during transfer;
- restore the affected worker without disrupting healthy nodes.

### UPS battery depletion

`inject_ups_battery_drain` starts a visible countdown. CPU capacity decreases
as the deadline approaches. If the agent does not restore utility or move the
load before the deadline, the target worker is hard-stopped.

Agent expectations:

- estimate urgency from seconds remaining and critical workload placement;
- act before depletion when possible;
- restore the system reactively if the deadline is missed;
- leave all nodes powered, unthrottled, and healthy.

## Hybrid compute and BMS integration boundary

The current kind implementation is the local-compute adapter. A production
hybrid benchmark should keep the same logical interfaces and add:

| Domain | Read adapter | Write adapter | Faults to add |
|---|---|---|---|
| BMS/UPS | charge, voltage, current, runtime, alarms | acknowledge alarm, select policy | sensor drift, stale data, pack imbalance |
| Local nodes | readiness, temperature, throttling, workload placement | power, cordon, drain, DVFS | node loss, thermal trip, fan loss |
| Cloud | quota, price, capacity, region health | scale out/in, migrate, fail over | quota exhaustion, region loss, network partition |
| Workloads | SLO, priority, checkpoint state, energy demand | pause, shed, reschedule, checkpoint | overload, deadline miss, bad placement |

Do not give an evaluation agent unrestricted BMS or cloud credentials. Expose
allow-listed actions, record every action, enforce node/site budgets, and
include an emergency-stop mechanism outside the agent.

## Benchmark suites

### Current AIOps controls

`current-smoke` is a representative upstream-only control suite. It includes
a no-fault case and existing virtualization, application, and network faults.
`current` selects every registered non-power problem and is the exhaustive,
heterogeneous suite.

The smoke suite uses:

- `noop_detection_hotel_reservation-1`;
- all four task variants of `k8s_target_port-misconfig-1`;
- all four task variants of `misconfig_app_hotel_res-1`;
- detection and localization for `network_delay_hotel_res-1`.

The no-op problem is mandatory when comparing agents because positive-only
fault suites cannot measure false alarms.

### Power suite

`power` contains 20 problems: five fault families multiplied by four tasks.
`power-smoke` covers detection for every family and all four tasks for UPS
depletion. The complete problem ID shape is:

```text
power_<fault>_hotel_res-<task>-1
```

where `<fault>` is `outage`, `brownout`, `load_shedding`,
`generator_cutover`, or `ups_drain`, and `<task>` is `detection`,
`localization`, `analysis`, or `mitigation`.

## Metrics and pass criteria

Scores use AIOpsLab's existing result fields rather than a second evaluator.

| Task | Primary score | Time metric | Minimum acceptance target |
|---|---|---|---|
| Detection | Correct/incorrect | TTD | >= 0.90 accuracy and <= 0.05 no-fault false-positive rate |
| Localization | Exact/partial node or service match, 0-100 | TTL | >= 0.80 normalized mean; no unrelated-site action |
| Analysis | Correct system level and fault type | TTA | >= 0.80 exact success |
| Mitigation | Nodes, pods, UPS and quotas healthy | TTM | >= 0.90 success; zero unrecovered runs |

The runner reports a normalized score for each task and the unweighted mean
of available task scores. It deliberately keeps these safety diagnostics
separate from the aggregate:

- no-fault false-positive rate;
- proactive mitigation rate for UPS depletion;
- steps, input tokens, and output tokens;
- wall time and AIOpsLab framework overhead.

For a power-resource agent, the following are hard gates even if the aggregate
score is high:

1. every injected fault is recovered after the run;
2. no healthy control-plane node is powered off;
3. mitigation does not increase unavailable critical replicas;
4. all CPU quotas are restored;
5. no action occurs outside the allow-listed testbed;
6. the same seed/configuration produces the same fault schedule.

Future hardware-in-the-loop scoring should add critical-workload energy
served, SLO violation area, battery reserve at recovery, cloud spillover
cost, thermal limit violations, and unnecessary workload shedding.

## Test layers

### Layer 1: deterministic unit tests

These tests require no cluster or Docker daemon. Fake clocks, containers,
and Kubernetes objects verify state transitions, schedules, exact recovery,
problem registration, evaluator behavior, and benchmark scoring.

```bash
python -m pytest tests/generators tests/registry/test_get_actions.py -q
```

### Layer 2: live injector tests

These tests require Docker and a multi-worker kind cluster. Each fault is
injected, its observable signature is asserted, and recovery is verified.

```bash
kind create cluster --config kind/kind-config-power.yaml --wait 120s
python -m pytest tests/integration/test_power_faults.py -m integration -v -s
```

On Windows with Docker Desktop use
`kind/kind-config-power-win.yaml`, which omits the Linux `/run/udev` host
mount.

### Layer 3: end-to-end agent evaluation

Configure `aiopslab/config.yml`, deploy the required observability stack, and
register the agent in `clients/registry.py` or pass an existing registered
agent name.

```bash
# Quick infrastructure checks
python clients/power_eval.py --agent dummy --suite power-smoke

# Compare the agent on representative current and power scenarios
python clients/power_eval.py --agent generic --suite all-smoke --max-steps 20

# Complete power benchmark
python clients/power_eval.py --agent generic --suite power --max-steps 20

# Exhaustive registered benchmark; expensive and testbed-dependent
python clients/power_eval.py --agent generic --suite all --max-steps 20
```

The dummy agent is only a lifecycle check. It always submits `Yes` and is not
a meaningful operational baseline.

## Reproducibility controls

The main timings can be shortened for CI or lengthened for realistic runs:

| Variable | Default | Meaning |
|---|---:|---|
| `POWER_RIDE_THROUGH_SECONDS` | 90 | Battery deadline |
| `POWER_BROWNOUT_SEVERITY` | 0.35 | Remaining CPU fraction |
| `POWER_SHED_ON_SECS` | 60 | Powered interval in schedule |
| `POWER_SHED_OFF_SECS` | 45 | Shed interval |
| `POWER_SHED_CYCLES` | 2 | Number of rotations |
| `POWER_CUTOVER_BLIP_SECS` | 1 | ATS transfer blip |
| `POWER_WORKLOAD_DURATION` | 180 | Workload duration |
| `AIOPSLAB_NODE_READY_TIMEOUT` | 420 | Node recovery deadline |

Record the Git commit, kind node image digest, cluster topology, all variables,
agent/model version, prompt, and random seed with every published score. Run
each scenario at least three times for an LLM agent and report mean, standard
deviation, failures, and raw session traces. Never compare agents using
different fault timings or different observability access.

## Known limitations

- CPU quota is a brownout/thermal-capacity proxy, not an electrical model.
- The current action surface can power nodes but does not yet expose cloud
  migration, workload priority, checkpointing, or explicit load-shed APIs.
- The workload is HotelReservation; additional stateful, batch, GPU, and
  deadline-sensitive applications are needed for broader conclusions.
- Prometheus can lag the direct ConfigMap state. The direct power-status API
  is retained so telemetry-pipeline failure does not make ground truth
  inaccessible.
- A real hybrid benchmark must inject WAN, cloud quota, and correlated
  workload faults in addition to local power events.

These limitations should be stated with benchmark results; passing this suite
demonstrates AIOps behavior in the simulator, not safe autonomous control of a
production electrical system.
