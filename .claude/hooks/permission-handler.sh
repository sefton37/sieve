#!/usr/bin/env bash
# ============================================================================
# permission-handler.sh — PermissionRequest Hook (*)
# ============================================================================
# Three-tier programmatic permission management:
#   🔴 BLOCKLIST  — Always deny. Catastrophic/irreversible operations.
#   🟢 ALLOWLIST  — Always approve. Safe everyday operations.
#   🟡 PASSTHROUGH — Abstain. Defer to Claude Code's default behavior.
#
# Works in TWO modes:
#   Dangerous mode → Circuit breaker (hard floor beneath permissiveness)
#   Normal mode   → Selective auto-approve (skip prompts for safe ops)
#
# All decisions logged to $LOG_FILE for audit.
#
# NOTE: In --dangerously-skip-permissions, PermissionRequest may not fire.
# PreToolUse hooks (deletion-guard, secrets-guard, etc.) are the primary
# safety layer in dangerous mode. This handler is defense-in-depth.
# ============================================================================
set -euo pipefail

PROJECT_DIR="${CLAUDE_PROJECT_DIR:-.}"
LOG_DIR="${PROJECT_DIR}/.claude/hooks/logs"
LOG_FILE="${LOG_DIR}/permissions.log"
mkdir -p "$LOG_DIR" 2>/dev/null || {
  LOG_DIR="/tmp/claude-permissions-logs"
  LOG_FILE="${LOG_DIR}/permissions.log"
  mkdir -p "$LOG_DIR"
}

json_input=$(cat)
tool_name=$(echo "$json_input" | jq -r '.tool_name // empty' 2>/dev/null)
tool_input=$(echo "$json_input" | jq -r '.tool_input // {}' 2>/dev/null)
command=$(echo "$json_input" | jq -r '.tool_input.command // empty' 2>/dev/null)
timestamp=$(date -u '+%Y-%m-%dT%H:%M:%SZ')

respond() {
  local decision="$1"
  local reason="$2"
  echo "${timestamp} | ${decision} | ${tool_name} | ${command:-N/A} | ${reason}" >> "$LOG_FILE"
  jq -n --arg decision "$decision" --arg reason "$reason" \
    '{ decision: $decision, reason: $reason }'
  exit 0
}

# ============================================================================
# 🔴 TIER 1: CATASTROPHIC BLOCKLIST
# ============================================================================
if [[ "$tool_name" == "Bash" && -n "$command" ]]; then

  # ---- Filesystem destruction ----
  if echo "$command" | grep -qP 'rm\s+.*-[a-zA-Z]*r[a-zA-Z]*f[a-zA-Z]*\s+/\s*$' || \
     echo "$command" | grep -qP 'rm\s+.*-[a-zA-Z]*r[a-zA-Z]*f[a-zA-Z]*\s+/\*'; then
    respond "deny" "CATASTROPHIC: Recursive forced deletion of root filesystem"
  fi

  if echo "$command" | grep -qP 'rm\s+.*-[a-zA-Z]*r[a-zA-Z]*f.*\s+(~|(\$HOME|\$\{HOME\}))\s*(/\*)?$'; then
    respond "deny" "CATASTROPHIC: Recursive deletion of home directory"
  fi

  # ---- Disk-level destruction ----
  if echo "$command" | grep -qP 'dd\s.*of=/dev/(sd|hd|nvme|vd|xvd|mmcblk)'; then
    respond "deny" "CATASTROPHIC: Raw disk write via dd"
  fi

  if echo "$command" | grep -qP '(?<![a-zA-Z0-9_/.-])mkfs' 2>/dev/null; then
    respond "deny" "CATASTROPHIC: Filesystem format operation"
  fi

  # ---- Privilege escalation ----
  if echo "$command" | grep -qP 'chmod\s+777\s+(-R\s+)?/'; then
    respond "deny" "SECURITY: Recursive world-writable permissions on system paths"
  fi

  if echo "$command" | grep -qP '(cat|echo|tee|sed|>).*/(etc/passwd|etc/shadow|etc/sudoers)'; then
    respond "deny" "SECURITY: Direct modification of system auth files"
  fi

  # ---- Remote code execution ----
  if echo "$command" | grep -qP '(curl|wget)\s.*\|\s*(ba)?sh'; then
    respond "deny" "SECURITY: Remote code execution (pipe to shell)"
  fi

  # ---- Secret exfiltration via HTTP ----
  if echo "$command" | grep -qP '(curl|wget).*(-d|--data).*(\$|env|password|secret|token|key)'; then
    respond "deny" "SECURITY: Potential secret exfiltration via HTTP"
  fi

  # ---- Network side-channel exfiltration ----
  if echo "$command" | grep -qP '(?<![a-zA-Z0-9_/.-])ping\s' 2>/dev/null; then
    respond "deny" "SECURITY: ping blocked (DNS exfiltration vector, CVE-2025-55284)"
  fi

  if echo "$command" | grep -qP '(?<![a-zA-Z0-9_/.-])(nc|ncat|netcat)\s' 2>/dev/null; then
    respond "deny" "SECURITY: Raw socket tool blocked (exfiltration vector)"
  fi

  if echo "$command" | grep -qP '(?<![a-zA-Z0-9_/.-])(socat|telnet)\s' 2>/dev/null; then
    respond "deny" "SECURITY: Socket/telnet tool blocked (exfiltration vector)"
  fi

  # ---- System service destruction ----
  if echo "$command" | grep -qP 'systemctl\s+(stop|disable|mask)\s+(sshd|networking|systemd|firewalld|ufw)'; then
    respond "deny" "CATASTROPHIC: Disabling critical system service"
  fi

  if echo "$command" | grep -qP 'iptables\s+-F'; then
    respond "deny" "SECURITY: Flushing all firewall rules"
  fi

  # ---- .env direct reads (defense in depth — secrets-guard is primary) ----
  if echo "$command" | grep -qP '(cat|head|tail|less|more|strings|base64)\s+.*\.env(\s|$|\.)' 2>/dev/null; then
    respond "deny" "SECURITY: Direct read of .env file (use secrets-guard two-strike for legitimate access)"
  fi

fi

# ============================================================================
# 🟢 TIER 2: SAFE ALLOWLIST
# ============================================================================
if [[ "$tool_name" == "Bash" && -n "$command" ]]; then

  # Read-only filesystem operations
  if echo "$command" | grep -qP '^(cat|head|tail|less|more|wc|file|stat|du|df|ls|tree|find\s.*-print|which|whereis|type|command\s+-v)\s'; then
    respond "approve" "Safe: read-only operation"
  fi

  # Search/grep operations
  if echo "$command" | grep -qP '^(grep|rg|ag|fd|ack)\s'; then
    respond "approve" "Safe: search operation"
  fi

  # Git read operations
  if echo "$command" | grep -qP '^git\s+(status|log|diff|show|branch|tag|remote|stash\s+list|blame|shortlog|rev-parse|describe)'; then
    respond "approve" "Safe: git read operation"
  fi

  # Git write operations (managed by push-commit hook)
  if echo "$command" | grep -qP '^git\s+(add|commit|stash\s+(push|pop|apply)|checkout|switch|merge|rebase|cherry-pick)'; then
    respond "approve" "Safe: git write operation (push-commit hook manages alignment)"
  fi

  # Package info queries (NOT installs)
  if echo "$command" | grep -qP '^(npm\s+(list|ls|info|view|outdated)|pip\s+(list|show|freeze)|cargo\s+(tree|metadata)|apt\s+(list|show|search))'; then
    respond "approve" "Safe: package manager query"
  fi

  # Development: build, test, run
  if echo "$command" | grep -qP '^(node|python3?|cargo\s+(build|test|run|check)|go\s+(build|test|run|vet)|make|npm\s+(test|run|start|build)|npx|yarn|pnpm)\s'; then
    respond "approve" "Safe: development command"
  fi

  # Directory operations
  if echo "$command" | grep -qP '^(cd|pwd|mkdir|pushd|popd)\s'; then
    respond "approve" "Safe: directory operation"
  fi

  # Info / output
  if echo "$command" | grep -qP '^(echo|printf|date|hostname|uname|whoami|id)\s'; then
    respond "approve" "Safe: info/output command"
  fi

  # Docker read operations
  if echo "$command" | grep -qP '^docker\s+(ps|images|logs|inspect|stats|top|network\s+ls|volume\s+ls)'; then
    respond "approve" "Safe: docker read operation"
  fi

fi

# Non-Bash tools
case "$tool_name" in
  Read|View|Glob|Grep|Search|Ls)
    respond "approve" "Safe: read-only tool"
    ;;
  Write|Edit|MultiEdit|NotebookEdit)
    respond "approve" "Safe: file edit tool (deletion-guard handles dangerous cases)"
    ;;
esac

# ============================================================================
# 🟡 TIER 3: PASSTHROUGH
# ============================================================================
respond "abstain" "No rule matched — deferring to default permission behavior"
