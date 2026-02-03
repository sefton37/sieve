#!/usr/bin/env bash
# ============================================================================
# package-guard.sh — PreToolUse Hook (Bash)
# ============================================================================
# Flags package install commands that introduce NEW dependencies not already
# in the project's lockfile or manifest. Supply chain attacks via trojanized
# npm/pip/cargo packages are a documented threat — postinstall scripts can
# exfiltrate SSH keys, inject backdoors, or modify system files.
#
# Research basis:
#   - Backslash Security: npm supply chain attacks with postinstall scripts
#     that copy ~/.ssh/id_rsa to remote servers
#   - Event-stream incident: popular package hijacked with crypto-stealer
#   - ua-parser-js incident: 8M weekly downloads, injected cryptominer
#   - Anthropic: "be cautious with npm/pip install in autonomous modes"
#
# ALLOWS without prompting:
#   - npm install (no args) — installs from existing package.json/lockfile
#   - pip install -r requirements.txt — installs from existing manifest
#   - cargo build / cargo test — compiles existing dependencies
#   - Any package already in the project's lockfile
#
# TWO-STRIKE for:
#   - npm install <new-package>
#   - pip install <new-package>
#   - cargo add <new-crate>
#   - gem install <new-gem>
#   - apt/brew install <new-package>
#   - go get <new-module>
# ============================================================================
set -euo pipefail

APPROVAL_FILE="/tmp/claude-approvals-packages"
APPROVAL_TTL=600
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-.}"

json_input=$(cat)
tool_name=$(echo "$json_input" | jq -r '.tool_name // empty' 2>/dev/null)
[[ "$tool_name" == "Bash" ]] || exit 0

command=$(echo "$json_input" | jq -r '.tool_input.command // empty' 2>/dev/null)
[[ -n "$command" ]] || exit 0

# ============================================================================
# Detect package install commands and extract package names
# ============================================================================
pkg_manager=""
declare -a new_packages=()
is_lockfile_install=false

# ---- npm / yarn / pnpm ----
if echo "$command" | grep -qP '(?<![a-zA-Z0-9_/.-])(npm|yarn|pnpm)\s+(install|add|i)\b' 2>/dev/null; then
  pkg_manager=$(echo "$command" | grep -oP '(npm|yarn|pnpm)' | head -1)

  # Extract what comes after install/add, skip flags
  args_after=$(echo "$command" | sed -E 's/^.*(npm|yarn|pnpm)\s+(install|add|i)\s*//' | sed 's/;.*$//' | sed 's/&&.*$//')

  # npm install with no package args = lockfile install (safe)
  if [[ -z "$args_after" ]] || echo "$args_after" | grep -qP '^\s*$'; then
    is_lockfile_install=true
  elif echo "$args_after" | grep -qP '^\s*-'; then
    # Only flags, no package names (e.g. npm install --production)
    remaining=$(echo "$args_after" | sed 's/\s*--?[a-zA-Z-]*//g' | tr -d '[:space:]')
    [[ -z "$remaining" ]] && is_lockfile_install=true
  fi

  if [[ "$is_lockfile_install" == "false" ]]; then
    # Extract package names (skip flags starting with -)
    for arg in $args_after; do
      [[ "$arg" =~ ^- ]] && continue
      [[ -z "$arg" ]] && continue
      # Strip version specifiers (@latest, @^1.0.0)
      pkg_name=$(echo "$arg" | sed 's/@[^/].*$//' | sed 's/@$//')
      [[ -n "$pkg_name" ]] && new_packages+=("$pkg_name")
    done
  fi
fi

# ---- pip ----
if echo "$command" | grep -qP '(?<![a-zA-Z0-9_/.-])pip3?\s+install\b' 2>/dev/null; then
  pkg_manager="pip"

  args_after=$(echo "$command" | sed -E 's/^.*pip3?\s+install\s*//' | sed 's/;.*$//' | sed 's/&&.*$//')

  # pip install -r requirements.txt = manifest install (safe)
  if echo "$args_after" | grep -qP '(-r|--requirement)\s+' 2>/dev/null; then
    is_lockfile_install=true
  # pip install . or pip install -e . = local install (safe)
  elif echo "$args_after" | grep -qP '^\s*(-e\s+)?\./?(\[.*\])?\s*$' 2>/dev/null; then
    is_lockfile_install=true
  fi

  if [[ "$is_lockfile_install" == "false" ]]; then
    for arg in $args_after; do
      [[ "$arg" =~ ^- ]] && continue
      [[ "$arg" == "--break-system-packages" ]] && continue
      [[ -z "$arg" ]] && continue
      # Skip if it looks like a flag value
      [[ "$arg" =~ ^/ ]] && continue
      # Strip version specifiers (==1.0, >=2.0, [extras])
      pkg_name=$(echo "$arg" | sed -E 's/[><=!~].*//' | sed 's/\[.*\]//')
      [[ -n "$pkg_name" ]] && new_packages+=("$pkg_name")
    done
  fi
fi

# ---- cargo ----
if echo "$command" | grep -qP '(?<![a-zA-Z0-9_/.-])cargo\s+add\b' 2>/dev/null; then
  pkg_manager="cargo"

  args_after=$(echo "$command" | sed -E 's/^.*cargo\s+add\s*//' | sed 's/;.*$//' | sed 's/&&.*$//')

  for arg in $args_after; do
    [[ "$arg" =~ ^- ]] && continue
    [[ -z "$arg" ]] && continue
    new_packages+=("$arg")
  done
fi

# ---- go get ----
if echo "$command" | grep -qP '(?<![a-zA-Z0-9_/.-])go\s+get\b' 2>/dev/null; then
  pkg_manager="go"

  args_after=$(echo "$command" | sed -E 's/^.*go\s+get\s*//' | sed 's/;.*$//' | sed 's/&&.*$//')

  for arg in $args_after; do
    [[ "$arg" =~ ^- ]] && continue
    [[ -z "$arg" ]] && continue
    new_packages+=("$arg")
  done
fi

# ---- gem install ----
if echo "$command" | grep -qP '(?<![a-zA-Z0-9_/.-])gem\s+install\b' 2>/dev/null; then
  pkg_manager="gem"

  args_after=$(echo "$command" | sed -E 's/^.*gem\s+install\s*//' | sed 's/;.*$//' | sed 's/&&.*$//')

  for arg in $args_after; do
    [[ "$arg" =~ ^- ]] && continue
    [[ -z "$arg" ]] && continue
    new_packages+=("$arg")
  done
fi

# ---- apt / brew (system-level) ----
if echo "$command" | grep -qP '(?<![a-zA-Z0-9_/.-])(apt|apt-get|brew)\s+install\b' 2>/dev/null; then
  pkg_manager=$(echo "$command" | grep -oP '(apt|apt-get|brew)' | head -1)

  args_after=$(echo "$command" | sed -E 's/^.*(apt|apt-get|brew)\s+install\s*//' | sed 's/;.*$//' | sed 's/&&.*$//')

  for arg in $args_after; do
    [[ "$arg" =~ ^- ]] && continue
    [[ -z "$arg" ]] && continue
    new_packages+=("$arg")
  done
fi

# ============================================================================
# No package install detected, or it's a lockfile install
# ============================================================================
[[ -n "$pkg_manager" ]] || exit 0
[[ "$is_lockfile_install" == "true" ]] && exit 0
(( ${#new_packages[@]} > 0 )) || exit 0

# ============================================================================
# Check if packages already exist in lockfile/manifest
# ============================================================================
truly_new=()

for pkg in "${new_packages[@]}"; do
  already_known=false

  case "$pkg_manager" in
    npm|yarn|pnpm)
      # Check package.json and lockfiles
      if [[ -f "$PROJECT_DIR/package.json" ]] && \
         grep -q "\"$pkg\"" "$PROJECT_DIR/package.json" 2>/dev/null; then
        already_known=true
      elif [[ -f "$PROJECT_DIR/package-lock.json" ]] && \
           grep -q "\"$pkg\"" "$PROJECT_DIR/package-lock.json" 2>/dev/null; then
        already_known=true
      elif [[ -f "$PROJECT_DIR/yarn.lock" ]] && \
           grep -q "\"${pkg}@" "$PROJECT_DIR/yarn.lock" 2>/dev/null; then
        already_known=true
      elif [[ -f "$PROJECT_DIR/pnpm-lock.yaml" ]] && \
           grep -q "'${pkg}'" "$PROJECT_DIR/pnpm-lock.yaml" 2>/dev/null; then
        already_known=true
      fi
      ;;
    pip)
      # Check requirements files and pyproject.toml
      for req_file in requirements.txt requirements*.txt pyproject.toml setup.py setup.cfg Pipfile; do
        if [[ -f "$PROJECT_DIR/$req_file" ]] && \
           grep -qi "$pkg" "$PROJECT_DIR/$req_file" 2>/dev/null; then
          already_known=true
          break
        fi
      done
      # Check pip freeze (installed packages)
      if pip list 2>/dev/null | grep -qi "^${pkg}\s" 2>/dev/null; then
        already_known=true
      fi
      ;;
    cargo)
      if [[ -f "$PROJECT_DIR/Cargo.toml" ]] && \
         grep -q "$pkg" "$PROJECT_DIR/Cargo.toml" 2>/dev/null; then
        already_known=true
      elif [[ -f "$PROJECT_DIR/Cargo.lock" ]] && \
           grep -q "\"$pkg\"" "$PROJECT_DIR/Cargo.lock" 2>/dev/null; then
        already_known=true
      fi
      ;;
    go)
      if [[ -f "$PROJECT_DIR/go.mod" ]] && \
         grep -q "$pkg" "$PROJECT_DIR/go.mod" 2>/dev/null; then
        already_known=true
      elif [[ -f "$PROJECT_DIR/go.sum" ]] && \
           grep -q "$pkg" "$PROJECT_DIR/go.sum" 2>/dev/null; then
        already_known=true
      fi
      ;;
    gem)
      if [[ -f "$PROJECT_DIR/Gemfile" ]] && \
         grep -q "$pkg" "$PROJECT_DIR/Gemfile" 2>/dev/null; then
        already_known=true
      elif [[ -f "$PROJECT_DIR/Gemfile.lock" ]] && \
           grep -q "$pkg" "$PROJECT_DIR/Gemfile.lock" 2>/dev/null; then
        already_known=true
      fi
      ;;
    *)
      # System package managers (apt, brew) — always flag
      ;;
  esac

  [[ "$already_known" == "false" ]] && truly_new+=("$pkg")
done

# All packages already known
(( ${#truly_new[@]} > 0 )) || exit 0

# ============================================================================
# Two-strike approval
# ============================================================================
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

# System package managers get extra warning
system_warning=""
if [[ "$pkg_manager" == "apt" || "$pkg_manager" == "apt-get" || "$pkg_manager" == "brew" ]]; then
  system_warning="
⚠ This is a SYSTEM-LEVEL package manager. These packages install globally
  and may include daemons, services, or setuid binaries."
fi

cat >&2 <<EOF
📦 PACKAGE GUARD — Blocked.

Manager: ${pkg_manager}
Command: ${command}

New package(s) not found in project lockfile/manifest:
$(printf '  ◦ %s\n' "${truly_new[@]}")
${system_warning}
Supply chain risk: packages can execute arbitrary code during install
(postinstall scripts, setup.py, build.rs). Trojanized packages have
been used to steal SSH keys, inject cryptominers, and backdoor systems.

→ Tell the user which new packages you want to install and why.
→ If possible, suggest the user verify the package first:
    npm: https://www.npmjs.com/package/<name>
    pip: https://pypi.org/project/<name>
    cargo: https://crates.io/crates/<name>
→ After confirmation, retry the EXACT same command.
→ Approval expires in $(( APPROVAL_TTL / 60 )) minutes.
EOF
exit 2
