"""Update get.ocr from the console (the owner and admins).

Check: fetch the branch's upstream and list the commits waiting.
Update: fast-forward only (never over local changes), reinstall requirements
when requirements.txt changed, load the new code in a separate process to prove
it starts, roll back to the previous commit if it does not, then restart this
server in place (the same command line, a few seconds of downtime).
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import json
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_lock = threading.Lock()


class UpdateError(RuntimeError):
    pass


def _git(*args: str, timeout: int = 120) -> str:
    r = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise UpdateError(f"git {' '.join(args)}: {(r.stderr or r.stdout).strip()[:500]}")
    return r.stdout.strip()


def version() -> dict:
    try:
        return {"commit": _git("rev-parse", "--short", "HEAD"), "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
                "date": _git("log", "-1", "--format=%cI"), "subject": _git("log", "-1", "--format=%s")}
    except Exception as exc:  # noqa: BLE001 - not a git checkout (e.g. a Docker image)
        return {"commit": None, "error": str(exc)[:200]}


def check() -> dict:
    """What an update would bring in."""
    v = version()
    if not v.get("commit"):
        return {**v, "updatable": False, "reason": "not running from a git checkout (update the Docker image instead)"}
    try:
        upstream = _git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    except UpdateError:
        return {**v, "updatable": False, "reason": "this branch has no upstream to update from"}
    _git("fetch", "--quiet", timeout=180)
    behind = commits("HEAD", upstream)
    dirty = bool(_git("status", "--porcelain", "--untracked-files=no"))
    return {**v, "upstream": upstream, "updatable": bool(behind) and not dirty,
            "reason": "local changes in the checkout: update from the terminal" if dirty and behind else None,
            "waiting": behind}


def commits(old: str | None, new: str = "HEAD", limit: int = 200) -> list[dict]:
    """The commits in `new` that `old` lacks, newest first (just `new` when old is None)."""
    span = [f"{old}..{new}"] if old else ["-1", new]
    out = _git("log", f"--max-count={limit}", "--format=%h%x02%s%x02%cI%x02%b%x03", *span)
    rows = []
    for rec in out.split("\x03"):
        parts = rec.strip("\n").split("\x02")
        if len(parts) == 4:
            body = "\n".join(l for l in parts[3].strip().splitlines()
                             if not l.startswith(("Co-Authored-By:", "Claude-Session:"))).strip()
            rows.append({"commit": parts[0], "subject": parts[1], "date": parts[2], "details": body[:2000]})
    return rows


# --- change log: one entry per version this server has run --------------------------------
def _log_path() -> Path:
    from .config import get_settings
    return get_settings().data_dir / "changelog.jsonl"


def changelog(limit: int = 100) -> list[dict]:
    p = _log_path()
    if not p.exists():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows[::-1][:limit]


def record(entry: dict) -> dict:
    entry = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **entry}
    p = _log_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def _last_version() -> str | None:
    for e in changelog(limit=1000):
        if e.get("status", "updated") == "updated" and e.get("to"):
            return e["to"]
    return None


def note_startup() -> dict | None:
    """Record the running version if it differs from the last one logged, so an
    update made from the terminal (git pull + restart) is captured too."""
    v = version()
    if not v.get("commit"):
        return None
    head = _git("rev-parse", "HEAD")
    last = _last_version()
    if last and head.startswith(last):
        return None
    try:
        changes = commits(last) if last else commits(None)
        if last:
            _git("merge-base", "--is-ancestor", last, "HEAD")
    except UpdateError:  # the old commit is gone (history rewritten): log just the current one
        changes = commits(None)
    how = "first recorded version" if not last else "updated outside the console"
    return record({"status": "updated", "how": how, "by": None, "from": last[:7] if last else None,
                   "to": head[:7], "changes": changes})



def _run(cmd: list[str], timeout: int = 900) -> str:
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise UpdateError(f"{' '.join(cmd[:3])}…: {(r.stderr or r.stdout).strip()[-800:]}")
    return r.stdout


def update() -> dict:
    """Pull, install, verify; raises UpdateError (after rolling back) on failure."""
    if not _lock.acquire(blocking=False):
        raise UpdateError("an update is already running")
    try:
        info = check()
        if not info.get("updatable"):
            raise UpdateError(info.get("reason") or "already up to date")
        before = _git("rev-parse", "HEAD")
        req_before = (ROOT / "requirements.txt").read_bytes()
        steps = []
        _git("merge", "--ff-only", info["upstream"])
        steps.append(f"code {before[:7]} → {_git('rev-parse', '--short', 'HEAD')}")
        try:
            if (ROOT / "requirements.txt").read_bytes() != req_before:
                _run([sys.executable, "-m", "pip", "install", "-q", "-r", "requirements.txt"])
                steps.append("requirements installed")
            if (ROOT / "scripts" / "get_models.sh").exists():  # optional models: the service runs without them
                try:
                    _run(["bash", "scripts/get_models.sh"], timeout=600)
                except Exception:  # noqa: BLE001
                    steps.append("the optional models could not be fetched")
            # Prove the new code starts before this server is replaced.
            _run([sys.executable, "-c", "import app.main"], timeout=120)
            steps.append("new version loads")
        except Exception as exc:
            _git("reset", "--hard", before)
            raise UpdateError(f"rolled back to {before[:7]}: {exc}") from exc
        return {"steps": steps, "from": before[:7], "to": _git("rev-parse", "--short", "HEAD"),
                "applied": info["waiting"]}
    finally:
        _lock.release()


def restart_soon(delay: float = 1.0) -> None:
    """Replace this process with a fresh one, same command line, after the
    response has gone out. Under Docker / launchd the supervisor would do the
    same on exit."""
    def go():
        time.sleep(delay)
        os.execv(sys.executable, [sys.executable, *sys.argv])
    threading.Thread(target=go, daemon=True).start()
