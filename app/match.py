"""Compares what was extracted from one document with another: an invoice against
its purchase order or delivery order, and payments against the lines of a bank
statement. Plain arithmetic and text comparison on the fields; no model."""
from __future__ import annotations

import re
from datetime import date
from typing import Optional

TOLERANCE = 0.05


def _key(text) -> str:  # noqa: ANN001
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def _alike(a, b) -> float:  # noqa: ANN001
    """0 to 1: how much two descriptions share, by their words."""
    wa, wb = set(re.findall(r"[a-z0-9]+", str(a or "").lower())), set(re.findall(r"[a-z0-9]+", str(b or "").lower()))
    return len(wa & wb) / max(len(wa | wb), 1)


def _near(a, b) -> bool:  # noqa: ANN001
    return a is not None and b is not None and abs(float(a) - float(b)) <= TOLERANCE + 1e-9


def compare(a: dict, b: dict) -> dict:
    """Two sales documents (their `fields`): which lines agree, which differ, which are only on one."""
    left, right = list(a.get("line_items") or []), list(b.get("line_items") or [])
    used: set = set()
    lines = []
    for item in left:
        best = max(((i, _alike(item.get("description"), other.get("description"))) for i, other in enumerate(right)
                    if i not in used), key=lambda x: x[1], default=(None, 0.0))
        if best[0] is None or best[1] < 0.4:
            lines.append({"status": "only_in_a", "a": item, "b": None, "differences": []})
            continue
        used.add(best[0])
        other = right[best[0]]
        diffs = [k for k in ("quantity", "unit_price", "amount")
                 if item.get(k) is not None and other.get(k) is not None and not _near(item[k], other[k])]
        lines.append({"status": "differs" if diffs else "same", "a": item, "b": other, "differences": diffs})
    lines += [{"status": "only_in_b", "a": None, "b": other, "differences": []}
              for i, other in enumerate(right) if i not in used]
    totals = {k: {"a": a.get(k), "b": b.get(k), "same": _near(a.get(k), b.get(k))}
              for k in ("subtotal", "tax", "total") if a.get(k) is not None and b.get(k) is not None}
    parties = {k: {"a": a.get(k), "b": b.get(k), "same": _alike(a.get(k), b.get(k)) >= 0.6}
               for k in ("supplier_name", "customer_name") if a.get(k) and b.get(k)}
    ok = all(ln["status"] == "same" for ln in lines) and all(t["same"] for t in totals.values())
    return {"agree": ok, "lines": lines, "totals": totals, "parties": parties}


def _day(text) -> Optional[date]:  # noqa: ANN001
    try:
        return date.fromisoformat(str(text)[:10])
    except ValueError:
        return None


def reconcile(statement: dict, documents: list[dict], days: int = 7) -> dict:
    """Which statement line pays which document. A line matches a document when the amount is the
    document's total, within `days` of its date; a line that names the document's number or the
    other party is preferred. Each statement line is used once."""
    txns = list(statement.get("transactions") or [])
    used: set = set()
    out = []
    for n, doc in enumerate(documents):
        f = doc.get("fields") or doc
        total, when = f.get("total") if f.get("total") is not None else f.get("total_due"), _day(f.get("date"))
        best, best_score = None, 0.0
        for i, t in enumerate(txns):
            amount = t.get("debit") if t.get("debit") is not None else t.get("credit")
            if i in used or not _near(amount, total):
                continue
            paid = _day(t.get("date"))
            if when and paid and abs((paid - when).days) > days and paid < when:
                continue  # paid long before the document existed
            text = _key(t.get("description")) + _key(t.get("reference"))
            score = 1.0 + (2.0 if f.get("document_number") and _key(f["document_number"]) in text else 0.0)
            score += max((_alike(f.get(k), t.get("description")) for k in ("supplier_name", "customer_name", "provider_name")),
                         default=0.0)
            if when and paid:
                score += max(0.0, 1 - abs((paid - when).days) / max(days, 1)) * 0.5
            if score > best_score:
                best, best_score = i, score
        if best is not None:
            used.add(best)
        out.append({"document": doc.get("ref", n), "total": total, "matched": best is not None,
                    "transaction_index": best, "transaction": txns[best] if best is not None else None,
                    "by": None if best is None else "reference" if best_score >= 3 else "amount"})
    return {"documents": out, "unmatched_transactions": [i for i in range(len(txns)) if i not in used],
            "all_matched": all(d["matched"] for d in out)}
