#!/bin/sh
# Report which of the repo's two apps a set of files belongs to.
#
# The fitness app sits at the repo root and SpendTrack under
# spending-tracker/, and 15 files share the same relative path in both.
# This answers "which app am I about to change, and where does it deploy?"
# before an edit lands in the wrong one.
#
# Usage:
#   bash tools/which-app.sh              # classify the current diff
#   bash tools/which-app.sh <file>...    # classify specific paths

cd "$(git rev-parse --show-toplevel)" || exit 1

if [ $# -gt 0 ]; then
  files=$(printf '%s\n' "$@")
  src="arguments"
else
  files=$( { git diff --name-only; git diff --cached --name-only; } | sort -u )
  src="current diff (staged + unstaged)"
fi

if [ -z "$files" ]; then
  echo "No changes. (Working tree is clean.)"
  exit 0
fi

spend=$(printf '%s\n' "$files" | grep '^spending-tracker/' \
        | grep -v '^spending-tracker/CLAUDE\.md$')
shared=$(printf '%s\n' "$files" \
         | grep -E '^(CLAUDE\.md|README(\.md)?|\.githooks/|\.gitignore$|tools/|spending-tracker/CLAUDE\.md$)')
root=$(printf '%s\n' "$files" | grep -v '^spending-tracker/' \
       | grep -vE '^(CLAUDE\.md|README(\.md)?|\.githooks/|\.gitignore$|tools/)')

echo "Source: $src"
echo

if [ -n "$root" ]; then
  echo "Path to Eldorado  (fitness app)"
  echo "  service  eldorado          port 5000"
  echo "  db       instance/dashboard.db"
  echo "  url      https://pt-eldorado.duckdns.org"
  printf '%s\n' "$root" | sed 's/^/    /'
  echo
fi

if [ -n "$spend" ]; then
  echo "SpendTrack"
  echo "  service  spendtrack        port 5001"
  echo "  db       spending-tracker/instance/spending.db"
  echo "  url      https://financecop.duckdns.org"
  printf '%s\n' "$spend" | sed 's/^/    /'
  echo
fi

if [ -n "$shared" ]; then
  echo "Shared infrastructure (belongs to neither app)"
  printf '%s\n' "$shared" | sed 's/^/    /'
  echo
fi

if [ -n "$root" ] && [ -n "$spend" ]; then
  echo "CONFINED: NO — this spans BOTH apps."
  echo "The pre-commit hook will block this. Split it into two commits."
  exit 1
fi

echo "CONFINED: YES — a single app. Safe to commit."
exit 0
