#!/usr/bin/env python3
"""Falsificador de tools/hooks/pre-push.

Cada caso se construye en un repo git temporal y se ejerce por el protocolo real
de pre-push (refs por stdin). El criterio no es solo el RC: se exige el mensaje
de rechazo específico. Un hook roto por sintaxis también devuelve RC != 0, así
que comparar solo RC da verde sobre un gate que no audita nada.

El falsificador se valida a sí mismo contra dos mutantes:
  - exit 0 (acepta todo)  -> la suite falla por los casos que deben rechazar
  - exit 1 (rechaza todo) -> la suite falla por los casos que deben pasar
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

# (id, descripción, mensaje, esperado, subcadena esperada en stderr)
CASES = [
    (1, "sin Model:", "cuerpo sin trailer\n", "reject", "sin Model:"),
    (2, "1 Model valido trailer", "cuerpo\n\nModel: deepseek-4.1\n", "accept", None),
    (3, "1 Model invalido", "cuerpo\n\nModel: best-model-ever\n", "reject", "no en models.txt"),
    (4, "1 trailer + 1 prosa", "Model: gpt-6-luna en prosa\n\nModel: deepseek-4.1\n", "reject", "en prosa"),
    (5, "2 trailers sin Squash-Reason", "cuerpo\n\nModel: deepseek-4.1\nModel: gpt-6-luna\n", "reject", "Squash-Reason"),
    (6, "2 trailers + 2 Squash-Reason", "cuerpo\n\nModel: deepseek-4.1\nModel: gpt-6-luna\nSquash-Reason: a\nSquash-Reason: b\n", "reject", "Squash-Reason"),
    (7, "2 trailers + Squash-Reason vacio", "cuerpo\n\nModel: deepseek-4.1\nModel: gpt-6-luna\nSquash-Reason:\n", "reject", "Squash-Reason"),
    (8, "2 trailers + Squash-Reason valido", "cuerpo\n\nModel: deepseek-4.1\nModel: gpt-6-luna\nSquash-Reason: convenience\n", "accept", None),
    (9, "violacion con diagnostico en stdout", "cuerpo\n", "reject", "sin Model:"),
    (10, "Model: none en commit ordinario", "cuerpo\n\nModel: none\n", "reject", "Model: none no es válido"),
    (11, "Squash-Reason vacio + 2 Model validos", "cuerpo\n\nModel: deepseek-4.1\nModel: gpt-6-luna\nSquash-Reason:\n", "reject", "Squash-Reason"),
    (12, "Model vacio + Model valido", "cuerpo\n\nModel:\nModel: deepseek-4.1\n", "reject", "vac"),
    (13, "Model vacio solo", "cuerpo\n\nModel:\n", "reject", "vac"),
    (14, "razon valida + razon vacia", "cuerpo\n\nModel: deepseek-4.1\nModel: gpt-6-luna\nSquash-Reason: convenience\nSquash-Reason:\n", "reject", "Squash-Reason"),
    # 15: git normaliza el espacio final del mensaje; sólo --cleanup=verbatim
    # conserva "Model:   ", que es la única vía que ejerce la guarda de espacios.
    (15, "Model: solo espacios (verbatim)", "cuerpo\n\nModel:   \n", "reject", "vac"),
    # 16/17: flujo de squash. El squash real deja los Model: de cada commit como
    # prosa e intercala sus asuntos: añadir un bloque final NO normaliza el mensaje.
    (16, "squash mal normalizado (append)", "c4-squash\n\nModel: deepseek-4.1\n\nc5-squash\n\nModel: gpt-6-luna\n\nModel: deepseek-4.1\nModel: gpt-6-luna\nSquash-Reason: agrupar\n", "reject", "en prosa"),
    (17, "squash normalizado (reescritura)", "c4-squash\nc5-squash\n\nModel: deepseek-4.1\nModel: gpt-6-luna\nSquash-Reason: agrupar fix y typo\n", "accept", None),
    (18, "Model none en revert", "Revert \"cambio anterior\"\n\nModel: none\n", "accept", None),
    (19, "Model none en automatización", "proceso automático\n\nModel: none\n", "accept", None),
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
    """Repo con tag hooks-epoch y un commit por caso, creado una vez."""

    def __init__(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="hooks_fixture_"))
        self.repo = self.dir / "repo"
        (self.repo / "tools" / "hooks").mkdir(parents=True)
        (self.repo / "tools" / "hooks" / "models.txt").write_text(
            "# enum de modelos (IDs del picker)\n" + "\n".join(MODELS) + "\n"
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
            raise unittest.SkipTest(f"hook ausente: {HOOK}")
        cls.fx = Fixture()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.fx.cleanup()

    def test_casos_contra_hook_correcto(self) -> None:
        for cid, desc, _msg, exp, sub in CASES:
            with self.subTest(caso=cid, desc=desc):
                p = _run_hook(self.fx.repo, HOOK, self.fx.shas[cid], f"b{cid}")
                if exp == "accept":
                    self.assertEqual(p.returncode, 0, f"caso {cid} debía pasar: {p.stderr}")
                    self.assertNotIn("RECHAZO", p.stderr, f"caso {cid} pasó con rechazo")
                else:
                    self.assertNotEqual(p.returncode, 0, f"caso {cid} debía rechazar")
                    self.assertIn(sub, p.stderr, f"caso {cid}: mensaje inesperado: {p.stderr!r}")

    def test_diagnostico_en_stdout_no_apaga_el_gate(self) -> None:
        p = _run_hook(self.fx.repo, HOOK, self.fx.shas[9], "b9")
        self.assertEqual(p.returncode, 1)
        self.assertIn("SCANNED:", p.stdout, "el hook debe imprimir el diagnóstico")

    def test_conteo_cero_bajo_set_eu(self) -> None:
        # HUELLA-HOOKS-CASO-DIEZ-RECHAZA-20261022 — el caso 10 (`Model: none`
        # en commit ordinario) ahora RECHAZA, asi que no puede medir una sonda
        # cuyo criterio es `rc == 0`. Se ejerce sobre el caso 2 (un solo Model
        # valido): deja en cero los recuentos de Squash-Reason, que es lo que
        # `set -eu` mataba antes de acumular el RC.
        stdin = f"refs/heads/b2 {self.fx.shas[2]} refs/heads/b2 {'0' * 40}\n"
        p = subprocess.run(
            ["bash", "-eu", str(HOOK)],
            cwd=self.fx.repo, input=stdin, capture_output=True, text=True, check=False,
        )
        self.assertEqual(p.returncode, 0, f"-eu abortó la auditoría: {p.stderr}")

    def test_mutante_acepta_todo_falla_la_suite(self) -> None:
        fallos_esperados = {cid for cid, _d, _m, exp, _s in CASES if exp == "reject"}
        sobrevive = {
            cid
            for cid in fallos_esperados
            if _run_hook(self.fx.repo, self.fx.mutant_accept, self.fx.shas[cid], f"b{cid}").returncode == 0
        }
        self.assertEqual(
            sobrevive, fallos_esperados, "el mutante exit 0 debe ser rechazado por todos los casos reject"
        )

    def test_mutante_rechaza_todo_falla_la_suite(self) -> None:
        aceptados = {cid for cid, _d, _m, exp, _s in CASES if exp == "accept"}
        matados = {
            cid
            for cid in aceptados
            if _run_hook(self.fx.repo, self.fx.mutant_reject, self.fx.shas[cid], f"b{cid}").returncode != 0
        }
        self.assertEqual(matados, aceptados, "el mutante exit 1 debe ser rechazado por todos los casos accept")

    EXEMPT = Path("tools") / "hooks" / "epoch-exempt.txt"

    def _exempt(self, text: str) -> Path:
        p = self.fx.repo / self.EXEMPT
        p.write_text(text)
        return p

    def test_exencion_declarada_salta_el_commit(self) -> None:
        sha = self.fx.shas[1]  # mensaje sin Model:
        p = self._exempt(f"{sha}\tmotivo declarado\n")
        try:
            out = _run_hook(self.fx.repo, HOOK, sha, "b1")
            self.assertEqual(out.returncode, 0, f"exento debía pasar: {out.stderr}")
            self.assertIn("EXENTOS: 1", out.stdout)
            self.assertNotIn("RECHAZO", out.stderr)
        finally:
            p.unlink()

    def test_sin_archivo_de_exenciones_el_gate_es_estricto(self) -> None:
        self.assertFalse((self.fx.repo / self.EXEMPT).exists())
        out = _run_hook(self.fx.repo, HOOK, self.fx.shas[1], "b1")
        self.assertNotEqual(out.returncode, 0, "sin archivo de exenciones nada se exenta")
        self.assertIn("sin Model:", out.stderr)

    def test_exencion_sin_razon_no_se_honra(self) -> None:
        sha = self.fx.shas[1]
        p = self._exempt(f"{sha}\n")
        try:
            out = _run_hook(self.fx.repo, HOOK, sha, "b1")
            self.assertNotEqual(out.returncode, 0, "una exención sin razón no puede pasar")
            self.assertIn("sin Model:", out.stderr)
            self.assertIn("EXENTOS: 0", out.stdout)
        finally:
            p.unlink()

    def test_exencion_de_otro_sha_no_blanquea(self) -> None:
        p = self._exempt(f"{self.fx.shas[2]}\texento en otro commit\n")
        try:
            out = _run_hook(self.fx.repo, HOOK, self.fx.shas[1], "b1")
            self.assertNotEqual(out.returncode, 0, "la exención no puede ser global")
            self.assertIn("sin Model:", out.stderr)
        finally:
            p.unlink()

    def test_commit_de_mecanismo_fuera_de_main(self) -> None:
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
        self.assertNotEqual(out.returncode, 0, "un commit de mecanismo fuera de main debe rechazar")
        self.assertIn("fuera_de_main", out.stderr)

    def test_espacios_requieren_verbatim(self) -> None:
        verbatim = _git(self.fx.repo, "log", "-1", "--format=%B", self.fx.shas[15]).stdout
        self.assertIn("Model:   ", verbatim, "--cleanup=verbatim debe conservar el espacio")

        # Sin verbatim, git normaliza "Model:   " a "Model:" y el caso colapsa en el 13:
        # la guarda de espacios no se ejerce por la vía normal.
        msg_file = self.fx.dir / "msg15normal"
        msg_file.write_text("cuerpo\n\nModel:   \n")
        _git(self.fx.repo, "checkout", "-q", "-B", "b15n", "hooks-epoch")
        _git(self.fx.repo, "commit", "-q", "--allow-empty", "-F", str(msg_file))
        sha = _git(self.fx.repo, "rev-parse", "HEAD").stdout.strip()
        normal = _git(self.fx.repo, "log", "-1", "--format=%B", sha).stdout
        self.assertNotIn("Model:   ", normal, "git normalizó el espacio")
        self.assertEqual(normal, _git(self.fx.repo, "log", "-1", "--format=%B", self.fx.shas[13]).stdout)

        self.assertIn("RECHAZO", _run_hook(self.fx.repo, HOOK, self.fx.shas[15], "b15").stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
