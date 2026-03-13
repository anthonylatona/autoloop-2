#!/bin/bash
# reset.sh — wipe loop state and restore the original artifact
# Run from the autoloop/ directory: bash reset.sh

set -e

echo "🔄 Resetting autoloop state..."

# 1. Restore original artifact
cp email_sample/artifact.original.html email_sample/artifact.html
echo "   ✓ artifact.html restored"

# 2. Wipe loop log
rm -f loop_log.json
echo "   ✓ loop_log.json removed"

# 3. Reset git to a single clean commit
if [ -d ".git" ]; then
  git checkout -- email_sample/artifact.html 2>/dev/null || true
  # Nuke all history and start fresh
  rm -rf .git
  git init -q
  git add -A
  git commit -q -m "Initial: baseline artifact (pre-loop)"
  echo "   ✓ git history reset to single baseline commit"
else
  git init -q
  git add -A
  git commit -q -m "Initial: baseline artifact (pre-loop)"
  echo "   ✓ git repo initialized"
fi

echo ""
echo "✅ Ready. Run the loop:"
echo "   python3 autoloop_meta_eval.py --artifact email_sample/artifact.html --goals email_sample/goals.md --eval-config email_sample/eval_config_email.yaml --iterations 20 --verbose"
