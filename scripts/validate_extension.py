#!/usr/bin/env python3
"""Static checks on the Chrome extension before loading it unpacked.

Chrome reports a bad manifest or a missing file only once you try to load the
extension, and a stale reference inside the offscreen document may not surface
until mid-call. This catches those cheaply, and also enforces two project
rules: no broad permissions, and no credential-shaped literal anywhere in the
extension (the provider key must stay server-side).

Usage:  python scripts/validate_extension.py [extension_dir]
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

BANNED_PERMISSIONS = ("tabs", "microphone", "camera", "audioCapture", "videoCapture")
BANNED_HOSTS = ("<all_urls>", "*://*/*", "http://*/*", "https://*/*")
SECRET_PATTERNS = (
    r"RESEMBLE_API_KEY",
    r"Bearer\s+\S+",
    r"api[_-]?key\s*[:=]\s*['\"]\S",
)


def main(argv: list[str]) -> int:
    root = pathlib.Path(argv[1] if len(argv) > 1 else "extension")
    if not root.is_dir():
        print(f"FAIL: {root} is not a directory")
        return 1

    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        print(f"FAIL: {manifest_path} is missing")
        return 1

    try:
        manifest = json.loads(manifest_path.read_text())
    except ValueError as exc:
        print(f"FAIL: manifest.json is not valid JSON: {exc}")
        return 1

    problems: list[str] = []
    checked = 0

    def check(rel: str, origin: str) -> None:
        nonlocal checked
        checked += 1
        if not (root / rel).is_file():
            problems.append(f"{origin}: missing file {rel!r}")

    # --- manifest structure ---------------------------------------------
    if manifest.get("manifest_version") != 3:
        problems.append("manifest_version must be 3")

    background = manifest.get("background", {})
    if "service_worker" in background:
        check(background["service_worker"], "background.service_worker")
    else:
        problems.append("no background.service_worker declared")

    for index, script in enumerate(manifest.get("content_scripts", [])):
        for rel in script.get("js", []) + script.get("css", []):
            check(rel, f"content_scripts[{index}]")
        for match in script.get("matches", []):
            if match in BANNED_HOSTS:
                problems.append(f"over-broad content_scripts match {match!r}")

    action = manifest.get("action", {})
    if "default_popup" in action:
        check(action["default_popup"], "action.default_popup")
    for size, path in (action.get("default_icon") or {}).items():
        check(path, f"action.default_icon[{size}]")
    for size, path in (manifest.get("icons") or {}).items():
        check(path, f"icons[{size}]")

    # --- permission hygiene ----------------------------------------------
    permissions = manifest.get("permissions", [])
    hosts = manifest.get("host_permissions", [])
    for banned in BANNED_PERMISSIONS:
        if banned in permissions:
            problems.append(f"unnecessary permission {banned!r}")
    for banned in BANNED_HOSTS:
        if banned in hosts:
            problems.append(f"over-broad host permission {banned!r}")

    # --- file references --------------------------------------------------
    for html in sorted(root.glob("*.html")):
        for ref in re.findall(r'(?:src|href)="([^"]+)"', html.read_text()):
            if ref.startswith(("http://", "https://", "data:", "#")):
                continue
            check(ref, html.name)

    for js in sorted(root.glob("*.js")):
        text = js.read_text()
        for ref in re.findall(r"getURL\(['\"]([^'\"]+)['\"]\)", text):
            check(ref, f"{js.name} getURL")
        for ref in re.findall(r"addModule\(['\"]([^'\"]+)['\"]\)", text):
            check(ref, f"{js.name} addModule")

    service_worker = (root / "service-worker.js")
    if service_worker.is_file():
        match = re.search(r"OFFSCREEN_PATH\s*=\s*['\"]([^'\"]+)['\"]", service_worker.read_text())
        if match:
            check(match.group(1), "service-worker OFFSCREEN_PATH")

    # --- worklet module load order ----------------------------------------
    offscreen = root / "offscreen.js"
    if offscreen.is_file():
        text = offscreen.read_text()
        dsp_at = text.find("addModule('audio-dsp.js')")
        worklet_at = text.find("addModule('audio-worklet.js')")
        if not (0 <= dsp_at < worklet_at):
            problems.append(
                "audio-dsp.js must be addModule()'d before audio-worklet.js: "
                "worklet modules share one global scope and cannot import"
            )

    # --- no credentials in the extension ----------------------------------
    for path in sorted(
        list(root.glob("*.js")) + list(root.glob("*.html")) + list(root.glob("*.json"))
    ):
        text = path.read_text()
        for pattern in SECRET_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                problems.append(
                    f"{path.name}: credential-shaped literal matching {pattern!r}"
                )

    print(f"checked {checked} file references in {root}")
    print(f"permissions:      {permissions}")
    print(f"host_permissions: {hosts}")

    if problems:
        print("\nPROBLEMS:")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    print("\nOK: manifest, references, permissions and secret scan are clean")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
