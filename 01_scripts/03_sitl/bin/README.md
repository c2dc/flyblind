# QGroundControl binaries (not included)

The two operator-view binaries the orchestrator launches are **not** in this repository: each
AppImage is 187 MB, above the per-file limit of common Git hosts. Place them here before running
with `--open-qgc` / `--open-qgc-mitigation`.

| expected file | what it is | how to obtain |
|---|---|---|
| `QGroundControl.AppImage` | **stock** operator view — the baseline that raises no staleness alarm | official QGroundControl **v5.1.5** release for Linux x86_64 |
| `QGroundControl-mitigation.AppImage` | the **detector** build (second contribution) | build from `../qgc_detector/` — see the README there |

```bash
# stock view (the release used by the reported runs)
cd "$(dirname "$0")"
wget -O QGroundControl.AppImage \
  https://github.com/mavlink/qgroundcontrol/releases/download/v5.1.5/QGroundControl-x86_64.AppImage
chmod +x QGroundControl.AppImage
```

The AppImage launcher needs FUSE (`libfuse2`), which `install_dependencies.sh` installs.

## Running without them

Both are optional for the measurements. Dropping `--open-qgc` from a run produces every number in the
paper; the operator view is **evidence of what the operator sees**, not an input to any metric.

Note that `04_campaign/campaign.sh` treats the stock AppImage as a hard requirement in its preflight,
because every campaign run opens it. Either place the file here, or run the orchestrator directly
without `--open-qgc`.
