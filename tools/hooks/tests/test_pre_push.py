#!/usr/bin/env python3
"""Falsifier for tools/hooks/pre-push.

HUELLA-TEST-PREPUSH-EN-20261023

Every case is built in a temporary git repo and exercised through the real
pre-push protocol (refs on stdin). The criterion is not the exit code alone: the
specific rejection message is required. A hook broken by a syntax error also
returns RC != 0, so comparing RC only goes green on a gate that audits nothing.

The falsifier validates itself against two mutants:
  - exit 0 (accept everything) -> the suite fails on the cases that must reject
  - exit 1 (reject everything) -> the suite fails on the cases that must pass
"""

from __future__ import annotations

import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
HOOK = REPO_ROOT / "tools" / "hooks" / "pre-push"
MODELS = ["deepseek-4.1", "gpt-6-luna", "none"]

# (id, description, message, expected, expected substring in stderr)
CASES = [
    (1, "no Model:", "body without a trailer\n", "reject", "missing Model:"),
    (2, "1 valid Model trailer", "body\n\nModel: deepseek-4.1\n", "accept", None),
    (3, "1 invalid Model", "body\n\nModel: best-model-ever\n", "reject", "is not in models.txt"),
    (4, "1 trailer + 1 in prose", "Model: gpt-6-luna in prose\n\nModel: deepseek-4.1\n", "reject", "in the body (prose)"),
    (5, "2 trailers without Squash-Reason", "body\n\nModel: deepseek-4.1\nModel: gpt-6-luna\n", "reject", "Squash-Reason"),
    (6, "2 trailers + 2 Squash-Reason", "body\n\nModel: deepseek-4.1\nModel: gpt-6-luna\nSquash-Reason: a\nSquash-Reason: b\n", "reject", "Squash-Reason"),
    (7, "2 trailers + empty Squash-Reason", "body\n\nModel: deepseek-4.1\nModel: gpt-6-luna\nSquash-Reason:\n", "reject", "Squash-Reason"),
    (8, "2 trailers + valid Squash-Reason", "body\n\nModel: deepseek-4.1\nModel: gpt-6-luna\nSquash-Reason: convenience\n", "accept", None),
    (9, "violation with the diagnostic on stdout", "body\n", "reject", "missing Model:"),
    (10, "Model: none on an ordinary commit", "body\n\nModel: none\n", "reject", "Model: none is not valid"),
    (11, "empty Squash-Reason + 2 valid Model", "body\n\nModel: deepseek-4.1\nModel: gpt-6-luna\nSquash-Reason:\n", "reject", "Squash-Reason"),
    (12, "empty Model + valid Model", "body\n\nModel:\nModel: deepseek-4.1\n", "reject", "is empty"),
    (13, "whitespace-only Model", "body\n\nModel:\n", "reject", "is empty"),
    (14, "valid reason + empty reason", "body\n\nModel: deepseek-4.1\nModel: gpt-6-luna\nSquash-Reason: convenience\nSquash-Reason:\n", "reject", "Squash-Reason"),
    # 15: git normalises the trailing space of the message; only --cleanup=verbatim
    # preserves "Model:   ", which is the single path that exercises the
    # whitespace guard.
    (15, "whitespace-only Model: (verbatim)", "body\n\nModel:   \n", "reject", "is empty"),
    # 16/17: squash flow. A real squash leaves each commit's Model: as prose and
    # interleaves their subjects: appending a final block does NOT normalise the
    # message.
    (16, "badly normalised squash (append)", "c4-squash\n\nModel: deepseek-4.1\n\nc5-squash\n\nModel: gpt-6-luna\n\nModel: deepseek-4.1\nModel: gpt-6-luna\nSquash-Reason: group\n", "reject", "in the body (prose)"),
    (17, "normalised squash (rewrite)", "c4-squash\nc5-squash\n\nModel: deepseek-4.1\nModel: gpt-6-luna\nSquash-Reason: group fix and typo\n", "accept", None),
    (18, "Model none on a revert", "Revert \"previous change\"\n\nModel: none\n", "accept", None),
    (19, "Model none on automation", "automated process\n\nModel: none\n", "accept", None),
]

VERBATIM_CASES = {15}


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )


def _run_hook(repo: Path, hook: Path, sha: str, ref: str, shell: str = "bash") -> subprocess.CompletedProcess:
    stdin = f"refs/heads/{ref} {sha} refs/heads/{ref} {'0' * 40}\n"
    return subprocess.run(
        [shell, str(hook)], cwd=repo, input=stdin, capture_output=True, text=True, check=False
    )


class Fixture:
    """Repo with a hooks-epoch tag and one commit per case, created once."""

    def __init__(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="hooks_fixture_"))
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
        _git(self.repo, "tag", "hooks-epoch")

        self.shas: dict[int, str] = {}
        for cid, _desc, msg, _exp, _sub in CASES:
            msg_file = self.dir / f"msg{cid}"
            msg_file.write_text(msg)
            _git(self.repo, "checkout", "-q", "-B", f"b{cid}", "hooks-epoch")
            args = ["commit", "-q", "--allow-empty"]
            if cid in VERBATIM_CASES:
                args.append("--cleanup=verbatim")
            if cid == 19:
                args += ["--author=GitHub Actions[bot] <41898282+github-actions[bot]@users.noreply.github.com>"]
            args += ["-F", str(msg_file)]
            _git(self.repo, *args)
            self.shas[cid] = _git(self.repo, "rev-parse", "HEAD").stdout.strip()

        self.mutant_accept = self.dir / "mutant_accept_all"
        self.mutant_accept.write_text("#!/usr/bin/env bash\nexit 0\n")
        self.mutant_reject = self.dir / "mutant_reject_all"
        self.mutant_reject.write_text("#!/usr/bin/env bash\nexit 1\n")
        for p in (self.mutant_accept, self.mutant_reject):
            p.chmod(p.stat().st_mode | stat.S_IEXEC)

    def cleanup(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)


class TestPrePush(unittest.TestCase):
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
                p = _run_hook(self.fx.repo, HOOK, self.fx.shas[cid], f"b{cid}")
                if exp == "accept":
                    self.assertEqual(p.returncode, 0, f"case {cid} should pass: {p.stderr}")
                    self.assertNotIn("REJECTED", p.stderr, f"case {cid} passed with a rejection")
                else:
                    self.assertNotEqual(p.returncode, 0, f"case {cid} should reject")
                    self.assertIn(sub, p.stderr, f"case {cid}: unexpected message: {p.stderr!r}")

    def test_stdout_diagnostic_does_not_turn_the_gate_off(self) -> None:
        p = _run_hook(self.fx.repo, HOOK, self.fx.shas[9], "b9")
        self.assertEqual(p.returncode, 1)
        self.assertIn("SCANNED:", p.stdout, "the hook must print its diagnostic")

    def test_zero_count_under_set_eu(self) -> None:
        # HUELLA-HOOKS-CASO-DIEZ-RECHAZA-20261022 — case 10 (`Model: none`
        # on an ordinary commit) now REJECTS, so it cannot exercise a probe whose
        # criterion is `rc == 0`. It runs on case 2 (a single valid Model), which
        # leaves the Squash-Reason counts at zero — what `set -eu` used to kill
        # before the exit code was accumulated.
        stdin = f"refs/heads/b2 {self.fx.shas[2]} refs/heads/b2 {'0' * 40}\n"
        p = subprocess.run(
            ["bash", "-eu", str(HOOK)],
            cwd=self.fx.repo, input=stdin, capture_output=True, text=True, check=False,
        )
        self.assertEqual(p.returncode, 0, f"-eu aborted the audit: {p.stderr}")

    def test_accept_all_mutant_fails_the_suite(self) -> None:
        expected_failures = {cid for cid, _d, _m, exp, _s in CASES if exp == "reject"}
        survivors = {
            cid
            for cid in expected_failures
            if _run_hook(self.fx.repo, self.fx.mutant_accept, self.fx.shas[cid], f"b{cid}").returncode == 0
        }
        self.assertEqual(
            survivors, expected_failures, "the exit 0 mutant must be caught by every reject case"
        )

    def test_reject_all_mutant_fails_the_suite(self) -> None:
        accepted = {cid for cid, _d, _m, exp, _s in CASES if exp == "accept"}
        killed = {
            cid
            for cid in accepted
            if _run_hook(self.fx.repo, self.fx.mutant_reject, self.fx.shas[cid], f"b{cid}").returncode != 0
        }
        self.assertEqual(killed, accepted, "the exit 1 mutant must be caught by every accept case")

    EXEMPT = Path("tools") / "hooks" / "epoch-exempt.txt"

    def _exempt(self, text: str) -> Path:
        p = self.fx.repo / self.EXEMPT
        p.write_text(text)
        return p

    def test_declared_exemption_skips_the_commit(self) -> None:
        sha = self.fx.shas[1]  # message with no Model:
        p = self._exempt(f"{sha}\tdeclared reason\n")
        try:
            out = _run_hook(self.fx.repo, HOOK, sha, "b1")
            self.assertEqual(out.returncode, 0, f"an exempted commit should pass: {out.stderr}")
            self.assertIn("EXEMPTED: 1", out.stdout)
            self.assertNotIn("REJECTED", out.stderr)
        finally:
            p.unlink()

    def test_without_an_exemption_file_the_gate_is_strict(self) -> None:
        self.assertFalse((self.fx.repo / self.EXEMPT).exists())
        out = _run_hook(self.fx.repo, HOOK, self.fx.shas[1], "b1")
        self.assertNotEqual(out.returncode, 0, "with no exemption file nothing is exempted")
        self.assertIn("missing Model:", out.stderr)

    def test_exemption_without_a_reason_is_not_honoured(self) -> None:
        sha = self.fx.shas[1]
        p = self._exempt(f"{sha}\n")
        try:
            out = _run_hook(self.fx.repo, HOOK, sha, "b1")
            self.assertNotEqual(out.returncode, 0, "an exemption without a reason cannot pass")
            self.assertIn("missing Model:", out.stderr)
            self.assertIn("EXEMPTED: 0", out.stdout)
        finally:
            p.unlink()

    def test_exemption_for_another_sha_does_not_whitelist(self) -> None:
        p = self._exempt(f"{self.fx.shas[2]}\texempted on another commit\n")
        try:
            out = _run_hook(self.fx.repo, HOOK, self.fx.shas[1], "b1")
            self.assertNotEqual(out.returncode, 0, "an exemption cannot be global")
            self.assertIn("missing Model:", out.stderr)
        finally:
            p.unlink()

    def test_mechanism_commit_outside_main(self) -> None:
        repo = self.fx.repo
        _git(repo, "checkout", "-q", "-B", "bmec", "hooks-epoch")
        (repo / "docs").mkdir(exist_ok=True)
        (repo / "docs" / "mec.md").write_text("mec\n")
        _git(repo, "add", "-A")
        msg = self.fx.dir / "msg_mec"
        msg.write_text("mec\n\nModel: deepseek-4.1\n")
        _git(repo, "commit", "-q", "-F", str(msg))
        sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
        out = _run_hook(repo, HOOK, sha, "bmec")
        self.assertNotEqual(out.returncode, 0, "a mechanism commit outside main must reject")
        self.assertIn("outside_main", out.stderr)

    def test_spaces_require_verbatim(self) -> None:
        verbatim = _git(self.fx.repo, "log", "-1", "--format=%B", self.fx.shas[15]).stdout
        self.assertIn("Model:   ", verbatim, "--cleanup=verbatim must preserve the space")

        # Without verbatim, git normalises "Model:   " to "Model:" and the case
        # collapses into 13: the whitespace guard is not exercised the normal way.
        msg_file = self.fx.dir / "msg15normal"
        msg_file.write_text("body\n\nModel:   \n")
        _git(self.fx.repo, "checkout", "-q", "-B", "b15n", "hooks-epoch")
        _git(self.fx.repo, "commit", "-q", "--allow-empty", "-F", str(msg_file))
        sha = _git(self.fx.repo, "rev-parse", "HEAD").stdout.strip()
        normal = _git(self.fx.repo, "log", "-1", "--format=%B", sha).stdout
        self.assertNotIn("Model:   ", normal, "git normalised the space")
        self.assertEqual(normal, _git(self.fx.repo, "log", "-1", "--format=%B", self.fx.shas[13]).stdout)

        self.assertIn("REJECTED", _run_hook(self.fx.repo, HOOK, self.fx.shas[15], "b15").stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
