# Power-Aware Faults for AIOpsLab

This document describes the **power-aware fault family** implemented in this
repository: utility **outages**, **brownouts**, **load shedding**,
**generator cutover**, and **UPS battery drain** — all in the presence of
**battery backup**. These are the first *facility/hardware-layer*,
*node-level*, and *time-phased* faults in AIOpsLab, and they target the
skills AIOps agents will need in real datacenters: detecting infrastructure
events from telemetry, localizing them to nodes, classifying them correctly,
and mitigating them (ideally *before* battery backup is exhausted).

---

## Table of contents

1. [Why power faults](#1-why-power-faults)
2. [The physical model (battery changes everything)](#2-the-physical-model-battery-changes-everything)
3. [Mapping physics to a kind cluster](#3-mapping-physics-to-a-kind-cluster)
4. [Architecture](#4-architecture)
5. [Fault catalog](#5-fault-catalog)
6. [Problems and evaluation contracts](#6-problems-and-evaluation-contracts)
7. [Agent action surface](#7-agent-action-surface)
8. [Telemetry and detectability](#8-telemetry-and-detectability)
9. [Testing strategy](#9-testing-strategy)
10. [Cluster requirements](#10-cluster-requirements)
11. [Testing it (three layers)](#11-testing-it-three-layers)
12. [Evaluation metrics](#12-evaluation-metrics)
13. [Future fault backlog](#13-future-fault-backlog)

---

## 1. Why power faults

Before this work, AIOpsLab's fault coverage was entirely at the pod /
container / service / config layer:

- `inject_hw.py` (the *hardware* layer) was an empty stub — every method
  raised `NotImplementedError`.
- No fault ever touched a **node**; everything targeted pods and services.
- No fault was **time-phased** — all existing faults are step functions
  (injected → present → recovered).

Power faults fill all three gaps at once:

| Gap | How power faults fill it |
|-----|--------------------------|
| Hardware layer | Power events are facility/hardware faults (`system_level: "Hardware"`) |
| Node level | Power-off = the whole kind node dies, ungracefully (no drain, no eviction) |
| Time-phased | Battery backup turns every event into a *warning phase* (on battery, countdown visible) followed by a *destructive phase* (hard power-off at depletion) |

The battery is the key design element: it converts an instantaneous outage
into a **detectable prelude** (`ups_state=ON_BATTERY`, seconds remaining
ticking down) before the destructive phase. This is exactly the "proactive
AIOps" skill we want to benchmark.

## 2. The physical model (battery changes everything)

```
                utility lost                     battery depleted
  ONLINE ────────────────────► ON_BATTERY ────────────────────────► POWER_LOST
    ▲                              │                                        │
    │                        ATS transfer                                power on
    │                              ▼                                        │
    └──── utility restored ──── ON_GENERATOR ───── utility restored ────────┘
```

- **Outage (blackout)**: input voltage drops to 0. Without a UPS the node
  dies instantly; with a UPS the node rides through for a bounded window.
- **Brownout**: input voltage sags (≈80% of nominal). The node survives but
  is degraded — CPU throttled, latency up. The node stays `Ready`: this is
  the *hard detection* case (SLOs degrade, nothing "fails").
- **Load shedding**: the utility schedules *rolling* outages across areas.
  Nodes rotate through off/on windows. Agents must recognize rotation, not
  just "a node is down".
- **Generator cutover**: the automatic transfer switch (ATS) moves the load
  to generator. Usually a brief blip + frequency/voltage transient; a
  single-PSU server may not ride through.
- **UPS battery drain**: the composite, "power-aware" scenario — utility
  lost, node runs on battery with a visible countdown, then hard power-off
  at depletion. Mitigation has a **deadline**; acting before depletion is
  scored as `proactive`.

The model is implemented as a deterministic, journaled state machine
(`PowerController`) so evaluation ground truth is exact.

## 3. Mapping physics to a kind cluster

The testbed is a **kind** cluster (kind nodes *are* Docker containers), so
power operations map to Docker operations on the node containers. The
injector uses **docker-py** (never `sudo` shell calls), so it works on
Windows hosts with Docker Desktop as well as on Linux CI runners.

| Real-world event | kind/K8s mechanism | Observable signature |
|------------------|-------------------|----------------------|
| Hard power-off   | `container.stop(timeout=0)` — ungraceful, no drain, no eviction | Node `NotReady` (~40 s kubelet grace), pods stuck `Unknown`/`Terminating`, no eviction events |
| UPS ride-through | Node CPU throttling via cgroup (`container.update(cpu_quota, cpu_period)`) + UPS countdown telemetry | `ups_state=1`, `ups_seconds_remaining` ↓, latency ↑ |
| Brownout         | CPU quota throttled to e.g. 35% + voltage sag in UPS telemetry | CPU throttled ratios, latency ↑, `power_input_volts` < nominal |
| Load shedding    | Scheduler thread: rolling `stop`/`start` per node per schedule | Node power state rotates over time; per-node off windows never overlap |
| Generator cutover| UPS transitions `ONLINE→ON_BATTERY→ON_GENERATOR` + brief transfer dip + optional blip node hard-off | Frequency transient (~58.5 Hz), blip node `NotReady` |
| Power restore    | `container.start()`, quota restored, UPS → `ONLINE` | Node returns `Ready` |

Notes:

- `container.update()` persists across container restarts, so recovery
  **must** unthrottle after powering a node back on. The injector handles
  this ordering.
- Power-off is *not* `kubectl drain`: the whole point is the ungraceful,
  facility-level signature (no eviction events, stale pod objects).

## 4. Architecture

```
┌──────────────────────────── Host (orchestrator process) ─────────────────────────────┐
│                                                                                      │
│  Problem (orchestrator/problems/power_faults/)                                       │
│    │ inject_fault() / recover_fault()                                                │
│    ▼                                                                                 │
│  PowerFaultInjector (generators/fault/inject_power.py)                               │
│    ├── docker-py: stop/start/update node containers                                  │
│    ├── PowerController (generators/fault/power/ups.py)                               │
│    │     state machine · journal · countdown · depletion callback                   │
│    │        │ publish snapshot (JSON) ──────────────────────────┐                     │
│    │        │                                                  │ (kubectl API)      │
│    └── worker threads: load-shedding scheduler, UPS drain loop   │                     │
│                                                                                      │
│  PowerOps registry (module-level active-injector handle)        │                     │
│    ▲ used by actions: get_power_status / power_node             │                     │
└──────────────────────────────────────────────────────────────────┼────────────────────┘
                                                                   ▼
┌────────────────────── In-cluster (observe namespace) ────────────────────────────────┐
│  ConfigMap ups-power-state  ◄── host publishes state every interval                 │
│         ▲                                                                            │
│         │ read on each scrape                                                        │
│  ups-exporter pod (python:3.12-slim, stdlib-only) ── /metrics ──► Prometheus          │
│  (pinned to control-plane so telemetry survives worker power loss)                   │
└──────────────────────────────────────────────────────────────────────────────────────┘
```

### Components

| File | Purpose |
|------|---------|
| `aiopslab/generators/fault/inject_power.py` | `PowerFaultInjector` — node power primitives + the five fault types; `PowerOps` active-session registry; node-selection helpers; `read_power_state()` |
| `aiopslab/generators/fault/power/ups.py` | `PowerController` — UPS state machine (`ONLINE / ON_BATTERY / ON_GENERATOR / POWER_LOST`), journal, battery countdown, depletion callback, ConfigMap publishing |
| `aiopslab/observer/ups_exporter.py` | `UpsExporter` — deploy/teardown of the in-cluster exporter |
| `aiopslab/observer/ups/ups-exporter.yaml` | Manifest: exporter Deployment (script embedded in a ConfigMap), RBAC, control-plane pinning, Prometheus scrape annotations |
| `aiopslab/observer/prometheus/prometheus/values.yaml` | `extraScrapeConfigs` added: annotation-based pod scraping (`prometheus.io/scrape: "true"`) |
| `aiopslab/observer/metric_api.py` | `power_metrics` export pass — UPS gauges land in the same metrics CSVs agents already read via `get_metrics()` |
| `aiopslab/orchestrator/actions/base.py` | `get_power_status()` action (read-only, all task types) |
| `aiopslab/orchestrator/actions/mitigation.py` | `power_node(node, "on"|"off")` action (write, mitigation tasks) |
| `aiopslab/orchestrator/tasks/analysis.py` | `'Power Issue'` added to the `fault_type` enum agents choose from |
| `aiopslab/orchestrator/problems/power_faults/` | The 5 problem modules × 4 task variants (20 registry entries) |
| `kind/kind-config-power.yaml` | 3-worker cluster config (load shedding / rescheduling need >1 worker) |
| `tests/generators/` | Unit tests (no cluster required) |
| `tests/integration/test_power_faults.py` | Integration + end-to-end tests (live kind cluster) |

### Safety model

- The orchestrator already wraps `inject_fault`/`recover_fault` in a
  `CriticalSection` with `atexit` cleanup. Power recovery is therefore
  **idempotent**: every `recover_*` can be called twice, out of order, or
  from an `atexit` handler, and converges to "all nodes powered on,
  unthrottled, UPS ONLINE".
- Background threads (load shedding, battery drain) are daemon threads
  controlled by `threading.Event`s that recovery sets and joins.
- The unit tests never touch Docker or the cluster — fakes are injected
  through the injector's constructor.

## 5. Fault catalog

All timings are configurable via environment variables:

| Env var | Default | Meaning |
|---------|---------|---------|
| `POWER_RIDE_THROUGH_SECONDS` | `90` | UPS ride-through window before depletion |
| `POWER_BROWNOUT_SEVERITY` | `0.35` | CPU fraction during brownout |
| `POWER_SHED_ON_SECS` | `60` | Load-shedding "on" window per node |
| `POWER_SHED_OFF_SECS` | `45` | Load-shedding "off" window per node |
| `POWER_SHED_CYCLES` | `2` | Load-shedding rotations |
| `POWER_CUTOVER_BLIP_SECS` | `1.0` | Transfer-switch blip duration |
| `POWER_WORKLOAD_DURATION` | `180` | wrk2 workload duration for power problems |

### 5.1 `power_outage` — utility loss, hard node power-off

`inject_power_outage(nodes)` → UPS: `POWER_LOST` immediately (no ride-through
variant), input voltage 0, target node container hard-stopped.
`recover_power_outage()` → node started, UPS `ONLINE`.

### 5.2 `power_brownout` — sustained undervoltage

`inject_brownout(nodes, severity)` → UPS telemetry shows voltage sag while
staying `ONLINE`; every worker node's CPU is throttled to `severity`.
`recover_brownout()` → quotas restored (original values are journaled and
restored exactly).

### 5.3 `power_load_shedding` — rolling scheduled outages

`inject_load_shedding(nodes, on_secs, off_secs, cycles)` → a scheduler
thread walks a deterministic round-robin schedule: one node is shed (hard
off) at a time, then restored, cycling through all worker nodes `cycles`
times. Per-node off windows never overlap. The schedule is journaled and
doubles as localization/analysis ground truth.

### 5.4 `power_generator_cutover` — ATS transfer to generator

`inject_generator_cutover(nodes, blip_node, blip_secs)` →
1. utility lost → UPS catches it (`ON_BATTERY`)
2. transfer dip: all nodes briefly CPU-throttled
3. `blip_node` (a single-PSU server that doesn't ride through) is hard
   stopped and **left off** — this is the mitigation target
4. ATS transfers → UPS `ON_GENERATOR`, frequency transient (~58.5 Hz)

`recover_generator_cutover()` → blip node restarted, UPS `ONLINE`.

### 5.5 `power_ups_battery_drain` — the composite power-aware scenario

`inject_ups_battery_drain(node, ride_through)` →
- **Phase A (on battery)**: UPS `ON_BATTERY`, `seconds_remaining` counts
  down from `ride_through`; node CPU is throttled progressively as the
  battery drains.
- **Phase B (depletion)**: the controller fires its depletion callback at
  the deadline → node hard-stopped, UPS `POWER_LOST`.

`recover_ups_battery_drain()` → node restarted + unthrottled (quota
survives container restarts, so unthrottle always follows power-on), UPS
`ONLINE`.

## 6. Problems and evaluation contracts

Five problem modules live in
`aiopslab/orchestrator/problems/power_faults/` — each defining a
`*BaseTask` (deploy telemetry → inject → recover) plus the four task
variants, all targeting **HotelReservation** (stateful MongoDB + OpenEBS
PVs make ungraceful node loss realistic):

| Problem ID pattern | Detection | Localization | Analysis | Mitigation |
|---|---|---|---|---|
| `power_outage_hotel_res-*-1` | Yes/No | faulty **node** | Hardware / Power Issue | node Ready + pods healthy + UPS ONLINE |
| `power_brownout_hotel_res-*-1` | Yes/No | worker nodes | Hardware / Power Issue | quotas restored + pods healthy |
| `power_load_shedding_hotel_res-*-1` | Yes/No | shed nodes | Hardware / Power Issue | rotation stopped, nodes on |
| `power_generator_cutover_hotel_res-*-1` | Yes/No | blip node | Hardware / Power Issue | blip node back + UPS ONLINE |
| `power_ups_drain_hotel_res-*-1` | Yes/No | drained node | Hardware / Power Issue | node back + **proactive** bonus |

Details:

- **Detection** — the agent submits `"Yes"`/`"No"`; scored as
  `Detection Accuracy` + TTD (standard contract).
- **Localization** — power faults are node-level, so these problems
  override the task description: the agent submits **node names**
  (`list[str]`). Scoring mirrors the service-localization contract:
  exact node-set match = 100%, superset = `len(expected)/len(pred)`, else 0.
- **Analysis** — expected `{"system_level": "Hardware", "fault_type":
  "Power Issue"}`. `'Power Issue'` was added to the enum in
  `tasks/analysis.py` (backward-compatible: existing problems compare
  against their own expected strings).
- **Mitigation** — success = all faulty nodes `Ready` **and**
  `wait_until_pods_healthy()` **and** UPS back `ONLINE`. The UPS-drain
  mitigation adds a `proactive` metric: `True` if the node never actually
  went down (the agent beat the battery), `False` if the agent recovered
  reactively after depletion.

Workloads run longer than the default 10 s (`POWER_WORKLOAD_DURATION`,
default 180 s) so traffic spans the fault window.

## 7. Agent action surface

Agents get two new, explicit, "IPMI-style" APIs (in addition to the existing
`exec_shell`, `get_logs`, `get_metrics`, …):

| Action | Type | Tasks | Behavior |
|--------|------|-------|----------|
| `get_power_status()` | read | all | Reads the UPS state ConfigMap and reports state, input voltage, frequency, battery charge, seconds remaining, and per-node power state. Returns a notice when power telemetry isn't deployed. |
| `power_node(node, action)` | write | mitigation | `"on"` / `"off"` a node through the active power session (used by the UPS-drain `proactive` scoring). Without an active session it returns an explanatory error. |

These are auto-documented to agents via the existing `@read`/`@write`
action discovery — no per-problem wiring needed.

## 8. Telemetry and detectability

Power faults must be *detectable from telemetry* — otherwise the benchmark
is vacuous. Three channels, in increasing fidelity:

1. **`get_power_status()`** — reads the `ups-power-state` ConfigMap in the
   `observe` namespace (written host-side by the `PowerController`). Works
   even when Prometheus is down.
2. **Prometheus** — the `ups-exporter` pod serves the UPS gauges
   (`power_input_volts`, `power_frequency_hz`, `ups_state`,
   `ups_battery_charge_ratio`, `ups_seconds_remaining`,
   `node_power_state{node}`) and is scraped via the new
   `extraScrapeConfigs` annotation job. The exporter is pinned to the
   control-plane node (monitoring on protected circuits, like real DCs) and
   is stdlib-only (runs on `python:3.12-slim`, no custom image build).
3. **`get_metrics()` CSVs** — `metric_api.py` gained a `power_metrics`
   export pass, so UPS gauges are exported to
   `metrics_output/metric_*/power/kpi_<metric>.csv` alongside the existing
   container metrics. Brownout CPU throttling is additionally visible in the
   standard `container_cpu_cfs_*` metrics.

The integration suite includes a **detectability gate**: a scripted agent
must be able to call `get_power_status()` and correctly answer `"Yes"`
during a live fault — proving a real agent can solve these problems.

## 9. Testing strategy

### Unit tests (`tests/generators/`, no cluster required)

| File | Covers |
|------|--------|
| `test_power_state_machine.py` | UPS transitions, countdown math, depletion callback (exactly once), ATS transfer, restore, journal ordering, publish throttling — with a fake clock |
| `test_power_injector.py` | Base-class name dispatch, outage stop/start, brownout quota math + exact restore, load-shedding schedule generation (non-overlap, rotation) and threaded execution, cutover blip, battery-drain two-phase with hard-off at deadline, idempotent recovery — with fake Docker/Controller |
| `test_power_problems.py` | All 20 registry IDs resolve; per-task eval logic (detection, node localization exact/subset, analysis enum, mitigation success) with fake cluster objects |
| `test_quantitative_eval.py` | Gap-fill: the pure evaluator helpers (`is_exact_match`, `is_subset`, …) had no tests |

### Integration tests (`tests/integration/test_power_faults.py`, live kind cluster)

1. **Injector-level per fault**: inject → assert measurable trace
   (container exited / quota set / UPS ConfigMap shows `ON_BATTERY`) →
   recover → assert cluster fully restored (container running, node Ready).
2. **Two-phase battery drain**: assert `ON_BATTERY` countdown decreases
   between reads, then node dies at the deadline.
3. **Load shedding rotation**: assert both workers cycle through off/on.
4. **End-to-end `DummyAgent`**: full pipeline
   (deploy → inject → workload → submit → eval → recover → cleanup) per
   problem ID; subset runs by default, `AIOPSLAB_POWER_E2E_ALL=1` runs all 20.
5. **Detectability gate**: a scripted `PowerStatusAgent` reads
   `get_power_status()` and answers correctly.

Tests skip automatically when no cluster/Docker is available.

### CI

`.github/workflows/integration-test.yml` gained a `power-faults` job:
3-worker kind cluster → OpenEBS + Prometheus preinstall → unit tests →
power integration suite (with failure diagnostics like the smoke job).

## 10. Cluster requirements

- **Outage / brownout / cutover / UPS-drain**: run on the stock 1-worker
  `kind/kind-config-x86.yaml` (stopping the only worker = total app outage,
  which is the point — mitigation must restore power).
- **Load shedding** (and any test observing pod rescheduling): use
  `kind/kind-config-power.yaml` — control-plane + **3 workers**:

```bash
kind create cluster --config kind/kind-config-power.yaml --wait 120s
```

- The exporter and all power problems work on any cluster shape; node
  selection prefers workers (`node-role.kubernetes.io/control-plane` is
  excluded).
- Works from Windows hosts with Docker Desktop (docker-py, no `sudo`).

## 11. Testing it (three layers)

```bash
# Layer 1 — Unit tests (no cluster needed, ~15 s)
python -m pytest tests/generators -v

# Layer 2 — Integration tests (require kind + Docker; tests skip without them)
kind create cluster --config kind/kind-config-power.yaml --wait 120s
python -m pytest tests/integration/test_power_faults.py -v -s -m integration

# Layer 3 — End-to-end agent evaluation (see §12)
python clients/power_eval.py --agent dummy --suite power-smoke
python clients/power_eval.py --agent generic --suite power

# Manual/human-agent testing of a single problem
python cli.py
(aiopslab) $ start power_ups_drain_hotel_res-detection-1
```

Layer guide:

| Layer | What it proves | Runtime |
|---|---|---|
| Unit (`tests/generators/`) | State machine, dispatch, schedule math, eval logic — with fakes | seconds |
| Integration (`tests/integration/test_power_faults.py`) | Faults leave a **measurable cluster trace** and recover cleanly; e2e lifecycle works per problem ID; the **detectability gate** (scripted agent reads `get_power_status()` and answers correctly) | ~5-10 min (subset), ~30-45 min (`AIOPSLAB_POWER_E2E_ALL=1`) |
| Agent evaluation (`clients/power_eval.py`) | Real detection/localization/analysis/mitigation **metrics** for an LLM agent | ~10-25 min per problem |

Tuning knobs are all environment variables (see the table in §5), so CI can
keep ride-through windows short.

## 12. Evaluation metrics

### What is measured

| Task type | Correctness metric | Timing metric | Notes |
|---|---|---|---|
| `power_*-detection-1` | `Detection Accuracy` = Correct/Incorrect/Invalid Format | `TTD` (time to detect) | answer contract: `submit("Yes"/"No")` |
| `power_*-localization-1` | `Localization Accuracy` (100 = exact node set; superset = `len(expected)/len(pred)`), `success`, `is_subset` | `TTL` | answer contract: `submit(["kind-worker", ...])` |
| `power_*-analysis-1` | `system_level_correct`, `fault_type_correct`, `success` | `TTA` | expected: `{"system_level": "Hardware", "fault_type": "Power Issue"}` |
| `power_*-mitigation-1` | `success` = nodes Ready + pods healthy + UPS ONLINE | `TTM` | UPS-drain mitigation adds **`proactive`**: True if the agent restored power *before* battery depletion (the node never died) |
| all | `steps`, `in_tokens`, `out_tokens` | — | cost/efficiency; `framework_overhead` = deploy+inject+recover time |

### Where the metrics land

1. **Console**: each `eval()` prints the scores; the orchestrator prints a
   final result block.
2. **Session JSON** (always): `aiopslab/data/results/<session_id>_<start>.json`
   — full agent-environment trace + the results dict.
3. **Batch CSV + aggregate summary** (via `clients/power_eval.py`):
   `aiopslab/data/results/power_eval_<agent>_<timestamp>.csv` with one row
   per problem (accuracy, success, time metric, steps, tokens, wall time),
   plus a per-task-type aggregate table (accuracy/success rates, mean TTD/
   TTL/TTA/TTM, proactive rate, token totals).
4. **W&B** (optional): `USE_WANDB=true` in `.env`.
5. **LLM-as-judge** (optional): `qualitative_eval: true` in
   `aiopslab/config.yml` adds `reasoning_score`/`reasoning_judgement`.

### Batch evaluation

`clients/power_eval.py` runs any registered agent over the power problems
and aggregates the metrics:

```bash
# Zero-cost validation of the whole eval loop (no API keys, no LLM):
python clients/power_eval.py --agent dummy

# Any registered agent over all 20 power problems:
python clients/power_eval.py --agent generic --suite power --max-steps 20

# Scoped runs:
python clients/power_eval.py --agent generic --faults ups_drain outage
python clients/power_eval.py --agent generic --task-types detection mitigation
python clients/power_eval.py --agent generic --output my_results.csv
```

The CSV is appended after every problem, so interrupted batches keep
partial results. Every run also persists its session JSON (trace + metrics)
to `aiopslab/data/results/`.

### Service API

The same benchmark problems can be run through the FastAPI service
(`python service.py`) with any agent registered in `clients/registry.py`:

```bash
curl -X POST http://localhost:1818/simulate \
  -H "Content-Type: application/json" \
  -d '{"problem_id": "power_ups_drain_hotel_res-mitigation-1",
       "agent_name": "generic", "max_steps": 30}'
# -> SimulationResponse with trace + results
```

Notes:

- The `power_node` mitigation action is wired into the **proactive**
  scoring: `power_node(node, "on")` during the ride-through window keeps
  `proactive=True`; `docker start` via `exec_shell` also works but is only
  visible as reactive recovery once the node died.
- Optionally point the `LLMJudge` (qualitative eval) at the same model
  endpoint for reasoning-quality scores.
- Longer term: an LLM **fault-generation** pipeline — the agent proposes new
  fault specs as structured JSON against the injector API surface; a
  conformance gate (unit dispatch tests + integration dry-run) accepts or
  rejects them before registration.

## 13. Future fault backlog

Gaps identified during the design of this family (candidates for the next
batch, several GLM-generatable):

- **Disk faults**: re-enable `disk_woreout` (BPF `err_inject` path or Chaos
  Mesh `IOChaos` latency/errors).
- **Unused Chaos Mesh capabilities**: `StressChaos` (CPU/mem burn),
  `TimeChaos` (clock skew), `DNSChaos`, `HTTPChaos` (5xx), `IOChaos`.
- **Operator faults**: the six TiDB problems currently commented out of the
  registry.
- **New classes**: certificate/TLS expiry, external dependency outage,
  disk-full eviction, memory leak, connection-pool exhaustion, broker
  backlog, config drift after Helm upgrade, failed rollout.
- **Compound faults**: power loss → dirty shutdown → filesystem corruption
  on the stateful pod (composes `power_outage` + `disk_woreout`).
