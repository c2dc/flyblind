# Per-run report (plan item 1.13)

run_dir: `ARTIFACT_ROOT/02_results/campaign/flyblind_20261111_232155/S1_core/runs/20261112_012557_045177_attack_cross_prb106_B0_setpoint-stream_traj-route_sp10p0hz_seed1_udp_multi-rogue_n4_ph25ms_vid12p0mbps_r1`
run_meta.valid: True

## Command leg x return leg table

| metric | command | return |
|---|---:|---:|
| phase | ATTACK | ATTACK |
| n | 5098 | 1208 |
| owd p50 (s) | 0.020164786 | 13.09213092 |
| owd p99 (s) | 0.039762676999999996 | 13.779610222 |
| owd max (s) | 0.053280695 | 13.88911595 |

## Criteria

### C1_command
```json
{
  "criterion": "C1",
  "definition": "effect present if owd p50 >= 1.0 s OR owd p99 >= 150 ms; effect only in the tail if p99 >= 150 ms and p50 < 100 ms",
  "owd_p50_s": 0.020164786,
  "owd_p99_s": 0.039762676999999996,
  "effect_present": false,
  "effect_tail_only": false,
  "belief_p50_m": null,
  "belief_note": "belief missing in run_dir; primary C1 (OWD) still holds; belief confirmation = NOT VERIFIED",
  "pass_effect": false,
  "percentiles_insufficient": false,
  "belief_source": null,
  "belief_confirmation_ge_5m": null
}
```

### C1_return
```json
{
  "criterion": "C1",
  "definition": "effect present if owd p50 >= 1.0 s OR owd p99 >= 150 ms; effect only in the tail if p99 >= 150 ms and p50 < 100 ms",
  "owd_p50_s": 13.09213092,
  "owd_p99_s": 13.779610222,
  "effect_present": true,
  "effect_tail_only": false,
  "belief_p50_m": null,
  "belief_note": "belief missing in run_dir; primary C1 (OWD) still holds; belief confirmation = NOT VERIFIED",
  "pass_effect": true,
  "percentiles_insufficient": false,
  "belief_source": null,
  "belief_confirmation_ge_5m": null
}
```

### C2
```json
{
  "criterion": "C2",
  "pass": null,
  "note": "C2 compares instrumentation on vs off (plan item 2.2). A single-run report does not evaluate C2; use compare_ni_runs.",
  "run_dir": "ARTIFACT_ROOT/02_results/campaign/flyblind_20261111_232155/S1_core/runs/20261112_012557_045177_attack_cross_prb106_B0_setpoint-stream_traj-route_sp10p0hz_seed1_udp_multi-rogue_n4_ph25ms_vid12p0mbps_r1"
}
```

### C3
```json
{
  "criterion": "C3",
  "pass": null,
  "note": "paper_comparable not found in this run_dir (NOT VERIFIED)",
  "source": null
}
```

### C4_command
```json
{
  "criterion": "C4",
  "scope": "kpi_owd_plus_vivacity",
  "checks": {
    "owd_le_1s_frac_ge_95pct": {
      "pass": true,
      "fraccao": 1.0,
      "source": "Honeywell TestReport-263 citing DO-377A"
    },
    "reliability_le_40ms_ge_99_9pct": {
      "pass": false,
      "reliability": 0.9914185639229423,
      "target": 0.999,
      "source": "TS 22.125 stick steering"
    },
    "reliability_le_1s_ge_99_9pct": {
      "pass": true,
      "reliability": 0.9996497373029772,
      "target": 0.999,
      "source": "TS 22.125 (1 s budget / waypoints class)"
    },
    "reliability_video_le_140ms_ge_99_99pct": {
      "pass": null,
      "reliability": null,
      "target": null,
      "role": "informative_or_not_applicable",
      "source": "TS 22.125 Tab. 7.2-2 video Non-VLOS"
    },
    "no_gap_gt_3s": {
      "pass": true,
      "gap_count": 0,
      "source": "AURA / TET 3 s (secondary citation)"
    },
    "no_gap_gt_5s": {
      "pass": true,
      "gap_count": 0,
      "source": "Honeywell / TET cruise 5 s (secondary citation)"
    },
    "FS_GCS": {
      "pass": true,
      "triggered": false,
      "max_gap_command_s": null,
      "source": "K7 link_vivacity",
      "role": "pass_fail"
    },
    "QGC_style_gap_3_5s": {
      "pass": true,
      "max_gap_s": 0.717906,
      "loss_declarations": 0,
      "pct_time_gap_gt_3_5s": 0.0,
      "source": "K8 link_vivacity (QGC VehicleLinkManager style)",
      "role": "informative_on_command_leg"
    }
  },
  "pass_aggregate_partial": false
}
```

### C4_return
```json
{
  "criterion": "C4",
  "scope": "kpi_owd_plus_vivacity",
  "checks": {
    "owd_le_1s_frac_ge_95pct": {
      "pass": false,
      "fraccao": 0.0041390728476821195,
      "source": "Honeywell TestReport-263 citing DO-377A"
    },
    "reliability_le_40ms_ge_99_9pct": {
      "pass": false,
      "reliability": 0.15691318327974277,
      "target": 0.999,
      "source": "TS 22.125 stick steering"
    },
    "reliability_le_1s_ge_99_9pct": {
      "pass": false,
      "reliability": 0.1787781350482315,
      "target": 0.999,
      "source": "TS 22.125 (1 s budget / waypoints class)"
    },
    "reliability_video_le_140ms_ge_99_99pct": {
      "pass": false,
      "reliability": 0.1729903536977492,
      "target": 0.9999,
      "role": "pass_fail",
      "source": "TS 22.125 Tab. 7.2-2 video Non-VLOS"
    },
    "no_gap_gt_3s": {
      "pass": true,
      "gap_count": 0,
      "source": "AURA / TET 3 s (secondary citation)"
    },
    "no_gap_gt_5s": {
      "pass": true,
      "gap_count": 0,
      "source": "Honeywell / TET cruise 5 s (secondary citation)"
    },
    "FS_GCS": {
      "pass": true,
      "triggered": false,
      "max_gap_command_s": null,
      "source": "K7 link_vivacity",
      "role": "informative_on_return_leg"
    },
    "QGC_style_gap_3_5s": {
      "pass": true,
      "max_gap_s": 0.717906,
      "loss_declarations": 0,
      "pct_time_gap_gt_3_5s": 0.0,
      "source": "K8 link_vivacity (QGC VehicleLinkManager style)",
      "role": "pass_fail"
    }
  },
  "pass_aggregate_partial": false
}
```

## Belief (C1 confirmation)
- belief_p50_m: None
- source: None

