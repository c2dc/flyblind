# Per-run report (plan item 1.13)

run_dir: `ARTIFACT_ROOT/02_results/campaign/flyblind_20261111_232155/S1_core/runs/20261112_002300_179157_attack_cross_prb106_B0_setpoint-stream_traj-route_sp10p0hz_seed1_udp_multi-rogue_n4_ph25ms_vid12p0mbps_r1`
run_meta.valid: True

## Command leg x return leg table

| metric | command | return |
|---|---:|---:|
| phase | ATTACK | ATTACK |
| n | 5098 | 1263 |
| owd p50 (s) | 0.020350614 | 13.104823336 |
| owd p99 (s) | 0.040490164999999995 | 13.666677154 |
| owd max (s) | 0.058141393 | 13.765326109 |

## Criteria

### C1_command
```json
{
  "criterion": "C1",
  "definition": "effect present if owd p50 >= 1.0 s OR owd p99 >= 150 ms; effect only in the tail if p99 >= 150 ms and p50 < 100 ms",
  "owd_p50_s": 0.020350614,
  "owd_p99_s": 0.040490164999999995,
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
  "owd_p50_s": 13.104823336,
  "owd_p99_s": 13.666677154,
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
  "run_dir": "ARTIFACT_ROOT/02_results/campaign/flyblind_20261111_232155/S1_core/runs/20261112_002300_179157_attack_cross_prb106_B0_setpoint-stream_traj-route_sp10p0hz_seed1_udp_multi-rogue_n4_ph25ms_vid12p0mbps_r1"
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
      "reliability": 0.9887915936952715,
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
      "max_gap_s": 0.756761,
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
      "fraccao": 0.003167062549485352,
      "source": "Honeywell TestReport-263 citing DO-377A"
    },
    "reliability_le_40ms_ge_99_9pct": {
      "pass": false,
      "reliability": 0.1519456454601606,
      "target": 0.999,
      "source": "TS 22.125 stick steering"
    },
    "reliability_le_1s_ge_99_9pct": {
      "pass": false,
      "reliability": 0.17109326744904263,
      "target": 0.999,
      "source": "TS 22.125 (1 s budget / waypoints class)"
    },
    "reliability_video_le_140ms_ge_99_99pct": {
      "pass": false,
      "reliability": 0.16553428042001236,
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
      "max_gap_s": 0.756761,
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

