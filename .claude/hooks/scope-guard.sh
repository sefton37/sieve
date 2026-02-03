#!/usr/bin/env bash
# ============================================================================
# scope-guard.sh — PreToolUse Hook (Bash|Write|Edit|MultiEdit)
# ============================================================================
# Detects file modifications outside $CLAUDE_PROJECT_DIR. Prevents the
# "Claude helpfully edits your system config" class of incidents where
# scope creep leads to unintended changes in unrelated directories.
#
# Research basis:
#   - Community incident: Claude removed system JSON configs that "seemed
#     related" to the project being worked on
#   - Anthropic best practice: scope tasks tightly with clear boundaries
#   - Multiple reports of Claude modifying ~/.bashrc, /etc/ files, and
#     sibling project directories without being asked
#
# Two-strike approval for legitimate out-of-scope access.
#
# SAFE EXCEPTIONS (always allowed):
#   /tmp/*, /dev/null, $HOME/.claude/*, the project dir itself
# ============================================================================
set -euo pipefail

APPROVAL_FILE="/tmp/claude-approvals-scope"
APPROVAL_TTL=600
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-.}"

# Resolve project dir to absolute path
PROJECT_DIR_REAL=$(cd "$PROJECT_DIR" 2>/dev/null && pwd -P || echo "$PROJECT_DIR")

json_input=$(cat)
tool_name=$(echo "$json_input" | jq -r '.tool_name // empty' 2>/dev/null)

# ============================================================================
# Path extraction by tool type
# ============================================================================
declare -a target_paths=()

case "$tool_name" in
  Write|Edit|MultiEdit|NotebookEdit)
    # These tools have file_path in tool_input
    path=$(echo "$json_input" | jq -r '.tool_input.file_path // empty' 2>/dev/null)
    [[ -n "$path" ]] && target_paths+=("$path")
    ;;

  Bash)
    command=$(echo "$json_input" | jq -r '.tool_input.command // empty' 2>/dev/null)
    [[ -n "$command" ]] || exit 0

    # Extract paths from write-capable commands
    # Strategy: find file path arguments in commands that modify files
    #
    # We look for:
    #   - Redirect targets: > file, >> file, tee file
    #   - In-place edit targets: sed -i ... file, perl -i ... file
    #   - Write commands: cp/mv destinations (handled by overwrite-guard, but
    #     scope is a different concern)
    #   - Direct file creation: touch, mkdir -p
    #   - Config modifications: echo >> ~/.bashrc, etc.

    # Redirect targets (> and >>)
    while IFS= read -r redir_path; do
      [[ -n "$redir_path" ]] && target_paths+=("$redir_path")
    done < <(echo "$command" | grep -oP '>{1,2}\s*\K[^\s;&|]+' 2>/dev/null || true)

    # tee targets
    while IFS= read -r tee_path; do
      [[ -n "$tee_path" ]] && target_paths+=("$tee_path")
    done < <(echo "$command" | grep -oP 'tee\s+(-a\s+)?\K[^\s;&|]+' 2>/dev/null || true)

    # sed -i targets (last non-flag argument)
    if echo "$command" | grep -qP 'sed\s+-i' 2>/dev/null; then
      # Extract the file argument(s) after the sed expression
      sed_target=$(echo "$command" | grep -oP "sed\s+-i[^\s]*\s+'[^']*'\s+\K[^\s;&|]+" 2>/dev/null || true)
      [[ -n "$sed_target" ]] && target_paths+=("$sed_target")
      # Also try double-quote variant
      sed_target=$(echo "$command" | grep -oP 'sed\s+-i[^\s]*\s+"[^"]*"\s+\K[^\s;&|]+' 2>/dev/null || true)
      [[ -n "$sed_target" ]] && target_paths+=("$sed_target")
    fi

    # chmod/chown targets (system file modifications)
    while IFS= read -r mod_path; do
      [[ -n "$mod_path" ]] && target_paths+=("$mod_path")
    done < <(echo "$command" | grep -oP '(chmod|chown)\s+[^\s]+\s+\K[^\s;&|]+' 2>/dev/null || true)

    # ln -s (symlink creation)
    while IFS= read -r ln_path; do
      [[ -n "$ln_path" ]] && target_paths+=("$ln_path")
    done < <(echo "$command" | grep -oP 'ln\s+(-[sf]+\s+)+[^\s]+\s+\K[^\s;&|]+' 2>/dev/null || true)

    ;;

  *)
    # Not a tool we monitor
    exit 0
    ;;
esac

# Nothing to check
(( ${#target_paths[@]} > 0 )) || exit 0

# ============================================================================
# Path resolution and scope checking
# ============================================================================
is_safe_exception() {
  local path="$1"
  # /tmp is always safe (temp files, build artifacts)
  [[ "$path" == /tmp/* || "$path" == /tmp ]] && return 0
  # /dev/null is always safe
  [[ "$path" == /dev/null ]] && return 0
  # Claude's own config directory
  [[ "$path" == "$HOME/.claude/"* ]] && return 0
  # Inside the project directory
  [[ "$path" == "$PROJECT_DIR_REAL/"* || "$path" == "$PROJECT_DIR_REAL" ]] && return 0
  return 1
}

resolve_path() {
  local path="$1"
  # Expand ~ to HOME
  path="${path/#\~/$HOME}"
  # Expand common env vars
  path="${path//\$HOME/$HOME}"
  path="${path//\$\{HOME\}/$HOME}"
  path="${path//\$CLAUDE_PROJECT_DIR/$PROJECT_DIR}"
  # Resolve relative paths against project dir
  if [[ "$path" != /* ]]; then
    path="${PROJECT_DIR_REAL}/${path}"
  fi
  # Normalize (resolve .., symlinks where possible)
  # Use readlink -m for paths that may not exist yet
  readlink -m "$path" 2>/dev/null || echo "$path"
}

out_of_scope_paths=()

for raw_path in "${target_paths[@]}"; do
  resolved=$(resolve_path "$raw_path")

  if is_safe_exception "$resolved"; then
    continue
  fi

  # Path is outside project and not a safe exception
  if [[ "$resolved" != "$PROJECT_DIR_REAL/"* && "$resolved" != "$PROJECT_DIR_REAL" ]]; then
    out_of_scope_paths+=("$raw_path → $resolved")
  fi
done

# All paths are in scope
(( ${#out_of_scope_paths[@]} > 0 )) || exit 0

# ============================================================================
# Two-strike approval
# ============================================================================
# Hash all out-of-scope paths together (same command = same approval)
hash_input="$tool_name:"
if [[ "$tool_name" == "Bash" ]]; then
  hash_input+="$command"
else
  hash_input+=$(printf '%s\n' "${out_of_scope_paths[@]}")
fi
cmd_hash=$(echo -n "$hash_input" | sha256sum | cut -d' ' -f1)

touch "$APPROVAL_FILE"
now=$(date +%s)

# Purge stale
if [[ -s "$APPROVAL_FILE" ]]; then
  tmp=$(mktemp)
  while IFS='|' read -r hash ts || [[ -n "$hash" ]]; do
    [[ -n "$hash" && -n "$ts" ]] && (( now - ts < APPROVAL_TTL )) && echo "${hash}|${ts}" >> "$tmp"
  done < "$APPROVAL_FILE"
  mv "$tmp" "$APPROVAL_FILE"
fi

# Strike 2: approved
if grep -q "^${cmd_hash}|" "$APPROVAL_FILE" 2>/dev/null; then
  sed -i "/^${cmd_hash}|/d" "$APPROVAL_FILE"
  exit 0
fi

# Strike 1: block
echo "${cmd_hash}|${now}" >> "$APPROVAL_FILE"

cat >&2 <<EOF
📍 SCOPE GUARD — Blocked.

Tool: ${tool_name}
$(if [[ "$tool_name" == "Bash" ]]; then echo "Command: ${command}"; fi)

Out-of-scope target(s):
$(printf '  ◦ %s\n' "${out_of_scope_paths[@]}")

Project directory: ${PROJECT_DIR_REAL}

This operation modifies files outside the current project.
→ Explain to the user what you need to modify and why.
→ If this is intentional, retry after confirmation.
→ Consider whether the file belongs inside the project instead.
→ Approval expires in $(( APPROVAL_TTL / 60 )) minutes.
EOF
exit 2
