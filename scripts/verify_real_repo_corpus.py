#!/usr/bin/env python3
"""Verify and summarize the real repository bug corpus."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from apkernel import SchemaRegistry, build_real_repo_corpus_report


def _display_path(path: Path) -> str:
    return str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()

    schemas = SchemaRegistry(ROOT / "schemas" / "artifacts").load()
    manifest_path = args.manifest
    if manifest_path is not None and not manifest_path.is_absolute():
        manifest_path = ROOT / manifest_path
    report = build_real_repo_corpus_report(
        ROOT,
        schemas,
        manifest_path,
        producer_command="python3 scripts/verify_real_repo_corpus.py",
    )
    run_id = dt.datetime.now(dt.timezone.utc).strftime("corpus-%Y%m%dT%H%M%SZ")
    output_dir = args.output_dir or ROOT / ".apk" / "real-repo-corpus" / run_id
    if not output_dir.is_absolute():
        output_dir = ROOT / output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "real_repo_corpus_report.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    current_report_path = None
    if args.output_dir is None:
        current_dir = ROOT / ".apk" / "real-repo-corpus" / "current"
        current_dir.mkdir(parents=True, exist_ok=True)
        current_report_path = current_dir / "real_repo_corpus_report.json"
        current_report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    summary = {
        "status": "pass",
        "report": _display_path(report_path),
        "current_report": _display_path(current_report_path) if current_report_path is not None else None,
        "target_met": report["summary"]["target_met"],
        "approval_status": report["approval_boundary"]["status"],
        "approval_required": report["approval_boundary"]["required"],
        "stop_reason": report["approval_boundary"]["reason"],
        "producer_roundtrip_status": report["freshness"]["producer_roundtrip_status"],
        "artifact_source": report["freshness"]["artifact_source"],
        "external_execution": report["freshness"]["external_execution"],
        "live_artifact_count": report["freshness"]["live_artifact_count"],
        "fresh_live_artifact_count": report["freshness"]["fresh_live_artifact_count"],
        "stale_live_artifact_count": report["freshness"]["stale_live_artifact_count"],
        "non_author_repo_count": report["summary"]["non_author_repo_count"],
        "failure_family_count": report["summary"]["failure_family_count"],
        "missing_non_author_repos": report["summary"]["missing_non_author_repos"],
        "missing_failure_families": report["summary"]["missing_failure_families"],
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
