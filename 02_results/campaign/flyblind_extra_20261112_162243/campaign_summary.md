# FlyBlind campaign summary (20261112_162243)

### X1_core_extra (seed 20261121)

| condition | planned | executed | valid | status |
|---|---:|---:|---:|---|
| b_atk_n4_vid6 | 3 | 3 | 3 | OK |
| d_atk_n4_vid16 | 3 | 3 | 3 | OK |
| d_atk_n4_vid24 | 3 | 3 | 3 | OK |
| cap_base_vid24 | 1 | 1 | 1 | expected invalid allowed (evaluate by goodput) |

### X6_poscontrol (seed 20261123)

| condition | planned | executed | valid | status |
|---|---:|---:|---:|---|
| pos_control_route | 1 | 1 | 0 | expected invalid allowed (evaluate by goodput) |
- invalid: pos_control_route_r1 gates=[{'gate': 'c2_health', 'reason': {'pass': False, 'heartbeat_gaps_over_threshold': 0, 'unexpected_failsafe': True, 'unexpected_mode_transitions': [{'t_rel_s': 132.923, 'from': 'GUIDED', 'to': 'RTL', 'steady_state': True}], 'position_samples': 2407, 'normal_land': False, 'min_position_samples': 10}}, {'gate': 'baseline_drone_quality', 'reason': 'failsafe; spatial/acquisition; missing GUIDED->LAND; abnormal_land'}] dir=ARTIFACT_ROOT/02_results/campaign/flyblind_extra_20261112_162243/X6_poscontrol/runs/20261112_191908_241355_baseline_cross_prb106_B0_setpoint-stream_traj-route_sp10p0hz_seed1_udp_vid12p0mbps_r1

### X3_prb51_extra (seed 20261122)

| condition | planned | executed | valid | status |
|---|---:|---:|---:|---|
| cap51_base_vid16 | 2 | 2 | 0 | expected invalid allowed (evaluate by goodput) |
- invalid: cap51_base_vid16_r2 gates=[{'gate': 'host', 'reason': {'pass': False, 'gate_type': 'host_quality', 'enforced': True, 'host_telemetry_rc': 0, 'loadavg1_max': 16.44, 'cpu_busy_pct_max': 71.03, 'cpu_busy_max_pct_threshold': 90.0, 'loadavg1_before': 7.0, 'loadavg1_after': 11.18, 'nproc': 16}}] dir=ARTIFACT_ROOT/02_results/campaign/flyblind_extra_20261112_162243/X3_prb51_extra/runs/20261112_195531_084895_baseline_cross_prb51_B0_setpoint-stream_traj-route_sp10p0hz_seed1_udp_vid16p0mbps_r1
- invalid: cap51_base_vid16_r1 gates=[{'gate': 'host', 'reason': {'pass': False, 'gate_type': 'host_quality', 'enforced': True, 'host_telemetry_rc': 0, 'loadavg1_max': 20.13, 'cpu_busy_pct_max': 80.07, 'cpu_busy_max_pct_threshold': 90.0, 'loadavg1_before': 5.75, 'loadavg1_after': 13.62, 'nproc': 16}}] dir=ARTIFACT_ROOT/02_results/campaign/flyblind_extra_20261112_162243/X3_prb51_extra/runs/20261112_201028_837998_baseline_cross_prb51_B0_setpoint-stream_traj-route_sp10p0hz_seed1_udp_vid16p0mbps_r1
