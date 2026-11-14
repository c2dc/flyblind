# Analysis

Read-only over `02_results/campaign/`; every output goes to `out/`. Numbers are never typed by hand:
the tables are generated from the per-run artifacts, with the harness's own KPI, phase-window and
belief functions (`kpi_leg`, `phase_windows`, `freshness_metrics`), so the analysis uses exactly the
same methodology as the per-run reports.

```bash
python3 run_phase4.py      # runs every step below, in dependency order
```

**Start here if you want to understand the metrics.** `formulas_walkthrough.py` takes one baseline run
and one attack run and computes every formula and predicate of the paper term by term — delivery age,
belief vs truth, `F_GCS`, `A_fresh`, `H_net`, `H_veh`, `SSS_i`, the C1 criterion, the queueing model
`AoI = (Q/C)(N+1)` and the destination-side detector — printing the definition, where it lives in the
code, which file it reads, concrete example deliveries, and a cross-check against the pipeline's own
value. It closes with the traps (belief-derived "tracking", whole-run vs attack-window belief, the two
`t_rel` origins) and the detector's two limitations, both reproduced on the data.

```bash
python3 formulas_walkthrough.py                                  # first a_baseline vs first a_atk_n4
python3 formulas_walkthrough.py --attack <run_dir_of_a_defence>  # any pair of runs; tau_trk via --tau-trk
```

It needs `out/campaign_manifest.json` (and uses `out/slack.json` for one cross-check), so run it after
`run_phase4.py`. Output: `out/formulas_walkthrough.txt` and `out/formulas_walkthrough.json`.

| Script | Output | Content |
|---|---|---|
| `build_manifest.py` | `campaign_manifest.json/.csv` | Runs that enter the analysis, derived from the runner's batch logs; capacity probes and the positive control are categorised separately (their `VALID=NO` is expected), excluded runs carry the failed gates as the reason; host-load flags |
| `legs_table.py` | `table_4_1_legs*` | Same KPIs on both legs (OWD p50/p95/p99/p99.9/max, IPDV p99, loss, n) per condition and phase, for real C2 traffic and for the per-leg probes |
| `criteria_table.py` | `table_4_2_*` | C1 and C4 per leg and phase with the measured values, plus K7 (FS_GCS) and K8 (QGC-style gaps) |
| `belief_by_phase.py` | `belief_by_phase*` | Belief error and telemetry AoI per phase; the ALL-phase belief is checked against the pipeline value |
| `dl_ul_by_phase.py` | `dl_ul_by_phase*` | Per-phase uplink/downlink rate per role from the gNB MAC byte counters: where the contended resource actually goes |
| `frontiers.py` | `frontiers*` | Video, dose and bandwidth frontiers (ATTACK window) with the mechanism columns (slice-A UL share, victim/rogue MAC UL, video goodput/loss); x values read from each run's metadata |
| `slack_recovery.py` | `slack*`, `drain_recovery*` | Victim UL capacity bracket and slack from the no-attack video ladder, **per cell bandwidth**, plus the proportional-fair share at N=4; DRAIN recovery with the harness criterion (`drain_policy`) |
| `mitigation_table.py` | `mitigation*` | The two defences (uplink slice enforcement, host AQM) against baseline and the undefended attack: freshness, per-leg OWD, delivery, failsafe and operator alerts |
| `physics_truth.py` | `physics_truth*` | Physical tracking from the SITL truth, decomposed: the start-up transient (hover point to route, ~9 s, ~1.6 % of samples, which is what sets the `trk_truth` p99/max), the along-track lag behind the moving reference (~2 s) and the cross-track distance from the path; whole-stream values checked against `trk_truth.json` |
| `formulas_walkthrough.py` | `formulas_walkthrough.txt/.json` | Every formula and predicate computed step by step on one baseline and one attack run, with examples, code locations and pipeline cross-checks (see above) |
| `positive_control.py` | `positive_control*` | The interruption control: the command leg is cut on purpose and the same monitors trip, with the failsafe latency resolved against the measured cut window |

`common.py` holds the condition labels (`CONDITIONS`, keyed by the ids emitted by
`01_scripts/04_campaign/campaign.sh`) and the pooling rule. Each table is written as per-run CSV,
aggregated CSV/JSON (mean, sd, min, max, n) and, where the paper uses it, a LaTeX fragment.

## The bulky captures ship gzipped

`run_kpi_report.json`, `mac-prb-raw.log`, `setpoints.csv` and the per-leg probe records
(`sidecars/*/rx_*.jsonl`, `tx_*.jsonl`) are released as `.gz` — 1.61 GB raw against 202 MB
compressed. Nothing needs to be unpacked: every reader here goes through `common.open_maybe_gz`,
which takes the plain file if it is there and the `.gz` beside it otherwise, so the analysis is
identical either way. Use `common.result_exists` rather than `Path.is_file` when gating on a result
file, or a gzipped run will look empty.

Paths recorded inside the results are the placeholder `ARTIFACT_ROOT/...`;
`common.resolve_run_dir` maps them onto wherever the repository is checked out. Anything written into
`out/` must go through `common.rel_to_artifact`, so a published file never carries the absolute path
of the machine that produced it.

## Two things that are easy to get wrong

**Two `t_rel` origins.** `flyblind_phase_windows.json`, `event_markers.csv` and the injection record
count from `T0`; the MAVLink captures (`statustext_gcs.csv`, `c2.json`) count from the mission start,
about 90 s later. Comparing the two directly makes a failsafe appear to precede its own cause. Phases
are therefore assigned from `t_mono_ns` against `T0_mono_ns`, and `positive_control.py` measures the
offset from a capture that carries both columns before comparing anything.

**Whole-run belief understates the effect.** The per-run pipeline reports belief over the whole
measurement window, which includes the pre-attack segment where the state is fresh. The ATTACK-window
value from `belief_by_phase.py` is the one that describes the attack; the whole-run value is the
conservative one. Both are emitted, and the whole-run value is validated against the pipeline's own.

## Conditions that are expected to be invalid

Two groups are read by their own evidence, never by the `VALID` verdict, and `build_manifest.py` gives
them their own category:

- `cap_*` / `cap51_*` — capacity probes. No adversary; the victim's own video is raised until it
  self-congests, so failing the availability gate *is* the measurement. Read by goodput.
- `pos_*` — the positive control. The failsafe is tripped on purpose, which fails the baseline mission
  gates by construction. Read by `positive_control.json`.
