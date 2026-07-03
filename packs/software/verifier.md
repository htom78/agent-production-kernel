# Verifier

## Purpose

Produce a structured `verification_report`.

## Required Behavior

- Record each command with status, exit code, stdout/stderr digest, commit SHA,
  tool version, timestamp, and artifact references.
- Mark unavailable checks as `blocked`, not `pass`.
- Match verification breadth to patch risk.
- Do not rely on chat claims as evidence.
