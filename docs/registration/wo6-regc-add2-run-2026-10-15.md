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

### D140 follow-up — the three lock-transition starts attributed; integrity rerun

Reconstruction from the surviving records (journal `events.jsonl`,
the watcher logs in `wo6-scratch`, the coordinator session's exec
logs, file birth times, and the live process list). Where a fact is
not in the records, it is written as not established.

Start 7 — 2026-10-16T20:39:48Z (console log
`regc-add2-console-run3-resume5.log`), the post-death-6 resume:

- Under the lock: NO. The lock file did not exist when this instance
  started — `.run.lock` birth is 2026-10-16T20:41:42.999Z, two
  minutes later — and its launch record shows a plain
  `setsid nohup python3 …` invocation with no `flock`. It ran as
  PID 12632 (PPID 12630; the coordinator's verification ps caught it
  at etime 00:05: "12632 12630 00:05 python3
  scripts/wo6/regc_add2_run.py …"). watcher9: "2026-10-16T20:41:17Z
  alive pid=12632 entries=2096".
- How it ended: KILLED by the coordinator applying D140. The kill of
  PID 12632 began ~20:41:48Z (runtime monitor record). watcher9:
  "2026-10-16T20:42:17Z PROCESS 12632 EXITED entries=2096"; the
  coordinator's monitor log: "=== MONITOR: process 12632 no longer
  alive at 2026-10-16T20:42:17Z ===". The exact death instant inside
  20:41:48Z–20:42:17Z is not pinned by the records, and no exit code
  exists (killed process; `events.jsonl` carries no exit event).

Start 8 — 2026-10-16T20:41:43Z (console log
`regc-add2-console-run3-resume6.log`), the first locked start:

- Under the lock: YES. `.run.lock` birth 2026-10-16T20:41:42.999Z
  precedes this start event (20:41:43.322Z) by 0.32 s — the lock file
  is created by this launch's `flock`.
- Alive at the same time as start 7: YES, briefly. Start 8 began at
  20:41:43.322Z while PID 12632 was still alive — the kill against
  it only began ~20:41:48Z and its exit was confirmed at 20:42:17Z.
  Because start 7 held no lock, the locked launch was not blocked;
  the overlap ended when the kill took effect, sometime inside
  20:41:48Z–20:42:17Z (the instant is not pinned). Caveat: the last
  watcher poll proving 12632 alive is at 20:41:17Z, 26 s before
  start 8; a spontaneous death of 12632 inside
  20:41:17Z–20:41:43Z cannot be excluded from the timestamps alone,
  and nothing in the records indicates one. Entries stood at 2,096 at
  both the 20:41:17Z and the 20:42:17Z polls — neither instance
  appended an entry during the overlap window.
- How it ended: not established by the surviving records. No kill
  command, exit code, or console output for start 8 survives (its
  console log is 0 bytes, like every run-3 console log, and no record
  preserves its PID). What is established: it had ended by
  2026-10-16T20:43:15Z, because start 9's launcher then acquired the
  same lock with `flock -n` and succeeded — the kernel grants that
  only when no process holds the lock.

Start 9 — 2026-10-16T20:43:18Z (console log
`regc-add2-console-run3-resume7.log`), the current live instance:

- Under the lock: YES — proven by its live process ancestry:
  launcher bash PID 2449 (started Fri Oct 16 20:43:15 2026) → PID
  2451 "flock -n /home/hatch/workspace/wo6-scratch/regc-add2-journal-2026-10-16/.run.lock
  python3 scripts/wo6/regc_add2_run.py --snapshot-date 2026-10-16
  --main-sha 29045fbee500ab4700c97fe661a5717bdc6b138e --journal
  /home/hatch/workspace/wo6-scratch/regc-add2-journal-2026-10-16" →
  python PID 2452. The launcher's recorded output: "launch issued at
  2026-10-16T20:43:15Z"; the start event followed at 20:43:18.007Z.
- Alive at the same time as either earlier start: NO. Start 7 was
  dead by 20:42:17Z, and start 9's `flock -n` acquisition at
  20:43:15Z succeeded, which is only possible once start 8 had
  released the lock. Since 20:43:15Z exactly one instance has been
  alive — watcher10: "2026-10-16T20:44:52Z alive pid=2452
  entries=2096", "2026-10-16T21:14:53Z alive pid=2452 entries=2121" —
  and it is still running at the time of this append.

Integrity rerun after the lock transition (2026-10-16 ~21:19Z):

- `entries.jsonl`: 2,127 lines; every line valid JSON (0 bad);
  2,127 distinct `full_name` values; duplicates: 0; the file ends
  with a newline, with 0 bytes after it — no torn or partial line.
  The 20:41:43Z–20:42:17Z overlap left no damage.
- `events.jsonl`: 9 lines, every line valid JSON.
- `header.json` unchanged: script sha256
  `b80013adf2056165733d8a90fbc4c0f73fd4608737f5b2ebce3c998ecfeee636`,
  main SHA `29045fbee500ab4700c97fe661a5717bdc6b138e`, snapshot
  date `2026-10-16`.


---

## D148 stop and D149 close-out (appended 2026-10-17)

Master stopped the run on his word (D148) at 2026-10-17T11:43:32Z,
at 2,936 of 3,000 pool repos screened. The reviewer ruled the
close-out (D149): the journal stays byte-untouched (no resume, no
"completed" exit event), and the outputs are derived from the
journal as-is with the script's own code. This section is that
record.

### Per-instance ledger — all 23 starts

Start times are the journal's own `events.jsonl` start events (23
starts, no exit events — no session ever recorded an exit). "Entries
appended" is stated only where the recorded checkpoints force it;
otherwise it is marked not separately recorded rather than guessed.
Cumulative checkpoints observed during the run: 406 entries at
13:22Z (during #2); 622 when #3 froze; 1,822 at #5's last write
(19:04:00Z); 2,094 at #6's last write (20:11:01Z); 2,127 at #9's
last write (21:18:34Z); 2,936 reached during #10 (last append
2026-10-17T03:00:07Z); 2,936 unchanged from then on.

| # | Start (UTC) | Under D140 lock | End (UTC) | Entries appended | Cause / evidence |
|---|---|---|---|---|---|
| 1 | 2026-10-16T12:02:26Z | n | ~12:18:34Z | not separately recorded | VM restart ~12:18:34Z (podman wiped; established at the time) |
| 2 | 2026-10-16T12:19:00Z | n | ~13:43–13:46Z | not separately recorded (406 cumulative observed 13:22Z during this instance) | runtime-layer interruption, NOT a VM reset (established at the time) |
| 3 | 2026-10-16T15:00:59Z | n | not established | not separately recorded (entries froze at 622 cumulative) | cause unknown — accepted as the honest record; entries froze at 622 with no exit record |
| 4 | 2026-10-16T15:57:29Z | n | 17:33:54Z | not separately recorded | VM reset 17:33:54Z (established at the time) |
| 5 | 2026-10-16T17:37:09Z | n | ~19:04Z (last write 19:04:00Z) | not separately recorded (1,822 cumulative at its last write) | ended ~19:04Z; established at the time |
| 6 | 2026-10-16T19:20:13Z | n | ~20:10Z (last write 20:11:01Z) | 272 (2,094 cumulative at its last write, from 1,822 at #5's end) | VM restart ~20:10Z (established at the time) |
| 7 | 2026-10-16T20:39:48Z | n | ~20:42:17Z | not separately recorded | killed by the coordinator ~20:42:17Z while applying the D140 lock (start 7 predated the lock) |
| 8 | 2026-10-16T20:41:43Z | y | by 20:43:15Z | not separately recorded | manner not established; brief two-writer overlap with #7 until ~20:42:17Z, no appends during the overlap and no journal damage (integrity rerun, previous section) |
| 9 | 2026-10-16T20:43:18Z | y | ~21:18Z (last write 21:18:34Z) | not separately recorded (2,127 cumulative at its last write) | runtime restart ~21:18Z (established at the time) |
| 10 | 2026-10-16T21:21:27Z | y | ~03:00Z (last append 2026-10-17T03:00:07Z) | 809 (2,936 cumulative reached, from 2,127 at #9's end) | mechanism not established — no exit record; the journal simply stops at 2,936 |
| 11 | 2026-10-17T03:32:15Z | y | ~03:56Z | 0 | VM/runtime restart ~03:56Z; wrote nothing |
| 12 | 2026-10-17T04:03:24Z | y | by 04:19:47Z | 0 | manner not established (gone when #13 started) |
| 13 | 2026-10-17T04:19:47Z | y | ~04:52Z | 0 | daemon restart ~04:52Z |
| 14 | 2026-10-17T04:52:32Z | y | ~04:59Z | 0 | VM restart ~04:59Z |
| 15 | 2026-10-17T05:00:33Z | y | ~06:44Z | 0 | daemon restart ~06:44Z; this instance ran 1h44m inside one boot without appending |
| 16 | 2026-10-17T06:44:55Z | y | by 07:23:46Z | 0 | manner not established |
| 17 | 2026-10-17T07:23:46Z | y | by 07:54:53Z | 0 | manner not established |
| 18 | 2026-10-17T07:54:53Z | y | 08:30:30Z | 0 | death observed by the monitor's watcher at 08:30:30Z with entries=2936; mechanism not established |
| 19 | 2026-10-17T08:30:48Z | y | by 09:27Z | 0 | manner not established (alive at 09:01Z, gone by 09:27Z) |
| 20 | 2026-10-17T09:27:06Z | y | by 10:16:02Z | 0 | manner not established (gone when #21 started) |
| 21 | 2026-10-17T10:16:02Z | y | by 10:55:24Z | 0 | manner not established (gone when #22 started) |
| 22 | 2026-10-17T10:55:24Z | y | by 11:32:04Z | 0 | manner not established (gone when #23 started) |
| 23 | 2026-10-17T11:32:04Z | y | 2026-10-17T11:43:32Z | 0 | STOPPED on Master's word (D148) — process terminated by Wally at 11:43:32Z; the monitor was stood down and nothing resumes the run |

### The stall

No entry was appended after 2026-10-17T03:00:07Z (during #10),
across starts #11–#23. Root cause is **not established** and this
record does not claim one. What was checked: a journaled resume
does not re-collect the pool (the script reads `pool.json` from the
journal; its mtime is still the original 2026-10-16 write), and
GitHub's rate-limit quota was full when checked (core 5,000/5,000).
The single direct observation of the stalled state, captured
2026-10-17T11:32Z on #23: the process was asleep in a network poll
(`poll_schedule_timeout`), holding one open connection to the
egress proxy (port 3128), waiting on a response. That is an
observation, not a cause: it is consistent with a proxied request
never completing, and a single snapshot does not prove it.

### The stop (D148) and the close-out (D149)

- D148: Master asked whether the searched work would be lost if
  the run stopped (it would not — the journal is append-only on
  disk), then directed the stop. The process was terminated at
  11:43:32Z; the journal's final state: 2,936 entries, all valid
  JSON, 2,936 distinct repos, no duplicates, no torn line.
- The script's own end-of-run sequence (`_retry_errored` →
  `_winners_and_error_history` → `_finalize` →
  `record_exit("completed")`) never ran: no exit event exists and
  the run did not earn one.
- D149 close-out (Option 2): the outputs were derived with the
  script's own `_winners_and_error_history` and `_finalize` by the
  new driver `scripts/wo6/regc_add2_closeout.py`, reading the
  journal as-is. The journal is byte-untouched — sha256 before and
  after the close-out: header.json
  `b8a1541e5bf0e1d6693f66d3677920957cad7617b2ee41ee4e5750059b8b88c5`,
  pool.json
  `a8c31371e56a59ecb5e7a94a0f89c91dda7a05fc01da21f0607aac28bf1054fd`,
  entries.jsonl
  `dce465227607d535230ff2f357f46fede9906ad095d067340a04ddf7fdc4894f`,
  events.jsonl
  `6a3e674a69b5bd1b4f7a7c6161e72f0df72c6fb6532aacb38a2cda072f851df0`.
  The run script is unchanged (sha256
  `b80013adf2056165733d8a90fbc4c0f73fd4608737f5b2ebce3c998ecfeee636`).
- Close-out outputs, in
  `docs/registration/wo6-regc-add2-closeout-2026-10-17/`:
  `regc-add2-raw.json` and `regc-add2-survivors.json` exactly as
  `_finalize` writes them; `closeout-header.json` (pool 3,000 /
  screened 2,936 / unscreened 64 / SURVIVORS 14 / ERRORED 0 /
  stopped 2026-10-17T11:43:32Z on Master's word (D148) / close-out
  per D149 / last append 2026-10-17T03:00:07Z / script and journal
  hashes / max unscreened stars); `UNSCREENED.json` (the 64
  unscreened in pool order, each with stars and its language
  query). `_retry_errored` is stated in the header as a verified
  no-op: no repo's latest attempt carries an error (ERRORED = 0),
  so the pass would re-screen nothing; the driver refuses any
  journal for which that does not hold.
- **max_unscreened_stars: 7,409** (Abdess/retrobios, Python query,
  first of the 64 in pool order). Per the D149 bound (from D148),
  applied at Phase C selection time: a survivor counts as the
  candidate only if its stars exceed 7,409; otherwise the walk
  stops and the unscreened repos at or above its stars are screened
  (same script, same journal, under the lock) before any sign-off.
- Raw journal evidence is committed beside this record at
  `docs/registration/wo6-regc-add2-journal-2026-10-16/` (header,
  pool and events byte-exact; entries gzipped) with
  `SHA256SUMS.txt` recording the raw and committed hashes.
- Phase C waits for the reviewer's verification of the close-out
  and the raw evidence (D149 item 5).
