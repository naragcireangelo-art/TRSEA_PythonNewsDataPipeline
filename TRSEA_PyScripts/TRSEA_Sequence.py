import subprocess
import sys
import time
import os
import json

# Configuration
COUNTRY = "Philippines"
INPUT_FILE = "input.txt"
OUTPUT_FILE = "output.json"
TEMP_OUTPUT_FILE = "output.json.tmp"
BUFFER_FILE = "survivor_buffer.json"
POLLING_DELAY = 600  # 10 minutes


def append_to_buffer(new_items):
    """Adds this cycle's survivor groups onto the accumulating buffer that
    finalize.py later reads. Every cycle keeps ALL its stories here — this
    file only ever grows until finalize.py runs and clears it, unlike
    output.json which is overwritten fresh every cycle."""
    tmp_path = BUFFER_FILE + ".tmp"
    existing = []
    if os.path.exists(BUFFER_FILE):
        with open(BUFFER_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if content:
                existing = json.loads(content)
    existing.extend(new_items)
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, BUFFER_FILE)  # atomic swap, same as output.json


def run_pipeline_cycle():
    print(f"\n{'='*50}")
    print(f"Starting new cycle for {COUNTRY} at {time.strftime('%X')}")
    print(f"{'='*50}")

    # 1. Run the Scraper. subprocess.run (not import) means this file's
    # top-level fetch code runs exactly once, exactly here — no hidden
    # side effects on anyone importing this module for a helper function.
    print("\n[1/2] Running Data Gatherer...")
    gather_process = subprocess.run([sys.executable, "TRSEA_Gatherscrpt.py"])

    if gather_process.returncode != 0:
        print("Error: gather script failed. Skipping AI step for this cycle.")
        return

    # 2. Run the AI Orchestrator. Write to a temp file first — if the AI
    # pipeline fails partway through, output.json (the last good result)
    # is never touched. Only a confirmed success replaces it.
    print(f"\n[2/2] Running AI Pipeline (Target: {COUNTRY})...")

    with open(TEMP_OUTPUT_FILE, "w", encoding="utf-8") as out_file:
        ai_process = subprocess.run(
            [sys.executable, "TRSEA_FilterPipeline.py", INPUT_FILE, COUNTRY],
            stdout=out_file,
            stderr=sys.stderr,
        )

    if ai_process.returncode == 0:
        os.replace(TEMP_OUTPUT_FILE, OUTPUT_FILE)  # atomic swap, POSIX and Windows
        print(f"Success! Results saved to {OUTPUT_FILE}")

        # This cycle's stories join the buffer for finalize.py to
        # consolidate later — output.json stays a per-cycle snapshot,
        # the buffer is where cross-cycle history accumulates.
        with open(OUTPUT_FILE, "r", encoding="utf-8",errors="replace") as f:
            this_cycle_items = json.load(f)
        append_to_buffer(this_cycle_items)
        print(f"Added {len(this_cycle_items)} item(s) to {BUFFER_FILE} for the next finalize pass.")
    else:
        print("Error: AI Pipeline failed. Previous output.json left untouched.")


if __name__ == "__main__":
    while True:
        run_pipeline_cycle()
        print(f"\nSleeping for {POLLING_DELAY / 60} minutes...")
        time.sleep(POLLING_DELAY)