# Accuracy test set

Real documents and their right answers go here; `scripts/evaluate.py` scores the service against them.
The documents themselves are not committed (`.gitignore` keeps everything in this folder out of git
except this file), so they stay on the machine.

```
eval/tnb-jan.pdf
eval/tnb-jan.expected.json
```

```json
{"type": "utility_bill", "fields": {"account_number": "220012345678", "total_due": 198.30, "due_date": "2026-02-04"}}
```

- `type`: the kind to extract as, or `auto` to test detection as well.
- `fields`: only the values you care about. Amounts as numbers, dates as `YYYY-MM-DD`.
- Lists of lines: the rows in order, each with the columns you care about, for example
  `"transactions": [{"date": "2026-01-02", "debit": 563.00}]`.
- For `"type": "custom"`, add `"fields_wanted"` with what to ask for, in the form `fields` takes.

```bash
.venv/bin/python scripts/evaluate.py eval
```

```bash
.venv/bin/python scripts/evaluate.py eval --models qwen2.5:7b,qwen2.5:14b --show
```
