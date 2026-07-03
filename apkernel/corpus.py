"""Real repository bug corpus reporting."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from .core import ContractError, SchemaRegistry, load_json, validate_artifact_semantics


DEFAULT_CORPUS_MANIFEST = "examples/real_repo_corpus_manifest.json"
DEFAULT_LIVE_RERUN_WINDOW_HOURS = 24.0


def build_real_repo_corpus_report(
    root: Path,
    schemas: SchemaRegistry,
    manifest_path: Path | None = None,
    *,
    producer_command: str = "apkernel.corpus.build_real_repo_corpus_report",
    now: dt.datetime | None = None,
    live_rerun_window_hours: float = DEFAULT_LIVE_RERUN_WINDOW_HOURS,
) -> dict[str, Any]:
    if live_rerun_window_hours <= 0:
        raise ContractError("live_rerun_window_hours must be positive")
    now_utc = _utc(now or dt.datetime.now(dt.timezone.utc))
    live_cutoff = now_utc - dt.timedelta(hours=live_rerun_window_hours)
    manifest_file = manifest_path or root / DEFAULT_CORPUS_MANIFEST
    manifest = load_json(manifest_file)
    if manifest.get("version") != "1.0":
        raise ContractError("real repo corpus manifest version must be 1.0")
    entries = manifest.get("entries")
    if not isinstance(entries, list):
        raise ContractError("real repo corpus manifest must define entries[]")
    target_repo_count = _positive_int(manifest, "target_repo_count")
    target_failure_family_count = _positive_int(manifest, "target_failure_family_count")

    report_entries: list[dict[str, Any]] = []
    repo_urls: set[str] = set()
    non_author_repo_urls: set[str] = set()
    failure_families: set[str] = set()
    artifact_files: set[str] = set()
    run_ids: set[str] = set()
    live_artifact_count = 0
    fresh_live_artifact_count = 0
    stale_live_artifact_count = 0
    live_candidates: list[dict[str, Any] | None] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ContractError(f"real repo corpus entry {index} must be an object")
        artifact_file = _required_string(entry, "artifact_file", f"entry {index}")
        failure_family = _required_string(entry, "failure_family", f"entry {index}")
        if artifact_file in artifact_files:
            raise ContractError(f"entry {index} duplicates artifact_file {artifact_file!r}")
        artifact_files.add(artifact_file)
        author_owned = entry.get("author_owned")
        if not isinstance(author_owned, bool):
            raise ContractError(f"entry {index} author_owned must be boolean")
        expected_run_id = entry.get("run_id")
        live_artifact = (
            root / ".apk" / "real-bug-runs" / expected_run_id / "real_repo_bug_run.json"
            if isinstance(expected_run_id, str)
            else None
        )
        if live_artifact is not None and live_artifact.exists():
            live_candidate = load_json(live_artifact)
            live_artifact_count += 1
            if _is_fresh_live_bug_run(live_candidate, live_cutoff):
                fresh_live_artifact_count += 1
            else:
                stale_live_artifact_count += 1
            live_candidates.append(live_candidate)
        else:
            live_candidates.append(None)

    live_external_rerun = (
        live_artifact_count == len(entries)
        and fresh_live_artifact_count == len(entries)
        and bool(entries)
    )
    selected_command_timestamps: list[dt.datetime] = []
    for index, entry in enumerate(entries):
        artifact_file = _required_string(entry, "artifact_file", f"entry {index}")
        failure_family = _required_string(entry, "failure_family", f"entry {index}")
        author_owned = entry.get("author_owned")
        expected_run_id = entry.get("run_id")
        if live_external_rerun and live_candidates[index] is not None:
            bug_run = live_candidates[index]
        else:
            bug_run = load_json(root / artifact_file)
        schemas.validate("real_repo_bug_run", bug_run)
        validate_artifact_semantics("real_repo_bug_run", bug_run)
        expected_repo_url = entry.get("repo_url")
        if isinstance(expected_repo_url, str) and expected_repo_url != bug_run["repo_url"]:
            raise ContractError(f"entry {index} repo_url does not match artifact")
        if isinstance(expected_run_id, str) and expected_run_id != bug_run["run_id"]:
            raise ContractError(f"entry {index} run_id does not match artifact")
        run_id = bug_run["run_id"]
        if run_id in run_ids:
            raise ContractError(f"entry {index} duplicates run_id {run_id!r}")
        run_ids.add(run_id)
        selected_command_timestamps.extend(_command_timestamps(bug_run))

        repo_urls.add(bug_run["repo_url"])
        failure_families.add(failure_family)
        if not author_owned:
            non_author_repo_urls.add(bug_run["repo_url"])
        failing_timestamp = _required_command_timestamp(bug_run, "failing_command")
        passing_timestamp = _required_command_timestamp(bug_run, "passing_command")
        report_entries.append(
            {
                "run_id": run_id,
                "repo_url": bug_run["repo_url"],
                "artifact_file": artifact_file,
                "failure_family": failure_family,
                "author_owned": author_owned,
                "bug_commit": bug_run["bug_commit"],
                "fix_commit": bug_run["fix_commit"],
                "failing_command_timestamp": failing_timestamp,
                "passing_command_timestamp": passing_timestamp,
                "status": "verified",
            }
        )

    non_author_repo_count = len(non_author_repo_urls)
    failure_family_count = len(failure_families)
    missing_non_author_repos = max(0, target_repo_count - non_author_repo_count)
    missing_failure_families = max(0, target_failure_family_count - failure_family_count)
    target_met = missing_non_author_repos == 0 and missing_failure_families == 0
    report = {
        "version": "1.0",
        "corpus_id": _required_string(manifest, "corpus_id", "manifest"),
        "target_repo_count": target_repo_count,
        "target_failure_family_count": target_failure_family_count,
        "entries": report_entries,
        "summary": {
            "total_repo_count": len(repo_urls),
            "non_author_repo_count": non_author_repo_count,
            "failure_family_count": failure_family_count,
            "target_met": target_met,
            "missing_non_author_repos": missing_non_author_repos,
            "missing_failure_families": missing_failure_families,
        },
        "freshness": {
            "producer_roundtrip_status": "pass",
            "producer_command": producer_command,
            "artifact_source": "live_external_rerun" if live_external_rerun else "checked_in_manifest_artifacts",
            "external_execution": live_external_rerun,
            "fresh_artifact_count": live_artifact_count if live_external_rerun else len(report_entries),
            "unique_artifact_count": len(artifact_files),
            "live_artifact_count": live_artifact_count,
            "fresh_live_artifact_count": fresh_live_artifact_count,
            "stale_live_artifact_count": stale_live_artifact_count,
            "freshness_window_hours": live_rerun_window_hours,
            "oldest_command_timestamp": _format_timestamp(min(selected_command_timestamps))
            if selected_command_timestamps
            else "unknown",
            "latest_command_timestamp": _format_timestamp(max(selected_command_timestamps))
            if selected_command_timestamps
            else "unknown",
        },
        "approval_boundary": _approval_boundary(
            target_met,
            missing_non_author_repos,
            missing_failure_families,
        ),
    }
    schemas.validate("real_repo_corpus_report", report)
    validate_artifact_semantics("real_repo_corpus_report", report)
    return report


def _positive_int(raw: dict[str, Any], field: str) -> int:
    value = raw.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ContractError(f"{field} must be a positive integer")
    return value


def _required_string(raw: dict[str, Any], field: str, context: str) -> str:
    value = raw.get(field)
    if not isinstance(value, str) or not value:
        raise ContractError(f"{context} {field} must be a non-empty string")
    return value


def _utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def _parse_timestamp(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _utc(parsed)


def _command_timestamps(bug_run: Any) -> list[dt.datetime]:
    if not isinstance(bug_run, dict):
        return []
    timestamps: list[dt.datetime] = []
    for key in ("failing_command", "passing_command"):
        command = bug_run.get(key)
        if not isinstance(command, dict):
            continue
        parsed = _parse_timestamp(command.get("timestamp"))
        if parsed is not None:
            timestamps.append(parsed)
    return timestamps


def _is_fresh_live_bug_run(bug_run: dict[str, Any], cutoff: dt.datetime) -> bool:
    timestamps = _command_timestamps(bug_run)
    return len(timestamps) == 2 and min(timestamps) >= cutoff


def _format_timestamp(value: dt.datetime) -> str:
    return _utc(value).isoformat()


def _required_command_timestamp(bug_run: dict[str, Any], command_key: str) -> str:
    command = bug_run.get(command_key)
    if not isinstance(command, dict):
        raise ContractError(f"{command_key} must be an object")
    timestamp = command.get("timestamp")
    if not isinstance(timestamp, str) or _parse_timestamp(timestamp) is None:
        raise ContractError(f"{command_key}.timestamp must be ISO-8601")
    return timestamp


def _approval_boundary(
    target_met: bool,
    missing_non_author_repos: int,
    missing_failure_families: int,
) -> dict[str, Any]:
    if target_met:
        return {
            "required": False,
            "status": "not_required",
            "reason": "The checked corpus already meets the public non-author repo and failure-family targets.",
            "requested_actions": [],
            "prohibited_without_approval": [],
        }
    return {
        "required": True,
        "status": "awaiting_human",
        "reason": (
            "Expanding the corpus requires selecting and operating on public external repositories, "
            "which is outside the local verification boundary."
        ),
        "requested_actions": [
            f"select {missing_non_author_repos} additional public non-author repositories",
            f"cover {missing_failure_families} additional failure families",
            "clone or inspect each selected repository only after explicit approval",
            "preserve each verified run as a real_repo_bug_run artifact",
        ],
        "prohibited_without_approval": [
            "selecting third-party repositories",
            "cloning third-party repositories",
            "running third-party repository test suites",
            "using credentials or paid network services",
            "opening pull requests or pushing branches",
        ],
    }
