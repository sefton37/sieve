#!/usr/bin/env bash
# ============================================================================
# network-guard.sh — PreToolUse Hook (Bash)
# ============================================================================
# Blocks network exfiltration vectors that bypass HTTP-level controls.
# The permission-handler already catches curl|bash and curl --data patterns.
# This hook covers the gaps: DNS exfiltration, raw sockets, side channels.
#
# Research basis:
#   - CVE-2025-55284: Secrets exfiltrated via ping subdomain encoding
#   - Embrace The Red: Claude Code DNS exfiltration of .env contents
#   - Backslash Security: Block curl, fetch, .env access by default
#   - Side-channel vectors: nc, socat, telnet, dig TXT queries
#
# HARD BLOCK (no two-strike) for exfiltration-capable tools.
# Two-strike for legitimate remote tools (ssh, scp, rsync).
# ============================================================================
set -euo pipefail

APPROVAL_FILE="/tmp/claude-approvals-network"
APPROVAL_TTL=600

json_input=$(cat)
tool_name=$(echo "$json_input" | jq -r '.tool_name // empty' 2>/dev/null)
[[ "$tool_name" == "Bash" ]] || exit 0

command=$(echo "$json_input" | jq -r '.tool_input.command // empty' 2>/dev/null)
[[ -n "$command" ]] || exit 0

# ---- Helper: hard block (no two-strike) ----
hard_block() {
  local reason="$1"
  cat >&2 <<EOF
🌐 NETWORK GUARD — Hard blocked.

${reason}
Command: ${command}

This command can exfiltrate data through non-HTTP channels.
It cannot be auto-approved. Run it manually if truly needed.
EOF
  exit 2
}

# ---- Helper: two-strike block ----
soft_block() {
  local reason="$1"
  local cmd_hash
  cmd_hash=$(echo -n "$command" | sha256sum | cut -d' ' -f1)
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

  # Strike 2
  if grep -q "^${cmd_hash}|" "$APPROVAL_FILE" 2>/dev/null; then
    sed -i "/^${cmd_hash}|/d" "$APPROVAL_FILE"
    return 0  # approved
  fi

  # Strike 1
  echo "${cmd_hash}|${now}" >> "$APPROVAL_FILE"

  cat >&2 <<EOF
🌐 NETWORK GUARD — Blocked.

${reason}
Command: ${command}

→ Explain to the user what remote connection is needed and why.
→ After explicit confirmation, retry the EXACT same command.
→ Approval expires in $(( APPROVAL_TTL / 60 )) minutes.
EOF
  exit 2
}

# ============================================================================
# TIER 1: HARD BLOCK — Exfiltration-capable side-channel tools
# ============================================================================
# These tools can encode arbitrary data into network requests in ways
# that bypass HTTP monitoring. Almost never needed in development.

# ping — primary DNS exfiltration vector (data encoded in subdomain)
if echo "$command" | grep -qP '(?<![a-zA-Z0-9_/.-])ping\s' 2>/dev/null; then
  hard_block "ping can exfiltrate data via DNS subdomain encoding (CVE-2025-55284)."
fi

# nc / ncat / netcat — raw socket connections
if echo "$command" | grep -qP '(?<![a-zA-Z0-9_/.-])(nc|ncat|netcat)\s' 2>/dev/null; then
  hard_block "netcat can open raw socket connections for data exfiltration."
fi

# socat — advanced socket relay
if echo "$command" | grep -qP '(?<![a-zA-Z0-9_/.-])socat\s' 2>/dev/null; then
  hard_block "socat can relay data through arbitrary socket connections."
fi

# telnet — unencrypted remote connection
if echo "$command" | grep -qP '(?<![a-zA-Z0-9_/.-])telnet\s' 2>/dev/null; then
  hard_block "telnet can send data over unencrypted connections."
fi

# dig/nslookup/host with data that could encode secrets in DNS queries
if echo "$command" | grep -qP '(?<![a-zA-Z0-9_/.-])(dig|nslookup|host)\s' 2>/dev/null; then
  # Allow simple lookups (dig example.com), block if there's variable expansion
  # or piped/substituted content that could contain secrets
  if echo "$command" | grep -qP '(\$|\`|<\(|xargs|pipe)' 2>/dev/null; then
    hard_block "DNS query with dynamic content — potential data exfiltration via DNS."
  fi
  # Simple static lookups are fine (exit 0 falls through)
fi

# ============================================================================
# TIER 2: HARD BLOCK — HTTP exfiltration patterns not caught by permission-handler
# ============================================================================

# curl/wget sending data FROM files or variables (more patterns than permission-handler)
if echo "$command" | grep -qP '(curl|wget).*(-F|--form|--upload-file|-T)\s' 2>/dev/null; then
  hard_block "HTTP file upload — potential data exfiltration."
fi

# curl with variable expansion in URL (secrets encoded in URL path/params)
if echo "$command" | grep -qP 'curl\s.*\$' 2>/dev/null; then
  # Check if variable looks like a secret
  if echo "$command" | grep -qiP 'curl\s.*\$(API|SECRET|TOKEN|KEY|PASSWORD|CREDENTIAL)' 2>/dev/null; then
    hard_block "HTTP request with secret variable in URL — data exfiltration."
  fi
fi

# ============================================================================
# TIER 3: TWO-STRIKE — Legitimate remote tools that need confirmation
# ============================================================================

# ssh to remote hosts (Kel manages VPS/home server — legitimate but should confirm)
if echo "$command" | grep -qP '(?<![a-zA-Z0-9_/.-])ssh\s' 2>/dev/null; then
  # Allow ssh-keygen, ssh-add, ssh-agent (local operations)
  if ! echo "$command" | grep -qP 'ssh-(keygen|add|agent)' 2>/dev/null; then
    soft_block "Outbound SSH connection."
  fi
fi

# scp — remote file copy
if echo "$command" | grep -qP '(?<![a-zA-Z0-9_/.-])scp\s' 2>/dev/null; then
  soft_block "Remote file copy via scp."
fi

# rsync to remote hosts (contains :)
if echo "$command" | grep -qP '(?<![a-zA-Z0-9_/.-])rsync\s' 2>/dev/null; then
  if echo "$command" | grep -qP 'rsync\s.*[a-zA-Z0-9_.@-]+:' 2>/dev/null; then
    soft_block "Remote file sync via rsync."
  fi
fi

exit 0
