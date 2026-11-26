"""One-tap update and change log, against a throwaway git remote."""
import subprocess

import pytest

from app import system
from tests.conftest import ADMIN


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    remote, work, dev = tmp_path / "remote.git", tmp_path / "work", tmp_path / "dev"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    subprocess.run(["git", "clone", "-q", str(remote), str(dev)], check=True)
    for d in (dev,):
        git(d, "config", "user.email", "t@example.com"); git(d, "config", "user.name", "t")
    (dev / "app").mkdir()
    (dev / "app" / "__init__.py").write_text("")
    (dev / "app" / "main.py").write_text("OK = 1\n")
    (dev / "scripts").mkdir()
    (dev / "scripts" / "get_models.sh").write_text("exit 0\n")
    (dev / "requirements.txt").write_text("")
    git(dev, "add", "."); git(dev, "commit", "-qm", "first")
    git(dev, "push", "-q", "-u", "origin", "main")
    subprocess.run(["git", "clone", "-q", str(remote), str(work)], check=True)
    monkeypatch.setattr(system, "ROOT", work)
    monkeypatch.setattr(system, "_log_path", lambda: tmp_path / "changelog.jsonl")
    restarts = []
    monkeypatch.setattr(system, "restart_soon", lambda *a: restarts.append(1))

    def push(subject, main="OK = 2\n", body=""):
        (dev / "app" / "main.py").write_text(main)
        git(dev, "commit", "-qam", subject + ("\n\n" + body if body else ""))
        git(dev, "push", "-q")
    return work, push, restarts


def test_check_lists_waiting_changes(repo):
    work, push, _ = repo
    assert system.check()["updatable"] is False
    push("Better statement reading", body="Reads the address too.\n\nCo-Authored-By: x")
    s = system.check()
    assert s["updatable"] and [c["subject"] for c in s["waiting"]] == ["Better statement reading"]
    assert s["waiting"][0]["details"] == "Reads the address too."


def test_update_fast_forwards_and_logs(repo, client):
    work, push, restarts = repo
    system.note_startup()
    push("New feature")
    r = client.post("/admin/api/system/update", headers=ADMIN)
    assert r.status_code == 200, r.text
    assert (work / "app" / "main.py").read_text() == "OK = 2\n" and restarts
    log = client.get("/admin/api/changelog", headers=ADMIN).json()["entries"]
    assert log[0]["status"] == "updated" and log[0]["how"] == "console"
    assert log[0]["changes"][0]["subject"] == "New feature"
    assert system.note_startup() is None  # the restart does not log it twice


def test_broken_version_rolls_back(repo, client):
    work, push, restarts = repo
    before = git(work, "rev-parse", "HEAD")
    push("Broken", main="raise SystemExit(1)\n")
    r = client.post("/admin/api/system/update", headers=ADMIN)
    assert r.status_code == 409 and "rolled back" in r.json()["message"]
    assert git(work, "rev-parse", "HEAD") == before and not restarts
    assert system.changelog()[0]["status"] == "failed"


def test_local_changes_block_update(repo):
    work, push, _ = repo
    (work / "app" / "main.py").write_text("OK = 99\n")
    push("Upstream change")
    s = system.check()
    assert not s["updatable"] and "local changes" in s["reason"]


def test_terminal_update_is_recorded(repo):
    work, push, _ = repo
    system.note_startup()
    push("Pulled by hand")
    git(work, "pull", "-q")
    e = system.note_startup()
    assert e["how"] == "updated outside the console" and e["changes"][0]["subject"] == "Pulled by hand"


def test_a_viewer_sees_the_changelog_but_cannot_update(repo, client):
    made = client.post("/admin/api/users", json={"name": "Looker", "role": "viewer"}, headers=ADMIN).json()
    viewer = {"X-Admin-Token": made["token"]}
    assert client.get("/admin/api/changelog", headers=viewer).status_code == 200
    assert client.get("/admin/api/system", headers=viewer).status_code == 403
    assert client.post("/admin/api/system/update", headers=viewer).status_code == 403
    assert client.get("/admin/api/changelog").status_code == 401
