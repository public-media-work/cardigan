#!/bin/bash
# Cardigan — Development Session Initializer
#
# Run this at the start of each development session:
#   ./init.sh
#
# This script:
# 1. Ensures you're in the right directory
# 2. Activates the virtual environment (if it exists)
# 3. Shows git status
# 4. Points at the canonical roadmap (GitHub issues — see CLAUDE.md)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=================================="
echo "Cardigan - Init"
echo "=================================="

# Check for virtual environment
if [ -d "venv" ]; then
    echo "✓ Virtual environment found"
    source venv/bin/activate
    echo "✓ Activated venv ($(python --version))"
else
    echo "⚠ No virtual environment found"
    echo "  Run: python3 -m venv venv && source venv/bin/activate && pip install -r requirements.lock"
fi

# Show git status
echo ""
echo "Git Status:"
git branch --show-current
git status --short

# Point at the roadmap
echo ""
echo "=================================="
echo "Roadmap:"
echo "=================================="
if command -v gh >/dev/null 2>&1; then
    gh issue list --state open --label epic --limit 20 \
        --json number,title --jq '.[] | "  #\(.number)  \(.title)"' 2>/dev/null \
        || echo "  (gh could not reach the repo — see CLAUDE.md)"
else
    echo "  gh CLI not installed — see CLAUDE.md for the roadmap"
fi
echo ""
echo "  Full roadmap: gh issue view 357"
echo "  Pick up work: gh issue list --state open --label ready-for-agent"

echo ""
echo "=================================="
echo "Ready for development!"
echo "=================================="
