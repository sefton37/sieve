#!/usr/bin/env bash
# ============================================================================
# push-commit.sh — Stop Hook for Claude Code
# ============================================================================
# PURPOSE:  Enforces a documentation-first commit workflow. When Claude stops,
#           this hook checks if code changes have corresponding documentation
#           updates. If not, it bounces Claude back with specific guidance.
#           Once docs are aligned, it auto-commits and pushes.
#
# SEQUENCE:
#   1. Claude finishes work → Stop event fires
#   2. Hook checks for uncommitted changes (exit 0 if none)
#   3. Hook categorizes changes: code vs docs vs config
#   4. If code changed without docs → EXIT 2 (Claude updates docs)
#   5. Claude updates docs → Stop fires again
#   6. This time changes are aligned → commit + push → EXIT 0
#
# EVENT:    Stop (matcher: "")
# EXIT 0:   No changes, or successfully committed and pushed
# EXIT 2:   Documentation needs updating — feedback to Claude
# EXIT 1:   Git error (non-blocking, shown to user)
#
# SKIP:     Create a .skip-doc-check file in project root to bypass
#           the documentation alignment check (commit still runs).
#           Or include [skip-docs] in any changed filename.
#
# INSTALL:  Copy to .claude/hooks/ and wire in settings.json
# ============================================================================

set -euo pipefail

PROJECT_DIR="${CLAUDE_PROJECT_DIR:-.}"
cd "$PROJECT_DIR"

# ---- Preflight: are we in a git repo? ----
if ! git rev-parse --is-inside-work-tree &>/dev/null; then
  # Not a git repo — nothing to do
  exit 0
fi

# ---- Step 0: Check for uncommitted changes ----
# Includes staged, unstaged, and untracked files
has_staged=$(git diff --cached --name-only 2>/dev/null)
has_unstaged=$(git diff --name-only 2>/dev/null)
has_untracked=$(git ls-files --others --exclude-standard 2>/dev/null)

all_changes=$(printf '%s\n%s\n%s' "$has_staged" "$has_unstaged" "$has_untracked" \
  | sort -u | grep -v '^$' || true)

if [[ -z "$all_changes" ]]; then
  # Nothing to commit
  exit 0
fi

# ---- Step 1: Categorize changes ----

# Source code patterns (things that should have doc coverage)
code_pattern='\.(sh|bash|py|js|ts|jsx|tsx|rs|go|rb|java|c|cpp|h|hpp|cs|php|swift|kt|scala|ex|exs|lua|zig|nix|toml|yaml|yml)$'

# Documentation patterns
doc_pattern='\.(md|txt|rst|adoc|org)$|README|CHANGELOG|CONTRIBUTING|docs/|doc/'

# Config/infra patterns (usually don't need doc updates)
config_pattern='\.(json|lock|sum|mod)$|package\.json|Cargo\.toml|go\.mod|Makefile|Dockerfile|docker-compose|\.github/|\.gitignore|\.env'

code_changes=$(echo "$all_changes" | grep -iE "$code_pattern" || true)
doc_changes=$(echo "$all_changes" | grep -iE "$doc_pattern" || true)
config_changes=$(echo "$all_changes" | grep -iE "$config_pattern" || true)

# Count by category
code_count=$(echo "$code_changes" | grep -c . || echo 0)
doc_count=$(echo "$doc_changes" | grep -c . || echo 0)
total_count=$(echo "$all_changes" | grep -c . || echo 0)

# ---- Step 2: Documentation alignment check ----

skip_doc_check=false

# Skip if .skip-doc-check exists
if [[ -f "$PROJECT_DIR/.skip-doc-check" ]]; then
  skip_doc_check=true
fi

# Skip if only config/infra files changed (no source code)
if [[ "$code_count" -eq 0 ]]; then
  skip_doc_check=true
fi

# Skip if [skip-docs] marker found
if echo "$all_changes" | grep -q 'skip-docs'; then
  skip_doc_check=true
fi

if [[ "$skip_doc_check" == "false" && "$code_count" -gt 0 && "$doc_count" -eq 0 ]]; then
  # ---- CODE CHANGED, NO DOCS UPDATED — BOUNCE BACK ----
  
  # Find relevant doc files that might need updating
  relevant_docs=""
  checked_dirs=""
  
  while IFS= read -r code_file; do
    dir=$(dirname "$code_file")
    # Avoid checking the same directory twice
    if echo "$checked_dirs" | grep -qF "$dir"; then
      continue
    fi
    checked_dirs+="$dir"$'\n'
    
    # Look for doc files in the same directory and parent
    for candidate in \
      "$dir/README.md" \
      "$dir/../README.md" \
      "README.md" \
      "CHANGELOG.md" \
      "docs/"; do
      if [[ -e "$PROJECT_DIR/$candidate" ]]; then
        relevant_docs+="  - $candidate"$'\n'
      fi
    done
  done <<< "$code_changes"
  
  relevant_docs=$(echo "$relevant_docs" | sort -u | grep -v '^$' || true)

  cat >&2 <<EOF
📋 DOCUMENTATION REVIEW — Required before commit.

${code_count} source file(s) changed:
$(echo "$code_changes" | sed 's/^/  ◦ /')

No documentation files were updated alongside these changes.

BEFORE I CAN COMMIT, please:
  1. Review the changes you just made
  2. Update relevant documentation to reflect the current state
  3. Focus on: what changed, why, and any new usage patterns

$(if [[ -n "$relevant_docs" ]]; then
  echo "Documentation files that likely need attention:"
  echo "$relevant_docs"
else
  echo "No existing docs found nearby. Consider adding a README.md"
  echo "or updating the project-level documentation."
fi)

$(if [[ -n "$config_changes" ]]; then
  echo ""
  echo "Note: Config changes detected too (these don't require doc updates):"
  echo "$config_changes" | sed 's/^/  ◦ /'
fi)

Once documentation is updated, I will automatically commit and push all changes.

To skip this check for trivial changes, create a .skip-doc-check file in the project root.
EOF
  exit 2
fi

# ---- Step 3: Commit and push ----

# Stage everything
git add -A

# Build a meaningful commit message from the diff
# Determine conventional commit type
commit_type="chore"

if git diff --cached --name-only | grep -qiE '(feat|feature)' || \
   git diff --cached --diff-filter=A --name-only | grep -c . | grep -qvE '^0$'; then
  # New files added → likely a feature
  commit_type="feat"
elif git diff --cached --name-only | grep -qiE '(fix|bug|patch|hotfix)'; then
  commit_type="fix"
elif [[ "$doc_count" -gt 0 && "$code_count" -eq 0 ]]; then
  commit_type="docs"
elif git diff --cached --name-only | grep -qiE '(test|spec|_test\.|\.test\.)'; then
  commit_type="test"
elif git diff --cached --name-only | grep -qiE '(refactor)'; then
  commit_type="refactor"
fi

# Determine scope from the most common directory
primary_dir=$(git diff --cached --name-only | head -5 \
  | xargs -I{} dirname {} 2>/dev/null \
  | sort | uniq -c | sort -rn | head -1 \
  | awk '{print $2}' || echo "root")

# Clean up scope
scope=$(echo "$primary_dir" | sed 's|^\./||' | sed 's|/|-|g' | head -c 20)
if [[ "$scope" == "." || -z "$scope" ]]; then
  scope="root"
fi

# Build the commit message
commit_subject="${commit_type}(${scope}): update ${total_count} file(s)"

commit_body="Changes:
$(echo "$all_changes" | sed 's/^/- /')

---
Auto-committed by push-commit hook"

# Commit
if ! git commit -m "$commit_subject" -m "$commit_body" 2>&1; then
  echo "Git commit failed" >&2
  exit 1
fi

# Push to current branch
current_branch=$(git branch --show-current 2>/dev/null)
if [[ -z "$current_branch" ]]; then
  echo "Detached HEAD — committed but cannot push without a branch" >&2
  exit 1
fi

# Check if remote exists
if ! git remote | grep -q 'origin'; then
  echo "No 'origin' remote configured — committed locally but not pushed" >&2
  echo "✅ Committed ${total_count} files to ${current_branch} (local only)" >&2
  exit 0
fi

if ! git push origin "$current_branch" 2>&1; then
  echo "Push failed — changes are committed locally" >&2
  exit 1
fi

echo "✅ Committed and pushed ${total_count} files to origin/${current_branch}"
exit 0
