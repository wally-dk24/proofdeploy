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

## Second execution (the D66 re-run) — ALSO DIED BEFORE ANY OUTPUT

- Started: 2026-10-16T06:17Z. Died: 2026-10-16T06:52Z (elapsed ~35m).
- Exit: code 1, no signal — the same uncaught-exception death.
- **No output was written.** The output directory was again never
  created; no run record or raw output file exists from this
  execution either. The working tree stayed clean.
- Cause, from the log: the same defect in a different call site.
  While fetching a recursive git tree (`repo_paths` → `get_json`),
  the connection broke mid-body after 382,279 of 432,459 bytes and
  `http.client` raised `IncompleteRead` inside `r.read()` — again an
  `HTTPException`, not an `OSError`, so the Fetcher's retry loop
  never engaged.
- Per D66, the re-run dying ends the matter on this side: **the run
  has no result — the survivor list is unknown, not zero (A6 is not
  triggered; it applies to a completed run with zero survivors).**
  What happens next is Master's decision. The defect itself is in
  the accepted script's fetch layer, not in the registration:
  `_urllib_fetch` reads with no timeout and `get_bytes` retries only
  `OSError`, so any mid-body connection break (`IncompleteRead`, and
  plausibly other `http.client` exceptions) kills the whole run
  instead of recording a per-repo harness error or retrying.

Full console log of the second execution (verbatim):

```
Traceback (most recent call last):
  File "/home/hatch/workspace/proofdeploy-wo6phasea/scripts/wo6/regc_add2_run.py", line 820, in <module>
    main()
  File "/home/hatch/workspace/proofdeploy-wo6phasea/scripts/wo6/regc_add2_run.py", line 805, in main
    result = run(fetcher, args.snapshot_date, args.out)
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/home/hatch/workspace/proofdeploy-wo6phasea/scripts/wo6/regc_add2_run.py", line 763, in run
    entries = [check_repo(fetcher, rec) for rec in pool.values()]
               ^^^^^^^^^^^^^^^^^^^^^^^^
  File "/home/hatch/workspace/proofdeploy-wo6phasea/scripts/wo6/regc_add2_run.py", line 716, in check_repo
    paths, truncated = repo_paths(fetcher, rec["full_name"], sha)
                       ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/home/hatch/workspace/proofdeploy-wo6phasea/scripts/wo6/regc_add2_run.py", line 264, in repo_paths
    data = fetcher.get_json(f"{API}/repos/{full_name}/git/trees/{sha}?recursive=1")
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/home/hatch/workspace/proofdeploy-wo6phasea/scripts/wo6/regc_add2_run.py", line 193, in get_json
    return json.loads(self.get_bytes(url).decode("utf-8"))
                      ^^^^^^^^^^^^^^^^^^
  File "/home/hatch/workspace/proofdeploy-wo6phasea/scripts/wo6/regc_add2_run.py", line 176, in get_bytes
    status, resp_headers, body = self._raw_fetch(url, headers)
                                 ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/home/hatch/workspace/proofdeploy-wo6phasea/scripts/wo6/regc_add2_run.py", line 150, in _urllib_fetch
    return r.status, dict(r.headers.items()), r.read()
                                              ^^^^^^^^
  File "/usr/lib/python3.12/http/client.py", line 500, in read
    s = self._safe_read(self.length)
        ^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/usr/lib/python3.12/http/client.py", line 650, in _safe_read
    raise IncompleteRead(data, amt - len(data))
http.client.IncompleteRead: IncompleteRead(382279 bytes read, 50180 more expected)
```

## Third execution — PENDING: fetcher fix and resume (D74)

Master's decision D74 (Option A, 2026-10-16): fetcher fix and resume
(D74); A1–A6 unchanged; the two prior executions recorded. Only the
script's fetch layer and run mechanics change:

- every request gets a timeout (120 s);
- `http.client.HTTPException` (including `IncompleteRead`) is retried
  exactly like `OSError`, with the same backoff; an exception left
  after retries for one repo becomes that repo's
  `harness or environment: <error>` row and never kills the run;
- the run journals as it goes (`--journal <dir>`): the collected pool
  and search metadata are written before any screening, one line is
  appended per screened repo, and the final outputs are built only
  from the journal. A death resumes the same journal; the journal
  header carries the script sha256, main SHA and snapshot date, and a
  resume with any of them different refuses.

The fixed script has a new sha256 and is accepted on its own before
the run; on the reviewer's acceptance the run goes ahead from current
main without a further authorization (D74). Its record will extend
this document.

## Third execution — journal events (2026-10-16)

The third execution runs under the D74 journal mechanics, journal at
`wo6-scratch/regc-add2-journal-2026-10-16/`. Journal header
(`header.json`, quoted verbatim):

- script sha256: `b80013adf2056165733d8a90fbc4c0f73fd4608737f5b2ebce3c998ecfeee636`
- main SHA: `29045fbee500ab4700c97fe661a5717bdc6b138e`
- snapshot date: `2026-10-16`

Start events recorded in the journal's `events.jsonl`: initial start
2026-10-16T12:02:26Z (`resumed: false`); resumes at
2026-10-16T12:19:00Z, 2026-10-16T15:00:59Z, 2026-10-16T15:57:29Z and
2026-10-16T17:37:09Z, each carrying `previous_exit_cause`: "not
recorded: the previous session ended without an exit record".

Journal event — death in the runtime restart, 2026-10-16 ~19:04Z:

- The instance resumed at 2026-10-16T17:37:09Z died in the
  runtime/VM restart at ~19:04Z (the same restart killed other
  sessions on this VM at ~19:04Z). It wrote no exit record.
- Its last journal write was 2026-10-16T19:04:00Z, at 1,822 entries
  in `entries.jsonl`; the journal itself was not damaged.
- Resumed: 2026-10-16T19:20:13Z from the same journal (the resume
  start event is recorded in `events.jsonl`, with
  `previous_exit_cause`: "not recorded: the previous session ended
  without an exit record").
- Header values on resume unchanged — verified against
  `header.json`: script sha256
  `b80013adf2056165733d8a90fbc4c0f73fd4608737f5b2ebce3c998ecfeee636`,
  main SHA `29045fbee500ab4700c97fe661a5717bdc6b138e`, snapshot date
  `2026-10-16`. The run continues from the journal.

## Run 3 — death causes, journal integrity, and the D140 single-writer lock (2026-10-16)

### The four earlier death causes (as reported; accepted by the reviewer, D140)

- Death 1 (instance started 2026-10-16T12:02:26Z): VM/runtime restart
  at ~12:18:34Z. The twilio watchdog logged podman wiped at
  12:18:34Z; death window: last journal entry 12:17:35Z → process
  found gone 12:18:49Z.
- Death 2 (instance resumed 2026-10-16T12:19:00Z): runtime-layer
  interruption ~13:43–13:46Z, NOT a VM reset. PID verified gone at
  13:46:48Z (kill -0 failed), entries frozen at 578; twilio watchdog
  receipt gap 13:43:21Z → 13:50:45Z (444 s); podman was not wiped; no
  finer mechanism survives in the records.
- Death 3 (instance resumed 2026-10-16T15:00:59Z): cause unknown —
  accepted as the honest record. Entries froze at 622 from 15:05:09Z
  and a resume replaced the instance at 15:57:29Z; there is no
  liveness check, podman event, or receipt gap in the window, and the
  frozen-entries symptom is non-diagnostic: instance 4 showed the
  identical symptom while verified alive 16:29–17:31Z during a
  rate-limit sleep. Reviewer finding (D140): a resume may have
  started while the previous instance was still alive (asleep in a
  rate-limit pause) — two writers on one journal is a defect even
  though the integrity check shows no damage; it is made impossible
  by the lock below.
- Death 4 (instance resumed 2026-10-16T15:57:29Z): VM reset at
  17:33:54Z. Instance verified alive at 17:31:32Z; podman wiped
  17:33:54Z and reinstalled 17:35:03Z; watchdog receipt gap 457 s.

### Death 6

- The instance resumed at 2026-10-16T19:20:13Z died in the ~20:10Z VM
  restart (system boot began 20:10:40Z). It wrote no exit record. Its
  last journal write was 2026-10-16T20:11:01Z, at 2,094 entries in
  `entries.jsonl`.
- Resumed: 2026-10-16T20:39:48Z from the same journal (start event
  recorded in `events.jsonl`).

### Journal integrity (verified 2026-10-16T20:37Z)

- `header.json` intact, never modified since creation: script sha256
  `b80013adf2056165733d8a90fbc4c0f73fd4608737f5b2ebce3c998ecfeee636`,
  main SHA `29045fbee500ab4700c97fe661a5717bdc6b138e`, snapshot date
  `2026-10-16`.
- `events.jsonl`: 6 lines at check time, every line valid JSON,
  timestamps strictly monotonic, no exit event anywhere in the file.
- `entries.jsonl`: 2,094 lines, every line valid JSON; 2,094 unique
  `full_name` values, 0 duplicates; the file ends with a newline —
  no partial or torn trailing line.
- `pool.json` holds the full pool: 3,000 names (progress
  2,094 / 3,000, consistent with the last reported count).
- All six run-3 console logs are 0 bytes — no traceback or script
  error was ever written by any instance; consistent with external
  kills, not script crashes.

### D140 single-writer lock + restart event

- From 2026-10-16T20:43:18Z the run runs under an exclusive,
  non-blocking lock on the journal directory, applied OUTSIDE the
  script so its sha256 stays
  `b80013adf2056165733d8a90fbc4c0f73fd4608737f5b2ebce3c998ecfeee636`:

  `flock -n <journal-dir>/.run.lock python3 scripts/wo6/regc_add2_run.py …`

  The lock is held for the life of the process; a second start exits
  immediately without touching the journal; the kernel releases the
  lock when the process dies, so a VM reset never leaves a stale
  lock.
- Restart event: the live instance was restarted under the lock;
  cause recorded: "restart under single-writer lock, D140". Killing
  the previous instance mid-repo is safe: the torn-line handling and
  the latest-line-wins rule cover it.
- Starts recorded in `events.jsonl` after death 6 (each carrying
  `previous_exit_cause`: "not recorded: the previous session ended
  without an exit record"): 2026-10-16T20:39:48Z — the post-death-6
  resume (console log `regc-add2-console-run3-resume5.log`);
  2026-10-16T20:41:43Z (console log
  `regc-add2-console-run3-resume6.log`); 2026-10-16T20:43:18Z
  (console log `regc-add2-console-run3-resume7.log`) — the lock
  transition. The 20:43:18Z instance is the current live process,
  running under the `flock` wrapper (verified in the process list).
- Proof (run 2026-10-16T21:14Z): a second launch under the same lock
  while the run was live exited immediately — exit code 1, no
  output; `events.jsonl` unchanged (9 lines); the journal was
  untouched.
