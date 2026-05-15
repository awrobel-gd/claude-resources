---
name: supply-chain-audit
description: Audit the local machine for Node.js projects affected by the mini-Shai-Hulud npm supply chain worm (first observed 2026-05). Discovers projects, checks each against known IOCs, and produces a structured findings report.
---

# Supply Chain Audit — mini-Shai-Hulud npm Worm

## Background

The mini-Shai-Hulud campaign compromised 100+ npm package versions by injecting a self-spreading worm via GitHub OIDC token theft. It reads CI runner memory directly (`/proc/{pid}/mem`) to steal every secret, then republishes infected versions of any package the victim maintains. Affected package families include `@tanstack/*`, `@mistralai/*`, `@opensearch-project/*`, `@uipath/*`, `@draftlab/*`.

Reference: https://www.stepsecurity.io/blog/mini-shai-hulud-is-back-a-self-spreading-supply-chain-attack-hits-the-npm-ecosystem

---

## Execution Steps

Execute all phases in order. Do not skip any phase.

---

### Phase 1 — Project Discovery

Locate all Node.js projects on the local machine. Exclude `node_modules` trees and common non-project paths.

```bash
# Find all package.json files, excluding node_modules and hidden system dirs
find ~ /opt /srv -name "package.json" \
  -not -path "*/node_modules/*" \
  -not -path "*/.cache/*" \
  -not -path "*/vendor/*" \
  2>/dev/null | sort
```

Build a working list of **project root directories** (the parent directory of each found `package.json`). For very large machines, scope to `~/code`, `~/projects`, `~/dev`, `~/src`, `~/work`, or whichever directories the user confirms hold their projects.

For each discovered project root, perform Phase 2 checks.

---

### Phase 2 — Per-Project IOC Checks

Run **all seven checks** for every project root. Capture output for the report.

#### Check A — Affected Package in Manifest

Inspect `package.json` fields: `dependencies`, `devDependencies`, `peerDependencies`, `optionalDependencies`.

```bash
PROJECT=<path>
grep -E \
  '"@tanstack/|"@mistralai/|"@opensearch-project/opensearch|"@uipath/|"@draftlab/' \
  "$PROJECT/package.json" 2>/dev/null
```

Flag if any match is found.

#### Check B — Affected Package in Lockfile

Lockfiles pin exact resolved URLs and can reveal a compromised version even if `package.json` only shows a semver range.

```bash
PROJECT=<path>
# npm
grep -E \
  '"@tanstack/|"@mistralai/|"@opensearch-project/opensearch|"@uipath/|"@draftlab/' \
  "$PROJECT/package-lock.json" 2>/dev/null | head -40

# yarn classic / berry
grep -E \
  '"@tanstack/|@mistralai/|@opensearch-project/opensearch|@uipath/|@draftlab/' \
  "$PROJECT/yarn.lock" 2>/dev/null | head -40

# pnpm
grep -E \
  '"@tanstack/|@mistralai/|@opensearch-project/opensearch|@uipath/|@draftlab/' \
  "$PROJECT/pnpm-lock.yaml" 2>/dev/null | head -40
```

Flag if any match is found. Note the resolved version strings for the report.

#### Check C — Malicious Worm Payload Files in node_modules

The worm injects wrapper scripts alongside legitimate package files.

```bash
PROJECT=<path>
find "$PROJECT/node_modules" \
  \( -name "router_init.js" \
  -o -name "tanstack_runner.js" \
  -o -name "opensearch_init.js" \) \
  2>/dev/null
```

Also check for the `@tanstack/setup` pseudo-package which the worm uses as a staging dependency:

```bash
ls "$PROJECT/node_modules/@tanstack/setup" 2>/dev/null && echo "FOUND @tanstack/setup"
```

Flag any matches as **HIGH SEVERITY**.

#### Check D — Injected Lifecycle Hook (prepare script)

The worm adds obfuscated code to the `prepare` lifecycle hook in affected packages.

```bash
PROJECT=<path>
find "$PROJECT/node_modules" -name "package.json" -maxdepth 4 \
  -not -path "*/node_modules/*/node_modules/*" \
  2>/dev/null \
  | xargs grep -l '"prepare"' 2>/dev/null \
  | xargs grep -E '"prepare".*eval|"prepare".*Buffer\.from|"prepare".*atob' 2>/dev/null
```

Flag any match as **CRITICAL**.

#### Check E — Injected Repository / CI Files

The worm writes files into the project tree to establish persistence and re-infection vectors.

```bash
PROJECT=<path>

# Unexpected GitHub Actions workflow
ls "$PROJECT/.github/workflows/codeql_analysis.yml" 2>/dev/null && \
  echo "SUSPICIOUS: codeql_analysis.yml present"

# VS Code task that auto-runs on folder open
grep -l "folderOpen\|onDidOpenTerminal" \
  "$PROJECT/.vscode/tasks.json" 2>/dev/null && \
  echo "SUSPICIOUS: .vscode/tasks.json contains auto-run task"

# Claude Code settings with injected SessionStart hook
grep -l "SessionStart\|PostToolUse\|PreToolUse" \
  "$PROJECT/.claude/settings.json" 2>/dev/null && \
  echo "SUSPICIOUS: .claude/settings.json contains lifecycle hook"
```

Also check for the Linux persistence unit (global, not per-project):

```bash
ls ~/.config/systemd/user/gh-token-monitor.service 2>/dev/null && \
  echo "CRITICAL: systemd persistence unit found"
```

Flag any file presence as **HIGH SEVERITY**. Verify file contents before dismissing; the worm uses legitimate-sounding filenames.

#### Check F — C2 Domain References in Source and Dependencies

```bash
PROJECT=<path>
grep -r \
  --include="*.js" --include="*.ts" --include="*.cjs" --include="*.mjs" \
  -l \
  "masscan\.cloud\|getsession\.org\|git-tanstack\.com" \
  "$PROJECT" 2>/dev/null
```

Also check inside `node_modules` for the affected package families only (to avoid excessive scanning):

```bash
grep -r \
  --include="*.js" \
  -l \
  "masscan\.cloud\|getsession\.org\|git-tanstack\.com" \
  "$PROJECT/node_modules/@tanstack" \
  "$PROJECT/node_modules/@mistralai" \
  "$PROJECT/node_modules/@opensearch-project" \
  2>/dev/null
```

Flag any match as **CRITICAL**.

#### Check G — Git History for Attacker Markers

```bash
PROJECT=<path>
cd "$PROJECT" 2>/dev/null || exit 0

# Commits authored by attacker's bot identity
git log --all --oneline --author="claude@users.noreply.github.com" 2>/dev/null | head -10

# Suspicious branch names using Dune terminology (attacker's convention)
git branch -a 2>/dev/null | grep -E "fremen|sandworm|melange|dependabot/github_actions/format" | head -10

# Any reference to attacker's fork
git log --all --oneline --grep="voicproducoes" 2>/dev/null | head -10
```

Flag any match.

---

### Phase 3 — Global Machine Checks

Run once, not per-project.

```bash
# Systemd persistence (Linux only)
ls ~/.config/systemd/user/gh-token-monitor.service 2>/dev/null

# npm tokens with ransom threat in description (requires npm login)
npm token list 2>/dev/null | grep -i "IfYouRevokeThis\|wipe"

# Any unexpected process connecting to known C2
lsof -i 2>/dev/null | grep -E "masscan\.cloud|getsession\.org|git-tanstack\.com"
```

---

### Phase 4 — Report Generation

After completing all checks, produce a structured markdown report using the template below. Fill in every section; use `None detected` for clean results.

```markdown
# Supply Chain Audit Report — mini-Shai-Hulud
**Date:** <date>
**Machine:** <hostname>
**Auditor:** <agent or user>

## Executive Summary
<1–3 sentences: how many projects scanned, how many flagged, overall risk level>

## Projects Scanned
| # | Project Path | Checks Run | Status |
|---|---|---|---|
| 1 | /path/to/project | A B C D E F G | CLEAN / FLAGGED |

## Findings

### CRITICAL
<List each critical finding with: project path, check that triggered, exact file/line/output>

### HIGH
<List each high-severity finding>

### INFORMATIONAL
<Affected packages detected in manifests/lockfiles but no payload evidence>

## Global Machine Checks
<Systemd service, npm token, active connections>

## Recommended Actions
For each flagged project:
1. **Isolate**: Do not run `npm install` or `npm run` until clean
2. **Rotate secrets**: Revoke all tokens, keys, and credentials that were ever present in CI for this project
3. **Remove malicious files**: Delete any files found in Check C, D, E
4. **Downgrade**: Pin affected packages to last known-clean versions (check npm advisory or registry history)
5. **Reinstall**: Delete `node_modules` and lockfile, then `npm install` fresh
6. **Audit git history**: Review recent commits for unauthorized changes

## References
- https://www.stepsecurity.io/blog/mini-shai-hulud-is-back-a-self-spreading-supply-chain-attack-hits-the-npm-ecosystem
```

---

## IOC Quick Reference

### Affected Package Families
- `@tanstack/*` — 84 malicious versions across 42 packages
- `@mistralai/mistralai`
- `@opensearch-project/opensearch`
- `@uipath/*` — 50+ packages
- `@draftlab/*`
- `@tanstack/setup` — worm staging dependency (should never exist legitimately)

### Malicious Payload Files
| File | Notes |
|---|---|
| `router_init.js` | SHA-256: `ab4fcadaec49c03278063dd269ea5eef82d24f2124a8e15d7b90f2fa8601266c` |
| `tanstack_runner.js` | |
| `opensearch_init.js` | |

### Injected Project Files
| File | Injection Purpose |
|---|---|
| `.github/workflows/codeql_analysis.yml` | Re-infection via CI |
| `.vscode/tasks.json` | Persistence on folder open |
| `.claude/settings.json` | Persistence via SessionStart hook |
| `~/.config/systemd/user/gh-token-monitor.service` | Linux boot persistence |

### C2 Domains
- `api.masscan.cloud`
- `filev2.getsession.org`
- `git-tanstack.com`

### Attacker Infrastructure
- GitHub account: `voicproducoes` (created 2026-03-19)
- Fork: `voicproducoes/router`
- Commit: `79ac49eedf774dd4b0cfa308722bc463cfe5885c`
- Bot email used in git commits: `claude@users.noreply.github.com`
- npm token description: `IfYouRevokeThisTokenItWillWipeTheComputerOfTheOwner`
- Branch naming pattern: `dependabot/github_actions/format/{fremen|sandworm|melange|...}`

### Tarball Size Anomaly
Compromised `@tanstack/router-core` versions: ~900 KB vs clean ~190 KB. A 4x+ size increase in a patch release is a strong signal.
