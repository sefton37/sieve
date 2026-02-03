#!/usr/bin/env bash
# ============================================================================
# session-checkpoint.sh — SessionStart Hook
# ============================================================================
# Creates a git checkpoint tag at the start of each Claude Code session.
# This gives you a known-good state to roll back to if anything goes wrong.
#
# Also injects safety context into Claude's awareness via stdout.
# (SessionStart stdout goes into additionalContext in the system prompt.)
#
# Research basis:
#   - Anthropic best practices: "Git is your best friend in autonomous modes"
#   - Multiple practitioners: "git commit checkpoint before YOLO mode"
#   - PromptLayer guide: "git add -A && git commit before starting"
#   - GitHub issue #12851: Archive-before-delete as default behavior
# ============================================================================
set -euo pipefail

PROJECT_DIR="${CLAUDE_PROJECT_DIR:-.}"
cd "$PROJECT_DIR" 2>/dev/null || exit 0

# ---- Check for git repo ----
if ! git rev-parse --is-inside-work-tree &>/dev/null; then
  # Not a git repo — warn via stdout (Claude sees this)
  cat <<EOF
⚠ SESSION NOTE: This project is not a git repository. There is no automatic
rollback mechanism. Consider running 'git init' for safety.
EOF
  exit 0
fi

# ---- Create checkpoint tag ----
timestamp=$(date -u '+%Y%m%d-%H%M%S')
tag_name="claude-checkpoint-${timestamp}"
current_branch=$(git branch --show-current 2>/dev/null || echo "detached")
head_short=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")

# Tag the current HEAD (local only, never pushed)
git tag "$tag_name" HEAD 2>/dev/null || true

# ---- Check for uncommitted changes ----
has_unstaged=false
has_staged=false
has_untracked=false

git diff --quiet 2>/dev/null || has_unstaged=true
git diff --cached --quiet 2>/dev/null || has_staged=true
[[ -n "$(git ls-files --others --exclude-standard 2>/dev/null)" ]] && has_untracked=true

uncommitted_warning=""
if [[ "$has_unstaged" == "true" || "$has_staged" == "true" || "$has_untracked" == "true" ]]; then
  parts=()
  [[ "$has_unstaged" == "true" ]] && parts+=("unstaged changes")
  [[ "$has_staged" == "true" ]] && parts+=("staged changes")
  [[ "$has_untracked" == "true" ]] && parts+=("untracked files")
  detail=$(IFS=', '; echo "${parts[*]}")
  uncommitted_warning="⚠ Uncommitted work detected (${detail}). The checkpoint tag covers committed state only."
fi

# ---- Clean old checkpoint tags (keep last 10) ----
old_tags=$(git tag -l 'claude-checkpoint-*' | sort | head -n -10 2>/dev/null || true)
if [[ -n "$old_tags" ]]; then
  echo "$old_tags" | xargs git tag -d &>/dev/null || true
fi

# ---- Output safety context (Claude sees this as system context) ----
cat <<EOF
📌 SESSION CHECKPOINT: ${tag_name}
Branch: ${current_branch} @ ${head_short}
${uncommitted_warning}

ROLLBACK: git reset --hard ${tag_name}

ACTIVE SAFETY HOOKS:
• Deletion guard — rm/rmdir/shred blocked until confirmed
• Secrets guard — .env, SSH keys, credentials protected
• Overwrite guard — mv/cp to existing files requires confirmation
• Network guard — DNS/socket exfiltration channels blocked
• Injection scanner — tool output scanned for prompt injection
• Permission handler — catastrophic ops hard-denied

SAFETY RULES FOR THIS SESSION:
• Never include secret values in responses, commits, or logs
• Prefer mv -n (no-clobber) over plain mv
• Prefer cp -n (no-clobber) over plain cp
• Before reorganizing files, explain the plan to the user first
• If a file's purpose is unclear, archive it (mv to .archive/) rather than delete
• Stay within the project directory scope
EOF

exit 0
