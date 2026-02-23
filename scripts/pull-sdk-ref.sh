#!/usr/bin/env bash
# pull-sdk-ref.sh — Print Wayfinder SDK reference docs, preferring OpenClaw (CLI)
# overrides under `openclaw/skills/` when present.
#
# Usage:
#   ./scripts/pull-sdk-ref.sh <topic>          Show docs for a specific topic
#   ./scripts/pull-sdk-ref.sh --list          List available topics
#   ./scripts/pull-sdk-ref.sh --all           Show all reference docs
#   ./scripts/pull-sdk-ref.sh --commit <ref>  Read docs from a specific git ref (no checkout)
#   ./scripts/pull-sdk-ref.sh --version       Show the current SDK git version
#
# Example:
#   ./scripts/pull-sdk-ref.sh hyperlend

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SDK_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SKILLS_DIR="$SDK_ROOT/.claude/skills"
OVERRIDES_DIR="$SDK_ROOT/openclaw/skills"

SDK_COMMIT=""

ORDERED_TOPICS=(
  setup
  strategies
  contracts
  aave_v3
  boros
  brap
  ccxt
  hyperlend
  hyperliquid
  moonwell
  morpho
  pendle
  polymarket
  projectx
  uniswap
  data
  simulation
  promote
)

topic_dir() {
  case "$1" in
    setup) echo "setup" ;;
    strategies) echo "developing-wayfinder-strategies" ;;
    contracts | contract) echo "contract-development" ;;

    aave | aave_v3) echo "using-aave-v3-adapter" ;;
    boros) echo "using-boros-adapter" ;;
    brap) echo "using-brap-adapter" ;;
    ccxt) echo "using-ccxt-adapter" ;;
    hyperlend) echo "using-hyperlend-adapter" ;;
    hyperliquid) echo "using-hyperliquid-adapter" ;;
    moonwell) echo "using-moonwell-adapter" ;;
    morpho) echo "using-morpho-adapter" ;;
    pendle) echo "using-pendle-adapter" ;;
    polymarket) echo "using-polymarket-adapter" ;;
    projectx) echo "using-projectx-adapter" ;;
    uniswap) echo "using-uniswap-adapter" ;;

    data) echo "using-pool-token-balance-data" ;;
    simulation) echo "simulation-dry-run" ;;
    promote) echo "promote-wayfinder-script" ;;
    *) return 1 ;;
  esac
}

print_topic_header() {
  local topic="$1"
  local dir_name="$2"
  echo ""
  echo "================================================================================"
  echo "  $topic  ($dir_name)"
  echo "================================================================================"
  echo ""
}

extract_description() {
  local filepath="$1"
  awk '
    BEGIN { in_yaml = 0 }
    $0 == "---" { in_yaml = !in_yaml; next }
    in_yaml && $0 ~ /^description:/ {
      sub(/^description:[[:space:]]*/, "", $0)
      print
      exit
    }
  ' "$filepath" 2>/dev/null || true
}

print_file_local() {
  local filepath="$1"
  local relpath="${filepath#$SDK_ROOT/}"
  echo "--- $relpath ---"
  echo ""
  cat "$filepath"
  echo ""
}

print_file_git() {
  local ref="$1"
  local relpath="$2"
  echo "--- $relpath @ $ref ---"
  echo ""
  git -C "$SDK_ROOT" show "$ref:$relpath"
  echo ""
}

show_topic_local() {
  local topic="$1"
  local dir_name=""
  dir_name="$(topic_dir "$topic" 2>/dev/null || true)"
  if [[ -z "$dir_name" ]]; then
    echo "ERROR: Unknown topic '$topic'." >&2
    echo "Run with --list to see available topics." >&2
    exit 1
  fi

  local base_skill_dir="$SKILLS_DIR/$dir_name"
  local override_skill_dir="$OVERRIDES_DIR/$dir_name"
  if [[ ! -d "$base_skill_dir" && ! -d "$override_skill_dir" ]]; then
    echo "ERROR: Skill directory not found for topic '$topic'." >&2
    echo "Tried:" >&2
    echo "  $base_skill_dir" >&2
    echo "  $override_skill_dir" >&2
    exit 1
  fi

  print_topic_header "$topic" "$dir_name"

  if [[ -f "$override_skill_dir/SKILL.md" ]]; then
    print_file_local "$override_skill_dir/SKILL.md"
  elif [[ -f "$base_skill_dir/SKILL.md" ]]; then
    print_file_local "$base_skill_dir/SKILL.md"
  fi

  local base_rules_dir="$base_skill_dir/rules"
  local override_rules_dir="$override_skill_dir/rules"
  if [[ -d "$base_rules_dir" || -d "$override_rules_dir" ]]; then
    local rule_name=""
    while IFS= read -r rule_name; do
      [[ -z "$rule_name" ]] && continue
      if [[ -f "$override_rules_dir/$rule_name" ]]; then
        print_file_local "$override_rules_dir/$rule_name"
      elif [[ -f "$base_rules_dir/$rule_name" ]]; then
        print_file_local "$base_rules_dir/$rule_name"
      fi
    done < <(
      {
        if [[ -d "$base_rules_dir" ]]; then
          local rule_file=""
          for rule_file in "$base_rules_dir"/*.md; do
            [[ -f "$rule_file" ]] && basename "$rule_file"
          done
        fi
        if [[ -d "$override_rules_dir" ]]; then
          local rule_file=""
          for rule_file in "$override_rules_dir"/*.md; do
            [[ -f "$rule_file" ]] && basename "$rule_file"
          done
        fi
      } | sort -u
    )
  fi
}

show_topic_git() {
  local topic="$1"
  local ref="$2"
  local dir_name=""
  dir_name="$(topic_dir "$topic" 2>/dev/null || true)"
  if [[ -z "$dir_name" ]]; then
    echo "ERROR: Unknown topic '$topic'." >&2
    echo "Run with --list to see available topics." >&2
    exit 1
  fi

  print_topic_header "$topic" "$dir_name"

  local base_skill_dir=".claude/skills/$dir_name"
  local override_skill_dir="openclaw/skills/$dir_name"

  local base_skill_md="$base_skill_dir/SKILL.md"
  local override_skill_md="$override_skill_dir/SKILL.md"
  if git -C "$SDK_ROOT" cat-file -e "$ref:$override_skill_md" 2>/dev/null; then
    print_file_git "$ref" "$override_skill_md"
  elif git -C "$SDK_ROOT" cat-file -e "$ref:$base_skill_md" 2>/dev/null; then
    print_file_git "$ref" "$base_skill_md"
  fi

  local base_rules_dir="$base_skill_dir/rules"
  local override_rules_dir="$override_skill_dir/rules"
  local rule_paths
  rule_paths="$(
    git -C "$SDK_ROOT" ls-tree -r --name-only "$ref" -- "$base_rules_dir" "$override_rules_dir" 2>/dev/null || true
  )"
  if [[ -n "$rule_paths" ]]; then
    local rel_rule=""
    while IFS= read -r rel_rule; do
      [[ -z "$rel_rule" ]] && continue
      local override_path="$override_skill_dir/$rel_rule"
      local base_path="$base_skill_dir/$rel_rule"
      if git -C "$SDK_ROOT" cat-file -e "$ref:$override_path" 2>/dev/null; then
        print_file_git "$ref" "$override_path"
      elif git -C "$SDK_ROOT" cat-file -e "$ref:$base_path" 2>/dev/null; then
        print_file_git "$ref" "$base_path"
      fi
    done < <(
      while IFS= read -r path; do
        [[ -z "$path" ]] && continue
        if [[ "$path" == "$base_skill_dir/"* ]]; then
          printf "%s\n" "${path#"$base_skill_dir/"}"
        elif [[ "$path" == "$override_skill_dir/"* ]]; then
          printf "%s\n" "${path#"$override_skill_dir/"}"
        fi
      done < <(printf "%s\n" "$rule_paths") | sort -u
    )
  fi
}

show_list() {
  if [[ ! -d "$SKILLS_DIR" ]]; then
    echo "ERROR: Skills directory not found at $SKILLS_DIR" >&2
    exit 1
  fi

  echo "Available topics:"
  echo ""
  local topic=""
  for topic in "${ORDERED_TOPICS[@]}"; do
    local dir_name=""
    dir_name="$(topic_dir "$topic")"
    local desc=""
    if [[ -f "$SKILLS_DIR/$dir_name/SKILL.md" ]]; then
      desc="$(extract_description "$SKILLS_DIR/$dir_name/SKILL.md")"
    fi
    if [[ -n "$desc" ]]; then
      printf "  %-12s %s\n" "$topic" "$desc"
    else
      printf "  %-12s (%s)\n" "$topic" "$dir_name"
    fi
  done

  echo ""
  echo "Usage:"
  echo "  $0 <topic>          Show docs for a topic"
  echo "  $0 --all            Show all docs"
  echo "  $0 --list           This list"
  echo "  $0 --commit <ref>   Read docs from a specific git ref (no checkout)"
  echo "  $0 --version        Show the current SDK git version"
  echo ""
  echo "SDK root: $SDK_ROOT"
}

show_all() {
  local topic=""
  for topic in "${ORDERED_TOPICS[@]}"; do
    if [[ -n "$SDK_COMMIT" ]]; then
      show_topic_git "$topic" "$SDK_COMMIT"
    else
      show_topic_local "$topic"
    fi
  done
}

show_version() {
  local desc=""
  desc="$(git -C "$SDK_ROOT" describe --tags --always --dirty 2>/dev/null || true)"
  if [[ -n "$desc" ]]; then
    echo "SDK version: $desc"
  else
    echo "SDK version: $(git -C "$SDK_ROOT" rev-parse --short HEAD)"
  fi
}

# --- Parse args ---
ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --commit)
      SDK_COMMIT="${2:?missing value for --commit}"
      shift 2
      ;;
    --version|-v)
      show_version
      exit 0
      ;;
    --help|-h)
      show_list
      exit 0
      ;;
    *)
      ARGS+=("$1")
      shift
      ;;
  esac
done
set -- "${ARGS[@]+"${ARGS[@]}"}"

if [[ $# -eq 0 ]]; then
  show_list
  exit 0
fi

case "$1" in
  --list|-l)
    show_list
    ;;
  --all|-a)
    show_all
    ;;
  *)
    if [[ ! -d "$SKILLS_DIR" ]]; then
      echo "ERROR: Skills directory not found at $SKILLS_DIR" >&2
      exit 1
    fi

    topic=""
    for topic in "$@"; do
      if [[ -n "$SDK_COMMIT" ]]; then
        show_topic_git "$topic" "$SDK_COMMIT"
      else
        show_topic_local "$topic"
      fi
    done
    ;;
esac
