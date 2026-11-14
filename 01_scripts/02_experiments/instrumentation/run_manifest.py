#!/usr/bin/env python3
##################################################################################################################################
# Module: run_manifest.py (Per-run manifest: image digests, source revisions and stamps (run provenance))                        #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Always includes: - image hashes (image_digests.json collected via kubectl at run start) - ArduPilot SITL and OAI  #
#   versions (commit + tag of the git trees) - FS_GCS_ENABLE / FS_GCS_TIMEOUT, sidecars profile, config hashes                   #
#                                                                                                                                #
# Git commit of this artifacts tree: not available. Field commit_git = null; replacement: scripts_tree_sha256 of                 #
#   artifacts/01_scripts.                                                                                                        #
#                                                                                                                                #
# If a field cannot be obtained, the reason is recorded (never silently empty).                                                  #
##################################################################################################################################
"""Per-run manifest: image digests, source revisions and stamps (run provenance).

Always includes:
  - image hashes (image_digests.json collected via kubectl at run start)
  - ArduPilot SITL and OAI versions (commit + tag of the git trees)
  - FS_GCS_ENABLE / FS_GCS_TIMEOUT, sidecars profile, config hashes

Git commit of this artifacts tree: not available. Field commit_git = null;
replacement: scripts_tree_sha256 of artifacts/01_scripts.

If a field cannot be obtained, the reason is recorded (never silently empty).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Iterable, Optional

HERE = Path(__file__).resolve().parent
SCRIPTS_ROOT = HERE.parents[1]  # artifacts/01_scripts
ARTIFACTS_ROOT = SCRIPTS_ROOT.parent
FLYBLIND_ROOT = ARTIFACTS_ROOT.parent
VENDOR_DIR = SCRIPTS_ROOT / "01_testbeds" / "vendor"
SOURCE_CODE_DIR = FLYBLIND_ROOT / "EXTERNAL_SOURCES"

# Preference: testbed runtime (vendor), then the local "EXTERNAL_SOURCES" source folder.
SOURCE_TREE_CANDIDATES: dict[str, list[Path]] = {
    "ardupilot": [
        Path(os.environ["ARDUPILOT_ROOT"]) if os.environ.get("ARDUPILOT_ROOT") else None,
        VENDOR_DIR / "ardupilot",
        SOURCE_CODE_DIR / "ardupilot",
    ],
    "oai_cn5g_fed": [
        Path(os.environ["OAI_CN5G_FED_ROOT"]) if os.environ.get("OAI_CN5G_FED_ROOT") else None,
        SOURCE_CODE_DIR / "oai-cn5g-fed",
        VENDOR_DIR / "oai-cn5g-fed",
    ],
    "ORANSlice": [
        Path(os.environ["ORANSLICE_ROOT"]) if os.environ.get("ORANSLICE_ROOT") else None,
        VENDOR_DIR / "ORANSlice",
        SOURCE_CODE_DIR / "ORANSlice",
        VENDOR_DIR / "ORANSlice-mitigation",
    ],
}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_tree(
    root: Path,
    *,
    suffixes: Optional[Iterable[str]] = None,
    exclude_dirs: Optional[Iterable[Path]] = None,
) -> dict[str, Any]:
    root = Path(root)
    suf = set(suffixes) if suffixes is not None else None
    include_extensionless = suf is not None and "" in suf
    exclude = {Path(d).resolve() for d in exclude_dirs} if exclude_dirs else set()
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune version-control noise and any explicitly excluded subtree (e.g. the
        # 1.7G testbed vendor runtime, which is versioned separately via git commit/
        # tag in collect_versions()).
        dirnames[:] = [
            d for d in sorted(dirnames)
            if d not in {".git", "__pycache__", ".pytest_cache"}
            and (Path(dirpath) / d).resolve() not in exclude
        ]
        for name in sorted(filenames):
            p = Path(dirpath) / name
            if suf is not None:
                if p.suffix:
                    if p.suffix not in suf:
                        continue
                elif not include_extensionless:
                    continue
            files.append(p)
    h = hashlib.sha256()
    listing = []
    for p in files:
        rel = str(p.relative_to(root)).replace(os.sep, "/")
        digest = sha256_file(p)
        h.update(f"{digest}  {rel}\n".encode("utf-8"))
        listing.append({"path": rel, "sha256": digest})
    return {
        "root": str(root),
        "n_files": len(files),
        "sha256": h.hexdigest(),
        "files_sample": listing[:20],
    }


def _git(cwd: Path, *args: str) -> tuple[int, str]:
    try:
        r = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    out = (r.stdout or "").strip() or (r.stderr or "").strip()
    return r.returncode, out


def describe_git_tree(path: Path) -> dict[str, Any]:
    """commit + tag (if any) of a tree with .git."""
    out: dict[str, Any] = {
        "path": str(path),
        "exists": path.is_dir(),
        "has_git": (path / ".git").exists(),
        "commit": None,
        "commit_short": None,
        "tag": None,
        "describe": None,
        "ok": False,
        "error": None,
    }
    if not path.is_dir():
        out["error"] = f"directory missing: {path}"
        return out
    if not (path / ".git").exists():
        out["error"] = f"no .git in {path}"
        return out
    rc, commit = _git(path, "rev-parse", "HEAD")
    if rc != 0 or not commit:
        out["error"] = f"git rev-parse HEAD failed: {commit}"
        return out
    out["commit"] = commit
    rc_s, short = _git(path, "rev-parse", "--short", "HEAD")
    out["commit_short"] = short if rc_s == 0 else commit[:12]
    rc_t, tag = _git(path, "describe", "--tags", "--exact-match", "HEAD")
    if rc_t == 0 and tag:
        out["tag"] = tag
    rc_d, desc = _git(path, "describe", "--tags", "--always")
    if rc_d == 0 and desc:
        out["describe"] = desc
    out["ok"] = True
    return out


def resolve_source_tree(name: str) -> dict[str, Any]:
    candidates = [p for p in SOURCE_TREE_CANDIDATES.get(name, []) if p is not None]
    tried = []
    for cand in candidates:
        tried.append(str(cand))
        info = describe_git_tree(cand)
        if info["ok"]:
            info["resolved_from"] = str(cand)
            info["candidates_tried"] = tried
            return info
    return {
        "path": None,
        "exists": False,
        "has_git": False,
        "commit": None,
        "commit_short": None,
        "tag": None,
        "describe": None,
        "ok": False,
        "error": (
            f"no usable git tree for {name}; "
            f"candidates: {tried}"
        ),
        "candidates_tried": tried,
    }


def collect_versions() -> dict[str, Any]:
    """Always returns the ardupilot + oai block (cn5g + ORANSlice), with a reason on failure."""
    ardupilot = resolve_source_tree("ardupilot")
    oran = resolve_source_tree("ORANSlice")
    cn5g = resolve_source_tree("oai_cn5g_fed")
    return {
        "schema": "flyblind_versions_v2",
        "ardupilot_sitl": {
            "commit": ardupilot.get("commit"),
            "commit_short": ardupilot.get("commit_short"),
            "tag": ardupilot.get("tag"),
            "describe": ardupilot.get("describe"),
            "path": ardupilot.get("path") or ardupilot.get("resolved_from"),
            "ok": ardupilot.get("ok"),
            "error": ardupilot.get("error"),
            "candidates_tried": ardupilot.get("candidates_tried"),
        },
        "oai": {
            "note": (
                "OAI = core (oai-cn5g-fed) + RAN (ORANSlice). "
                "Runtime images may derive from vendor/ORANSlice*."
            ),
            "oai_cn5g_fed": {
                "commit": cn5g.get("commit"),
                "commit_short": cn5g.get("commit_short"),
                "tag": cn5g.get("tag"),
                "describe": cn5g.get("describe"),
                "path": cn5g.get("path") or cn5g.get("resolved_from"),
                "ok": cn5g.get("ok"),
                "error": cn5g.get("error"),
                "candidates_tried": cn5g.get("candidates_tried"),
            },
            "ORANSlice": {
                "commit": oran.get("commit"),
                "commit_short": oran.get("commit_short"),
                "tag": oran.get("tag"),
                "describe": oran.get("describe"),
                "path": oran.get("path") or oran.get("resolved_from"),
                "ok": oran.get("ok"),
                "error": oran.get("error"),
                "candidates_tried": oran.get("candidates_tried"),
            },
            "ok": bool(cn5g.get("ok") or oran.get("ok")),
            "error": None if (cn5g.get("ok") or oran.get("ok")) else (
                "neither oai-cn5g-fed nor ORANSlice resolved"
            ),
        },
    }


def read_topology_fs(run_dir: Path) -> dict[str, Any]:
    out = {
        "FS_GCS_ENABLE": None,
        "FS_GCS_TIMEOUT": None,
        "source": None,
        "error": None,
    }
    for name in ("topology.env", "fs_gcs.parm", "tm_gcs_failsafe.parm"):
        path = run_dir / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("FS_GCS_ENABLE"):
                parts = line.replace("=", " ").split()
                if len(parts) >= 2:
                    out["FS_GCS_ENABLE"] = parts[-1]
                    out["source"] = name
            if line.startswith("FS_GCS_TIMEOUT"):
                parts = line.replace("=", " ").split()
                if len(parts) >= 2:
                    out["FS_GCS_TIMEOUT"] = parts[-1]
                    out["source"] = name
    meta = run_dir / "run_meta.json"
    if meta.is_file():
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
        stamp = data.get("topology_stamp") or {}
        if isinstance(stamp, dict):
            for k in ("FS_GCS_ENABLE", "FS_GCS_TIMEOUT"):
                if stamp.get(k) is not None and out[k] is None:
                    out[k] = stamp.get(k)
                    out["source"] = out["source"] or "run_meta.topology_stamp"
    if out["FS_GCS_TIMEOUT"] is None:
        out["FS_GCS_TIMEOUT"] = "5"
        out["error"] = (
            (out.get("error") or "")
            + "FS_GCS_TIMEOUT not in run_dir; expected value = 5. "
        ).strip()
    if out["FS_GCS_ENABLE"] is None:
        out["FS_GCS_ENABLE"] = "1"
        out["error"] = (
            (out.get("error") or "")
            + "FS_GCS_ENABLE not in run_dir; expected value = 1."
        ).strip()
    return out


def read_sidecar_profile(run_dir: Path) -> Optional[str]:
    meta = run_dir / "sidecars" / "flyblind_sidecars_meta.json"
    if meta.is_file():
        data = json.loads(meta.read_text(encoding="utf-8"))
        return data.get("profile") or "LEGACY_FLAGS"
    run_meta = run_dir / "run_meta.json"
    if run_meta.is_file():
        data = json.loads(run_meta.read_text(encoding="utf-8"))
        return (data.get("sidecars") or {}).get("profile") or "off"
    return "off"


def hash_config_files(run_dir: Path) -> dict[str, Any]:
    configs = []
    for pattern in ("*.yaml", "*.yml", "*.parm", "*.env", "analysis_params.json", "topology.env"):
        for p in sorted(run_dir.glob(pattern)):
            if p.is_file():
                configs.append({"path": p.name, "sha256": sha256_file(p)})
    h = hashlib.sha256()
    for c in configs:
        h.update(f"{c['sha256']}  {c['path']}\n".encode())
    return {
        "n": len(configs),
        "sha256": h.hexdigest() if configs else None,
        "files": configs,
        "error": None if configs else "no config file in run_dir",
    }


def load_images_hash(run_dir: Path) -> dict[str, Any]:
    """Always returns a block with ok/error; reads image_digests.json if present."""
    path = run_dir / "image_digests.json"
    if not path.is_file():
        return {
            "ok": False,
            "sha256_of_imageIDs": None,
            "imageIDs": [],
            "n_unique_imageIDs": 0,
            "source": None,
            "error": (
                "image_digests.json missing in run_dir; "
                "orchestrate must call collect_image_digests at run start"
            ),
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {
            "ok": False,
            "sha256_of_imageIDs": None,
            "imageIDs": [],
            "n_unique_imageIDs": 0,
            "source": "image_digests.json",
            "error": f"invalid JSON: {exc}",
        }
    digests = data.get("digests") or {}
    ids = list(digests.get("imageIDs") or [])
    h = hashlib.sha256()
    for i in ids:
        h.update((i + "\n").encode("utf-8"))
    ok = bool(data.get("ok")) and bool(ids)
    return {
        "ok": ok,
        "sha256_of_imageIDs": h.hexdigest() if ids else None,
        "imageIDs": ids,
        "n_unique_imageIDs": len(ids),
        "source": "image_digests.json",
        "collector_error": data.get("error"),
        "error": None if ok else (
            data.get("error") or "image_digests.json without usable imageIDs"
        ),
        "raw_ok": data.get("ok"),
        "commands": data.get("commands"),
    }


def build_manifest(
    run_dir: Path,
    *,
    sidecar_profile: Optional[str] = None,
    scripts_tree: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    run_dir = Path(run_dir)
    fs = read_topology_fs(run_dir)
    tree = scripts_tree or sha256_tree(
        SCRIPTS_ROOT,
        suffixes={".py", ".sh", ".md", ".json", ".yaml", ".yml", ".parm", ".txt", ""},
        exclude_dirs={VENDOR_DIR},
    )
    versions = collect_versions()
    images = load_images_hash(run_dir)
    return {
        "schema": "flyblind_run_manifest_v2",
        "run_dir": str(run_dir),
        "commit_git": None,
        "commit_git_note": (
            "not available: there is no git repository in this artifacts tree; "
            "replaced by scripts_tree_sha256 of the artifacts/01_scripts folder"
        ),
        "scripts_tree_sha256": tree.get("sha256"),
        "scripts_tree": {
            "root": tree.get("root"),
            "n_files": tree.get("n_files"),
            "sha256": tree.get("sha256"),
        },
        "configs_hash": hash_config_files(run_dir),
        "images_hash": images,
        "FS_GCS_ENABLE": fs.get("FS_GCS_ENABLE"),
        "FS_GCS_TIMEOUT": fs.get("FS_GCS_TIMEOUT"),
        "FS_GCS_source": fs.get("source"),
        "FS_GCS_error": fs.get("error"),
        "sidecar_profile": sidecar_profile or read_sidecar_profile(run_dir),
        "versions": versions,
        "ardupilot_sitl": versions["ardupilot_sitl"],
        "oai": versions["oai"],
    }


def write_manifest(run_dir: Path, manifest: dict[str, Any] | None = None) -> Path:
    run_dir = Path(run_dir)
    man = manifest or build_manifest(run_dir)
    path = run_dir / "run_manifest.json"
    path.write_text(json.dumps(man, indent=2) + "\n", encoding="utf-8")
    return path


def main() -> None:
    ap = argparse.ArgumentParser(description="Per-run manifest: image digests, source revisions and stamps (run provenance)")
    ap.add_argument("--run-dir", type=Path, required=True)
    ap.add_argument("--sidecar-profile", default="")
    ap.add_argument("--hash-scripts-only", action="store_true")
    ap.add_argument("--print-versions-only", action="store_true",
                    help="Only print the git tree versions (bench)")
    args = ap.parse_args()
    if args.hash_scripts_only:
        tree = sha256_tree(
            SCRIPTS_ROOT,
            suffixes={".py", ".sh", ".md", ".json", ".yaml", ".yml", ".parm", ".txt", ""},
            exclude_dirs={VENDOR_DIR},
        )
        print(json.dumps({"scripts_tree_sha256": tree["sha256"], "n_files": tree["n_files"]}, indent=2))
        return
    if args.print_versions_only:
        print(json.dumps(collect_versions(), indent=2))
        return
    man = build_manifest(
        args.run_dir,
        sidecar_profile=args.sidecar_profile or None,
    )
    path = write_manifest(args.run_dir, man)
    print(f"[manifest] → {path}")
    print(json.dumps({
        "commit_git": man["commit_git"],
        "scripts_tree_sha256": man["scripts_tree_sha256"],
        "sidecar_profile": man["sidecar_profile"],
        "FS_GCS_TIMEOUT": man["FS_GCS_TIMEOUT"],
        "images_ok": man["images_hash"].get("ok"),
        "images_error": man["images_hash"].get("error"),
        "ardupilot_ok": man["ardupilot_sitl"].get("ok"),
        "ardupilot_commit": man["ardupilot_sitl"].get("commit_short"),
        "ardupilot_tag": man["ardupilot_sitl"].get("tag"),
        "oai_cn5g_ok": man["oai"]["oai_cn5g_fed"].get("ok"),
        "oran_ok": man["oai"]["ORANSlice"].get("ok"),
    }, indent=2))


if __name__ == "__main__":
    main()
