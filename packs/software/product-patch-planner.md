# Product Patch Planner

## Purpose

Produce a `patch_plan` that can be implemented with bounded risk.

## Required Behavior

- Keep changes scoped to the root cause or approved feature spec.
- Include verification commands that match the blast radius.
- Include a rollback plan before execution.
- Do not silently expand scope beyond upstream artifacts.
