#!/usr/bin/env bash
# ============================================================================
# secrets-guard.sh — PreToolUse Hook (Bash|Read)
# ============================================================================
# Prevents accidental or malicious access to sensitive files: .env, SSH keys,
# AWS credentials, private keys, /proc/environ. Handles both Bash commands
# (cat .env, strings .e*) and Read tool calls (Read .env.local).
#
# Two-strike approval for legitimate access.
#
# Research basis:
#   - CVE-2025-55284: DNS exfiltration of .env contents via ping
#   - Claude Code auto-loads .env into memory (hooks can't prevent this,
#     but CAN prevent explicit reads and output of the content)
#   - Knostic research: env vars leaked via echo, printenv, /proc/environ
# ============================================================================
set -euo pipefail

APPROVAL_FILE="/tmp/claude-approvals-secrets"
APPROVAL_TTL=600

json_input=$(cat)
tool_name=$(echo "$json_input" | jq -r '.tool_name // empty' 2>/dev/null)

# ============================================================================
# Sensitive path patterns (shared between Bash and Read checks)
# ============================================================================
# These are checked against file paths in commands and Read tool input.
# Be specific enough to avoid false positives on things like "env.go"

SENSITIVE_PATH_PATTERNS=(
  '\.env$'
  '\.env\.'
  '\.env\.local'
  '\.env\.production'
  '\.env\.development'
  '\.env\.staging'
  '/\.ssh/'
  '\.ssh/id_'
  '\.ssh/authorized_keys'
  '\.ssh/known_hosts'
  '\.ssh/config'
  '/\.aws/'
  '\.aws/credentials'
  '\.aws/config'
  '/\.gnupg/'
  '/\.config/gh/'
  '\.netrc'
  '\.npmrc'
  '\.pypirc'
  '/proc/[0-9]*/environ'
  '/proc/self/environ'
  '\.pem$'
  '\.key$'
  '_rsa$'
  '_ed25519$'
  '_ecdsa$'
  'secrets\.json'
  'secrets\.yaml'
  'secrets\.yml'
  'credentials\.json'
  'service.account\.json'
  '\.secret$'
)

check_path_sensitive() {
  local path="$1"
  for pattern in "${SENSITIVE_PATH_PATTERNS[@]}"; do
    if echo "$path" | grep -qP "$pattern" 2>/dev/null; then
      echo "$pattern"
      return 0
    fi
  done
  return 1
}

# ============================================================================
# Two-strike mechanism
# ============================================================================
two_strike_check() {
  local cmd_hash="$1"
  touch "$APPROVAL_FILE"
  local now
  now=$(date +%s)

  # Purge stale
  if [[ -s "$APPROVAL_FILE" ]]; then
    local tmp
    tmp=$(mktemp)
    while IFS='|' read -r hash ts || [[ -n "$hash" ]]; do
      [[ -n "$hash" && -n "$ts" ]] && (( now - ts < APPROVAL_TTL )) && echo "${hash}|${ts}" >> "$tmp"
    done < "$APPROVAL_FILE"
    mv "$tmp" "$APPROVAL_FILE"
  fi

  # Strike 2: approved
  if grep -q "^${cmd_hash}|" "$APPROVAL_FILE" 2>/dev/null; then
    sed -i "/^${cmd_hash}|/d" "$APPROVAL_FILE"
    return 0  # approved
  fi

  # Strike 1: record
  echo "${cmd_hash}|${now}" >> "$APPROVAL_FILE"
  return 1  # blocked
}

block_with_message() {
  local reason="$1"
  local detail="$2"
  cat >&2 <<EOF
🔐 SECRETS GUARD — Blocked.

${reason}
${detail}

→ Tell the user exactly which sensitive file/data you need to access and why.
→ After explicit confirmation, retry the EXACT same command.
→ Approval expires in $(( APPROVAL_TTL / 60 )) minutes.

⚠ NEVER include secret values in your response, commit messages, or logs.
EOF
  exit 2
}

# ============================================================================
# Handle Read tool
# ============================================================================
if [[ "$tool_name" == "Read" ]]; then
  file_path=$(echo "$json_input" | jq -r '.tool_input.file_path // empty' 2>/dev/null)
  [[ -n "$file_path" ]] || exit 0

  matched=$(check_path_sensitive "$file_path") || exit 0

  cmd_hash=$(echo -n "Read:${file_path}" | sha256sum | cut -d' ' -f1)
  if two_strike_check "$cmd_hash"; then
    exit 0
  fi

  block_with_message \
    "Sensitive file read: ${file_path}" \
    "Matched pattern: ${matched}"
fi

# ============================================================================
# Handle Bash tool
# ============================================================================
[[ "$tool_name" == "Bash" ]] || exit 0

command=$(echo "$json_input" | jq -r '.tool_input.command // empty' 2>/dev/null)
[[ -n "$command" ]] || exit 0

# ---- Check 1: Direct file reads of sensitive paths ----
# Commands that read file contents: cat, head, tail, less, more, strings,
# base64, xxd, od, source, .  (dot-source)
read_commands='(cat|head|tail|less|more|strings|base64|xxd|od|source|\.)\s'

if echo "$command" | grep -qP "$read_commands" 2>/dev/null; then
  # Extract file arguments (everything after the command and flags)
  for pattern in "${SENSITIVE_PATH_PATTERNS[@]}"; do
    if echo "$command" | grep -qP "$pattern" 2>/dev/null; then
      cmd_hash=$(echo -n "$command" | sha256sum | cut -d' ' -f1)
      if two_strike_check "$cmd_hash"; then
        exit 0
      fi
      block_with_message \
        "Command reads sensitive file." \
        "Command: ${command}\nMatched: ${pattern}"
    fi
  done
fi

# ---- Check 2: Grep/search through sensitive files ----
if echo "$command" | grep -qP '(grep|rg|ag|ack|sed|awk)\s' 2>/dev/null; then
  for pattern in "${SENSITIVE_PATH_PATTERNS[@]}"; do
    if echo "$command" | grep -qP "$pattern" 2>/dev/null; then
      cmd_hash=$(echo -n "$command" | sha256sum | cut -d' ' -f1)
      if two_strike_check "$cmd_hash"; then
        exit 0
      fi
      block_with_message \
        "Command searches sensitive file." \
        "Command: ${command}\nMatched: ${pattern}"
    fi
  done
fi

# ---- Check 3: /proc/*/environ access (CVE vector) ----
if echo "$command" | grep -qP '/proc/.*environ' 2>/dev/null; then
  cmd_hash=$(echo -n "$command" | sha256sum | cut -d' ' -f1)
  if two_strike_check "$cmd_hash"; then
    exit 0
  fi
  block_with_message \
    "Process environment access detected." \
    "Command: ${command}\nThis can expose all environment variables including secrets loaded from .env files."
fi

# ---- Check 4: Dumping all environment variables ----
# printenv and env with no args dump everything — including auto-loaded .env content
if echo "$command" | grep -qP '^\s*(printenv|env)\s*$' 2>/dev/null; then
  cmd_hash=$(echo -n "$command" | sha256sum | cut -d' ' -f1)
  if two_strike_check "$cmd_hash"; then
    exit 0
  fi
  block_with_message \
    "Full environment dump detected." \
    "Command: ${command}\n'printenv' and 'env' expose all variables including secrets auto-loaded from .env files.\nUse 'printenv VAR_NAME' for specific non-secret variables instead."
fi

# ---- Check 5: Echo/printf of common secret variable names ----
# This catches: echo $API_KEY, echo "$SECRET", printf "%s" $TOKEN, etc.
secret_var_pattern='\$(API[_-]?KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL|PRIVATE[_-]?KEY|AWS_SECRET|DATABASE_URL|DB_PASSWORD|ANTHROPIC_API_KEY|OPENAI_API_KEY|GITHUB_TOKEN|STRIPE_KEY)'
if echo "$command" | grep -qiP "(echo|printf).*${secret_var_pattern}" 2>/dev/null; then
  cmd_hash=$(echo -n "$command" | sha256sum | cut -d' ' -f1)
  if two_strike_check "$cmd_hash"; then
    exit 0
  fi
  block_with_message \
    "Output of likely secret variable detected." \
    "Command: ${command}\nThis would expose secret values in the transcript."
fi

exit 0
