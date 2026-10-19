"""O21 replay experiment (D236): re-ask the 11 failed golden bug runs.

Serial, one slot at a time; one optional correction round; offline judge.
Live mode uses the pinned proofdeploy.model_client.call_model with its
default runner. --stub uses a fake runner (no network, no credential).
Never prints or stores credential or environment values.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
OUT = HERE / "out"
CLIENT_SHA256 = "a35238a5a38b36ae691ab97861a064938787c5b14a49c380c009067a99f3785b"
RETRY_WAIT_S = 60
PACE_S = 20  # v2 (D240): pause between model calls to stay under the tokens-per-minute limit

sys.path.insert(0, str(HERE))
import check  # noqa: E402
import judge  # noqa: E402


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(p: Path) -> str:
    return sha256_bytes(p.read_bytes())


def sha256_text(s: str) -> str:
    return sha256_bytes(s.encode("utf-8"))


def verify_sums() -> None:
    for line in (HERE / "SHA256SUMS").read_text().splitlines():
        if not line.strip():
            continue
        digest, name = line.split(None, 1)
        name = name.strip().lstrip("*")
        if sha256_file(HERE / name) != digest:
            raise SystemExit(f"ABORT: SHA256SUMS mismatch for {name}")


def git_head() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout.strip()


@dataclass
class StubResponse:
    content: str
    finish_reason: str | None = "stub"
    usage: dict | None = None
    request_record: dict | None = None


def make_call(stub_reply: str | None):
    """Return call(prompt) -> response. Live mode imports the pinned client."""
    if stub_reply is not None:
        return lambda prompt: StubResponse(content=stub_reply)
    from proofdeploy.model_client import call_model

    return lambda prompt: call_model(prompt)


def call_with_retry(call, prompt: str, pace: bool = True):
    """Returns (response, retries, error_string). One retry after 60 s."""
    if pace:
        time.sleep(PACE_S)
    try:
        return call(prompt), 0, None
    except RuntimeError as e:
        first = str(e)
    print(f"transport error ({first[:200]}); waiting {RETRY_WAIT_S}s then retrying once", flush=True)
    time.sleep(RETRY_WAIT_S)
    try:
        return call(prompt), 1, None
    except RuntimeError as e:
        return None, 1, f"first: {first} | second: {e}"


def rej(res):
    return [list(r) for r in res if r[2] != "ok"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stub", action="store_true")
    ap.add_argument("--stub-reply", default=None)
    args = ap.parse_args()
    if args.stub and not args.stub_reply:
        ap.error("--stub requires --stub-reply PATH")
    stub_reply = Path(args.stub_reply).read_text() if args.stub else None

    verify_sums()
    client_sha = sha256_file(REPO / "proofdeploy" / "model_client.py")
    if client_sha != CLIENT_SHA256:
        raise SystemExit(f"ABORT: model_client.py sha256 {client_sha} != pinned {CLIENT_SHA256}")
    head = git_head()
    slots = json.loads((HERE / "slots.json").read_text())["slots"]
    call = make_call(stub_reply)
    global PACE_S
    if args.stub:
        PACE_S = 0
    OUT.mkdir(exist_ok=True)

    results = []
    caught = 0
    errored = False
    for s in slots:
        n = s["slot"]
        tag = f"slot{n:02d}"
        pfile = HERE / s["prompt"]
        prompt_text = pfile.read_text()
        rec = {
            "slot": n, "replaces_run": s["replaces_run"], "prompt_file": s["prompt"],
            "prompt_sha256": sha256_text(prompt_text), "verdict": None,
            "correction_used": False, "r1_rejections": None, "r2_rejections": None,
            "r1": None, "r2": None, "retries": {"r1": 0, "r2": 0}, "error": None,
        }
        results.append(rec)
        r1, retries, err = call_with_retry(call, prompt_text)
        rec["retries"]["r1"] = retries
        if err:
            rec["verdict"], rec["error"] = "ERROR", f"r1 call: {err}"
            errored = True
            break
        (OUT / f"{tag}_r1.txt").write_text(r1.content)
        rec["r1"] = {"finish_reason": r1.finish_reason, "usage": r1.usage, "sha256": sha256_text(r1.content)}
        _, res1 = check.surviving(str(pfile), r1.content)
        rec["r1_rejections"] = rej(res1)
        msg = check.correction_message(res1)
        final = r1
        if msg is not None:
            rec["correction_used"] = True
            p2 = prompt_text + "\n\n---\n\nYour previous answer:\n\n" + r1.content + "\n\n" + msg
            (OUT / f"{tag}_prompt_r2.txt").write_text(p2)
            rec["prompt_r2_sha256"] = sha256_text(p2)
            r2, retries, err = call_with_retry(call, p2)
            rec["retries"]["r2"] = retries
            if err:
                rec["verdict"], rec["error"] = "ERROR", f"r2 call: {err}"
                errored = True
                break
            (OUT / f"{tag}_r2.txt").write_text(r2.content)
            rec["r2"] = {"finish_reason": r2.finish_reason, "usage": r2.usage, "sha256": sha256_text(r2.content)}
            final = r2
        verdict, details = judge.judge(str(pfile), final.content)
        rec["verdict"] = verdict
        rec["judge_details"] = details
        if rec["correction_used"]:
            rec["r2_rejections"] = rej([tuple(x) for x in details["rejections"]])
        caught += verdict == "CAUGHT"
        print(f"slot {n}: {verdict} correction_used={rec['correction_used']}", flush=True)

    summary = f"CAUGHT {caught}/{len(slots)}"
    if errored:
        summary += f" (stopped on ERROR after {len(results)} slots)"
    doc = {
        "summary": summary, "mode": "stub" if args.stub else "live", "git_head": head,
        "client_sha256": client_sha, "script_sha256": sha256_file(Path(__file__)),
        "slots": results,
    }
    (OUT / "results.json").write_text(json.dumps(doc, indent=2))
    (OUT / "summary.txt").write_text(summary + "\n")
    print(summary)
    return 1 if errored else 0


if __name__ == "__main__":
    sys.exit(main())
