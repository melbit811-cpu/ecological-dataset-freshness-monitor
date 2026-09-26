#!/usr/bin/env python3
"""
Ecological Dataset Freshness Monitor
=====================================

Checks a configured list of ecological data APIs (USGS NWIS, GBIF, EPA WQP,
ESA WorldCover, Global Forest Watch) for three failure modes:

  1. broken_api    -- request errors, times out, or returns non-2xx
  2. schema_change -- the response's top-level field set no longer matches
                       the stored baseline fingerprint
  3. stale         -- the most recent record is older than max_staleness_days

Usage:
    python monitor.py                    # run all checks, alert on issues
    python monitor.py --update-baseline  # accept current schema as the new baseline
    python monitor.py --dataset "GBIF"   # run just one dataset (substring match)

Exit code is non-zero if any dataset reports an issue -- useful for CI/cron
to distinguish "all healthy" runs from ones that need attention, independent
of whether a webhook is configured.
"""

import argparse
import csv
import io
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).parent
STATE_DIR = ROOT / "state"
BASELINE_PATH = STATE_DIR / "schema_baseline.json"
STATUS_PATH = STATE_DIR / "status.json"
REQUEST_TIMEOUT_SECONDS = 20


def load_config():
    with open(ROOT / "config.yaml") as f:
        return yaml.safe_load(f)


def load_baseline():
    if BASELINE_PATH.exists():
        with open(BASELINE_PATH) as f:
            return json.load(f)
    return {}


def save_baseline(baseline):
    STATE_DIR.mkdir(exist_ok=True)
    with open(BASELINE_PATH, "w") as f:
        json.dump(baseline, f, indent=2, sort_keys=True)


def dig(obj, dot_path):
    """Walk a dot-path like 'a.b.0.c' through nested dicts/lists. Returns None on any miss."""
    if not dot_path:
        return None
    current = obj
    for part in dot_path.split("."):
        try:
            if isinstance(current, list):
                current = current[int(part)]
            else:
                current = current[part]
        except (KeyError, IndexError, ValueError, TypeError):
            return None
    return current


def fingerprint_json(obj, level_path):
    """Return a sorted list of top-level field names at the given dot-path, for schema comparison."""
    target = dig(obj, level_path) if level_path else obj
    if isinstance(target, dict):
        return sorted(target.keys())
    if isinstance(target, list) and target and isinstance(target[0], dict):
        return sorted(target[0].keys())
    return None


def fingerprint_csv(text):
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
        return sorted(h.strip() for h in header)
    except StopIteration:
        return None


def parse_timestamp(value):
    if not value or not isinstance(value, str):
        return None
    # Try a handful of common formats seen across these APIs before giving up.
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(value, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    # Last resort: fromisoformat handles most remaining ISO-8601 variants.
    try:
        cleaned = value.replace("Z", "+00:00")
        return datetime.fromisoformat(cleaned)
    except ValueError:
        return None


def check_dataset(entry, baseline):
    name = entry["name"]
    result = {
        "name": name,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "broken_api": False,
        "schema_change": False,
        "stale": False,
        "detail": "",
    }

    try:
        resp = requests.get(entry["url"], timeout=REQUEST_TIMEOUT_SECONDS)
        resp.raise_for_status()
    except requests.RequestException as e:
        result["broken_api"] = True
        result["detail"] = f"Request failed: {e}"
        return result

    dtype = entry.get("type", "json")
    fingerprint = None

    if dtype == "json":
        try:
            payload = resp.json()
        except ValueError:
            result["broken_api"] = True
            result["detail"] = "Response was not valid JSON"
            return result

        fingerprint = fingerprint_json(payload, entry.get("schema_check_level", ""))

        freshness_field = entry.get("freshness_field", "")
        if freshness_field:
            raw_ts = dig(payload, freshness_field)
            ts = parse_timestamp(raw_ts) if isinstance(raw_ts, str) else None
            max_days = entry.get("max_staleness_days")
            if ts is None:
                result["detail"] += " Could not locate/parse freshness timestamp (field path may have changed)."
            elif max_days is not None:
                age_days = (datetime.now(timezone.utc) - ts).days
                if age_days > max_days:
                    result["stale"] = True
                    result["detail"] += f" Latest record is {age_days}d old (limit {max_days}d)."

    elif dtype == "csv":
        fingerprint = fingerprint_csv(resp.text)
        if fingerprint is None:
            result["broken_api"] = True
            result["detail"] = "CSV response had no header row / was empty"
            return result

    # Schema comparison against stored baseline (only for entries that produced a fingerprint)
    if fingerprint is not None:
        prev = baseline.get(name)
        if prev is None:
            result["detail"] += " No baseline yet -- run with --update-baseline to set one."
        elif prev != fingerprint:
            result["schema_change"] = True
            added = sorted(set(fingerprint) - set(prev))
            removed = sorted(set(prev) - set(fingerprint))
            result["detail"] += f" Schema changed. Added: {added or 'none'}; Removed: {removed or 'none'}."
        baseline[name] = fingerprint  # always update to latest for the next run's comparison

    result["detail"] = result["detail"].strip()
    return result


def send_webhook_alert(webhook_url, issues):
    lines = ["🚨 *Ecological Dataset Freshness Monitor* -- issue(s) detected:"]
    for r in issues:
        flags = ", ".join(
            f for f in ("broken_api", "schema_change", "stale") if r[f]
        )
        lines.append(f"- *{r['name']}* [{flags}]: {r['detail'] or 'see status.json'}")
    payload = {"text": "\n".join(lines)}
    try:
        requests.post(webhook_url, json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
    except requests.RequestException as e:
        print(f"WARNING: failed to send webhook alert: {e}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--update-baseline", action="store_true",
                         help="Accept current schema fingerprints as the new baseline without treating diffs as alerts")
    parser.add_argument("--dataset", default=None,
                         help="Only run datasets whose name contains this substring")
    args = parser.parse_args()

    config = load_config()
    baseline = load_baseline()
    results = []

    datasets = config["datasets"]
    if args.dataset:
        datasets = [d for d in datasets if args.dataset.lower() in d["name"].lower()]
        if not datasets:
            print(f"No dataset matched '{args.dataset}'", file=sys.stderr)
            sys.exit(2)

    for entry in datasets:
        if args.update_baseline:
            # Force this run's fingerprint to be accepted as-is (suppress the schema_change flag).
            r = check_dataset(entry, {**baseline, entry["name"]: None})
            baseline[entry["name"]] = None  # cleared so check_dataset can't diff against stale value
            r2 = check_dataset(entry, baseline)
            r2["schema_change"] = False
            results.append(r2)
        else:
            results.append(check_dataset(entry, baseline))

    save_baseline(baseline)

    STATE_DIR.mkdir(exist_ok=True)
    with open(STATUS_PATH, "w") as f:
        json.dump({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "results": results,
        }, f, indent=2)

    issues = [r for r in results if r["broken_api"] or r["schema_change"] or r["stale"]]

    for r in results:
        status = "ISSUE" if r in issues else "ok"
        print(f"[{status}] {r['name']}: {r['detail'] or 'healthy'}")

    if issues:
        webhook_url = os.environ.get(config.get("webhook_env_var", "WEBHOOK_URL"))
        if webhook_url:
            send_webhook_alert(webhook_url, issues)
        else:
            print("NOTE: issues found but no WEBHOOK_URL set -- skipping alert send.", file=sys.stderr)
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
