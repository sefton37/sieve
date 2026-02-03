#!/usr/bin/env bash
# ============================================================================
# overwrite-guard.sh — PreToolUse Hook (Bash)
# ============================================================================
# Detects mv and cp commands that would silently overwrite existing files.
# Unlike rm, mv/cp don't warn when the destination already exists — the
# original file is gone with no undo.
#
# Research basis:
#   - Product Talk safety guide: "mv and cp overwrite without warning...
#     this is functionally the same as a delete"
#   - GitHub issue #14964: "Claude overwrote existing file without reading
#     it first - data loss"
#   - GitHub issue #12851: "Restore backup-before-delete safety behavior"
#
# Two-strike approval. Only blocks when destination EXISTS as a file.
# ============================================================================
set -euo pipefail

APPROVAL_FILE="/tmp/claude-approvals-overwrite"
APPROVAL_TTL=600
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-.}"

json_input=$(cat)
tool_name=$(echo "$json_input" | jq -r '.tool_name // empty' 2>/dev/null)
[[ "$tool_name" == "Bash" ]] || exit 0

command=$(echo "$json_input" | jq -r '.tool_input.command // empty' 2>/dev/null)
[[ -n "$command" ]] || exit 0

# ---- Detect mv/cp commands (not mv/cp with --no-clobber/-n) ----
is_move=false
is_copy=false

if echo "$command" | grep -qP '(?<![a-zA-Z0-9_/.-])mv\s' 2>/dev/null; then
  # Skip if --no-clobber or -n flag is present (safe)
  if ! echo "$command" | grep -qP '\s(--no-clobber|-n)\s' 2>/dev/null; then
    is_move=true
  fi
fi

if echo "$command" | grep -qP '(?<![a-zA-Z0-9_/.-])cp\s' 2>/dev/null; then
  if ! echo "$command" | grep -qP '\s(--no-clobber|-n)\s' 2>/dev/null; then
    is_copy=true
  fi
fi

[[ "$is_move" == "true" || "$is_copy" == "true" ]] || exit 0

# ---- Extract destination (last non-flag argument) ----
# Parse the command to find the destination file/dir.
# Strategy: split on whitespace, skip flags (starting with -), take last arg.

op_type="mv"
[[ "$is_copy" == "true" ]] && op_type="cp"

# Extract arguments after the mv/cp command
args_str=$(echo "$command" | sed -E "s/^.*${op_type}\s+//" | sed 's/;.*$//' | sed 's/&&.*$//' | sed 's/\|.*$//')

# Split into array, filter out flags
declare -a args=()
for arg in $args_str; do
  [[ "$arg" =~ ^- ]] && continue
  # Expand ~ to HOME
  arg="${arg/#\~/$HOME}"
  # Resolve relative to project dir if not absolute
  [[ "$arg" =~ ^/ ]] || arg="${PROJECT_DIR}/${arg}"
  args+=("$arg")
done

# Need at least 2 args (source + destination)
(( ${#args[@]} >= 2 )) || exit 0

# Destination is the last argument
dest="${args[-1]}"

# ---- Check if destination would be overwritten ----
# If dest is a directory, check if source filename exists inside it
# If dest is an existing file, it would be overwritten

will_overwrite=false
overwrite_target=""

if [[ -f "$dest" ]]; then
  # Destination is an existing file — would be overwritten
  will_overwrite=true
  overwrite_target="$dest"
elif [[ -d "$dest" ]]; then
  # Destination is a directory — check each source file
  for (( i=0; i < ${#args[@]} - 1; i++ )); do
    src="${args[$i]}"
    basename=$(basename "$src")
    target="${dest}/${basename}"
    if [[ -f "$target" ]]; then
      will_overwrite=true
      overwrite_target="$target"
      break
    fi
  done
fi

[[ "$will_overwrite" == "true" ]] || exit 0

# ---- Two-strike approval ----
cmd_hash=$(echo -n "$command" | sha256sum | cut -d' ' -f1)
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
📁 OVERWRITE GUARD — Blocked.

Operation: ${op_type}
Command: ${command}
Would overwrite: ${overwrite_target}

This file already exists. ${op_type} will silently replace it with no undo.

→ Tell the user which existing file would be overwritten.
→ Consider: backup first (cp file file.bak), or use ${op_type} -n (no-clobber).
→ After explicit confirmation, retry the EXACT same command.
→ Approval expires in $(( APPROVAL_TTL / 60 )) minutes.
EOF
exit 2
