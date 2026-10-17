"""Публикует краткий итог полного CI для точного commit."""

from __future__ import annotations

import json
import os
from pathlib import Path


# Только полный успех всех обязательных проверок может создать success.
CHECKS = ("checkout", "unit", "smokes", "migrations", "candidate")


def main() -> None:
    """Пишет JSON-манифест и job summary из итогов обязательных шагов."""
    checks = {name: os.environ.get(f"CHECK_{name.upper()}", "unknown") for name in CHECKS}
    manifest = {
        "repository": os.environ.get("GITHUB_REPOSITORY", ""),
        "ref": os.environ.get("GITHUB_REF", ""),
        "sha": os.environ.get("GITHUB_SHA", ""),
        "previous_sha": os.environ.get("PREVIOUS_SHA", ""),
        "workflow_ref": os.environ.get("GITHUB_WORKFLOW_REF", ""),
        "run_id": os.environ.get("GITHUB_RUN_ID", ""),
        "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", ""),
        "checks": checks,
        "result": "success" if all(value == "success" for value in checks.values()) else "failure",
    }
    destination = Path(os.environ["RUNNER_TEMP"]) / "full-suite-manifest.json"
    destination.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary = Path(os.environ["GITHUB_STEP_SUMMARY"])
    lines = [
        "## Full CI manifest",
        "",
        f"- Repository: `{manifest['repository']}`",
        f"- Ref: `{manifest['ref']}`",
        f"- SHA: `{manifest['sha']}`",
        f"- Previous SHA: `{manifest['previous_sha']}`",
        f"- Workflow: `{manifest['workflow_ref']}`",
        f"- Run: `{manifest['run_id']}` attempt `{manifest['run_attempt']}`",
        f"- Result: **{manifest['result']}**",
        "",
        "| Check | Result |",
        "| --- | --- |",
        *(f"| {name} | {value} |" for name, value in checks.items()),
        "",
    ]
    with summary.open("a", encoding="utf-8") as output:
        output.write("\n".join(lines))
    print(f"Full CI manifest: {destination} ({manifest['result']})")


if __name__ == "__main__":
    main()
