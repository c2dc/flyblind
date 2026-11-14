# State-freshness detector — QGroundControl overlay

This is the QGroundControl *custom build* that carries the paper's second contribution: a
**destination-side state-freshness detector**. It is the source of the
`QGroundControl-mitigation.AppImage` used by `orchestrate.py --open-qgc-mitigation`.

The built AppImage is **not** included: at 187 MB it exceeds the per-file limit of common Git
hosts. Everything needed to rebuild it is here.

## What the detector does

`src/FlyBlindDetector.{h,cc}` recovers the Age-of-Information (AoI) of the vehicle state **as seen
at the GCS**, using only the `time_boot_ms` stamp that the autopilot writes into
`GLOBAL_POSITION_INT` *before* the uplink queue:

```
implied_i = t_i - s_i                     # = T0 + D_i,  D_i >= 0 transport delay
e_hat     = min(e_hat, implied_i)         # calibrate: the freshest sample has D ~= 0
A_i       = implied_i - e_hat             # = D_i - min_j D_j  ~=  age of this state
A_i       = max(A_i, 0)
stale_i   = (A_i > tau),  tau = 1 s
```

where `s_i` is the generation stamp the autopilot writes *before* the uplink queue
(`GLOBAL_POSITION_INT.time_boot_ms`) and `t_i` is the local arrival time at the GCS. Age is only
measurable because the message carries *when it was produced*.

It uses **no simulator ground truth**, unlike the `belief_vs_truth_m` metric used to quantify the
damage — so it is deployable on a real flight, not only in the testbed.

`tau = 1 s` separates baseline (`A ~ 18 ms`) from attack (`A ~ 13 s`) by three orders of magnitude.

### Why not simply detect silence

Because there is none. Under attack the position reports **keep arriving**, roughly every 0.25 s —
each one is just about 13 s old. An inter-arrival-gap detector would barely react: the gap p99 stays
around 1.3 s while the age is ~13 s. That is the whole point of the attack, and the reason the signal
has to be the **age** of each report rather than the interval between them. It is also why the native
monitors stay quiet: they watch heartbeat liveness, which the attack preserves.

### Validation

On the testbed the overlay reported `A = 13.2 s` while the harness, measuring independently from both
endpoints, recorded `tel_delay p50 = 13.26 s`. The detector recovers the real age injected by uplink
contention using only `time_boot_ms`. The same run was scored `VALID=YES`, confirming that the custom
build changes no measurement — it only adds a view. The negative control also holds: a baseline run
opened with `--open-qgc-mitigation` leaves the alarm dark.

**Known limitation.** `estT0` is a running minimum, so calibration needs at least one *fresh*
stretch of telemetry. The experimental protocol guarantees this (the GCS connects before the attack
starts, `--attack-start-after-s 45`). A GCS that attached while the attack was already running would
over-estimate `estT0` and therefore under-report the age; a deployment would calibrate during an
uncontended phase and/or use a leaky minimum that tracks slow clock drift.

## Files that matter

| path | role |
|---|---|
| `src/FlyBlindDetector.h` / `.cc` | the detector: running minimum, AoI, stale flag |
| `src/CustomPlugin.cc` / `.h` | feeds `GLOBAL_POSITION_INT` to the detector and always forwards the message on (never consumes it) |
| `src/FlyViewCustomLayer.qml` | the central overlay, bound to `FlyBlindDetector.telemetryStale` |
| `CMakeLists.txt`, `cmake/`, `custom.qrc` | wiring of the above into the QGC custom-build mechanism |

The remaining files come from QGroundControl's own `custom-example` template and are kept only
because the build expects them. The template was stripped so the result looks like stock QGC **plus**
the alarm: the example compass, attitude widget and demo rectangle were removed.

## What was changed, relative to QGC's own `custom-example`

This overlay started as upstream's `custom-example` template. Besides adding the detector, the
template was **stripped** so the result reads as stock QGC *plus* the alarm — the figure must not look
like a different product:

| change | why |
|---|---|
| `CMakeLists.txt`: dropped `CustomFirmwarePlugin`, `CustomFirmwarePluginFactory`, `CustomAutoPilotPlugin` from `CUSTOM_SOURCES` | the template's firmware plugin is **PX4**, but the vehicle is ArduCopter, so the mode indicator read `Custom:0x4`. Without it QGC falls back to the stock ArduPilot plugin. `PerimeterScan` was kept: it is a Plan-view feature and never appears in the Fly View. |
| `CustomPlugin.h`: `showInstrumentPanel()` and `showMultiVehicleList()` → `true` | restores QGC's standard panels |
| `FlyViewCustomLayer.qml`: rewritten as the stock tool-inset pass-through **plus** the alarm | removed the template's own compass, attitude widget and demo rectangle (and the `Custom.Widgets` import) |
| `paletteOverride` (dark colours) | kept — visually close to stock, low impact |

`CustomPlugin.cc` registers the detector as a QML singleton
(`qmlRegisterSingletonInstance("FlyBlind", 1, 0, "FlyBlindDetector", ...)`) and overrides
`QGCCorePlugin::mavlinkMessage(...)` to decode `GLOBAL_POSITION_INT`, skip bogus `lat==0 && lon==0`
frames, feed the detector, and then **always** call the base implementation — the message is observed,
never consumed.

## Rebuilding

Built against **QGroundControl v5.1.5** (the version the reported runs used), Qt 6.8.3, via QGC's own
Docker build. First build takes roughly 15-20 minutes.

```bash
git clone --recursive -b v5.1.5 https://github.com/mavlink/qgroundcontrol.git
cd qgroundcontrol

# QGC auto-detects a directory named exactly "custom" (QGC_CUSTOM_DIR=custom -> QGC_CUSTOM_BUILD ON)
cp -r /path/to/this/qgc_detector ./custom

# Optional but recommended: see "Known build issue" below
git apply custom/patches/0001-docker-build-use-direct-apt-sources.patch

./deploy/docker/run-docker.sh ubuntu Release     # Ubuntu 24.04 + Qt 6.8.3, CMake + Ninja
```

The AppImage appears as `build/*Custom*x86_64.AppImage`. Install it where the orchestrator expects it:

```bash
cp build/Custom-QGroundControl-x86_64.AppImage ../bin/QGroundControl-mitigation.AppImage
chmod +x ../bin/QGroundControl-mitigation.AppImage
```

### Known build issue

`deploy/docker/lib/setup-base.sh` (upstream, outside this directory) rewrites the container's apt
sources to the `mirror://mirrors.ubuntu.com/mirrors.txt` redirector. On networks where that mirror list
cannot be fetched the build fails early with *"Downloading mirror file failed"*. The patch in
`patches/` disables that rewrite and keeps the direct `archive.ubuntu.com` sources. It is purely an
environment workaround and touches nothing in QGroundControl itself.

The stock operator view used by `--open-qgc` is an unmodified QGroundControl release of the same
version; it is the baseline that shows **no** alarm, which is the silence the attack exploits.

## What the two views show

| | state at the GCS | native monitors | this overlay |
|---|---|---|---|
| baseline | fresh (~18 ms) | quiet | off |
| under attack | stale (~13 s) | **quiet** (0 alerts in 26/26 runs) | **"STALE STATE — C2 TIMELINESS ALARM"** |
| command leg cut (positive control) | fresh | **GCS Failsafe -> RTL** | off |

The alarm is **visual only** in this build: it raises an on-screen overlay and emits no sound.

## Licence

QGroundControl is dual-licensed **Apache-2.0 / GPLv3** and explicitly supports custom builds through
this mechanism. Every file here other than `src/FlyBlindDetector.{h,cc}`, the alarm block in
`src/FlyViewCustomLayer.qml`, the detector hook in `src/CustomPlugin.{h,cc}` and
`patches/0001-docker-build-use-direct-apt-sources.patch` derives from upstream's `custom-example`
template and remains under its original licence. The FlyBlind additions are released under the MIT
licence of this artifact (see the top-level `LICENSE`).
