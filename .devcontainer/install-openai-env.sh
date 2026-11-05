#!/bin/bash
# Append a source line to ~/.bashrc so new terminals pick up openai-env.sh.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
env_sh="${here}/openai-env.sh"
bashrc="${HOME}/.bashrc"
marker="# agent-toolkit openai env"

touch "$bashrc"
if grep -qF "$marker" "$bashrc"; then
  exit 0
fi

cat >> "$bashrc" <<EOF

${marker}
[ -f "${env_sh}" ] && . "${env_sh}"
EOF
