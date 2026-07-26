"""Regression tests for git-as-memory — the third of the three original bugs.

The bug: `commit_memory` pushed unconditionally, and the VPS clone deliberately has no remote (it
is the live copy, synced by file transfer). So every single cycle logged "No configured push
destination" for no reason, and the real warnings drowned in it.

The other thing pinned here is the promise in the module docstring — never force-push. Memory is
the only asset this pipeline accumulates; a force-push is the one command that can erase it.

Run:  python -m pytest tests/test_git_memory.py -q
"""
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from brain import config, memory


class _Git:
    """Records every git invocation and replies from a scripted table."""

    def __init__(self, status="M brain-memory/PORTFOLIO.json", remote="origin", push_rc=0,
                 fail_on=None):
        self.calls, self.status, self.remote = [], status, remote
        self.push_rc, self.fail_on = push_rc, fail_on

    def __call__(self, argv, capture_output=True, text=True, check=True):
        assert argv[0] == "git"
        args = argv[3:]                       # strip `git -C <root>`
        self.calls.append(args)
        rc, out, err = 0, "", ""
        if args[0] == "status":
            out = self.status
        elif args[0] == "remote":
            out = self.remote
        elif args[0] == "push":
            rc, err = self.push_rc, "rejected" if self.push_rc else ""
        if self.fail_on and args[0] == self.fail_on:
            rc, err = 1, f"{self.fail_on} failed"
        if rc and check:
            raise subprocess.CalledProcessError(rc, argv, output=out, stderr=err)
        return subprocess.CompletedProcess(argv, rc, out, err)

    def verbs(self):
        return [c[0] for c in self.calls]


@pytest.fixture
def git(monkeypatch):
    g = _Git()
    monkeypatch.setattr(memory.subprocess, "run", g)
    return g


def _use(monkeypatch, **kw):
    g = _Git(**kw)
    monkeypatch.setattr(memory.subprocess, "run", g)
    return g


# ── the bug ────────────────────────────────────────────────────────────────────

def test_no_remote_means_no_push_attempt(monkeypatch):
    """The VPS clone has no remote on purpose. Asking it to push warned on every cycle."""
    g = _use(monkeypatch, remote="")
    assert memory.commit_memory("cycle") is True
    assert "push" not in g.verbs() and "pull" not in g.verbs()


def test_a_configured_remote_is_pulled_then_pushed(git):
    assert memory.commit_memory("cycle") is True
    assert git.verbs() == ["rev-parse", "add", "status", "commit", "remote", "pull", "push"]


def test_the_pull_rebases_rather_than_merging(git):
    memory.commit_memory("cycle")
    pull = next(c for c in git.calls if c[0] == "pull")
    assert "--rebase" in pull


def test_memory_is_never_force_pushed(git):
    """The one command that can destroy the accumulated journal."""
    memory.commit_memory("cycle")
    flat = [tok for c in git.calls for tok in c]
    assert not {"--force", "-f", "--force-with-lease"} & set(flat)


# ── the ordinary paths ─────────────────────────────────────────────────────────

def test_an_unchanged_memory_directory_does_not_commit(monkeypatch):
    g = _use(monkeypatch, status="")
    assert memory.commit_memory("cycle") is False
    assert "commit" not in g.verbs()


def test_outside_a_git_repo_it_is_a_quiet_no_op(monkeypatch):
    g = _use(monkeypatch, fail_on="rev-parse")
    assert memory.commit_memory("cycle") is False
    assert g.verbs() == ["rev-parse"]


def test_a_rejected_push_still_counts_as_committed(monkeypatch):
    """The commit landed locally. The next cycle's push will carry it — nothing is lost."""
    g = _use(monkeypatch, push_rc=1)
    assert memory.commit_memory("cycle") is True
    assert "push" in g.verbs()


def test_a_failed_commit_reports_failure(monkeypatch):
    _use(monkeypatch, fail_on="commit")
    assert memory.commit_memory("cycle") is False


def test_only_brain_memory_is_staged(git):
    memory.commit_memory("cycle")
    assert next(c for c in git.calls if c[0] == "add") == ["add", "brain-memory"]


def test_git_runs_against_the_repo_root_not_the_working_directory(monkeypatch):
    seen = {}

    def run(argv, **kw):
        seen.setdefault("argv", argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(memory.subprocess, "run", run)
    memory.commit_memory("cycle")
    assert seen["argv"][:3] == ["git", "-C", str(config.REPO_ROOT)]


def test_the_message_reaches_the_commit(git):
    memory.commit_memory("weekly review 2026-07-31")
    assert next(c for c in git.calls if c[0] == "commit") == [
        "commit", "-m", "weekly review 2026-07-31"]
