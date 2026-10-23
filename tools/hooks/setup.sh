#!/usr/bin/env bash
# Activa los hooks del repo y verifica que la época y el enum existan.
# Sin este paso, tools/hooks/pre-push existe y nunca corre.
set -euo pipefail

# Se resuelve desde la ubicación del script, no desde el cwd: invocarlo por ruta
# desde otro repo configuraría el repo equivocado.
ROOT=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
cd "$ROOT"

git rev-parse -q --verify 'refs/tags/hooks-epoch^{commit}' >/dev/null || {
  echo "hooks-epoch ausente: el gate no tiene frontera declarada." >&2
  echo "Se crea en el commit de adopción: git tag hooks-epoch <sha>" >&2
  exit 1
}

[ -f tools/hooks/models.txt ] || {
  echo "tools/hooks/models.txt ausente: el gate rechazaría todo push." >&2
  exit 1
}

# Un hook sin bit de ejecución existe y nunca corre: el gate se cree activo y no
# muerde. Se exige el bit en los dos, no solo en pre-push.
# HUELLA-SETUP-EXIGE-EJECUTABLE-20261022
for h in pre-push commit-msg; do
  [ -x "tools/hooks/$h" ] || {
    echo "tools/hooks/$h no es ejecutable: el gate no correría." >&2
    echo "Arreglo: chmod +x tools/hooks/$h && git update-index --chmod=+x tools/hooks/$h" >&2
    exit 1
  }
done

# Se activa al final: si algo falta, el repo queda como estaba.
git config core.hooksPath tools/hooks

echo "hooks activos: core.hooksPath=$(git config --get core.hooksPath)"
echo "epoch: $(git rev-parse --short 'refs/tags/hooks-epoch^{commit}')"
printf 'modelos: '; grep -cv '^\s*\(#\|$\)' tools/hooks/models.txt
