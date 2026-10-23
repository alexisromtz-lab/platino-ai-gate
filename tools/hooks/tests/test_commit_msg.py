#!/usr/bin/env python3
"""Falsificador de tools/hooks/commit-msg.

HUELLA-TEST-COMMITMSG-MATRIZ-20261022

El hook se ejerce como lo ejerce git: con la ruta del archivo de mensaje como
primer argumento. El criterio no es solo el RC — un hook roto por sintaxis
también devuelve RC != 0 — sino el mensaje de rechazo específico y el efecto
sobre el archivo, que el hook NO debe tocar.

La matriz es la misma de pre-push (una sola regla, dos momentos), más los dos
casos que sólo existen al crear:
  - líneas de comentario (`# Model: …`): git las limpia DESPUÉS del hook, así que
    si el hook no las limpia antes, la plantilla firmaría el commit sola.
  - merge en curso: pre-push salta merges con --no-merges; aquí se declara la
    misma frontera en vez de exigir un trailer a quien no lo escribió.

El falsificador se valida a sí mismo contra dos mutantes:
  - exit 0 (acepta todo)  -> la suite falla por los casos que deben rechazar
  - exit 1 (rechaza todo) -> la suite falla por los casos que deben pasar
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

# (id, descripción, mensaje, esperado, subcadena esperada en stderr)
CASES = [
    (1, "sin Model:", "asunto\n\ncuerpo\n", "reject", "no declara Model:"),
    (2, "1 Model valido", "asunto\n\ncuerpo\n\nCo-authored-by: X <x@y>\nModel: deepseek/deepseek-flash\n", "accept", None),
    (3, "1 Model fuera del enum", "asunto\n\nModel: best-model-ever\n", "reject", "no está en models.txt"),
    (4, "Model en prosa", "asunto\n\nModel: deepseek/deepseek-flash\n\ncuerpo posterior\n", "reject", "en prosa"),
    (5, "2 trailers sin Squash-Reason", "asunto\n\nModel: deepseek/deepseek-flash\nModel: openai/OpenAI/gpt-6-luna\n", "reject", "Squash-Reason"),
    (6, "2 trailers + Squash-Reason valido", "asunto\n\nModel: deepseek/deepseek-flash\nModel: openai/OpenAI/gpt-6-luna\nSquash-Reason: dos pestanas\n", "accept", None),
    (7, "Model vacio", "asunto\n\nModel:\n", "reject", "vacío"),
    (8, "Model solo en comentario de plantilla", "asunto\n\nCo-authored-by: X <x@y>\n# Model: deepseek/deepseek-flash\n", "reject", "no declara Model:"),
    (9, "Model: none en commit ordinario", "asunto\n\nModel: none\n", "reject", "Model: none no es válido"),
    # 10: `interpret-trailers` recorta los espacios finales del valor, así que un ID
    # válido con cola de espacios es válido en los DOS gates. Se fija el acuerdo en
    # vez de inventar una asimetría: el que sólo tiene espacios es el caso 11.
    # HUELLA-TEST-COMMITMSG-PARIDAD-ESPACIOS-20261022
    (10, "Model con espacios finales (paridad con pre-push)", "asunto\n\nModel: deepseek/deepseek-flash   \n", "accept", None),
    (11, "Model solo espacios (vacio)", "asunto\n\nModel:   \n", "reject", "vacío"),
    (12, "Model none en revert", "Revert \"cambio anterior\"\n\nModel: none\n", "accept", None),
    (13, "Model none en automatización", "asunto automático\n\nModel: none\n", "accept", None),
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
    """Repo temporal con el enum; un archivo de mensaje por caso."""

    def __init__(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="commitmsg_fixture_"))
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
            raise unittest.SkipTest(f"hook ausente: {HOOK}")
        cls.fx = Fixture()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.fx.cleanup()

    def test_casos_contra_hook_correcto(self) -> None:
        for cid, desc, _msg, exp, sub in CASES:
            with self.subTest(caso=cid, desc=desc):
                env = None
                if cid == 13:
                    env = dict(os.environ, GIT_AUTHOR_EMAIL="41898282+github-actions[bot]@users.noreply.github.com")
                p = _run_hook(self.fx.repo, HOOK, self.fx.msgs[cid], env=env)
                if exp == "accept":
                    self.assertEqual(p.returncode, 0, f"caso {cid} debía pasar: {p.stderr}")
                    self.assertNotIn("RECHAZO", p.stderr, f"caso {cid} pasó con rechazo")
                else:
                    self.assertNotEqual(p.returncode, 0, f"caso {cid} debía rechazar")
                    self.assertIn(sub, p.stderr, f"caso {cid}: mensaje inesperado: {p.stderr!r}")

    def test_el_hook_no_muta_el_mensaje(self) -> None:
        antes = {cid: p.read_bytes() for cid, p in self.fx.msgs.items()}
        for cid in self.fx.msgs:
            _run_hook(self.fx.repo, HOOK, self.fx.msgs[cid])
        for cid, p in self.fx.msgs.items():
            self.assertEqual(p.read_bytes(), antes[cid], f"caso {cid}: el hook reescribió el mensaje")

    def test_merge_en_curso_no_exige_trailer(self) -> None:
        mh = self.fx.merge_head()
        mh.write_text(_git(self.fx.repo, "rev-parse", "HEAD").stdout.strip() + "\n")
        try:
            p = _run_hook(self.fx.repo, HOOK, self.fx.msgs[1])
        finally:
            mh.unlink(missing_ok=True)
        self.assertEqual(p.returncode, 0, f"un merge no debe exigir Model: {p.stderr}")

    def test_mutante_acepta_todo_hace_fallar_la_matriz(self) -> None:
        rechazos = [cid for cid, _d, _m, exp, _s in CASES if exp == "reject"]
        fallos = [
            cid for cid in rechazos
            if _run_hook(self.fx.repo, self.fx.mutant_accept, self.fx.msgs[cid]).returncode == 0
        ]
        self.assertEqual(len(fallos), len(rechazos), "el mutante accept-all debe delatarse en todos los rechazos")

    def test_mutante_rechaza_todo_hace_fallar_la_matriz(self) -> None:
        aceptados = [cid for cid, _d, _m, exp, _s in CASES if exp == "accept"]
        fallos = [
            cid for cid in aceptados
            if _run_hook(self.fx.repo, self.fx.mutant_reject, self.fx.msgs[cid]).returncode != 0
        ]
        self.assertEqual(len(fallos), len(aceptados), "el mutante reject-all debe delatarse en todos los aceptados")


if __name__ == "__main__":
    unittest.main()
