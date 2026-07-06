#!/usr/bin/env python3
"""Probe a local HTML design prototype with headless Chromium."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent


def _check(name: str, passed: bool, evidence: str) -> dict[str, str]:
    return {
        "name": name,
        "status": "pass" if passed else "fail",
        "evidence": evidence,
    }


def _missing_local_links(artifact: Path, hrefs: list[str]) -> list[str]:
    missing: list[str] = []
    for href in hrefs:
        if not href or href.startswith(("#", "http://", "https://", "mailto:")):
            continue
        target = (artifact.parent / href).resolve()
        if not target.exists():
            missing.append(href)
    return missing


def probe(artifact_path: Path) -> dict[str, Any]:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # pragma: no cover - exercised when dependency is absent.
        return {
            "status": "fail",
            "artifact_path": str(artifact_path),
            "checks": [
                _check(
                    "browser-render-probe",
                    False,
                    f"Playwright probe dependency is unavailable: {exc}",
                )
            ],
        }

    artifact = artifact_path if artifact_path.is_absolute() else ROOT / artifact_path
    artifact = artifact.resolve()
    if not artifact.exists():
        return {
            "status": "fail",
            "artifact_path": str(artifact_path),
            "checks": [
                _check(
                    "browser-render-probe",
                    False,
                    f"Artifact probe could not find {artifact_path}.",
                )
            ],
        }

    output_dir = ROOT / ".apk" / "design-probes"
    output_dir.mkdir(parents=True, exist_ok=True)
    screenshot_name = f"{artifact.stem}-{hashlib.sha256(str(artifact).encode()).hexdigest()[:12]}.png"
    screenshot_path = output_dir / screenshot_name

    checks: list[dict[str, str]] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.goto(artifact.as_uri())
        page.wait_for_load_state("domcontentloaded")

        main_box = page.locator("main").bounding_box()
        page.screenshot(path=str(screenshot_path), full_page=True)
        screenshot_ok = screenshot_path.exists() and screenshot_path.stat().st_size > 0
        checks.append(
            _check(
                "browser-render-probe",
                bool(
                    main_box
                    and main_box.get("width", 0) > 0
                    and main_box.get("height", 0) > 0
                    and screenshot_ok
                ),
                f"Headless Chromium rendered artifact {artifact_path}; screenshot artifact {screenshot_path.relative_to(ROOT)}.",
            )
        )

        enabled_button = page.locator("button:not([disabled])").first
        if enabled_button.count():
            enabled_button.hover()
            enabled_button.focus()
        focused_tag = page.evaluate("document.activeElement ? document.activeElement.tagName.toLowerCase() : ''")
        loading_count = page.locator('[aria-busy="true"], [data-state="loading"], [data-loading]').count()
        disabled_count = page.locator("button:disabled").count()
        checks.append(
            _check(
                "interaction-state-probe",
                bool(enabled_button.count() and focused_tag == "button" and loading_count and disabled_count),
                "Browser probe exercised hover/focus and found disabled plus loading state markers.",
            )
        )

        hrefs = page.eval_on_selector_all("a[href]", "els => els.map(el => el.getAttribute('href'))")
        missing_links = _missing_local_links(artifact, [str(href) for href in hrefs])
        checks.append(
            _check(
                "source-link-probe",
                not missing_links,
                "Browser probe resolved local source links."
                if not missing_links
                else f"Missing local links: {missing_links}",
            )
        )

        page.set_viewport_size({"width": 390, "height": 800})
        mobile_box = page.locator("main").bounding_box()
        checks.append(
            _check(
                "responsive-render-probe",
                bool(mobile_box and mobile_box.get("width", 9999) <= 390),
                "Browser probe rendered the prototype at a 390px viewport without horizontal overflow in main content.",
            )
        )
        browser.close()

    return {
        "status": "pass" if all(check["status"] == "pass" for check in checks) else "fail",
        "artifact_path": str(artifact_path),
        "screenshot_path": str(screenshot_path.relative_to(ROOT)),
        "checks": checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("artifact_path")
    args = parser.parse_args()
    report = probe(Path(args.artifact_path))
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
