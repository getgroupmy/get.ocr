#!/usr/bin/env python3
"""Measure how well the service extracts your own documents.

Put documents in a folder, each with the right answer beside it:

    eval/tnb-jan.pdf
    eval/tnb-jan.expected.json     {"type": "utility_bill", "fields": {"total_due": 198.30, "due_date": "2026-02-04"}}

List only the fields you care about. For a list of lines (transactions, line items), give the rows in
order with the columns you care about. Then, on the machine the service runs on:

    .venv/bin/python scripts/evaluate.py eval
    .venv/bin/python scripts/evaluate.py eval --models qwen2.5:7b,qwen2.5:14b    # compare installed models
    .venv/bin/python scripts/evaluate.py eval --engine glm                       # read with the vision model

It calls the console's own endpoint, so nothing is stored or counted, and prints for each kind of document how
many fields were right. The admin token is taken from OCR_ADMIN_TOKEN or the .env file."""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time

import httpx


def token() -> str:
    if os.environ.get("OCR_ADMIN_TOKEN"):
        return os.environ["OCR_ADMIN_TOKEN"]
    env = pathlib.Path(__file__).resolve().parent.parent / ".env"
    for line in env.read_text().splitlines() if env.exists() else []:
        if line.startswith("OCR_ADMIN_TOKEN="):
            return line.split("=", 1)[1].strip().strip('"')
    sys.exit("No admin token: set OCR_ADMIN_TOKEN or run from a checkout with a .env file.")


def same(want, got) -> bool:  # noqa: ANN001
    if want is None or got is None:
        return want is None and got is None
    if isinstance(want, bool) or isinstance(got, bool):
        return want == got
    if isinstance(want, (int, float)):
        try:
            return abs(float(got) - float(want)) <= 0.011
        except (TypeError, ValueError):
            return False
    return "".join(str(want).lower().split()) == "".join(str(got).lower().split())


def compare(want: dict, got: dict) -> tuple[int, int, list[str]]:
    """(right, total, what was wrong)."""
    right, total, wrong = 0, 0, []
    for key, value in want.items():
        if isinstance(value, list):
            rows = got.get(key) if isinstance(got.get(key), list) else []
            if len(rows) != len(value):
                wrong.append(f"{key}: {len(rows)} rows, expected {len(value)}")
            for i, row in enumerate(value):
                for col, cell in row.items():
                    total += 1
                    have = rows[i].get(col) if i < len(rows) and isinstance(rows[i], dict) else None
                    if same(cell, have):
                        right += 1
                    else:
                        wrong.append(f"{key}[{i}].{col}: got {have!r}, expected {cell!r}")
        else:
            total += 1
            if same(value, got.get(key)):
                right += 1
            else:
                wrong.append(f"{key}: got {got.get(key)!r}, expected {value!r}")
    return right, total, wrong


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("folder")
    ap.add_argument("--url", default="http://127.0.0.1:8090")
    ap.add_argument("--models", default="", help="installed extraction models to compare, separated by commas")
    ap.add_argument("--engine", default="standard", choices=["standard", "glm"])
    ap.add_argument("--show", action="store_true", help="list every wrong field")
    args = ap.parse_args()
    cases = sorted(pathlib.Path(args.folder).glob("*.expected.json"))
    if not cases:
        sys.exit(f"No *.expected.json files in {args.folder}. See the top of this script for the layout.")
    headers = {"X-Admin-Token": token()}
    for model in [m.strip() for m in args.models.split(",") if m.strip()] or [""]:
        by_kind: dict = {}
        print(f"\n=== model: {model or 'the configured one'} · reader: {args.engine} ===")
        for case in cases:
            want = json.loads(case.read_text())
            stem = case.name[:-len(".expected.json")]
            doc = next((p for p in case.parent.glob(stem + ".*") if not p.name.endswith(".json")), None)
            if doc is None:
                print(f"  {stem}: no document beside the expected answer, skipped")
                continue
            data = {"type": want.get("type", "auto"), "engine": args.engine}
            if model:
                data["model"] = model
            if want.get("fields_wanted"):
                data["fields"] = json.dumps(want["fields_wanted"])
            started = time.time()
            try:
                r = httpx.post(args.url + "/admin/api/try", headers=headers, data=data,
                               files={"file": (doc.name, doc.read_bytes())}, timeout=1800)
                body = r.json()
            except (httpx.HTTPError, ValueError) as exc:
                body, r = {"error": "no_answer", "message": str(exc)}, None
            took = time.time() - started
            kind = by_kind.setdefault(want.get("type", "auto"), {"right": 0, "total": 0, "docs": 0, "perfect": 0,
                                                                 "checks_ok": 0, "seconds": 0.0, "failed": 0})
            kind["docs"] += 1
            kind["seconds"] += took
            if r is None or r.status_code != 200:
                kind["failed"] += 1
                n = sum(len(v) * len(v[0]) if isinstance(v, list) and v else 1 for v in want.get("fields", {}).values())
                kind["total"] += n
                print(f"  {stem}: FAILED {body.get('error')}: {body.get('message')}")
                continue
            right, total, wrong = compare(want.get("fields", {}), body.get("fields", {}))
            kind["right"] += right
            kind["total"] += total
            kind["perfect"] += right == total
            kind["checks_ok"] += body.get("checks_passed") is True
            flag = "" if body.get("document_type") == want.get("type", body.get("document_type")) else \
                f" (detected as {body.get('document_type')})"
            print(f"  {stem}: {right}/{total} fields, checks {body.get('checks_passed')}, {took:.0f} s{flag}")
            for w in wrong if args.show else wrong[:3]:
                print(f"      {w}")
            if len(wrong) > 3 and not args.show:
                print(f"      … {len(wrong) - 3} more (--show lists all)")
        print(f"\n  {'kind':<24}{'docs':>5}{'fields right':>15}{'all right':>11}{'checks ok':>11}{'s/doc':>8}")
        for name, k in sorted(by_kind.items()):
            share = f"{k['right']}/{k['total']} ({100 * k['right'] / k['total']:.0f}%)" if k["total"] else "–"
            print(f"  {name:<24}{k['docs']:>5}{share:>15}{k['perfect']:>11}{k['checks_ok']:>11}"
                  f"{k['seconds'] / k['docs']:>8.0f}")


if __name__ == "__main__":
    main()
