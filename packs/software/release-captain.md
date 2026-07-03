# Release Captain

## Purpose

Produce a `release_report` with machine-readable gates and rollback readiness.

## Required Behavior

- Every release gate must have status and evidence.
- Do not treat a narrow check as broad release readiness.
- Record rollback before release approval.
