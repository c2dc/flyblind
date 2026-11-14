# Figures

Every figure in the paper is generated here. Nothing is drawn by hand and nothing is edited after
generation, so a figure can always be traced back to the runs it was computed from.

```
04_images/
├── scripts/    the generator for each figure, plus build_all.sh and sync_to_paper.sh
├── pdfs/       vector output, what the manuscript includes
├── pngs/       raster previews of the same figures, for quick inspection
└── icons/      bitmap assets used by the TikZ figures
```

## Rebuilding

```bash
cd scripts
bash build_all.sh        # writes ../pdfs/*.pdf and ../pngs/*.png
bash sync_to_paper.sh    # copies ../pdfs/*.pdf into the manuscript tree
```

Run `build_all.sh` before `sync_to_paper.sh`. Keeping the copy in one direction is what stops the
manuscript and the artifact from drifting apart.

## What each figure shows

| Figure | Generator | Source |
|---|---|---|
| `fig_teaser` | `build_fig_teaser.sh` (TikZ) | `generate_fig_teaser_numbers.py` reads the campaign manifest |
| `fig_uldlgrants` | `build_fig_uldlgrants.sh` (TikZ) | schematic, no run data |
| `fig_pipeline` | `build_fig_pipeline.sh` (TikZ) | schematic, no run data |
| `fig_paths` | `build_fig_paths.sh` (TikZ) | schematic, no run data |
| `fig_pre_attack_drain` | `generate_fig_pre_attack_drain.py` | a `g_drain_video_on` run, delivery age across the three phases |
| `fig_route_divergence` | `generate_fig_route_divergence.py` | an `a_baseline` and an `a_atk_n4` run, displayed position against the aircraft |

`fig_orbit*` belongs to the earlier circular-trajectory campaign and is kept for reference. Its source
runs sit outside the released results, so `build_all.sh` reports it as skipped and carries on.

## Conventions

Generators resolve their own paths from `__file__`, so they run from anywhere and write to `../pdfs`
and `../pngs`. Condition ids follow `01_scripts/04_campaign/campaign.sh`, the same ids the analysis in
`03_analysis/` uses. The two matplotlib generators accept `--out-dir` when a figure is wanted
somewhere else without touching the canonical copies.
