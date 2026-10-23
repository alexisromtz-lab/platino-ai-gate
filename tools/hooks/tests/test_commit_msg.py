#!/usr/bin/env python3
"""Falsifier for tools/hooks/commit-msg.

HUELLA-TEST-COMMITMSG-MATRIZ-20261022
HUELLA-TEST-COMMITMSG-EN-20261023

The hook is exercised the way git exercises it: with the message file path as the
first argument. The criterion is not the exit code alone — a hook broken by a
syntax error also returns RC != 0 — but the specific rejection message and the
effect on the file, which the hook must NOT touch.

The matrix is the same one pre-push uses (a single rule, two moments), plus the
two cases that only exist at creation time:
  - comment lines (`# Model: ...`): git strips them AFTER the hook runs, so if
    the hook does not strip them first, the template would sign the commit by
    itself.
  - merge in progress: pre-push skips merges with --no-merges; here the same
    boundary is declared instead of demanding a trailer from whoever did not
    write the message.

The falsifier validates itself against two mutants:
  - exit 0 (accept everything) -> the suite fails on the cases that must reject
  - exit 1 (reject everything) -> the suite fails on the cases that must pass
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
HOOK = REPO_ROOT / "tools" / "hooks" / "commit-msg"
MODELS = ["deepseek/deepseek-flash", "openai/OpenAI/gpt-6-luna", "none"]

# (id, description, message, expected, expected substring in stderr)
CASES = [
    (1, "no Model:", "subject\n\nbody\n", "reject", "does not declare a Model:"),
    (2, "1 valid Model", "subject\n\nbody\n\nCo-authored-by: X <x@y>\nModel: deepseek/deepseek-flash\n", "accept", None),
    (3, "1 Model outside the allowlist", "subject\n\nModel: best-model-ever\n", "reject", "is not in models.txt"),
    (4, "Model in prose", "subject\n\nModel: deepseek/deepseek-flash\n\nbody after it\n", "reject", "in the body (prose)"),
    (5, "2 trailers without Squash-Reason", "subject\n\nModel: deepseek/deepseek-flash\nModel: openai/OpenAI/gpt-6-luna\n", "reject", "Squash-Reason"),
    (6, "2 trailers + valid Squash-Reason", "subject\n\nModel: deepseek/deepseek-flash\nModel: openai/OpenAI/gpt-6-luna\nSquash-Reason: two tabs\n", "accept", None),
    (7, "empty Model", "subject\n\nModel:\n", "reject", "is empty"),
    (8, "Model only in a template comment", "subject\n\nCo-authored-by: X <x@y>\n# Model: deepseek/deepseek-flash\n", "reject", "does not declare a Model:"),
    (9, "Model: none on an ordinary commit", "subject\n\nModel: none\n", "reject", "Model: none is not valid"),
    # 10: `interpret-trailers` trims trailing spaces from the value, so a valid id
    # with trailing spaces is valid in BOTH gates. The agreement is pinned down
    # instead of inventing an asymmetry: the whitespace-only case is 11.
    # HUELLA-TEST-COMMITMSG-PARIDAD-ESPACIOS-20261022
    (10, "Model with trailing spaces (parity with pre-push)", "subject\n\nModel: deepseek/deepseek-flash   \n", "accept", None),
    (11, "whitespace-only Model (empty)", "subject\n\nModel:   \n", "reject", "is empty"),
    (12, "Model none on a revert", "Revert \"previous change\"\n\nModel: none\n", "accept", None),
    (13, "Model none on automation", "automated subject\n\nModel: none\n", "accept", None),
]


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )


def _run_hook(
    repo: Path,
    hook: Path,
    msg_file: Path,
    shell: str = "bash",
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [shell, str(hook), str(msg_file)],
        cwd=repo, env=env, capture_output=True, text=True, check=False,
    )


class Fixture:
    """Temporary repo with the allowlist; one message file per case."""

    def __init__(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="commitmsg_fixture_"))
        self.repo = self.dir / "repo"
        (self.repo / "tools" / "hooks").mkdir(parents=True)
        (self.repo / "tools" / "hooks" / "models.txt").write_text(
            "# model allowlist (picker ids)\n" + "\n".join(MODELS) + "\n"
        )
        _git(self.repo, "init", "-q", "-b", "main", ".")
        _git(self.repo, "config", "user.email", "hook@test.local")
        _git(self.repo, "config", "user.name", "hook test")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "base")

        self.msgs: dict[int, Path] = {}
        for cid, _desc, msg, _exp, _sub in CASES:
            p = self.dir / f"msg{cid}"
            p.write_text(msg)
            self.msgs[cid] = p

        self.mutant_accept = self.dir / "mutant_accept_all"
        self.mutant_accept.write_text("#!/usr/bin/env bash\nexit 0\n")
        self.mutant_reject = self.dir / "mutant_reject_all"
        self.mutant_reject.write_text("#!/usr/bin/env bash\nexit 1\n")
        for p in (self.mutant_accept, self.mutant_reject):
            p.chmod(p.stat().st_mode | stat.S_IEXEC)

    def merge_head(self) -> Path:
        return self.repo / ".git" / "MERGE_HEAD"

    def cleanup(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)


class TestCommitMsg(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not HOOK.is_file():
            raise unittest.SkipTest(f"hook missing: {HOOK}")
        cls.fx = Fixture()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.fx.cleanup()

    def test_cases_against_the_correct_hook(self) -> None:
        for cid, desc, _msg, exp, sub in CASES:
            with self.subTest(case=cid, desc=desc):
                env = None
                if cid == 13:
                    env = dict(os.environ, GIT_AUTHOR_EMAIL="41898282+github-actions[bot]@users.noreply.github.com")
                p = _run_hook(self.fx.repo, HOOK, self.fx.msgs[cid], env=env)
                if exp == "accept":
                    self.assertEqual(p.returncode, 0, f"case {cid} should pass: {p.stderr}")
                    self.assertNotIn("REJECTED", p.stderr, f"case {cid} passed with a rejection")
                else:
                    self.assertNotEqual(p.returncode, 0, f"case {cid} should reject")
                    self.assertIn(sub, p.stderr, f"case {cid}: unexpected message: {p.stderr!r}")

    def test_the_hook_does_not_mutate_the_message(self) -> None:
        before = {cid: p.read_bytes() for cid, p in self.fx.msgs.items()}
        for cid in self.fx.msgs:
            _run_hook(self.fx.repo, HOOK, self.fx.msgs[cid])
        for cid, p in self.fx.msgs.items():
            self.assertEqual(p.read_bytes(), before[cid], f"case {cid}: the hook rewrote the message")

    def test_merge_in_progress_requires_no_trailer(self) -> None:
        mh = self.fx.merge_head()
        mh.write_text(_git(self.fx.repo, "rev-parse", "HEAD").stdout.strip() + "\n")
        try:
            p = _run_hook(self.fx.repo, HOOK, self.fx.msgs[1])
        finally:
            mh.unlink(missing_ok=True)
        self.assertEqual(p.returncode, 0, f"a merge must not demand Model: {p.stderr}")

    def test_accept_all_mutant_fails_the_matrix(self) -> None:
        rejections = [cid for cid, _d, _m, exp, _s in CASES if exp == "reject"]
        failures = [
            cid for cid in rejections
            if _run_hook(self.fx.repo, self.fx.mutant_accept, self.fx.msgs[cid]).returncode == 0
        ]
        self.assertEqual(len(failures), len(rejections), "the accept-all mutant must expose itself on every rejection")

    def test_reject_all_mutant_fails_the_matrix(self) -> None:
        accepted = [cid for cid, _d, _m, exp, _s in CASES if exp == "accept"]
        failures = [
            cid for cid in accepted
            if _run_hook(self.fx.repo, self.fx.mutant_reject, self.fx.msgs[cid]).returncode != 0
        ]
        self.assertEqual(len(failures), len(accepted), "the reject-all mutant must expose itself on every acceptance")
