"""Batch runner: run a file or folder of JSON test inputs against the API; write every result to one folder.

  python -m cli.batch_runner samples/ --out results/ --api http://localhost:8000 --token change-me-admin-token

Each input file: {"name": "...", "destination": {catalogue row}  (or "destination_id": "..."),
                  "action": "publish" | "certify", "rendition": {...}, "expect": "succeeded" | "failed:CODE"}
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import httpx


def _key(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]


def run_case(cl: httpx.Client, case: dict, name: str, run_id: str) -> dict:
    out: dict = {"name": name, "action": case.get("action", "publish")}
    dest_id = case.get("destination_id")
    if case.get("destination"):
        r = cl.post("/api/v1/destinations", json=case["destination"], headers={"Idempotency-Key": _key("dest", run_id, name)})
        out["destination_response"] = r.json()
        if r.status_code >= 400:
            out.update(status="error", ok=False)
            return out
        dest_id = r.json()["id"]
    for sim in case.get("faults", []):  # optional: {"instance": "ghost", "rename": true}
        cl.post(f"/api/v1/simulation/faults/{sim['instance']}", json={k: v for k, v in sim.items() if k != "instance"})
    if case.get("action") == "certify":
        r = cl.post(f"/api/v1/destinations/{dest_id}/certify", headers={"Idempotency-Key": _key("cert", run_id, name)})
    else:
        r = cl.post("/api/v1/publish", json={"destination_id": dest_id, "rendition": case["rendition"]},
                    headers={"Idempotency-Key": _key("pub", run_id, name)})
    out["submit_status_code"] = r.status_code
    if r.status_code != 202:
        out.update(response=r.json(), status="error", ok=False)
        return out
    job_id = r.json()["job_id"]
    j: dict = {}
    for _ in range(300):
        j = cl.get(f"/api/v1/jobs/{job_id}").json()
        if j["status"] in ("succeeded", "failed"):
            break
        time.sleep(0.5)
    out["job"] = j
    out["status"] = j["status"]
    code = (j.get("error") or {}).get("code")
    expect = case.get("expect", "succeeded")
    out["expect"] = expect
    out["ok"] = (j["status"] == "succeeded") if expect == "succeeded" else (j["status"] == "failed" and f"failed:{code}" == expect)
    for sim in case.get("faults", []):
        cl.post(f"/api/v1/simulation/faults/{sim['instance']}", json={k: False for k in sim if k != "instance"})
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", help="a JSON file or a folder of JSON files")
    ap.add_argument("--out", default="batch_results")
    ap.add_argument("--api", default=os.environ.get("API_URL", "http://localhost:8000"))
    ap.add_argument("--token", default=os.environ.get("API_TOKEN", "change-me-admin-token"))
    a = ap.parse_args(argv)
    src = Path(a.inputs)
    files = sorted(src.glob("*.json")) if src.is_dir() else [src]
    out_dir = Path(a.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    run_id = str(int(time.time()))
    index = []
    with httpx.Client(base_url=a.api, headers={"Authorization": f"Bearer {a.token}"}, timeout=30) as cl:
        for f in files:
            case = json.loads(f.read_text(encoding="utf-8"))
            try:
                res = run_case(cl, case, case.get("name", f.stem), run_id)
            except Exception as e:  # record the failure and continue with the next input
                res = {"name": f.stem, "status": "error", "ok": False, "exception": f"{type(e).__name__}: {e}"}
            (out_dir / f"{f.stem}.result.json").write_text(json.dumps(res, indent=2, default=str), encoding="utf-8")
            index.append({"input": f.name, "result": f"{f.stem}.result.json", "status": res.get("status"), "ok": res.get("ok")})
            print(f"{'PASS' if res.get('ok') else 'FAIL'}  {f.name}  -> {res.get('status')}")
    summary = {"run_id": run_id, "total": len(index), "passed": sum(1 for i in index if i["ok"]), "cases": index}
    (out_dir / "index.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"{summary['passed']}/{summary['total']} as expected; results in {out_dir}/index.json")
    return 0 if summary["passed"] == summary["total"] else 1


if __name__ == "__main__":
    sys.exit(main())
