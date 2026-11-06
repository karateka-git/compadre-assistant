"""Читает полный exact-SHA CI и проверяет compatibility artifact без VPS."""

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys


CANDIDATE = "cabc0e24d28947123868db8a60f6b55095feb63e"
PREVIOUS = "dd71c2ba42a0744b5b654cedcf73009ce8879058"
RUN = 42249037867
POLICY = "eb1dad4fee8a176ec4870db12da5e1051a3d1c6e"
REPOSITORY = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY))


def collect():
    """Проверяет Git bytes verifier и live evidence, возвращая только metadata.

    Token gh читается только в память процесса и не сохраняется в результате.
    Это CI admission, не опубликованный Release и не image/snapshot допуск.
    """
    from scripts import controller_snapshot

    subprocess.run(["git", "status", "--short"], cwd=REPOSITORY, check=True, capture_output=True)
    names = subprocess.run(["git", "ls-tree", "-r", "--name-only", CANDIDATE, "--", "scripts", "deploy",
                            *controller_snapshot.DEPENDENCIES], cwd=REPOSITORY, check=True,
                           capture_output=True, text=True).stdout.splitlines()
    for name in names:
        expected = subprocess.run(["git", "show", f"{CANDIDATE}:{name}"], cwd=REPOSITORY,
                                  check=True, capture_output=True).stdout
        if (REPOSITORY / name).read_bytes().replace(b"\r\n", b"\n") != expected:
            raise ValueError("Verifier checkout не совпадает с candidate Git bytes")
    from scripts.predeploy_compatibility import EvidenceAPI, verify_evidence
    from scripts.select_deploy_release import SelectedRelease

    external_pin = subprocess.run(["gh", "variable", "get", "COMPATIBILITY_POLICY_SHA"], cwd=REPOSITORY,
                                  check=True, capture_output=True, text=True).stdout.strip()
    if external_pin != POLICY:
        raise ValueError("Reviewed external policy pin изменился")
    token = subprocess.run(["gh", "auth", "token"], check=True, capture_output=True, text=True).stdout.strip()
    api = EvidenceAPI("karateka-git/compadre-assistant", token)
    # Stage7 не выдаёт несуществующий GitHub Release за опубликованный.
    # verify_evidence использует exact SHA/workflow/run и previous baseline;
    # version/release_id здесь не являются release admission.
    selected = SelectedRelease("karateka-git/compadre-assistant", "stage7-model", "main", CANDIDATE,
                               0, "main-full.yml", RUN, 1)
    evidence = verify_evidence(api, selected, POLICY, PREVIOUS)
    run = api.get(f"/actions/runs/{RUN}/attempts/1")
    if run["head_sha"] != CANDIDATE or run["conclusion"] != "success" or run["status"] != "completed":
        raise ValueError("CI изменился после проверки evidence")
    return {"checked_utc": datetime.now(timezone.utc).isoformat(), "repository": selected.repository,
            "candidate_sha": CANDIDATE, "previous_sha": PREVIOUS, "run_id": RUN, "run_attempt": 1,
            "workflow": "main-full.yml", "full_ci_success": True, "release_verified": False,
            "image_verified": False, "snapshot_verified": False, **evidence}


if __name__ == "__main__":
    print(json.dumps(collect(), sort_keys=True), flush=True)
