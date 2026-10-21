# Changelog

## sequentialist-quality 0.1.0 — unreleased

- Plans `quality.plan/1` by attributes, nonconformities and variables (known
  sigma, one limit); sequential plans designed with
  `design.kiefer_weiss_plan` (truncated, both risks met, smallest average or
  maximum number of items) and single sampling plans with curtailment; the
  designed boundaries and operating characteristic stored in the plan.
- Operating characteristic and average sample number at any quality,
  exact for attributes and nonconformities; checked against the binomial and
  Poisson formulas of single sampling and by simulation of the executed plans.
- Schemes `quality.scheme/1`: lots inspected item by item with `PlanTest`;
  switching between normal, tightened and reduced inspection; discontinuation
  and resumption; rework and resubmission; dispositions and concessions with
  actor and reason; a CUSUM watch on the supplier; every decision in the audit
  log; inspection records of lots and reports of schemes.
- `sequentialist quality design | oc | check | schema | protocol | report`.

## sequentialist-core 0.1.0, sequentialist-ab 0.1.0, @sequentialist/ab-client 0.1.0 — unreleased

First version of the family, on sequential-analysis 0.4.

### sequentialist-core

- Specs with declarative schemas, every error at once with its JSON pointer,
  normal forms, spec ids, JSON Schema; JSON, and YAML with PyYAML.
- The engine: tasks of several sequential-analysis runs; idempotent intake by
  (source, seq); a journal written before processing; checkpoints; replay after
  a crash giving the uninterrupted task bit for bit; segments; commands with an
  actor and a reason; one writer per task (file locks, version checks); shards.
- Storage in memory, in a directory of JSON files, in SQLite.
- The audit log: decisions and actions chained by SHA-256, with the procedure,
  guarantee, step and history digest behind each; `verify_audit`.
- Reports in JSON and Markdown in which every finding names its guarantee.
- Simulations of whole pipelines, replays on the data's clock, notification
  sinks (log, JSON lines, webhooks), pseudonyms (HMAC-SHA-256).
- The `sequentialist` command, with subcommands added by installed libraries.

### sequentialist-ab

- The experiment spec `ab.experiment/1`: variants and weights, ramp-up,
  overrides for QA, attribution windows and lateness, maximum duration,
  primary, secondary and guardrail metrics of four types (conversion, bounded,
  ratio, rate), direction, MDE, margins, alpha, continuous or planned looks.
- Assignment by SHA-256 of the salt and the unit, identical in Python and
  TypeScript (shared vectors), with the probability of each assignment.
- Aggregation per unit over its attribution window; each unit enters the
  analysis once, when its window closes.
- Analysis with the procedures of sequential-analysis, each labelled with its
  guarantee; e-BH or Bonferroni over metrics and treatments; sample ratio
  mismatch on every exposure.
- Decisions (ship, drop a variant, stop) on primary success, guardrail harm or
  non-inferiority, futility, duration, by hand; each in the audit log.
- Reports, planning of power and duration, A/A on the real pipeline.
- Checked against GrowthBook (gbstats 0.8.0) and the mSPRT of Optimizely
  (`references/`), and run end to end on the MineThatData e-mail experiment.

### @sequentialist/ab-client

- Assignment of variants for browsers and Node.js 16+, no dependencies,
  Apache-2.0.
