#!/usr/bin/env bash
# A/B eval: the sports method stack (current `wayfinder` agent: sports worker, slate
# pipelines, sports_posterior adjudication) vs the pre-sports baseline
# (`wayfinder-baseline`: no sports tools, no sports worker, sports skill hidden —
# web research + Polymarket only).
#
# Per question the arms run back-to-back (baseline first) so market drift between
# arms stays small. Final answers are harvested from the opencode session DB (the
# CLI does not always flush the last message to stdout) into
# .wayfinder_runs/evals/q<N>_<arm>.md, ready for the blind judge
# (scripts/eval_sports_ab_judge.md).
#
# Env: WAYFINDER_API_KEY (LLM proxy key) and WAYFINDER_CONFIG_PATH must be exported.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$REPO/.wayfinder_runs/evals"
DB="$HOME/.local/share/opencode/opencode.db"
OPENCODE="${OPENCODE_BIN:-$HOME/.opencode/bin/opencode}"
MODEL="${EVAL_MODEL:-wayfinder/deepseek-v4-pro}"
TIMEOUT="${EVAL_TIMEOUT:-1500}"
SKILL_DIR="$REPO/.claude/skills/using-sports-data"
SKILL_HIDDEN="/tmp/_eval_hidden_using-sports-data"
RESEARCH_MD="$REPO/.opencode/agents/wayfinder-research.md"

# Question set: one question per line in $EVAL_QUESTIONS_FILE, else the built-in pair.
if [ -n "${EVAL_QUESTIONS_FILE:-}" ]; then
  QUESTIONS=()
  while IFS= read -r line; do
    [ -n "$line" ] && QUESTIONS+=("$line")
  done < "$EVAL_QUESTIONS_FILE"
  [ ${#QUESTIONS[@]} -gt 0 ] || { echo "no questions in $EVAL_QUESTIONS_FILE" >&2; exit 1; }
else
  QUESTIONS=(
    "Scan the World Cup winner (trophy) market — are any countries mispriced, especially vs what's tradeable on Polymarket? Show the numbers behind every call."
    "Are the odds on the next Canada vs Bosnia & Herzegovina World Cup game priced correctly? Blend the data with current news — injuries, lineups, late team news."
  )
fi

mkdir -p "$OUT"
hide_sports() {
  mv "$SKILL_DIR" "$SKILL_HIDDEN"
  # research can delegate to the sports worker — close that path for the baseline arm
  python3 - "$RESEARCH_MD" <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1])
p.write_text(p.read_text().replace("    wayfinder-sports: allow\n", "    wayfinder-sports: deny\n", 1))
PY
}
restore_sports() {
  [ -d "$SKILL_HIDDEN" ] && mv "$SKILL_HIDDEN" "$SKILL_DIR" || true
  (cd "$REPO" && git checkout -q -- .opencode/agents/wayfinder-research.md) || true
}
trap restore_sports EXIT

harvest() { # $1 = question text, $2 = out file
  python3 - "$1" "$2" "$DB" <<'PY'
import sqlite3, sys, json
question, out_path, db = sys.argv[1], sys.argv[2], sys.argv[3]
con = sqlite3.connect(db)
needle = question[:60]
# the primary session = newest session containing the question text
row = con.execute(
    """SELECT m.session_id FROM part p JOIN message m ON p.message_id = m.id
       WHERE json_extract(p.data,'$.type')='text' AND json_extract(p.data,'$.text') LIKE ?
       ORDER BY m.time_created DESC LIMIT 1""",
    (f"%{needle}%",),
).fetchone()
if not row:
    sys.exit(f"no session found for question: {needle!r}")
answer = con.execute(
    """SELECT json_extract(p.data,'$.text') FROM part p JOIN message m ON p.message_id = m.id
       WHERE m.session_id=? AND json_extract(p.data,'$.type')='text'
         AND length(json_extract(p.data,'$.text')) > 400
       ORDER BY m.time_created DESC LIMIT 1""",
    (row[0],),
).fetchone()
if not answer:
    sys.exit(f"no final answer in session {row[0]}")
open(out_path, "w").write(answer[0])
print(f"harvested {len(answer[0])} chars -> {out_path}")
PY
}

run_arm() { # $1 = agent, $2 = question idx (1-based), $3 = arm label
  local q="${QUESTIONS[$(($2 - 1))]}"
  local log="$OUT/q$2_$3.log" ans="$OUT/q$2_$3.md"
  echo "=== q$2 / $3 ($1) ==="
  (cd "$REPO" && timeout "$TIMEOUT" "$OPENCODE" run --agent "$1" -m "$MODEL" "$q") \
    > "$log" 2>&1 || echo "  (session exit nonzero — continuing)"
  harvest "$q" "$ans"
  # contamination check for the baseline arm
  if [ "$3" = "baseline" ]; then
    if grep -qE "wayfinder_sports_|prop_slate|game_slate|futures_slate|sports_posterior" "$log"; then
      echo "  WARNING: baseline log mentions sports tooling — inspect $log" >&2
    fi
  fi
  sleep 20
}

for i in $(seq 1 ${#QUESTIONS[@]}); do
  hide_sports
  run_arm wayfinder-baseline "$i" baseline
  restore_sports
  run_arm wayfinder "$i" new
done

echo "done — answers in $OUT (q*_baseline.md / q*_new.md); judge with scripts/eval_sports_ab_judge.md"
