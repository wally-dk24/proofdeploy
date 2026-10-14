# ADR-0012: Snapshot Symlink Handling (WO-8.4b)

**Date:** 2026-10-14
**Status:** Accepted

## Context

Snapshot materialization refused every symlink outright: `git archive`
extraction rejected link members, and the author-bundle assembler
rejected any snapshot tree containing a link. Real repositories carry
harmless in-tree symlinks — in the 2026-10-13 provisioning readiness
pass, 18 of 42 sides failed at the snapshot stage on symlinks alone
(Directus `readme.md`, Vendure `CLAUDE.md`, and others), never reaching
their next real blocker. A blanket refusal measures ProofDeploy's
restriction, not the app.

## Decision

Master's decision (approved 2026-10-14, recorded verbatim):

1. A symlink in a snapshot is kept only if its target is relative and resolves inside the snapshot tree.
2. Anything else is refused, with a typed reason naming the path. That covers absolute targets and targets that escape the tree through `..` or chained links.
3. The probe author's bundle never follows links. A link's target content never reaches the author through a link, and a leak-check test proves it.

## Implementation notes (append-only)

- `proofdeploy/fixture.py` resolves a link target component-wise over
  the tree's own link map (symlink components substituted eagerly, as
  the kernel resolves them), so chained links are judged by where the
  chain lands, not by the first hop's spelling. Refusals raise
  `SnapshotLinkError` — a typed reason (`symlink-target-absolute`,
  `symlink-target-escapes-tree`, or `symlink-resolution-loop`), the
  link's path, and its target string.
- Provisioning (`create_snapshot`) defers link members until every
  file is extracted, validates the whole link map, then creates the
  kept links in the snapshot, so the app's build sees them.
- The author bundle omits link entries entirely; each omitted link is
  recorded in the bundle manifest as `omitted_links` (path and target
  string). The answer-key leak check applies the same rule to its
  equality reference: B's symlink entries are excluded from the
  expected file list and cross-verified against the manifest record.
- The author prompt is built from the bundle's snapshot, not the
  provisioning tree. For a link-free tree the copy is byte-identical,
  so the rendered prompt — and the pinned prompt builder — are
  unchanged.
