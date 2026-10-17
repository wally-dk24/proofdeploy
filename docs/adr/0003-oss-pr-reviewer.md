# ADR 0003: OSS agent reviewer for every PR

Date: 2026-10-08
Status: Superseded by Master decision D116 (2026-10-16): the pr-agent
workflow is removed. It ran the pinned model on every push against a
repo-level GROQ_API_KEY secret, competing with measurement runs for the
same provider rate limit. The decision below stands as the historical
record of why the reviewer was adopted.

## Context

Every PR must get an agent review. GitHub Copilot code review was evaluated
first: it requires a paid Copilot plan (Pro/Pro+/Max/Business/Enterprise) and
is not included in Copilot Free. The account has no Copilot subscription, and
the reviewer API silently ignores the request without entitlement.

## Decision

Use an open-source PR review agent (Qodo PR Agent, Apache 2.0) as a GitHub
Action, reviewing every PR automatically. Advisory, non-blocking.

## Rationale

- PR Agent is the established OSS choice: configurable, runs in our own
  Actions minutes, no vendor lock-in.
- It speaks OpenAI-compatible APIs, so it can run against the same model
  provider as the probe author.
- Advisory-first matches the product's own philosophy: automation earns
  blocking authority by being right.

## Consequences

- Needs an LLM API key in GitHub Actions secrets (never in the repo).
- If the OSS reviewer proves noisy or unhelpful, revisit: Copilot Pro for the
  account, or a different OSS reviewer.
