#!/usr/bin/env python3
##################################################################################################################################
# Module: collect_image_digests.py (Collects the pods' image digests via kubectl (run provenance))                               #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: At run start it writes run_dir/image_digests.json. --dry-run mode: prints the commands without running them.      #
##################################################################################################################################
"""Collects the pods' image digests via kubectl (run provenance).

At run start it writes run_dir/image_digests.json.
--dry-run mode: prints the commands without running them.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


DEFAULT_DEPLOYS = (
    "oai-gnb",
    "oai-ue-uav",
    "oai-ue-gcs",
    "oai-amf",
    "oai-smf",
    "oai-upf",
)


def kubectl_get_pods_cmd(ctx: str, namespace: str) -> list[str]:
    return [
        "kubectl", "--context", ctx, "get", "pods",
        "-n", namespace,
        "-o", "json",
    ]


def kubectl_get_deploy_cmd(ctx: str, namespace: str, deploy: str) -> list[str]:
    return [
        "kubectl", "--context", ctx, "get", "deploy", deploy,
        "-n", namespace,
        "-o", "json",
    ]


def extract_digests_from_pod_list(payload: dict[str, Any]) -> dict[str, Any]:
    """Parse kubectl get pods -o json → image / imageID per container."""
    items = payload.get("items") or []
    pods: list[dict[str, Any]] = []
    digest_set: set[str] = set()
    for item in items:
        meta = item.get("metadata") or {}
        status = item.get("status") or {}
        containers = []
        for st in (status.get("containerStatuses") or []) + (status.get("initContainerStatuses") or []):
            image = st.get("image")
            image_id = st.get("imageID")
            if image_id:
                digest_set.add(str(image_id))
            containers.append({
                "name": st.get("name"),
                "image": image,
                "imageID": image_id,
                "ready": st.get("ready"),
            })
        pods.append({
            "name": meta.get("name"),
            "labels": meta.get("labels") or {},
            "phase": status.get("phase"),
            "nodeName": (item.get("spec") or {}).get("nodeName"),
            "containers": containers,
        })
    return {
        "n_pods": len(pods),
        "n_unique_imageIDs": len(digest_set),
        "imageIDs": sorted(digest_set),
        "pods": pods,
    }


def _namespace_list(namespace: str, core_namespace: str | None) -> list[str]:
    """ran + core, dedup, preserving order; ignores empty values."""
    out: list[str] = []
    for ns in (namespace, core_namespace):
        ns = (ns or "").strip()
        if ns and ns not in out:
            out.append(ns)
    return out


def _collect_one(context: str, namespace: str) -> dict[str, Any]:
    """Collects one namespace. Returns a block with ok/error always set."""
    cmd = kubectl_get_pods_cmd(context, namespace)
    block: dict[str, Any] = {
        "namespace": namespace,
        "command": " ".join(cmd),
        "ok": False,
        "error": None,
        "digests": None,
    }
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        block["error"] = f"kubectl failed to run: {exc}"
        return block
    if r.returncode != 0:
        block["error"] = (
            f"kubectl rc={r.returncode}: {(r.stderr or r.stdout or '').strip()[:500]}"
        )
        return block
    try:
        payload = json.loads(r.stdout)
    except json.JSONDecodeError as exc:
        block["error"] = f"invalid JSON from kubectl: {exc}"
        return block
    digests = extract_digests_from_pod_list(payload)
    block["digests"] = digests
    if digests["n_unique_imageIDs"] == 0:
        block["error"] = (
            "kubectl answered but no imageID was found "
            "(pods without containerStatuses? empty namespace?)"
        )
        return block
    block["ok"] = True
    return block


def collect(
    *,
    context: str,
    namespace: str,
    core_namespace: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Collects ran + core digests and merges them into one image_digests.json.

    Kept compatible: `namespace` alone is still valid (core_namespace=None).
    """
    namespaces = _namespace_list(namespace, core_namespace)
    out: dict[str, Any] = {
        "schema": "flyblind_image_digests_v2",
        "context": context,
        "namespaces": namespaces,
        "namespace": namespace,            # legacy: primary namespace (ran)
        "core_namespace": core_namespace,
        "commands": [],
        "dry_run": bool(dry_run),
        "ok": False,
        "error": None,
        "per_namespace": {},
        "digests": None,
    }
    for ns in namespaces:
        out["commands"].append(" ".join(kubectl_get_pods_cmd(context, ns)))

    if dry_run:
        for ns in namespaces:
            for d in DEFAULT_DEPLOYS:
                out["commands"].append(" ".join(kubectl_get_deploy_cmd(context, ns, d)))
        out["error"] = "dry-run: commands listed; digests not collected"
        return out

    union: set[str] = set()
    pods: list[dict[str, Any]] = []
    errors: list[str] = []
    any_ok = False
    for ns in namespaces:
        block = _collect_one(context, ns)
        out["per_namespace"][ns] = block
        if block.get("ok"):
            any_ok = True
            d = block["digests"] or {}
            union.update(d.get("imageIDs") or [])
            for p in d.get("pods") or []:
                p = dict(p)
                p["namespace"] = ns
                pods.append(p)
        elif block.get("error"):
            errors.append(f"{ns}: {block['error']}")

    out["digests"] = {
        "n_pods": len(pods),
        "n_unique_imageIDs": len(union),
        "imageIDs": sorted(union),
        "pods": pods,
    }
    out["ok"] = any_ok and len(union) > 0
    # Never silently empty: records the reason per namespace.
    out["error"] = None if out["ok"] else ("; ".join(errors) or "no imageID collected")
    return out


def write_image_digests(run_dir: Path, payload: dict[str, Any]) -> Path:
    path = Path(run_dir) / "image_digests.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def main() -> None:
    ap = argparse.ArgumentParser(description="Collect image digests via kubectl (run provenance)")
    ap.add_argument("--context", default="kind-tm-oranslice")
    ap.add_argument("--namespace", default="ran")
    ap.add_argument("--core-namespace", default="core",
                    help="core namespace (amf/smf/upf); '' to disable")
    ap.add_argument("--out-dir", type=Path, default=None,
                    help="Run directory (writes image_digests.json)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    payload = collect(
        context=args.context,
        namespace=args.namespace,
        core_namespace=args.core_namespace,
        dry_run=args.dry_run,
    )
    if args.out_dir:
        path = write_image_digests(args.out_dir, payload)
        print(f"[images] → {path}", file=sys.stderr)
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
