#!/usr/bin/env python3
"""Validate a ProofDeploy probes.json file against the v0 authoring rules.

Stdlib only. Implements the rules in
plugins/proofdeploy/skills/proofdeploy/references/probe-schema.md (v0, draft):
structural checks the skill can enforce at authoring time. The runner performs
the deeper checks (SQL parsing for read-only queries, live execution).

Usage: python3 validate.py <probes.json>
Exit 0: valid. Exit 1: prints each problem, one per line.
"""

import json
import sys

HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}
WRITE_KEYWORDS = ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "TRUNCATE")


def err(errors, where, msg):
    errors.append(f"{where}: {msg}")


def check_probe(p, seen_ids, errors):
    where = f"probes[{p.get('id', '?')}]"
    if not isinstance(p, dict):
        err(errors, where, "probe must be an object")
        return
    pid = p.get("id")
    if not isinstance(pid, str) or not pid:
        err(errors, where, "'id' must be a non-empty string")
    elif pid in seen_ids:
        err(errors, where, f"duplicate id '{pid}'")
    else:
        seen_ids.add(pid)
    ptype = p.get("type")
    if ptype not in ("http", "db"):
        err(errors, where, "'type' must be 'http' or 'db'")
        return
    if not isinstance(p.get("description"), str) or not p["description"]:
        err(errors, where, "'description' must be a non-empty string")

    if ptype == "http":
        req = p.get("request")
        if not isinstance(req, dict):
            err(errors, where, "'request' must be an object")
            return
        if req.get("method") not in HTTP_METHODS:
            err(errors, where, f"'request.method' must be one of {sorted(HTTP_METHODS)}")
        path = req.get("path")
        if not isinstance(path, str) or not path.startswith("/"):
            err(errors, where, "'request.path' must be a path starting with '/' (no absolute URLs)")
        headers = req.get("headers", {})
        if not isinstance(headers, dict) or any(not isinstance(v, str) for v in headers.values()):
            err(errors, where, "'request.headers' must be a string-to-string map")
        exp = p.get("expect")
        if not isinstance(exp, dict):
            err(errors, where, "'expect' must be an object")
            return
        if not isinstance(exp.get("status"), int):
            err(errors, where, "'expect.status' must be an integer HTTP status code")
        if "json" not in exp and "contains" not in exp:
            err(
                errors,
                where,
                "at least one of 'expect.json' / 'expect.contains' is required "
                "(a status-only probe is a health check, out of scope)",
            )
    else:  # db
        q = p.get("query")
        if not isinstance(q, str) or not q.strip():
            err(errors, where, "'query' must be a non-empty string")
        else:
            first = q.strip().split(None, 1)[0].upper().rstrip(";")
            if first != "SELECT":
                err(errors, where, f"'query' must be a single SELECT (starts with '{first}')")
            if ";" in q.strip().rstrip(";"):
                err(errors, where, "'query' must be a single statement (no stacked queries)")
            if any(k in q.upper() for k in WRITE_KEYWORDS):
                # Keyword scan is a heuristic; the runner enforces read-only by
                # parsing and by database role. Kept here to catch the obvious.
                err(errors, where, "'query' looks like a write; only SELECT is allowed")
        exp = p.get("expect")
        if not isinstance(exp, dict) or not isinstance(exp.get("rows"), list):
            err(errors, where, "'expect.rows' must be an array of row arrays")


def validate(doc):
    errors = []
    if not isinstance(doc, dict):
        return ["document: top level must be an object"]
    if doc.get("version") != "0":
        err(errors, "document", "'version' must be \"0\" (this validator implements v0)")
    probes = doc.get("probes")
    if not isinstance(probes, list) or not probes:
        err(errors, "document", "'probes' must be a non-empty array")
        return errors
    seen = set()
    for p in probes:
        check_probe(p, seen, errors)
    return errors


def main(argv):
    if len(argv) != 2:
        print(f"usage: {argv[0]} <probes.json>", file=sys.stderr)
        return 2
    try:
        with open(argv[1], encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"{argv[1]}: cannot read as JSON: {e}")
        return 1
    errors = validate(doc)
    if errors:
        print("\n".join(errors))
        return 1
    print(f"{argv[1]}: valid ({len(doc['probes'])} probes, schema v0)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
