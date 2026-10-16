# REG-C Addendum 2 run record — WO-6 candidate query (plan 1.5, Phase B)

Executed exactly as registered in
`docs/registration/wo6-selection-rule-addendum-2.md` (REG-C as amended by
Addendum 2, A1–A5), with the script accepted in PR #108 at head
`ff5ff378ca780b831b0ec8bf7b5e5e48cb6d7285`.

- Script as run: `scripts/wo6/regc_add2_run.py`, sha256
  `f0a3b99c7cb84a5d917d8e28f2ce8ef5e369c372191e8dab152f350cbeab51e5`
  (byte-identical to the accepted head; verified before each execution).
- Run from main at `0889721874744f6cd6c0b76c9e6c56d0d610566a` (A4).
- Snapshot date: 2026-10-15 (`pushed:>=2026-07-17`).
- Gate before the run (A5, one run only): CI on the accepted head —
  ci run `38826471742` SUCCESS, sandbox-tests run `38826471563` SUCCESS.
- The GitHub API was queried with a read-only token; the token appears
  nowhere in any output (the record carries only `authenticated`).

## First execution — DIED BEFORE ANY OUTPUT (D66)

- Started: 2026-10-16T00:18Z. Died: 2026-10-16T06:14Z (elapsed ~5h56m).
- Exit: code 1, no signal — an uncaught exception escaped `main()`.
- **No output was written.** The script writes its output files only at
  the end of a completed run; the output directory
  (`regc-add2-raw/`) was never created, no run record or raw output file
  exists from this execution, and the working tree stayed clean. The
  only artifact is the console log, whose full contents are the
  traceback below.
- Cause, from the log: while downloading a repository tarball for the
  Python sqlite scan (`repo_contains_string` → `Fetcher.get_bytes` →
  `_urllib_fetch`), the connection broke mid-body after 35,618,816
  bytes and `http.client` raised `IncompleteRead`. That exception is
  an `http.client.HTTPException`, not an `OSError`, so the Fetcher's
  retry loop (which catches `OSError` only) never engaged and the
  process died. The long quiet period before the death was this same
  download trickling through the local egress at a very low rate.
- Per D66 (Master's decision, 2026-10-15), a run that dies before
  writing any output may be re-run exactly once with the same accepted
  script unchanged. That single re-run follows; its output, whatever
  it is, is the run's result. If it also dies, work stops for Master's
  decision.

Full console log of the first execution (verbatim):

```
Traceback (most recent call last):
  File "/usr/lib/python3.12/http/client.py", line 606, in _read_chunked
    value.append(self._safe_read(chunk_left))
                 ^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/usr/lib/python3.12/http/client.py", line 650, in _safe_read
    raise IncompleteRead(data, amt - len(data))
http.client.IncompleteRead: IncompleteRead(9581 bytes read, 72339 more expected)

The above exception was the direct cause of the following exception:

Traceback (most recent call last):
  File "/home/hatch/workspace/proofdeploy-wo6phasea/scripts/wo6/regc_add2_run.py", line 820, in <module>
    main()
  File "/home/hatch/workspace/proofdeploy-wo6phasea/scripts/wo6/regc_add2_run.py", line 805, in main
    result = run(fetcher, args.snapshot_date, args.out)
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/home/hatch/workspace/proofdeploy-wo6phasea/scripts/wo6/regc_add2_run.py", line 763, in run
    entries = [check_repo(fetcher, rec) for rec in pool.values()]
               ^^^^^^^^^^^^^^^^^^^^^^^^
  File "/home/hatch/workspace/proofdeploy-wo6phasea/scripts/wo6/regc_add2_run.py", line 732, in check_repo
    passed, app_path, reasons, info = python_filter(
                                      ^^^^^^^^^^^^^^
  File "/home/hatch/workspace/proofdeploy-wo6phasea/scripts/wo6/regc_add2_run.py", line 677, in python_filter
    found, skipped = repo_contains_string(fetcher, full_name, sha, SQLITE_BACKEND)
                     ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/home/hatch/workspace/proofdeploy-wo6phasea/scripts/wo6/regc_add2_run.py", line 469, in repo_contains_string
    blob = fetcher.get_bytes(f"{API}/repos/{full_name}/tarball/{sha}")
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/home/hatch/workspace/proofdeploy-wo6phasea/scripts/wo6/regc_add2_run.py", line 176, in get_bytes
    status, resp_headers, body = self._raw_fetch(url, headers)
                                 ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/home/hatch/workspace/proofdeploy-wo6phasea/scripts/wo6/regc_add2_run.py", line 150, in _urllib_fetch
    return r.status, dict(r.headers.items()), r.read()
                                              ^^^^^^^^
  File "/usr/lib/python3.12/http/client.py", line 478, in read
    return self._read_chunked(amt)
           ^^^^^^^^^^^^^^^^^^^^^^
  File "/usr/lib/python3.12/http/client.py", line 612, in _read_chunked
    raise IncompleteRead(b''.join(value)) from exc
http.client.IncompleteRead: IncompleteRead(35618816 bytes read)
```

## Second execution (the D66 re-run)

_(Results appended when the re-run completes.)_
