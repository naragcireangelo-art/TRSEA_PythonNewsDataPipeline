# TRSEA — Philippine News Filtering & Story-Tracking Pipeline

A local, Ollama-based pipeline that pulls RSS news feeds, filters them down
to domestic stories in a handful of fixed categories, groups duplicate
coverage of the same event, and tracks stories across time so an escalating
disaster or ongoing trial reads as one continuous thread instead of
disconnected snapshots every cycle.

Runs entirely on local models via Ollama — no API keys, no cloud calls.

---

## Pipeline overview

```
TRSEA_Gatherscrpt.py  →  input.txt
        ↓
TRSEA_FilterPipeline.py  (4-stage AI pipeline, runs every cycle)
        ↓
    output.json  (this cycle's snapshot only — overwritten every cycle)
        ↓
    survivor_buffer.json  (accumulates across cycles, never overwritten)
        ↓
TRSEA_Finalize.py  (run on its own schedule, NOT every cycle)
        ↓
    finalized_stories.json  (persistent, cross-cycle story clusters — "the database")
```

`TRSEA_Sequence.py` is the always-on loop that ties the first three steps
together: gather → filter/classify → append to buffer → sleep → repeat.
`TRSEA_Finalize.py` is intentionally separate and does not need to run on
the same cadence — ingestion must run constantly because news doesn't
stop; connecting stories is a batch job over whatever's accumulated, and
can run hourly, daily, or by hand.

---

## The 4-stage AI pipeline (`TRSEA_FilterPipeline.py`)

Each stage is its own Ollama model, and each one only ever sees what the
stage before it decided to pass along — a discarded or untagged article is
**physically absent** from later prompts, not just "told to be ignored."
This is deliberate: earlier single-prompt versions of this pipeline let a
model blend unrelated judgments together (e.g. merging two unrelated
stories that happened to be the only two survivors in a batch). Splitting
strictly by judgment type closed that off.

| Stage | Model | Job | Batched? | Temp |
|---|---|---|---|---|
| 1 | `geo-filter` | Is this article's event set in the Target Country? | Yes, `TRSEA_BATCH_SIZE` per call | 0.1 |
| 2 | `category-tag` | Which of Governance / SystemicIssue / Disaster (if any)? | Yes, over geo survivors only | 0.1 |
| 3a | `group-only` | Which tagged survivors describe the *same* event? | **No** — needs full visibility (see below) | 0.0 |
| 3b | `summarize-only` | Write prose for each already-fixed group | No | 0.3 |

**Why stage 3a is never batched:** grouping needs to compare every survivor
against every other survivor to catch duplicate coverage across outlets.
Chunking it the same way as stages 1/2 would risk two articles about the
same event landing in different chunks and never being compared — silently
reintroducing the exact "missed duplicate" bug this pipeline was built to
avoid. Since geo+category filtering usually cuts the raw pull down a lot
before grouping runs, this is normally a much smaller call than stage 1/2
ever see. A warning prints if it exceeds 40 survivors — that's a signal to
revisit this assumption, not something silently absorbed.

The final assembly step (in code, not by any model) attaches the real
article URL and outlet by integer id, looked up from the original parsed
feed. **The model never touches a URL.** Early versions asked the LLM to
reproduce or reassemble URLs and it reliably corrupted them, especially
ones wrapped across lines by the scraper's console output — small models
are unreliable at character-perfect string reconstruction. Code does that
job instead.

---

## Cross-cycle story tracking (`TRSEA_Finalize.py`)

Without this step, the pipeline is fully stateless: `output.json` is
overwritten every cycle, and an escalating story (48 stranded passengers
on day 1, 32 dead and lahar warnings on day 5) produces two disconnected
entries instead of one continuously-updated thread, because grouping only
ever compares items *within* one cycle's batch.

`TRSEA_Finalize.py` fixes this by:
1. Reading everything accumulated in `survivor_buffer.json` since the last
   finalize run.
2. Reading `finalized_stories.json` (the persistent store) and filtering
   it to clusters updated within `TRSEA_CANDIDATE_WINDOW_DAYS` (default 5)
   — an *unbounded* "compare against every cluster ever created" call
   would reintroduce the same too-much-context problem that caused real
   bugs earlier in this project.
3. Asking the `connect-or-create` model, for each buffered item: does this
   continue an existing cluster, or is it new? Judged on the underlying
   real-world event, not shared classification tags — an ongoing story
   can legitimately shift tags across days.
4. Saving the updated store, **then** clearing the buffer — never the
   other order (see Known limitations).

This is currently a placeholder for a smarter candidate-narrower. Swapping
in FAISS + embeddings later only changes how the candidate cluster list
gets built in step 2 — the connect-or-create prompt and logic that
consumes it doesn't need to change.

---

## Setup

Register all five models (re-run any of these whenever its `.Modelfile`
changes — editing the file does **nothing** until you do this):

```
ollama create geo-filter      -f 01-geo-filter.Modelfile
ollama create category-tag    -f 02-category-tag.Modelfile
ollama create group-only      -f 03a-group.Modelfile
ollama create summarize-only  -f 03b-summarize.Modelfile
ollama create connect-or-create -f 04-connect-or-create.Modelfile
```

Run the always-on loop:
```
python TRSEA_Sequence.py
```

Run a one-off pipeline pass manually (bypasses the buffer — nothing from
this reaches `TRSEA_Finalize.py`):
```
python TRSEA_FilterPipeline.py input.txt "Philippines" > output.json
```

Consolidate whatever's accumulated in the buffer:
```
python TRSEA_Finalize.py
```

---

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `TRSEA_BATCH_SIZE` | `10` | Articles per geo-filter/category-tag call |
| `TRSEA_MAX_PARALLEL` | `1` | Concurrent batches per stage — see caution below |
| `TRSEA_TIMEOUT` | `900` | Seconds to wait for one Ollama response |
| `TRSEA_DEBUG_DIR` | unset | If set, dumps every stage's validated output to this folder |
| `TRSEA_DUPLICATE_FILE` | `possible_missed_duplicates.json` | Where flagged near-duplicates get written |
| `TRSEA_BUFFER_FILE` | `survivor_buffer.json` | Cross-cycle accumulation file |
| `TRSEA_STORE_FILE` | `finalized_stories.json` | Persistent cluster store |
| `TRSEA_CANDIDATE_WINDOW_DAYS` | `5` | How far back `TRSEA_Finalize.py` looks for candidate clusters |

**On `TRSEA_MAX_PARALLEL`:** CPU-only/iGPU hardware (no discrete GPU) is
the assumed environment here. A single inference call typically already
uses most available CPU cores, so running two "at once" often means both
compete for the same cores rather than genuinely parallelizing — this can
make each one slower with no net throughput gain. It also requires
Ollama's own `OLLAMA_NUM_PARALLEL` server-side setting to be raised, or
requests just queue internally regardless of what this script does. Never
assume a value above 1 helps — time a real run at 1 vs 2 vs 3 before
trusting it in the always-on loop. This once caused a full system freeze
under memory pressure when tried without measuring first.

---

## Known limitations (stated plainly, not glossed over)

- **`possible_missed_duplicates.json` is text-similarity only.** It uses
  `difflib.SequenceMatcher` on headline strings — it reliably catches two
  outlets covering the same wire story with similar headlines, but has no
  way to catch two headlines describing the same event in completely
  different words (confirmed live: an English headline and a Tagalog
  headline about related flooding scored far below the flag threshold).
  It only ever flags candidates for review; nothing is ever auto-merged.
- **Grouping doesn't scale past ~40 survivors per cycle without revisiting
  the batching assumption** — see the stage 3a note above.
- **Connecting a story in `TRSEA_Finalize.py` doesn't regenerate its
  summary.** References and metadata get merged into the existing
  cluster; the prose summary stays as it was when the cluster was
  created. Re-summarizing on every connection is a reasonable next step,
  deliberately left out to keep this pass to one job.
- **`TRSEA_Finalize.py` has a narrow, real crash-window risk.** The store
  is saved before the buffer is cleared (on purpose — see below), but if
  the process crashes in the gap between those two writes, next run will
  reprocess the same buffer items. Reference URLs are deduplicated, so a
  re-connected item won't produce literal duplicate references — but a
  brand-new cluster created in that exact window is *not* fully
  idempotent and could be duplicated. Small, real, not hidden.
- **CPU-only inference is genuinely slow at scale.** A ~20-article cycle
  through all 4 stages has taken 900+ seconds in testing. `keep_alive:
  "30m"` on every Ollama call keeps models resident in memory across the
  10-minute polling interval to avoid a cold-reload penalty every cycle,
  but raw generation time on CPU/iGPU hardware is still the dominant cost.

---

## Hard-won debugging lessons

- **Editing a `.Modelfile` does nothing until you re-run `ollama create`
  for it.** This has caused "the fix isn't working" confusion more than
  once — the registered model is frozen at creation time, not live-linked
  to the file.
- **When updating a script, fully replace the file — don't hand-patch.**
  Version drift between what's on disk and what's actually been tested
  has caused two separate incidents where old, already-fixed bugs (a
  hardcoded timeout, an un-batched pipeline call) resurfaced because a
  stale copy of a file was still running. If this project doesn't already
  have one, `git init` here and diff before running — cheap insurance
  against exactly this.
- **Ollama's `format: "json"` guarantees valid JSON syntax, not the shape
  you asked for.** Models under that constraint often wrap an array in an
  object (e.g. `{"results": [...]}`) even when told to return a bare
  array. Every stage's parser tolerates both shapes rather than assuming
  one.
- **A silent freeze usually means "still working, no output," not
  "crashed."** Staging logs (stderr, timestamped, one line per batch and
  per stage) exist specifically because a long CPU-bound run with zero
  output is indistinguishable from a hang otherwise.
