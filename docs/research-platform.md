# Research platform — from "here's a link" to a ranked bot backlog

Status: **design notes** (consolidated 2026-07-15; not yet implemented).

bot-engine today is the *runtime* layer: a host-agnostic bot lifecycle
(registry, executor, scheduler). This document sketches the layer that sits
**above** it — a research platform that turns raw strategy sources into a
ranked backlog of bot candidates, expressed in a system-agnostic **strategy
spec** DSL that compiles down to bot-engine's entry-bot / exit-bot YAML (or
to any other bot system's format).

## The loop

**Drop in anything:**

- a chat description
- a YouTube link
- an SSRN paper
- a theme to research

**Out comes a ranked backlog of bot candidates**, each with:

- a draft bot definition
- a validation plan

## Design

### The strategy spec — a system-agnostic intermediate representation

A YAML contract capturing the edge hypothesis. Fields:

- **universe**
- **entry signals**
  - underlying level
  - market level
- **position structure**
- **exit rules**
- **sizing**
- **open parameters** — a list of everything the source left unspecified.
  *Nothing gets invented silently*: if the paper never named a DTE or a
  delta, that gap is recorded as an open parameter, not filled with a
  plausible-looking default.

A **compiler** maps the IR to a bot system's entry-bot and exit-bot format.
This decouples the knowledge base from any one bot DSL — the same spec can
target bot-engine's YAML registry, the ZTS strategy engine, or another
platform entirely.

### Trade history drives recommendations

Live/paper trade history feeds back into scoring, four ways:

1. **Gap analysis** — portfolio-level holes boost complementary candidates.
   Example: "everything you run loses when VIX > 25 → long-vol candidates
   get boosted."
2. **Redundancy detection** — a new idea scores low if its returns would
   correlate > 0.8 with bots already running.
3. **Variant proposals** for top-quartile bots.
4. **Retirement flags** for persistent losers.

Each trade gets enriched with a **market-regime snapshot at entry**, so
performance is conditioned on regime rather than averaged across it.

### Scoring rubric

Candidates are scored on a six-part rubric:

1. **Evidence tier** — SSRN paper vs. YouTube anecdote
2. **Mechanism** — is there a causal story for the edge?
3. **Portfolio fit** — gap-filling vs. redundant (from the history loop)
4. **Personal affinity**
5. **Feasibility in the DSL** — can the spec express it; can a compiler
   target emit it?
6. **Cost robustness** — does the edge survive commissions/slippage?

### User-facing workflows

- **"Here's a link"** — a single idea taken end-to-end: ingest → distill to
  a strategy spec → score → backlog entry with draft definition +
  validation plan.
- **"Research theme X"** — a sweep producing 3–5 graded briefs.
- **Weekly "what should I build next" review** — combines fresh trade
  history with the standing backlog.

## Implementation stages

1. **File-based knowledge base in a git repo**
   - Claude Code commands: `/idea`, `/research`, `/recommend`
     1. ingestion
     2. distillation
     3. scoring
2. **A Python package with a real store**
   - embedding-based dedup
   - scheduled research sweeps
   - marimo dashboard for the backlog
3. **Continuous multi-agent system**
   - scouts, analysts, portfolio-manager agents

## Guardrails

- **History-based scoring shows sample sizes** so thin data can't
  masquerade as signal.
- *(The source notes list further guardrails not yet captured here —
  extend this section as they're recovered.)*

## Relationship to bot-engine

| Layer | Owns | Status |
|---|---|---|
| Research platform (this doc) | intake, knowledge base, strategy-spec IR, scoring, backlog | design |
| Spec compiler | strategy spec → target bot format (bot-engine YAML, ZTS, …) | design |
| bot-engine runtime | bot lifecycle: registry, executor, scheduler | shipped (pre-alpha) |
| Host platform | market data, orders, positions, risk | external (e.g. ZTS) |

The strategy spec is deliberately **not** the registry YAML that
`BotRegistry` loads: the spec captures a hypothesis (with open parameters
and provenance), while the registry YAML is one concrete, fully-parameterized
compilation target of it.
