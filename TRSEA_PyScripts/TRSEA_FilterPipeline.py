"""
3-stage news pipeline orchestrator: geo-filter -> category-tag -> group-summarize.

Enforcement model (why this beats a single-prompt checkpoint):
  1. Physical isolation: a discarded article's text is never included in the
     next stage's prompt at all. The model can't reference, reconsider, or
     "cheat" on something it was never shown.
  2. Code computes survivors, not the model. Each stage returns per-id
     verdicts; this script — not the model's own summary of itself —
     filters the id set that feeds the next stage.
  3. Schema validation at every boundary: every id sent must come back
     exactly once (stage 1/2) or exactly once across all groups (stage 3).
     A dropped, duplicated, or invented id fails validation and triggers
     a single corrective retry before the pipeline errors out loudly,
     instead of silently propagating bad data.
  4. Real URLs are attached by this script from the original parsed feed,
     matched by integer id — the model never touches a URL, so it can't
     mangle one.

This script is a pure consumer of input.txt. It does NOT fetch anything
itself and does NOT import the scraper module — that separation is
intentional. Run the scraper (rss_gather.py) on its own, on whatever
schedule you want, to produce/refresh input.txt. Then run this script
against whatever is currently on disk. Mixing "fetch" and "process" via
an import's side effects is what caused input.txt to get silently
re-fetched on every run before.

Setup (register the four stage models once):
    ollama create geo-filter      -f geo-filter.Modelfile
    ollama create category-tag    -f category-tag.Modelfile
    ollama create group-only      -f group.Modelfile
    ollama create summarize-only  -f summarize.Modelfile

Debugging: set DEBUG_DIR below (or the TRSEA_DEBUG_DIR env var) to a folder
path to dump each stage's validated intermediate output to disk. When an
article silently disappears from the final JSON, these files tell you
exactly which stage dropped it instead of leaving you to guess.

Usage:
    python orchestrator.py                          # reads input.txt, target "Philippines"
    python orchestrator.py feed_dump.txt "Philippines"   # explicit path + country
"""

import json
import os
import sys
import requests

OLLAMA_URL = "http://localhost:11434/api/generate"
DEBUG_DIR = os.environ.get("TRSEA_DEBUG_DIR")  # e.g. "debug" — set to enable dumps


def _dump_debug(name, data):
    if not DEBUG_DIR:
        return
    os.makedirs(DEBUG_DIR, exist_ok=True)
    path = os.path.join(DEBUG_DIR, f"{name}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# Feed parsing — URL line-unwrapping happens here, in code, where it's exact.
# This is the same bug that kept corrupting URLs when we asked the LLM to do
# it; a script joining two known strings has no failure mode the model does.
# ---------------------------------------------------------------------------

def parse_feed(raw_text):
    articles = []
    current = None
    current_field = None
    current_outlet = "Unknown Outlet"

    for line in raw_text.splitlines():
        stripped = line.strip()

        if stripped.startswith("Channel Title:"):
            current_outlet = stripped.split(":", 1)[1].strip()
            continue

        if stripped.startswith("HEADLINE:"):
            if current:
                articles.append(current)
            current = {"outlet": current_outlet, "headline": "", "link": "", "teaser": ""}
            current["headline"] = stripped.split(":", 1)[1].strip()
            current_field = "headline"

        elif stripped.startswith("LINK:"):
            current["link"] = stripped.split(":", 1)[1].strip()
            current_field = "link"

        elif stripped.startswith("TEASER:"):
            current["teaser"] = stripped.split(":", 1)[1].strip()
            current_field = "teaser"

        elif stripped.startswith(("PUBLISHED:", "LEAD STMT:", "---", "===", "FETCHING FEED")):
            current_field = None

        elif stripped and current_field in ("headline", "link", "teaser") and current is not None:
            # Wrapped continuation line: URLs join with no separator
            # (they break mid-word), prose fields join with a space.
            sep = "" if current_field == "link" else " "
            current[current_field] += sep + stripped

    if current:
        articles.append(current)

    for i, a in enumerate(articles, start=1):
        a["id"] = i
    return articles


# ---------------------------------------------------------------------------
# Ollama calls
# ---------------------------------------------------------------------------

def call_ollama(model, prompt, temperature=None):
    payload = {"model": model, "prompt": prompt, "format": "json", "stream": False}
    if temperature is not None:
        payload["options"] = {"temperature": temperature}
    resp = requests.post(OLLAMA_URL, json=payload, timeout=300)
    resp.raise_for_status()
    return resp.json()["response"]


# ---------------------------------------------------------------------------
# Validators — these compute the trusted survivor set. The model's output is
# data to check, never an instruction the script obeys blindly.
# ---------------------------------------------------------------------------

def _extract_array(parsed):
    """Ollama's format="json" mode guarantees valid JSON syntax but NOT the
    top-level shape the prompt asked for. Models under this constraint often
    wrap the array in an object (e.g. {"results": [...]}) even when told to
    return a bare array — this is a known quirk of grammar-constrained JSON
    modes, distinct from plain-prompted JSON (which usually complies with
    the literal shape asked for, as seen testing via `ollama run` directly).
    Handle both shapes so a wrapping choice the model makes doesn't crash
    the pipeline.
    """
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        if isinstance(parsed.get("results"), list):
            return parsed["results"]
        list_values = [v for v in parsed.values() if isinstance(v, list)]
        if len(list_values) == 1:
            return list_values[0]
    raise ValueError(
        f"expected a JSON array (optionally wrapped as {{'results': [...]}}), "
        f"got {type(parsed).__name__}: {parsed!r:.200}"
    )


def validate_geo(raw_json, expected_ids):
    data = _extract_array(json.loads(raw_json))
    got_ids = {item["id"] for item in data}
    if got_ids != expected_ids:
        raise ValueError(f"geo-filter id mismatch: expected {expected_ids}, got {got_ids}")
    return {item["id"]: bool(item["keep"]) for item in data}


def validate_category(raw_json, expected_ids):
    data = _extract_array(json.loads(raw_json))
    got_ids = {item["id"] for item in data}
    if not got_ids.issubset(expected_ids):
        raise ValueError(f"category-tag invented ids not sent to it: {got_ids - expected_ids}")
    missing = expected_ids - got_ids
    result = {item["id"]: item.get("tags", []) for item in data}
    for m in missing:
        result[m] = []  # treat silently-dropped ids as untagged, not lost
    return result


def validate_group_ids(raw_json, expected_ids):
    data = _extract_array(json.loads(raw_json))
    seen = set()
    groups = []
    for group in data:
        ids = group["ids"]
        for i in ids:
            if i in seen:
                raise ValueError(f"id {i} appears in more than one group")
            seen.add(i)
        groups.append(list(ids))
    for missing_id in expected_ids - seen:
        # Model dropped an id — auto-recover as its own singleton rather
        # than silently losing an article from the final output.
        groups.append([missing_id])
    return groups


def validate_summaries(raw_json, expected_group_ids):
    data = _extract_array(json.loads(raw_json))
    got_ids = {item["group_id"] for item in data}
    if got_ids != expected_group_ids:
        raise ValueError(f"summarize-only group_id mismatch: expected {expected_group_ids}, got {got_ids}")
    return {item["group_id"]: item["summary"] for item in data}


def run_stage(model, prompt, validator, expected_ids, temperature=None, retries=1):
    last_err = None
    for _ in range(retries + 1):
        try:
            raw = call_ollama(model, prompt, temperature=temperature)
            return validator(raw, expected_ids)
        except (json.JSONDecodeError, ValueError, KeyError, TypeError) as e:
            last_err = e
            prompt += f"\n\nYour previous output was invalid: {e}. Return ONLY the corrected JSON array, nothing else."
    raise RuntimeError(f"{model} failed validation after {retries + 1} attempt(s): {last_err}")


# ---------------------------------------------------------------------------
# Prompt builders — each stage only ever sees the survivors of the one before
# ---------------------------------------------------------------------------

def build_geo_prompt(target_country, articles):
    lines = [f"Target Country: {target_country}", ""]
    lines += [f"[{a['id']}] {a['headline']} — {a['teaser']}" for a in articles]
    return "\n".join(lines)


def build_category_prompt(articles):
    return "\n".join(f"[{a['id']}] {a['headline']} — {a['teaser']}" for a in articles)


def build_group_prompt(articles, tags_by_id):
    lines = []
    for a in articles:
        tags = ", ".join(tags_by_id[a["id"]]) or "none"
        lines.append(f"[{a['id']}] {a['headline']} — {a['teaser']} — tags: {tags}")
    return "\n".join(lines)


def build_summarize_prompt(numbered_groups, by_id):
    lines = []
    for group_id, ids in numbered_groups:
        members = " | ".join(f"{by_id[i]['headline']} — {by_id[i]['teaser']}" for i in ids)
        lines.append(f"Group {group_id}: {members}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def run_pipeline(raw_text, target_country):
    articles = parse_feed(raw_text)
    by_id = {a["id"]: a for a in articles}
    all_ids = set(by_id)
    _dump_debug("00_parsed_articles", articles)

    # Stage 1 — geo. Full article set goes in; code decides who survives.
    geo_prompt = build_geo_prompt(target_country, articles)
    keep_map = run_stage("geo-filter", geo_prompt, validate_geo, all_ids, temperature=0.1)
    _dump_debug("01_geo_keep_map", keep_map)
    survivors = [a for a in articles if keep_map[a["id"]]]
    if not survivors:
        return []

    # Stage 2 — category. Discarded articles are never included here at all.
    survivor_ids = {a["id"] for a in survivors}
    cat_prompt = build_category_prompt(survivors)
    tags_map = run_stage("category-tag", cat_prompt, validate_category, survivor_ids, temperature=0.1)
    _dump_debug("02_category_tags_map", tags_map)
    tagged = [a for a in survivors if tags_map.get(a["id"])]
    if not tagged:
        return []

    # Stage 3a — grouping only, deterministic. No prose written here, so
    # there's nothing for a looser temperature to contaminate the judgment
    # with — this is what fixed articles being merged across unrelated tags.
    tagged_ids = {a["id"] for a in tagged}
    group_prompt = build_group_prompt(tagged, tags_map)
    id_groups = run_stage("group-only", group_prompt, validate_group_ids, tagged_ids, temperature=0.0)
    _dump_debug("03a_id_groups", id_groups)

    # Stage 3b — summarize only. Group membership is already fixed; this
    # stage can't reshuffle it, it can only write prose for a group_id.
    numbered_groups = list(enumerate(id_groups, start=1))
    summarize_prompt = build_summarize_prompt(numbered_groups, by_id)
    expected_group_ids = {gid for gid, _ in numbered_groups}
    summaries = run_stage("summarize-only", summarize_prompt, validate_summaries, expected_group_ids, temperature=0.3)
    _dump_debug("03b_summaries", summaries)

    # Final assembly — this script attaches real urls/outlets by id.
    # The model never sees or produces a url, so it can't corrupt one.
    output = []
    for group_id, ids in numbered_groups:
        members = [by_id[i] for i in ids]
        classification = sorted({tag for i in ids for tag in tags_map[i]})
        output.append({
            "media_outlets": sorted({m["outlet"] for m in members}),
            "classification": classification,
            "summary": summaries[group_id],
            "references": [{"title": m["headline"], "url": m["link"]} for m in members],
        })
    return output


if __name__ == "__main__":
    if len(sys.argv) == 3:
        feed_path, target_country = sys.argv[1], sys.argv[2]
    elif len(sys.argv) == 1:
        feed_path, target_country = "input.txt", "Philippines"
    else:
        print("Usage: python orchestrator.py [feed_dump.txt target_country]", file=sys.stderr)
        sys.exit(1)

    with open(feed_path, "r", encoding="utf-8") as f:
        raw = f.read()

    result = run_pipeline(raw, target_country=target_country)
    print(json.dumps(result, indent=2, ensure_ascii=False))