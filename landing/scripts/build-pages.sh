#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
output_dir="${project_root}/out"

if [[ "${output_dir}" != "${project_root}/out" || "${project_root}" == "/" ]]; then
  echo "Refusing to clean an unexpected Pages output directory." >&2
  exit 64
fi

rm -rf "${output_dir}"

cd "${project_root}"
GITHUB_PAGES=true \
NEXT_PUBLIC_TRAXION_CANONICAL_URL=https://traxion.lucianonovello.com/ \
NEXT_PUBLIC_TRAXION_API_URL=https://api.traxion.lucianonovello.com \
  ./node_modules/.bin/next build

test -f "${output_dir}/index.html"

if grep -qE '(href|src)="/hypercopy/' "${output_dir}/index.html"; then
  echo "The custom-domain build still contains the legacy /hypercopy asset prefix." >&2
  exit 65
fi

grep -q 'https://traxion.lucianonovello.com/' "${output_dir}/index.html"
