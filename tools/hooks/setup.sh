#!/usr/bin/env bash
# Activates the repo's hooks and verifies that the epoch and the allowlist exist.
# Without this step tools/hooks/pre-push exists and never runs.
#
# HUELLA-SETUP-EN-20261023
set -euo pipefail

# Resolved from the script's own location, not from cwd: invoking it by path from
# another repo would configure the wrong repo.
ROOT=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
cd "$ROOT"

git rev-parse -q --verify 'refs/tags/hooks-epoch^{commit}' >/dev/null || {
  echo "hooks-epoch tag missing: the gate has no declared boundary." >&2
  echo "It is created at the adoption commit: git tag hooks-epoch <sha>" >&2
  exit 1
}

[ -f tools/hooks/models.txt ] || {
  echo "tools/hooks/models.txt is missing: the gate would reject every push." >&2
  exit 1
}

if ! grep -qv '^\s*\(#\|$\)' tools/hooks/models.txt; then
  echo "tools/hooks/models.txt has no ids: the gate would reject every push." >&2
  echo "Add at least one id exactly as your model picker emits it." >&2
  exit 1
fi

# A hook without the executable bit exists and never runs: the gate looks armed
# and never bites. The bit is required on both, not only on pre-push.
# HUELLA-SETUP-EXIGE-EJECUTABLE-20261022
for h in pre-push commit-msg; do
  [ -x "tools/hooks/$h" ] || {
    echo "tools/hooks/$h is not executable: the gate would not run." >&2
    echo "Fix: chmod +x tools/hooks/$h && git update-index --chmod=+x tools/hooks/$h" >&2
    exit 1
  }
done

# Activated last: if anything is missing, the repo is left as it was.
git config core.hooksPath tools/hooks

echo "hooks active: core.hooksPath=$(git config --get core.hooksPath)"
echo "epoch: $(git rev-parse --short 'refs/tags/hooks-epoch^{commit}')"
printf 'models: '; grep -cv '^\s*\(#\|$\)' tools/hooks/models.txt
