```text
Artifact: IEEE S&P 2027 supplementary material
Paper:    FlyBlind: Silent State Staleness in UAS Command and Control over 5G
Author:   (anonymous; double-blind submission)
Inst.:    withheld (double-blind review)
Date:     2026-11
License:  MIT (see LICENSE)
```

This package contains the **FlyBlind** experimental stack and the complete campaign behind the
paper: a Kind-hosted OpenAirInterface (OAI) 5G SA lab with ORANSlice slice-aware scheduling,
ArduPilot SITL flying a real route over UDP MAVLink on the PDU path, a QGroundControl operator view
(stock plus a detector build), realistic companion video, a four-layer measurement harness (network,
MAC/PRB, C2, physics), the unattended campaign driver, the **65 recorded runs**, and the analysis that
turns them into every table in the paper.

Two things a reviewer can do without a testbed:

1. **Re-derive every number.** `03_analysis/run_phase4.py` regenerates all 37 output files from the
   recorded runs, including the per-sample recomputation of the paper's central metric. Section 7.
2. **Follow every formula.** `03_analysis/formulas_walkthrough.py` computes each definition and
   predicate of the paper term by term on one baseline and one attack run, printing where each lives
   in the code and cross-checking against the pipeline's own value. Section 7.1.

Reproducing the measurements themselves additionally needs Docker and ~50 GB of disk. Sections 4-6.

---

## 1. What FlyBlind demonstrates

A UAV joins the 5G network as an **ordinary eMBB subscriber** (5QI 9). A few **authorized co-tenant
subscribers** in the **same slice** generate legitimate uplink traffic. The reference uplink scheduler
is slice- and QoS-agnostic proportional-fair, so every backlogged UE gets a share `C/(N+1)`. The UAV's
telemetry leg is the *fat* flow, so it is the one that is starved: its state **ages** at the Ground
Control Station (GCS) while

- the command leg stays responsive (it is thin, so never backlogged),
- the MAVLink heartbeat stays alive, so the native GCS failsafe never fires,
- every link-health KPI reads green.

The result is a **false-healthy** condition: link health looks fine while the *state* the operator
acts on is seconds old. The attack needs no rogue base station, no jamming and no protocol
manipulation, and it never touches the victim's C2 flow.

**Headline numbers** (N = 4 co-tenants, 106 PRB, 12 Mb/s video, route; medians over runs):

| Metric | Baseline | Attack | source |
|---|---|---|---|
| Telemetry Age-of-Information, p50 | **18.5 ms** | **13.01 s** (12.97–13.07) | `out/belief_by_phase.csv` |
| GCS belief vs. ground truth, p50 — whole flight | 1.0 m | **39.3 m** (37.7–41.0) | `out/belief_by_phase.csv` (`ALL`) |
| GCS belief vs. ground truth, p50 — while the state is stale | — | **47.8 m** (47.5–48.1) | `out/belief_by_phase.csv` (`ATTACK`) |
| Command-leg one-way delay, p50 / p99 | 20.1 / 38.7 ms | **20.3 / 40.0 ms** | `out/table_4_1_legs.csv` |
| Telemetry delivery ratio, UAV→GCS | 0.84 | 0.56 | `telemetry_downlink_summary.json` |
| GCS failsafe triggered | no | **no (0 of 26)** | `out/positive_control.json` |
| Operator alerts during the attack | — | **0 of 26 runs** | `out/positive_control.json` |

Means over the five replicates of the headline condition, with the observed range in brackets. The
belief error is reported twice on purpose: over the **whole flight** it is diluted by the pre-attack
segment, where the state is fresh; restricted to the interval in which telemetry actually arrives
stale it is 47.8 m. The two must not be mixed in one table without saying which window is meant.

The physical flight is **unaffected**: tracking error against the plan from SITL ground truth is
p50 7.9 m / p95 9.6 m in baseline *and* under attack (`out/physics_truth.csv`). The damage is
entirely perceptual, which is what makes the condition silent.

**Second contribution:** a **destination-side state-freshness detector**, shipped as a custom
QGroundControl build, that recovers the Age-of-Information from the telemetry timestamp alone — no
ground truth — and raises a central *"STALE STATE"* alarm. See `01_scripts/03_sitl/qgc_detector/`.

**Defences measured** (`out/mitigation.csv`): uplink slice enforcement restores baseline freshness
exactly, with no PRB reservation; host AQM (fq_codel on the victim's uplink) changes nothing. The
queue that ages the state is in the radio scheduler, not in the host.

---

## 2. Directory layout

```
.
├── 01_scripts/
│   ├── 01_testbeds/
│   │   ├── start_testbed.sh              DEFAULT testbed: the attack scenario
│   │   ├── start_testbed_mitigation.sh   defences: --slicing | --aqm
│   │   ├── stop_testbed.sh               universal teardown, from any partial state
│   │   ├── build/                        one-off image builders (clone ORANSlice, patch, docker build)
│   │   ├── docker/                       Dockerfiles for the OAI gNB / nr-UE images
│   │   ├── patches/                      ul_rrmpolicy_mitigation.patch (slice-aware UL scheduler)
│   │   └── reference/                    pinned OAI/ORANSlice config + rrmPolicy template
│   ├── 02_experiments/
│   │   ├── orchestrate.py                run driver: one run, all four layers, campaign defaults baked in
│   │   ├── lib/                          KPI definitions, results layout, terminal report
│   │   ├── probes/                       MAC/PRB, OWD, C2 arrival, UPF telemetry
│   │   ├── instrumentation/              run manifest, phase windows, sidecars, batch wrapper
│   │   └── tests/                        171 unit tests
│   ├── 03_sitl/
│   │   ├── ardupilot-sitl-runtime.tar.xz pre-built ArduCopter SITL runtime (1.6 MB)
│   │   ├── install_ardupilot.sh           extracts it (or clone+build fallback)
│   │   ├── lib/                           MAVLink proxy, mission runner, trajectory, video, QGC mirror
│   │   ├── bin/                           where the QGC AppImages go — see bin/README.md
│   │   └── qgc_detector/                  SOURCE of the detector build — see its README.md
│   └── 04_campaign/
│       └── campaign.sh                   unattended driver for the whole campaign (65 runs, 5 series)
├── 02_results/campaign/                  the 65 recorded runs (see section 7.2 on what is included)
├── 03_analysis/                          read-only analysis; out/ holds the 37 generated outputs
├── 04_images/                            every paper figure: scripts/, pdfs/, pngs/, icons/
├── 05_videos/                            the operator's screen, one recording per condition (+ .srt captions)
├── .gitignore
├── install_dependencies.sh
├── LICENSE
└── README.md
```

---

## 3. Requirements

### Reference machine

Every number reported in the paper was produced on this host. It is not a minimum, it is the
configuration the measurements describe:

| | |
|---|---|
| CPU | Intel Core Ultra 7 265KF — **16 cores / 16 threads** (no SMT) |
| RAM | 48 GB, no swap in use |
| Disk | 183 GB SSD |
| OS | Linux Mint 22.3 (Ubuntu 24.04 base), kernel 7.0 |
| Docker | 29.6.1 |
| kind / kubectl | 0.20.0 / v1.36.2 |
| Python | 3.12.3 |

### Minimums, and why

| | |
|---|---|
| **Analysis only** (sections 7, 7.1) | Python **3.11+** and nothing else. Any laptop. Runs in a few minutes, no Docker, no root. |
| **CPU for experiments** | **8 cores** usable, **16 recommended**. The whole stack runs on one host: 5G core, gNB, six UEs, ArduPilot SITL, the four probes, the sidecars and QGroundControl. |
| **RAM** | **16 GB** minimum, **32 GB+** comfortable. 48 GB was never a constraint here. |
| **Disk** | **50 GB free** for a full campaign (budget below). `campaign.sh` refuses to start a series with less than `MIN_FREE_GB` (default 10) available. |
| **OS** | A Debian-derived Linux, amd64 or arm64. Root is needed once, for `install_dependencies.sh`. |
| **Graphical session** | Only for the operator view. Headless hosts reproduce **every number**; QGroundControl is skipped. |
| **Time** | **30-90 min** of one-off image building (section 4.2), then ~9 min per testbed bring-up and ~16 min per run. The full 65-run campaign took **~18 h** unattended. |

### Disk budget (measured)

| item | size |
|---|---|
| This repository | 342 MB (256 MB code, results and figures; 86 MB screen recordings) |
| OAI/ORANSlice images after the build (gNB 470 MB + nr-UE 390 MB) | 860 MB |
| `kindest/node` base image | 1.4 GB |
| Build intermediates and Docker layer cache | several GB; `docker system prune` reclaims them afterwards |
| A full 65-run campaign, raw (~43 MB per run) | 2.8 GB |

### A note on core count

The per-run host-quality gate is **relative to the machine**: the limit is
`max(nproc − 2, floor)` on the 1-minute load average — on 16 cores that is 14. A run taken while the
host was busier than that is scored invalid, so the measurement is never silently degraded by
contention from other work. The consequence on a smaller machine is not a wrong number, it is more
runs rejected: with fewer cores the stack itself sits closer to the limit. Three runs of the reported
campaign were rejected this way and re-run (see `out/campaign_manifest.csv`).

Do not run anything heavy on the host during a campaign — including analysis of earlier runs.

## 4. One-time setup

### 4.1 Host dependencies

```bash
sudo bash install_dependencies.sh      # Docker, kubectl, kind, SCTP, pymavlink, host tools
newgrp docker                          # or re-login, so your user can talk to Docker
```

### 4.2 Build the OAI/ORANSlice images — **the one long step: 30-90 min, once**

> ⚠️ **If you skip this section, the first `./start_testbed.sh` will do it for you** — and that
> bring-up will take **30 to 90 minutes** instead of the usual ~9, with no prompt. The build is
> one-off: the images are cached in Docker and loaded into the Kind cluster, so every later bring-up
> is fast. Run it deliberately, or at least know why the first start is slow.

The builder:

1. clones ORANSlice from upstream (`github.com/wineslab/ORANSlice`, shallow) into `vendor/`;
2. applies ORANSlice's own `doc/rrmPolicyJson.patch`;
3. applies **our** `patches/ul_rrmpolicy_mitigation.patch` — the slice-aware uplink scheduler;
4. compiles OpenAirInterface from source into the `oai-gnb` and `oai-nr-ue` images.

Step 4 is what takes the time; it is a full OAI build, so the range depends on the machine and on how
much Docker layer cache it can reuse.

```bash
cd 01_scripts/01_testbeds
./build/build_oranslice_images_mitigation.sh      # builds oai-gnb / oai-nr-ue, once

# already built? verify in one second:
docker image ls 'localhost/oai-*-oranslice-mitigation'
```

To make a missing image a **fast failure** instead of an unannounced one-hour build — which is what
the campaign driver does, so it never starts a build unattended:

```bash
ORANSLICE_SKIP_BUILD=1 ./start_testbed.sh         # dies immediately if an image is absent
```

The gNB image is checked at bring-up for the uplink patch, so a stale image is reported rather than
silently used (`gNB image lacks FlyBlind UL rrmPolicy scheduler patch`).

> The ArduPilot SITL runtime ships pre-built and is extracted automatically on first bring-up; no
> ArduPilot build is required.

### 4.3 The QGroundControl views (optional)

Neither AppImage is in this repository — each is 187 MB, above the per-file limit of common Git
hosts. `01_scripts/03_sitl/bin/README.md` gives the download for the stock view and
`01_scripts/03_sitl/qgc_detector/README.md` the build for the detector. Runs work without them.

---

## 5. Testbeds

All stacks share one Kind cluster and the same images; they differ in slice provisioning and in which
defence is armed. **Switching between them requires a clean restart.**

A bring-up takes about **9 minutes** once the images exist — but the **first** one builds them if they
are missing, and then takes 30-90 minutes (section 4.2). Prefix with `ORANSLICE_SKIP_BUILD=1` to turn
that into an immediate error instead.

```bash
cd 01_scripts/01_testbeds

# DEFAULT: the attack scenario (reproduces 13.0 s AoI / 40 m belief)
./start_testbed.sh
#   victim (GCS+UAV) = ordinary eMBB subscriber (5QI 9), co-located in slice A with 4 co-tenants;
#   slice B provisioned but idle; the UL slice-enforcement patch is ARMED but inert, because with a
#   single slice there is no boundary to enforce.

# DEFENCES: pick one
./start_testbed_mitigation.sh --slicing   # victim on its OWN slice, patch ON, no PRB floor
./start_testbed_mitigation.sh --aqm       # fq_codel on the UAV uplink, applied at runtime

# TEARDOWN: works for any of the above, from any partial state
./stop_testbed.sh
```

Every bring-up also starts the **ACK probe relay** on `UDP:5763 -> TCP:5762` (default
`ACK_PROBE_SOCAT=1`). It is a separate path from the mission C2 on `:5760` and is required by the FULL
sidecar profile; the bring-up logs `[SITL] OK — ACK probe relay ...` when it is up.

Why `--slicing` neutralises the attack **with no PRB floor**: with the victim in its own slice, the
thin-first slice-aware scheduler serves the victim's low-demand slice first, so its buffer never
grows. The protection comes from the scheduler *being slice-aware*, not from a reservation.

---

## 6. Running experiments

### 6.1 A single run

The orchestrator runs against a testbed that is already up. Campaign-invariant parameters are **baked
in as defaults** (106 PRB, cross topology, 600 s, route at 4 m/s, 12 Mb/s AVIATOR-shaped video, no PRB
floor, FULL sidecars), so a run states only what varies.

```bash
cd 01_scripts/02_experiments

# baseline
python3 orchestrate.py --condition baseline --open-qgc \
    --results-tag BASELINE --out ../../02_results/smoke

# attack: 4 co-tenants, starting 45 s in
python3 orchestrate.py --condition attack --attack multi-rogue --attack-start-after-s 45 --open-qgc \
    --results-tag ATTACK --out ../../02_results/smoke

# attack seen through the state-freshness DETECTOR
python3 orchestrate.py --condition attack --attack multi-rogue --attack-start-after-s 45 \
    --open-qgc-mitigation --results-tag ATTACK_DETECTOR --out ../../02_results/smoke
```

- `--open-qgc` launches the **stock** view: it shows the vehicle position and **no** staleness alarm.
  That silence is the attack.
- `--open-qgc-mitigation` launches the **detector** build, which raises the central
  *"STALE STATE — C2 TIMELINESS"* overlay once the telemetry age crosses 1 s. The two views
  side by side are the second contribution.
- Every run records the **FULL sidecar profile** by default: per-leg one-way delay (command
  `GCS->UAV` on UDP 48002, return `UAV->GCS` on UDP 48003), host load, `COMMAND_ACK` RTT, RLC
  rejects and UE interface counters. `--sidecars MINIMAL` or `off` reduces this.

### 6.2 The whole campaign

```bash
cd 01_scripts/04_campaign
./campaign.sh --list     # print the plan
./campaign.sh            # DRY RUN: validate the plan, touch nothing
./campaign.sh --go       # execute: ~18 h, asks for the sudo password once
```

65 runs over 5 series, each series getting a fresh, verified bring-up:

| series | runs | testbed | what it establishes |
|---|---|---|---|
| `S1_core` | 48 | `start_testbed.sh` | dose-response N=0..4, video onset 4/6/8, video plateau 16/24, no-video control, capacity probe, drain |
| `S3_prb51` | 10 | `GNB_PRB=51` | the effect at 20 MHz + a two-sided capacity probe |
| `S4_slicing` | 3 | `--slicing` | the defence that works |
| `S5_aqm` | 3 | `--aqm` | the defence that does not |
| `S6_poscontrol` | 1 | `start_testbed.sh` | positive control: the command leg is cut on purpose, so the native failsafe must fire |

`campaign.sh` never touches the testbed without `--go`, retries a failed bring-up once, skips a series
it cannot verify rather than aborting the campaign, and tears the stack down at the end.

### 6.3 The positive control

A negative result about a monitor only means something if the monitor can be made to fire. `S6`
installs a blackhole route in the GCS pod toward the UAV's PDU address, cutting the **command** leg
for longer than `FS_GCS_TIMEOUT`, with no adversary present. Direction matters: `FS_GCS` watches
whether the UAV still *hears* the GCS, so only a cut on that leg can arm it — cutting the return leg
would age the operator's state while leaving the failsafe disarmed, which is exactly what the attack
does.

Recorded outcome (`02_results/.../X6_poscontrol/blackhole_injection.json`): the failsafe fired
**4.88 s** after the cut (`FS_GCS_TIMEOUT` is 5 s) and cleared 0.71 s after it was removed, with the
state *fresh* (AoI 18 ms) throughout. The same severity-4 channel is silent in all 26 attack runs.

---

## 7. Re-deriving the results

```bash
cd 03_analysis
python3 run_phase4.py          # regenerates every file in out/
```

Read-only over `02_results/`; nothing outside `out/` is written. The ten steps and their outputs are
listed in `03_analysis/README.md`. Several of them cross-check themselves against the recorded
pipeline values and report the agreement, for example:

```
[belief]  62 runs; ALL-phase belief p50 equals the pipeline value in 62/62 runs (tolerance 1e-06 m)
[physics] 53 route runs; whole-stream stats match trk_truth.json in 53/53
[4.6]     slack: 17 runs; recovery: 11 runs; PRE baseline equals belief_by_phase in 11/11 runs
```

The unit tests are part of the artifact:

```bash
cd 01_scripts/02_experiments/tests && python3 -m unittest discover -s . -p "test_*.py"
# Ran 171 tests ... OK
```

### 7.1 Following the formulas

```bash
cd 03_analysis
python3 formulas_walkthrough.py        # after run_phase4.py
```

Takes one baseline run and one attack run and computes, term by term: delivery age, belief vs. truth,
`F_GCS`, `A_fresh`, `H_net`, `H_veh`, the `SSS_i` predicate, the C1 criterion, the queueing model
`AoI = (Q/C)(N+1)` and the destination-side detector — printing the definition, the file it reads, the
line of code it lives in, worked example deliveries, and a comparison against the pipeline's own
value. It closes with the traps (belief-derived "tracking" metrics, whole-run vs. attack-window
belief, the two `t_rel` origins) and the detector's limitations, each reproduced on the data.

### 7.2 What is in `02_results/` and what is not

Each run directory holds the per-run summaries, the phase windows, the provenance manifest, the
human-readable report, and the raw captures the analysis consumes. The bulkiest captures ship
**gzipped** — 1.61 GB raw, 202 MB compressed (8.2×), which is the difference between a repository that
can be hosted and one that cannot:

```
run_kpi_report.json.gz      per-leg KPIs per phase        mac-prb-raw.log.gz    gNB MAC byte counters
setpoints.csv.gz            generated setpoints           sidecars/*/rx_*.jsonl.gz, tx_*.jsonl.gz
```

Everything reads through one helper (`common.open_maybe_gz`), so the analysis behaves identically
whether a run directory holds the plain file or the `.gz`. Nothing needs to be unpacked by hand.

Per-sample captures that no table depends on (`aoi_timeseries.csv`, `mavlink_rx_gcs.csv`,
`c2_arrival.csv`, `prb.csv` and similar, ~1.3 GB) are **not** included.

A run is `VALID` only if every mandatory gate passes (clock integrity, instrumentation integrity,
mission integrity, freshness correlatable, attack actually executed, host quality). Two families are
**expected** to be scored invalid and are read by their own evidence instead, each with its own
category in `out/campaign_manifest.json`:

- **capacity probes** (`cap_*`, `cap51_*`) — no adversary; the victim's own video is raised until it
  self-congests, so failing the availability gate *is* the measurement. Read by goodput.
- **the positive control** (`pos_*`) — the failsafe is tripped deliberately, which fails the baseline
  mission gates by construction. Read by `out/positive_control.json`.

The released manifest is `53 campaign + 11 capacity_probe + 1 positive_control`, with **0 excluded**.

---

## 8. The operator's screen (`05_videos/`)

Three screen recordings of the QGroundControl window, one per condition. They are the same evidence
as the tables, seen the way the operator sees it: nothing here is a reconstruction or an animation.

| file | condition | what it shows |
|---|---|---|
| `1_BASELINE.mp4` | no adversary | the reference flight. Telemetry age 18 ms, displayed position within 1.0 m of ground truth, every link KPI green. |
| `2_ATTACK.mp4` | four co-tenants, no defence | the attack starts mid-flight and **nothing on the screen changes**. Age reaches 13 s, the symbol sits ~48 m from the aircraft, and no alarm of any kind fires. |
| `3_ATTACK_WITH_DETECTOR.mp4` | same attack, detector build | identical network, identical autopilot. Only the ground station differs: the STALE STATE alarm raises about 2 s after the attack begins and shows the live age. |

Each video ships with a `.srt` subtitle track of the same name; most players load it automatically.
The captions carry the measured values, so the numbers on screen can be checked against
`03_analysis/out/`.

Watch them in order. The point of the second is what **does not** happen in it, and that only reads
as a result once the first has established what a healthy screen looks like.

**On the audio.** The recordings carry the real sound of the session, unedited. In `2_ATTACK.mp4`
that means silence: the native monitors emit no warning, which is the finding, not an omission. The
detector's alarm in `3_ATTACK_WITH_DETECTOR.mp4` is **visual only** — this build raises an on-screen
overlay and plays no sound.

Why the third video does not end the attack: the detector does not mitigate anything. The state is
just as stale (~51 m) and the aircraft is just as unaffected. What changes is that the operator can
see it. The defence that does remove the effect is uplink slice enforcement, measured in
`out/mitigation.csv`, not shown here.

---

## 9. Figures

```bash
cd 04_images/scripts
./build_all.sh                  # regenerates every PDF and PNG in ../pdfs and ../pngs
```

Each figure has exactly one generator (`04_images/README.md` maps them). The numbers come from
`03_analysis/out/`, so a figure cannot drift from the tables.

---

## 10. Troubleshooting

- **`kubectl`/cluster context errors after a crash** — `./stop_testbed.sh` clears every partial state
  (Kind clusters, leftover containers, host helpers, runtime directories); then start again.
- **`Missing mitigation image(s)`** — the images were never built, or were removed. Run
  `./build/build_oranslice_images_mitigation.sh` once (30-90 min, section 4.2). A bring-up without
  `ORANSLICE_SKIP_BUILD=1` would start that build itself, with no prompt.
- **`gNB image lacks FlyBlind UL rrmPolicy scheduler patch`** — the image predates the uplink patch.
  Rebuild it with the same script; the check exists so a stale image is never used silently.
- **The first bring-up seems stuck for an hour** — it is compiling OpenAirInterface. Look for
  `[00] Building ORANSlice images` in its output. Later bring-ups take ~9 min.
- **QGC window does not appear** — the AppImage needs FUSE (`libfuse2`) and a display, and must be
  present in `01_scripts/03_sitl/bin/`. On a headless host QGC is skipped and every metric still
  completes.
- **A baseline run fails the `host` gate** — the gate rejects a run taken while the machine was busy
  (load average above 14). Re-run it with nothing else on the host. Attack runs do not enforce it.
- **Mode shows `Custom:0xN` instead of `Guided/Land` in QGC** — cosmetic: the one-way telemetry mirror
  does not answer `AUTOPILOT_VERSION`, so QGC cannot name the firmware. No metric is affected.
- **Switching testbed or defence seems ignored** — a clean restart is required; `./stop_testbed.sh`
  first.

---

## 11. License and anonymity

Released under the MIT License (see `LICENSE`).

This artifact is prepared for double-blind review: it carries no author names, no institutional
affiliation, and no workstation-specific paths. Paths recorded inside the results are rewritten to
the placeholders `ARTIFACT_ROOT` and `EXTERNAL_SOURCES`, and the analysis resolves them against
wherever the repository is checked out, so the recorded provenance stays readable without disclosing
where it was produced. Image and PDF metadata has been stripped.

One maintenance note: the path rewriting above was applied once, to the recorded results. A run
executed later writes the absolute paths of *that* machine into its own `run_manifest.json`, so new
runs added to this repository would need the same pass. (The call for papers asks that artifact
repositories not be updated after the submission deadline, so this should not arise.)

Third-party components are used under their own licences: ORANSlice and OpenAirInterface (cloned by
the image builder), ArduPilot (the bundled SITL runtime, GPLv3) and QGroundControl (dual-licensed
Apache-2.0 / GPLv3; the detector is a custom overlay built through the mechanism upstream provides for
it — see `01_scripts/03_sitl/qgc_detector/README.md`).
