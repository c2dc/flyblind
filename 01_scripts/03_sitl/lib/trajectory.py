#!/usr/bin/env python3
##################################################################################################################################
# Module: trajectory.py (parametric LOCAL_NED reference paths for setpoint stream)                                               #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Circle, figure-8, sine, line trajectories with phase alignment at orbit entry.                                    #
##################################################################################################################################

"""Parametric reference paths for the setpoint-stream workload (LOCAL_NED, metres).

Used by ``setpoint_stream.py`` to generate ``SET_POSITION_TARGET_LOCAL_NED`` at
a fixed rate (default 10 Hz). Each shape exposes position + velocity so ArduPilot
can track smoothly without waypoint jumps.

Phase handling
--------------
``initial_phase_from_xy`` + ``phase_offset_rad`` rotate the path so ``t=0`` lines
up with the UAV horizontal position at orbit entry (option B in the paper). Near
the origin after takeoff, phase stays 0 and the acquisition window absorbs the
transient.

``tracking_errors`` compares live NED against the same reference — feeds
``flight_state`` / freshness accounting, not VALID gates directly.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

TrajectoryName = Literal["circle", "figure8", "sine", "line", "route"]


@dataclass(frozen=True)
class TrajectoryConfig:
    name: TrajectoryName
    duration_s: float
    speed_mps: float = 5.0
    radius_m: float = 30.0
    amplitude_m: float = 20.0
    period_s: float = 20.0
    seed: int = 1
    phase_offset_rad: float = 0.0

    def __post_init__(self) -> None:
        if self.duration_s <= 0:
            raise ValueError("duration_s must be > 0")


# Below this distance from origin we skip rephase (takeoff hover noise).
CENTER_EPS_M = 1.0

# --- Real-route mode: Scene B logistics route (Low-Altitude 5G dataset), scaled ~1/6 to a
#     ~1.87 km closed loop (~467 s @ 4 m/s), centred at origin so the vehicle stays within the
#     flyaway gate. Flown in LOOP like the circle; turns make the stale-state lag visible. ---
ROUTE_WAYPOINTS = (
    (-16.1,19.6),(-8.9,26.6),(-1.8,33.6),(5.4,40.6),(13.1,46.8),(21.4,52.4),
    (29,58.9),(36.2,65.8),(43.4,72.7),(50.6,79.7),(57.6,86.7),(64.8,93.7),
    (72,100.6),(79.1,107.7),(86,114.8),(86,124.5),(84.4,134.3),(87,137.7),
    (95.2,131.9),(103.1,126.4),(95.7,133.2),(88.4,140),(82.2,147.5),(82.4,157.1),
    (88.4,165.1),(94.5,172.9),(100.6,180.9),(106.9,188.6),(108.8,189.7),(113.2,190.6),
    (108.3,194.9),(107.9,189),(101.7,181.2),(95.7,173.2),(89.5,165.4),(83.2,157.6),
    (82,148.3),(83.4,138.5),(85.1,128.6),(86.7,118.7),(82.4,110.5),(75.4,103.4),
    (68.3,96.4),(61.2,89.3),(53.9,82.4),(47.1,75.2),(51.6,80.7),(58,88.4),
    (64.4,96),(70.9,103.7),(77.3,111.4),(83.7,119),(90.1,126.7),(88.8,125.2),
    (82.3,117.6),(75.9,110),(69.4,102.4),(62.9,94.8),(56.4,87.1),(49.9,79.5),
    (43.5,71.9),(36.4,64.8),(29.4,57.7),(21.7,51.4),(13.3,46),(4.9,40.5),
    (-3.3,34.8),(-11.5,29.2),(-19.3,23),(-27.1,16.8),(-34.9,10.5),(-43,4.6),
    (-52.5,1.7),(-62.2,-1.1),(-71.8,-3.6),(-79.9,-8.7),(-85.8,-16.7),(-91.6,-24.9),
    (-97.7,-30.3),(-100.3,-31.5),(-102.7,-40.1),(-108.6,-48.2),(-114.6,-56.2),(-111.9,-65.1),
    (-107.4,-74),(-102.9,-82.9),(-98.3,-91.8),(-93.8,-100.7),(-89.3,-109.7),(-84.9,-118.6),
    (-80.2,-127.4),(-79.7,-136.3),(-84.6,-145),(-89.2,-153.8),(-93.7,-162.8),(-98.4,-171.6),
    (-102.9,-180.5),(-104.3,-182.9),(-103.2,-172.9),(-102.1,-163),(-101,-153.1),(-99.8,-143.1),
    (-98.7,-133.2),(-97.6,-123.3),(-96.4,-113.3),(-95.3,-103.4),(-95,-100.4),(-96.1,-110.3),
    (-97.2,-120.2),(-98.4,-130.2),(-99.5,-140.1),(-100.6,-150),(-101.8,-160),(-102.9,-169.9),
    (-104,-179.8),(-100.5,-182.7),(-91.6,-178.3),(-82.6,-173.8),(-73.7,-169.4),(-69.5,-167.4),
    (-78.5,-171.8),(-87.4,-176.3),(-96.4,-180.7),(-104.6,-184.9),(-95.7,-185.5),(-85.8,-186.4),
    (-75.8,-187.2),(-65.9,-188.1),(-73.6,-187.5),(-83.6,-186.6),(-93.5,-185.8),(-103.5,-185),
    (-99.2,-184.2),(-103.1,-185.9),(-103.2,-185.9),(-102.4,-179.3),(-98.1,-170.3),(-93.3,-161.5),
    (-88.8,-152.6),(-84,-144.7),(-76.8,-151.7),(-69.6,-158.6),(-62.4,-165.6),(-55.3,-172.6),
    (-48.1,-179.5),(-40.9,-186.5),(-33.7,-193.5),(-35.5,-191.1),(-42.3,-183.7),(-49,-176.3),
    (-55.7,-168.9),(-62.4,-161.5),(-69.1,-154.1),(-75.9,-146.7),(-81.7,-139.1),(-79.3,-130.1),
    (-83.8,-121.2),(-88.4,-112.3),(-92.8,-103.3),(-97.3,-94.4),(-101.7,-85.4),(-106.2,-76.5),
    (-110.7,-67.6),(-115.4,-58.8),(-111.6,-50.2),(-105.7,-42.1),(-99.6,-34.2),(-90.8,-32.6),
    (-80.9,-33.7),(-70.9,-34.8),(-61,-35.9),(-70.5,-33.6),(-80.2,-31.3),(-89.9,-29),
    (-92.2,-24.1),(-86.3,-16.1),(-80.3,-8.1),(-72.1,-3.2),(-62.4,-0.8),(-52.8,1.8),
    (-43.2,4.5),(-35.5,10.8),(-27.7,17.1),(-19.7,23.2),(-12,29.5),(-3.9,35.3),
    (4.3,41),(-2.2,34.4),(-9.3,27.3),(-16.4,20.2),(-23.4,13.1),(-30.5,6.1),
    (-37.5,-1),(-44.6,-8.1),(-51.6,-15.2),(-51.8,-15.4),(-44.6,-8.4),(-37.5,-1.4),
    (-30.3,5.6),(-23.2,12.6),(-16.1,19.6),
)
_ROUTE_CUM = [0.0]
for _i in range(1, len(ROUTE_WAYPOINTS)):
    _ax, _ay = ROUTE_WAYPOINTS[_i - 1]
    _bx, _by = ROUTE_WAYPOINTS[_i]
    _ROUTE_CUM.append(_ROUTE_CUM[-1] + math.hypot(_bx - _ax, _by - _ay))
ROUTE_LENGTH_M = _ROUTE_CUM[-1]


def _route_pos_vel(dist_m: float, speed_mps: float):
    """Position + velocity at arc-length ``dist_m`` along the closed route loop."""
    if ROUTE_LENGTH_M <= 0:
        return 0.0, 0.0, 0.0, 0.0
    d = dist_m % ROUTE_LENGTH_M
    lo, hi = 0, len(_ROUTE_CUM) - 1
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if _ROUTE_CUM[mid] <= d:
            lo = mid
        else:
            hi = mid
    ax, ay = ROUTE_WAYPOINTS[lo]
    bx, by = ROUTE_WAYPOINTS[lo + 1]
    seg = _ROUTE_CUM[lo + 1] - _ROUTE_CUM[lo]
    f = (d - _ROUTE_CUM[lo]) / seg if seg > 1e-9 else 0.0
    x = ax + (bx - ax) * f
    y = ay + (by - ay) * f
    if seg > 1e-9:
        ux, uy = (bx - ax) / seg, (by - ay) / seg
    else:
        ux, uy = 1.0, 0.0
    return x, y, ux * speed_mps, uy * speed_mps


def initial_phase_from_xy(x_m: float, y_m: float, cfg: TrajectoryConfig) -> float:
    """Align t=0 with the UAV's current angle on circle/figure-8."""
    if cfg.name in ("circle", "figure8"):
        if math.hypot(x_m, y_m) < CENTER_EPS_M:
            return 0.0
        return math.atan2(y_m, x_m)
    if cfg.name == "sine":
        return 0.0
    if cfg.name == "line":
        return 0.0
    return 0.0


@dataclass(frozen=True)
class TrajectorySample:
    t_s: float
    x_m: float
    y_m: float
    z_m: float
    vx_mps: float
    vy_mps: float
    vz_mps: float
    yaw_rad: float
    yaw_rate_rps: float


def _omega(cfg: TrajectoryConfig) -> float:
    if cfg.period_s <= 0:
        raise ValueError("period_s must be > 0")
    return 2.0 * math.pi / cfg.period_s


def sample(cfg: TrajectoryConfig, t_s: float, alt_m: float) -> TrajectorySample:
    """Sample reference in LOCAL_NED (z down, negative = up).

    ``t_s`` is seconds since the first setpoint (trajectory_t0), not mission start.
    """
    t = max(0.0, min(float(t_s), cfg.duration_s))
    z = -float(alt_m)
    phi0 = float(cfg.phase_offset_rad)

    if cfg.name == "circle":
        w = _omega(cfg)
        wt = w * t + phi0
        x = cfg.radius_m * math.cos(wt)
        y = cfg.radius_m * math.sin(wt)
        vx = -cfg.radius_m * w * math.sin(wt)
        vy = cfg.radius_m * w * math.cos(wt)
        yaw = math.atan2(vy, vx) if cfg.speed_mps > 0 else 0.0
        return TrajectorySample(t, x, y, z, vx, vy, 0.0, yaw, w)

    if cfg.name == "figure8":
        w = _omega(cfg)
        wt = w * t + phi0
        sx = math.sin(wt)
        cx = math.cos(wt)
        x = cfg.radius_m * sx * cx
        y = cfg.radius_m * sx
        vx = cfg.radius_m * w * (cx * cx - sx * sx)
        vy = cfg.radius_m * w * cx
        yaw = math.atan2(vy, vx) if abs(vx) + abs(vy) > 1e-6 else 0.0
        return TrajectorySample(t, x, y, z, vx, vy, 0.0, yaw, 0.0)

    if cfg.name == "sine":
        w = _omega(cfg)
        x = cfg.speed_mps * t
        y = cfg.amplitude_m * math.sin(w * t)
        vx = cfg.speed_mps
        vy = cfg.amplitude_m * w * math.cos(w * t)
        yaw = math.atan2(vy, vx)
        return TrajectorySample(t, x, y, z, vx, vy, 0.0, yaw, 0.0)

    if cfg.name == "line":
        # Long leg with gentle heading wobble — no discontinuous jumps.
        w = _omega(cfg)
        heading = 0.25 * math.sin(w * t)
        vx = cfg.speed_mps * math.cos(heading)
        vy = cfg.speed_mps * math.sin(heading)
        x = (cfg.speed_mps / w) * math.sin(heading) if w > 0 else cfg.speed_mps * t
        y = (cfg.speed_mps / w) * (1.0 - math.cos(heading)) if w > 0 else 0.0
        return TrajectorySample(
            t, x, y, z, vx, vy, 0.0, heading,
            cfg.speed_mps * 0.25 * w * math.cos(w * t),
        )

    if cfg.name == "route":
        dist = cfg.speed_mps * t
        x, y, vx, vy = _route_pos_vel(dist, cfg.speed_mps)
        yaw = math.atan2(vy, vx) if (abs(vx) + abs(vy) > 1e-6) else 0.0
        return TrajectorySample(t, x, y, z, vx, vy, 0.0, yaw, 0.0)

    raise ValueError("unknown trajectory %r" % cfg.name)


def tracking_errors(
    cfg: TrajectoryConfig,
    t_s: float,
    alt_m: float,
    x_act: float,
    y_act: float,
    z_act: float,
) -> dict[str, float]:
    """3D tracking error + decomposed cross/along-track components."""
    ref = sample(cfg, t_s, alt_m)
    dx, dy, dz = x_act - ref.x_m, y_act - ref.y_m, z_act - ref.z_m
    track = math.sqrt(dx * dx + dy * dy + dz * dz)
    if cfg.name == "circle":
        dist_center = math.sqrt(x_act * x_act + y_act * y_act)
        cross = abs(dist_center - cfg.radius_m)
        along = abs(math.atan2(y_act, x_act) - math.atan2(ref.y_m, ref.x_m)) * cfg.radius_m
    elif cfg.name == "route":
        # cross/along relative to the path tangent at the reference point
        vnorm = math.hypot(ref.vx_mps, ref.vy_mps)
        if vnorm > 1e-6:
            tx, ty = ref.vx_mps / vnorm, ref.vy_mps / vnorm
            cross = abs(-ty * dx + tx * dy)
            along = tx * dx + ty * dy
        else:
            cross = math.hypot(dx, dy)
            along = 0.0
    else:
        cross = math.sqrt(dy * dy) if cfg.name == "sine" else abs(dy)
        along = dx
    return {
        "tracking_error_m": track,
        "cross_track_error_m": cross,
        "along_track_error_m": along,
        "x_ref": ref.x_m,
        "y_ref": ref.y_m,
        "z_ref": ref.z_m,
    }
