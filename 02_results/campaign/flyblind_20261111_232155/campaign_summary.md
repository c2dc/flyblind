# FlyBlind campaign summary (20261111_232155)

### S1_core (seed 20261111)

| condition | planned | executed | valid | status |
|---|---:|---:|---:|---|
| g_drain_video_on | 3 | 3 | 3 | OK |
| b_atk_n4_vid4 | 3 | 3 | 3 | OK |
| c_atk_n4_novideo | 3 | 3 | 3 | OK |
| a_atk_n4 | 5 | 5 | 5 | OK |
| cap_base_vid40 | 2 | 2 | 0 | expected invalid allowed (evaluate by goodput) |
| a_baseline | 3 | 3 | 3 | OK |
| cap_base_vid24 | 2 | 2 | 1 | expected invalid allowed (evaluate by goodput) |
| b_atk_n4_vid8 | 3 | 3 | 3 | OK |
| g_drain_video_off | 3 | 3 | 3 | OK |
| a_atk_n2 | 3 | 3 | 3 | OK |
| a_atk_n1 | 3 | 3 | 3 | OK |
| a_atk_n3 | 3 | 3 | 3 | OK |
| cap_base_vid32 | 2 | 2 | 0 | expected invalid allowed (evaluate by goodput) |
- invalid: cap_base_vid40_r1 gates=[{'gate': 'c2_health', 'reason': {'pass': False, 'heartbeat_gaps_over_threshold': 89, 'unexpected_failsafe': False, 'unexpected_mode_transitions': [], 'position_samples': 2024, 'normal_land': True, 'min_position_samples': 10}}, {'gate': 'availability', 'reason': {'pass': False, 'availability_post_warmup_pct': 99.1492, 'min_pct': 99.9, 'enforced_on_baseline_only': True}}, {'gate': 'baseline_drone_quality', 'reason': 'heartbeat_gaps'}] dir=ARTIFACT_ROOT/02_results/campaign/flyblind_20261111_232155/S1_core/runs/20261112_014053_876430_baseline_cross_prb106_B0_setpoint-stream_traj-route_sp10p0hz_seed1_udp_vid40p0mbps_r1
- invalid: cap_base_vid40_r2 gates=[{'gate': 'c2_health', 'reason': {'pass': False, 'heartbeat_gaps_over_threshold': 114, 'unexpected_failsafe': False, 'unexpected_mode_transitions': [], 'position_samples': 2041, 'normal_land': True, 'min_position_samples': 10}}, {'gate': 'availability', 'reason': {'pass': False, 'availability_post_warmup_pct': 99.104, 'min_pct': 99.9, 'enforced_on_baseline_only': True}}, {'gate': 'baseline_drone_quality', 'reason': 'heartbeat_gaps'}] dir=ARTIFACT_ROOT/02_results/campaign/flyblind_20261111_232155/S1_core/runs/20261112_015539_542123_baseline_cross_prb106_B0_setpoint-stream_traj-route_sp10p0hz_seed1_udp_vid40p0mbps_r1
- invalid: cap_base_vid24_r2 gates=[{'gate': 'host', 'reason': {'pass': False, 'gate_type': 'host_quality', 'enforced': True, 'host_telemetry_rc': 0, 'loadavg1_max': 15.35, 'cpu_busy_pct_max': 58.65, 'cpu_busy_max_pct_threshold': 90.0, 'loadavg1_before': 8.1, 'loadavg1_after': 8.65, 'nproc': 16}}] dir=ARTIFACT_ROOT/02_results/campaign/flyblind_20261111_232155/S1_core/runs/20261112_022511_759566_baseline_cross_prb106_B0_setpoint-stream_traj-route_sp10p0hz_seed1_udp_vid24p0mbps_r1
- invalid: cap_base_vid32_r2 gates=[{'gate': 'c2_health', 'reason': {'pass': False, 'heartbeat_gaps_over_threshold': 61, 'unexpected_failsafe': False, 'unexpected_mode_transitions': [], 'position_samples': 2222, 'normal_land': True, 'min_position_samples': 10}}, {'gate': 'availability', 'reason': {'pass': False, 'availability_post_warmup_pct': 98.5822, 'min_pct': 99.9, 'enforced_on_baseline_only': True}}, {'gate': 'baseline_drone_quality', 'reason': 'heartbeat_gaps'}] dir=ARTIFACT_ROOT/02_results/campaign/flyblind_20261111_232155/S1_core/runs/20261112_082134_004294_baseline_cross_prb106_B0_setpoint-stream_traj-route_sp10p0hz_seed1_udp_vid32p0mbps_r1
- invalid: cap_base_vid32_r1 gates=[{'gate': 'c2_health', 'reason': {'pass': False, 'heartbeat_gaps_over_threshold': 74, 'unexpected_failsafe': False, 'unexpected_mode_transitions': [], 'position_samples': 2221, 'normal_land': True, 'min_position_samples': 10}}, {'gate': 'availability', 'reason': {'pass': False, 'availability_post_warmup_pct': 97.8719, 'min_pct': 99.9, 'enforced_on_baseline_only': True}}, {'gate': 'baseline_drone_quality', 'reason': 'heartbeat_gaps'}] dir=ARTIFACT_ROOT/02_results/campaign/flyblind_20261111_232155/S1_core/runs/20261112_092329_663126_baseline_cross_prb106_B0_setpoint-stream_traj-route_sp10p0hz_seed1_udp_vid32p0mbps_r1

### S3_prb51 (seed 20261113)

| condition | planned | executed | valid | status |
|---|---:|---:|---:|---|
| e_atk51_n4 | 3 | 3 | 3 | OK |
| cap51_base_vid16 | 1 | 1 | 0 | expected invalid allowed (evaluate by goodput) |
| cap51_base_vid24 | 1 | 1 | 0 | expected invalid allowed (evaluate by goodput) |
| e_base51 | 3 | 3 | 3 | OK |
- invalid: cap51_base_vid16_r1 gates=[{'gate': 'host', 'reason': {'pass': False, 'gate_type': 'host_quality', 'enforced': True, 'host_telemetry_rc': 0, 'loadavg1_max': 15.42, 'cpu_busy_pct_max': 56.3, 'cpu_busy_max_pct_threshold': 90.0, 'loadavg1_before': 6.95, 'loadavg1_after': 10.09, 'nproc': 16}}] dir=ARTIFACT_ROOT/02_results/campaign/flyblind_20261111_232155/S3_prb51/runs/20261112_103554_670559_baseline_cross_prb51_B0_setpoint-stream_traj-route_sp10p0hz_seed1_udp_vid16p0mbps_r1
- invalid: cap51_base_vid24_r1 gates=[{'gate': 'host', 'reason': {'pass': False, 'gate_type': 'host_quality', 'enforced': True, 'host_telemetry_rc': 0, 'loadavg1_max': 15.89, 'cpu_busy_pct_max': 61.35, 'cpu_busy_max_pct_threshold': 90.0, 'loadavg1_before': 7.47, 'loadavg1_after': 11.15, 'nproc': 16}}, {'gate': 'c2_health', 'reason': {'pass': False, 'heartbeat_gaps_over_threshold': 22, 'unexpected_failsafe': False, 'unexpected_mode_transitions': [], 'position_samples': 2458, 'normal_land': True, 'min_position_samples': 10}}, {'gate': 'baseline_drone_quality', 'reason': 'heartbeat_gaps'}] dir=ARTIFACT_ROOT/02_results/campaign/flyblind_20261111_232155/S3_prb51/runs/20261112_105053_921298_baseline_cross_prb51_B0_setpoint-stream_traj-route_sp10p0hz_seed1_udp_vid24p0mbps_r1

### S4_slicing (seed 20261114)

| condition | planned | executed | valid | status |
|---|---:|---:|---:|---|
| f1_slicing_n4 | 3 | 3 | 3 | OK |

### S5_aqm (seed 20261115)

| condition | planned | executed | valid | status |
|---|---:|---:|---:|---|
| f2_aqm_n4 | 3 | 3 | 3 | OK |
