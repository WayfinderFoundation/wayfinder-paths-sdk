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
TIMEOUT="${EVAL_TIMEOUT:-600}"
ATTEMPTS="${EVAL_ATTEMPTS:-1}"
IDLE_TIMEOUT="${EVAL_IDLE_TIMEOUT:-180}"
SKIP_EXISTING="${EVAL_SKIP_EXISTING:-0}"
ONLY_INDEXES="${EVAL_ONLY_INDEXES:-}"
SKILL_DIR="$REPO/.claude/skills/using-sports-data"
SKILL_HIDDEN="/tmp/_eval_hidden_using-sports-data"
RESEARCH_MD="$REPO/.opencode/agents/wayfinder-research.md"
RESEARCH_BACKUP="/tmp/_eval_wayfinder-research.md.$$"

# Question set: one question per line in $EVAL_QUESTIONS_FILE, else the built-in battery.
if [ -n "${EVAL_QUESTIONS_FILE:-}" ]; then
  QUESTIONS=()
  while IFS= read -r line; do
    [ -n "$line" ] && QUESTIONS+=("$line")
  done < "$EVAL_QUESTIONS_FILE"
  [ ${#QUESTIONS[@]} -gt 0 ] || { echo "no questions in $EVAL_QUESTIONS_FILE" >&2; exit 1; }
else
  QUESTIONS=(
    "What are the most mispriced World Cup markets right now — across everything: match markets, group winners, and who will win the trophy. Show the numbers behind every call, condition on current results, and say whether any edge is actually executable."
    "For the next World Cup matches involving Saudi Arabia, Austria, and Jordan, price the moneyline and estimate fair spreads and point/goal totals. Use PM/HL as the executable betting surface, use creative supporting data beyond just the World Cup dataset where available, treat provider sportsbook odds as optional context only, and clearly label any unavailable or model-estimated lines."
    "Analyze Melissa Mullins vs Bia Mesquita in UFC/MMA: is there any betting edge, and what do the available data and markets say? If the sports provider or executable markets do not support this fight, say so cleanly and do not invent odds, stats, or a recommendation."
  )
fi

mkdir -p "$OUT"
cp "$RESEARCH_MD" "$RESEARCH_BACKUP"
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
  [ -f "$RESEARCH_BACKUP" ] && cp "$RESEARCH_BACKUP" "$RESEARCH_MD" || true
}
cleanup() {
  restore_sports
  rm -f "$RESEARCH_BACKUP"
}
trap cleanup EXIT

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
         AND lower(json_extract(p.data,'$.text')) LIKE '%final answer%'
       ORDER BY m.time_created DESC LIMIT 1""",
    (row[0],),
).fetchone()
if not answer:
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

validate_harvested_answer() { # $1 = answer file
  python3 - "$1" <<'PY'
import pathlib, sys

path = pathlib.Path(sys.argv[1])
text = path.read_text(errors="replace")
lower = text.lower()

handoff_markers = [
    "continue if you have next steps",
    "stop and ask for clarification if you are unsure how to proceed",
]
checkpoint_shape = (
    "## progress" in lower
    and "## next steps" in lower
    and ("## blocked" in lower or "## critical context" in lower)
)

if text.strip() in {"(no answer harvested)", "(invalid checkpoint/handoff answer)"}:
    sys.exit("missing answer")
if "final answer" not in lower:
    sys.exit("missing FINAL ANSWER marker")
if any(marker in lower for marker in handoff_markers) or checkpoint_shape:
    sys.exit("checkpoint/handoff answer")
PY
}

extract_final_from_log() { # $1 = log file, $2 = out file
  python3 - "$1" "$2" <<'PY'
import pathlib, sys

log_path, out_path = map(pathlib.Path, sys.argv[1:3])
text = log_path.read_text(errors="replace")
start = text.find("FINAL ANSWER")
if start < 0:
    sys.exit("missing FINAL ANSWER in log")
end = text.find("<userSuggestions>", start)
if end < 0:
    end = text.find("\n## Goal", start)
if end < 0:
    end = len(text)
answer = text[start:end].strip()
if len(answer) < 400:
    sys.exit("log final answer too short")
out_path.write_text(answer)
print(f"extracted final answer from log -> {out_path}")
PY
}

run_opencode_guarded() { # $1 = agent, $2 = prompt, $3 = log
  local agent="$1" prompt="$2" log="$3"
  (cd "$REPO" && timeout "$TIMEOUT" "$OPENCODE" run --agent "$agent" -m "$MODEL" "$prompt") \
    > "$log" 2>&1 &
  local pid=$!
  local last_size=0 idle_seconds=0
  while kill -0 "$pid" 2>/dev/null; do
    if [ -f "$log" ] && grep -q "Continue if you have next steps" "$log"; then
      if grep -q "FINAL ANSWER" "$log"; then
        echo "  final answer observed before checkpoint marker — stopping session" >&2
        pkill -TERM -P "$pid" 2>/dev/null || true
        kill "$pid" 2>/dev/null || true
        wait "$pid" 2>/dev/null || true
        return 0
      fi
      echo "  checkpoint marker observed — stopping session" >&2
      pkill -TERM -P "$pid" 2>/dev/null || true
      kill "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
      return 124
    fi
    local size=0
    [ -f "$log" ] && size="$(wc -c < "$log" | tr -d ' ')"
    if [ "$size" = "$last_size" ]; then
      idle_seconds=$((idle_seconds + 5))
      if [ "$idle_seconds" -ge "$IDLE_TIMEOUT" ]; then
        echo "  idle timeout observed (${IDLE_TIMEOUT}s without log growth) — stopping session" >&2
        pkill -TERM -P "$pid" 2>/dev/null || true
        kill "$pid" 2>/dev/null || true
        wait "$pid" 2>/dev/null || true
        return 124
      fi
    else
      last_size="$size"
      idle_seconds=0
    fi
    sleep 5
  done
  wait "$pid"
}

run_arm() { # $1 = agent, $2 = question idx (1-based), $3 = arm label
  local q="${QUESTIONS[$(($2 - 1))]}"
  local prompt="$q

Eval harness instruction: finish the task in this single run. Do not output a progress
summary, checkpoint, TODO list, or \"continue if you have next steps\" handoff. Do not use
progress-only headings like Goal, Constraints, Progress, Done, In Progress, Blocked,
Critical Context, or Next Steps. If some data is unavailable, state the limitation and
produce the best final answer now. You have a hard budget of 10 external tool calls
total; after that, stop gathering data and write the final answer. For broad sports scan
questions across multiple market categories, after loading the sports skill, delegate to
wayfinder-sports as your first non-skill action for the annotated board/eventStatePack.
Use at most 8 external tool calls before writing the answer. Reserve one call for current
sport state/results with a generous limit, prioritize group boards for groups with current
results, and one call for match-market mids when match boards surface. Treat three-way
match boards as home/draw/away, not binary yes/no markets. Your final answer must start
with \"FINAL ANSWER\"."
  local log="$OUT/q$2_$3.log" ans="$OUT/q$2_$3.md"
  echo "=== q$2 / $3 ($1) ==="
  if [ "$SKIP_EXISTING" = "1" ] && [ -s "$ans" ] \
    && ! grep -qx "(no answer harvested)" "$ans" \
    && ! grep -qx "(invalid checkpoint/handoff answer)" "$ans"; then
    echo "  existing answer present — skipping $ans"
    return
  fi
  local attempt
  for attempt in $(seq 1 "$ATTEMPTS"); do
    run_opencode_guarded "$1" "$prompt" "$log" && break
    echo "  (session exit nonzero on attempt $attempt — $( [ "$attempt" -lt "$ATTEMPTS" ] && echo retrying || echo continuing))"
    [ "$attempt" -lt "$ATTEMPTS" ] && sleep 30
  done
  # a failed harvest marks the arm and moves on — one bad session must not kill the battery
  harvest "$q" "$ans" || { echo "  HARVEST FAILED for q$2/$3 — marked missing" >&2; echo "(no answer harvested)" > "$ans"; }
  if ! validate_harvested_answer "$ans"; then
    echo "  DB harvest invalid for q$2/$3 — trying log FINAL ANSWER fallback" >&2
    if ! extract_final_from_log "$log" "$ans" || ! validate_harvested_answer "$ans"; then
      echo "  INVALID ANSWER for q$2/$3 — checkpoint/handoff or missing answer" >&2
      echo "(invalid checkpoint/handoff answer)" > "$ans"
    fi
  fi
  # contamination check for the baseline arm
  if [ "$3" = "baseline" ]; then
    if grep -qE "wayfinder_sports_|prop_slate|game_slate|futures_slate|sports_posterior" "$log"; then
      echo "  WARNING: baseline log mentions sports tooling — inspect $log" >&2
    fi
  fi
  sleep 20
}

should_run_question() { # $1 = question idx
  [ -z "$ONLY_INDEXES" ] && return 0
  case ",$ONLY_INDEXES," in
    *",$1,"*) return 0 ;;
    *) return 1 ;;
  esac
}

for i in $(seq 1 ${#QUESTIONS[@]}); do
  should_run_question "$i" || continue
  hide_sports
  run_arm wayfinder-baseline "$i" baseline
  restore_sports
  run_arm wayfinder "$i" new
done

echo "done — answers in $OUT (q*_baseline.md / q*_new.md); judge with scripts/eval_judge.sh <tag> <question> <ansA> <ansB> (grounded judge)"
