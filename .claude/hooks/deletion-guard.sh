#!/usr/bin/env bash
# ============================================================================
# deletion-guard.sh — PreToolUse Hook (Bash)
# ============================================================================
# Intercepts file/folder deletion attempts. Two-strike approval: first hit
# blocks and records, retry of the same command passes through once.
#
# Install globally: ~/.claude/hooks/deletion-guard.sh
# Install per-project: .claude/hooks/deletion-guard.sh
# ============================================================================
set -euo pipefail

APPROVAL_FILE="/tmp/claude-approvals-deletion"
APPROVAL_TTL=600

json_input=$(cat)
tool_name=$(echo "$json_input" | jq -r '.tool_name // empty' 2>/dev/null)
[[ "$tool_name" == "Bash" ]] || exit 0

command=$(echo "$json_input" | jq -r '.tool_input.command // empty' 2>/dev/null)
[[ -n "$command" ]] || exit 0

# ---- Pattern detection ----
deletion_detected=false
matched_pattern=""

patterns=(
  'rm\s'             'rm'
  'rmdir\s'          'rmdir'
  'unlink\s'         'unlink'
  'shred\s'          'shred'
  'find\s.*-delete'  'find -delete'
  'find\s.*-exec\s+rm'  'find -exec rm'
  'git\s+clean\s.*-[a-zA-Z]*[fdx]'  'git clean'
  'truncate\s'       'truncate'
)

i=0
while (( i < ${#patterns[@]} )); do
  regex="${patterns[$i]}"
  label="${patterns[$((i+1))]}"
  if echo "$command" | grep -qP "(?<![a-zA-Z0-9_/.-])${regex}" 2>/dev/null; then
    deletion_detected=true
    matched_pattern="$label"
    break
  fi
  i=$((i + 2))
done

[[ "$deletion_detected" == "true" ]] || exit 0

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

# Strike 2: already seen → consume and allow
if grep -q "^${cmd_hash}|" "$APPROVAL_FILE" 2>/dev/null; then
  sed -i "/^${cmd_hash}|/d" "$APPROVAL_FILE"
  exit 0
fi

# Strike 1: record and block
echo "${cmd_hash}|${now}" >> "$APPROVAL_FILE"

targets=$(echo "$command" \
  | sed -E 's/^(rm|rmdir|unlink|shred|truncate)\s+(-[a-zA-Z]+\s+)*//' \
  | tr ' ' '\n' | grep -v '^-' | head -10 || echo "(parse failed)")

cat >&2 <<EOF
🛑 DELETION GUARD — Blocked.

Pattern: ${matched_pattern}
Command: ${command}
Target(s):
$(echo "$targets" | sed 's/^/  ◦ /')

→ Explain to the user what will be deleted and why.
→ After explicit confirmation, retry the EXACT same command.
→ Approval expires in $(( APPROVAL_TTL / 60 )) minutes.
EOF
exit 2
