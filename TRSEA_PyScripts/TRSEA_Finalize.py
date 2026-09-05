"""
Finalize stage: consolidates buffered per-cycle survivor groups into a
persistent, cross-cycle store of story clusters — connecting a new item to
an existing ongoing story where one genuinely exists, instead of every
mention of the same real-world event staying permanently isolated from
every other cycle that ever touched it.

This is a SEPARATE script from TRSEA_FilterPipeline.py on purpose and does
not need to run on the polling loop's cadence. Ingestion must run
constantly because news doesn't stop; connecting stories is a batch job
over whatever's accumulated in the buffer since the last finalize run, and
can run as often or as rarely as you like — hourly, daily, or by hand.

Files:
  survivor_buffer.json    — appended to by run_loop.py after every
                             successful pipeline cycle. Cleared by this
                             script once its contents are safely merged.
  finalized_stories.json  — the persistent cross-cycle store. This is
                             where "the database" lives — plain JSON, no
                             SQL, easy to json.load() from anywhere.

Candidate scoping: a new item is only compared against clusters updated
within TRSEA_CANDIDATE_WINDOW_DAYS (default 5) — an unbounded "compare
against every cluster ever created" call would reintroduce the exact
too-much-context problem that caused real bugs earlier in this pipeline
(the missing-article and wrong-merge bugs). This time-window filter is a
placeholder for a smarter candidate-narrower (e.g. FAISS + embeddings) —
swapping that in later only changes how `candidate_clusters` gets built
below, not the connect-or-create prompt or logic that consumes it.

Known limitation, stated plainly rather than glossed over: connecting a
new item to an existing cluster does NOT currently regenerate that
cluster's summary text to reflect the new information — it appends
references and refreshes metadata, but the prose summary stays as it was
when the cluster was created. Re-summarizing on every connection is a
reasonable next addition, deliberately left out of this pass to keep it
to one job at a time.

Usage:
    python finalize.py
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

OLLAMA_URL = "http://localhost:11434/api/generate"
REQUEST_TIMEOUT_SECONDS = int(os.environ.get("TRSEA_TIMEOUT", "900"))

BUFFER_FILE = os.environ.get("TRSEA_BUFFER_FILE", "survivor_buffer.json")
STORE_FILE = os.environ.get("TRSEA_STORE_FILE", "finalized_stories.json")
CANDIDATE_WINDOW_DAYS = int(os.environ.get("TRSEA_CANDIDATE_WINDOW_DAYS", "5"))


def _log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        content = f.read().strip()
        return json.loads(content) if content else default


def atomic_write(path, data):
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, path)  # atomic swap — same pattern as output.json


def call_ollama(model, prompt, temperature=None):
    payload = {
        "model": model, "prompt": prompt, "format": "json", "stream": False,
        "keep_alive": "30m",
    }
    if temperature is not None:
        payload["options"] = {"temperature": temperature}
    resp = requests.post(OLLAMA_URL, json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
    resp.raise_for_status()
    return resp.json()["response"]


def _extract_array(parsed):
    # Same tolerance as the main pipeline — format="json" mode doesn't
    # guarantee the top-level shape asked for.
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        if isinstance(parsed.get("results"), list):
            return parsed["results"]
        list_values = [v for v in parsed.values() if isinstance(v, list)]
        if len(list_values) == 1:
            return list_values[0]
    raise ValueError(f"expected a JSON array (optionally wrapped as results), got {type(parsed).__name__}")


def build_connect_prompt(buffer_items, candidate_clusters):
    lines = ["NEW ITEMS:"]
    for item in buffer_items:
        lines.append(
            f"[buffer_id {item['buffer_id']}] tags: {', '.join(item['classification']) or 'none'} — "
            f"{item['summary']}"
        )
    lines.append("")
    lines.append("EXISTING CLUSTERS (may be empty):")
    if not candidate_clusters:
        lines.append("(none)")
    for c in candidate_clusters:
        lines.append(
            f"[cluster_id {c['cluster_id']}] tags: {', '.join(c['classification']) or 'none'} — "
            f"{c['summary']}"
        )
    return "\n".join(lines)


def validate_connections(raw_json, expected_buffer_ids, valid_cluster_ids):
    data = _extract_array(json.loads(raw_json))
    got_ids = {item["buffer_id"] for item in data}
    if got_ids != expected_buffer_ids:
        raise ValueError(f"connect-or-create buffer_id mismatch: expected {expected_buffer_ids}, got {got_ids}")
    result = {}
    for item in data:
        cluster_id = item.get("connects_to_cluster_id")
        if cluster_id is not None and cluster_id not in valid_cluster_ids:
            raise ValueError(f"invented cluster_id {cluster_id} not in candidate set")
        result[item["buffer_id"]] = cluster_id
    return result


def run_stage(model, prompt, validator, *validator_args, temperature=None, retries=1):
    last_err = None
    for attempt in range(retries + 1):
        try:
            raw = call_ollama(model, prompt, temperature=temperature)
            return validator(raw, *validator_args)
        except (json.JSONDecodeError, ValueError, KeyError, TypeError) as e:
            last_err = e
            _log(f"[{model}] attempt {attempt + 1} gave invalid output ({e}); retrying with correction")
            prompt += f"\n\nYour previous output was invalid: {e}. Return ONLY the corrected JSON, nothing else."
        except requests.exceptions.RequestException as e:
            last_err = e
            _log(f"[{model}] attempt {attempt + 1} timed out/network error ({e}); retrying")
    raise RuntimeError(f"{model} failed after {retries + 1} attempt(s): {last_err}")


def main():
    buffer_raw = load_json(BUFFER_FILE, [])
    if not buffer_raw:
        _log("buffer is empty — nothing to finalize")
        return

    # buffer_id only needs to be unique within this one run, so it's
    # assigned fresh at load time rather than needing to be tracked
    # persistently across cycles.
    buffer = [{**item, "buffer_id": i} for i, item in enumerate(buffer_raw, start=1)]

    store = load_json(STORE_FILE, {"next_cluster_id": 1, "clusters": []})

    cutoff = datetime.now(timezone.utc) - timedelta(days=CANDIDATE_WINDOW_DAYS)
    candidate_clusters = [
        c for c in store["clusters"]
        if datetime.fromisoformat(c["last_updated_at"]) >= cutoff
    ]
    _log(
        f"{len(buffer)} buffered item(s), {len(candidate_clusters)} candidate cluster(s) "
        f"within {CANDIDATE_WINDOW_DAYS}-day window (of {len(store['clusters'])} total stored)"
    )

    prompt = build_connect_prompt(buffer, candidate_clusters)
    expected_ids = {item["buffer_id"] for item in buffer}
    valid_cluster_ids = {c["cluster_id"] for c in candidate_clusters}
    connections = run_stage(
        "connect-create", prompt, validate_connections,
        expected_ids, valid_cluster_ids, temperature=0.0,
    )

    clusters_by_id = {c["cluster_id"]: c for c in store["clusters"]}
    next_id = store["next_cluster_id"]
    now = _now_iso()
    new_count, connected_count = 0, 0

    for item in buffer:
        target_id = connections[item["buffer_id"]]
        if target_id is None:
            new_cluster = {
                "cluster_id": next_id,
                "created_at": now,
                "last_updated_at": now,
                "classification": item["classification"],
                "media_outlets": item["media_outlets"],
                "summary": item["summary"],
                "references": item["references"],
            }
            store["clusters"].append(new_cluster)
            clusters_by_id[next_id] = new_cluster
            next_id += 1
            new_count += 1
        else:
            cluster = clusters_by_id[target_id]
            cluster["last_updated_at"] = now
            cluster["media_outlets"] = sorted(set(cluster["media_outlets"]) | set(item["media_outlets"]))
            cluster["classification"] = sorted(set(cluster["classification"]) | set(item["classification"]))
            # Dedupe by URL before extending — this is what keeps a
            # re-run over an un-cleared buffer (the narrow crash window
            # noted below) from producing literal duplicate references,
            # even though it can't prevent a duplicate NEW cluster in
            # that same narrow window (see note in run comments below).
            existing_urls = {r["url"] for r in cluster["references"]}
            new_refs = [r for r in item["references"] if r["url"] not in existing_urls]
            cluster["references"].extend(new_refs)
            connected_count += 1

    store["next_cluster_id"] = next_id
    _log(f"{new_count} new cluster(s) created, {connected_count} item(s) connected to existing clusters")

    # Save the finalized store FIRST, atomically. Only clear the buffer
    # AFTER that write is confirmed on disk. If this process crashes in
    # the narrow window between these two writes, the buffer still has
    # items next run — they'll be reprocessed, which is mostly harmless
    # (the URL-dedupe above keeps a re-connected item from duplicating
    # references) but NOT fully idempotent for the create-new-cluster
    # case — a crash in that exact window could produce one duplicate
    # cluster. That residual risk is real and small; it is not silently
    # claimed to be fully solved.
    atomic_write(STORE_FILE, store)
    _log(f"saved {STORE_FILE}: {len(store['clusters'])} total cluster(s)")

    atomic_write(BUFFER_FILE, [])
    _log(f"cleared {BUFFER_FILE}")


if __name__ == "__main__":
    main()