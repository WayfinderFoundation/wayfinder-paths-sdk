#!/usr/bin/env bash
# Grounded eval judge runner: builds a blind judge prompt (rubric + question + two
# anonymous answers), runs the wayfinder-eval-judge agent (which researches the live
# markets first — Polymarket, Hyperliquid, sports snapshot — then scores), and extracts
# the verdict JSON to .wayfinder_runs/evals/judge_<tag>.json.
#
# Usage:
#   scripts/eval_judge.sh <tag> <question_text> <answerA_file> <answerB_file>
# Env: WAYFINDER_API_KEY + WAYFINDER_CONFIG_PATH must be exported.
set -euo pipefail

TAG="$1"; QUESTION="$2"; ANS_A="$3"; ANS_B="$4"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$REPO/.wayfinder_runs/evals"
DB="$HOME/.local/share/opencode/opencode.db"
OPENCODE="${OPENCODE_BIN:-$HOME/.opencode/bin/opencode}"
# The judge prefers a stronger, DIFFERENT model than the arms (avoids self-preference
# bias). Default is GPT-5.5. Fallback is opt-in only for local/debug runs so evals do not
# silently change judge quality.
JUDGE_MODEL="${JUDGE_MODEL:-openai/gpt-5.5}"
JUDGE_FALLBACK_MODEL="${JUDGE_FALLBACK_MODEL:-wayfinder/deepseek-v4-pro}"
JUDGE_ALLOW_FALLBACK="${JUDGE_ALLOW_FALLBACK:-0}"
JUDGE_ATTEMPTS="${JUDGE_ATTEMPTS:-1}"
TIMEOUT="${JUDGE_TIMEOUT:-}"

# For an openai/* judge, resolve credentials from the wayfinder system config
# (system.openai.*, env fallback) into the environment so opencode's OpenAI provider can
# authenticate — single source of truth in the config, the key never touches a tracked
# file or stdout. If no credentials are available, fail unless fallback is explicit.
if [[ "$JUDGE_MODEL" == openai/* ]]; then
  eval "$(cd "$REPO" && poetry run python - <<'PY' 2>/dev/null || true
from wayfinder_paths.core.config import load_config, get_openai_credentials
import shlex
load_config()
c = get_openai_credentials()
if c["api_key"]:
    print(f"export OPENAI_API_KEY={shlex.quote(c['api_key'])}")
if c["organization"]:
    print(f"export OPENAI_ORGANIZATION={shlex.quote(c['organization'])}")
PY
)"
  if [ -z "${OPENAI_API_KEY:-}" ]; then
    if [ "$JUDGE_ALLOW_FALLBACK" = "1" ]; then
      echo "WARN: $JUDGE_MODEL needs OpenAI credentials (system.openai.* or OPENAI_API_KEY)" \
           "— none found; falling back to $JUDGE_FALLBACK_MODEL because" \
           "JUDGE_ALLOW_FALLBACK=1." >&2
      JUDGE_MODEL="$JUDGE_FALLBACK_MODEL"
    else
      echo "ERROR: $JUDGE_MODEL needs OpenAI credentials (system.openai.* or OPENAI_API_KEY)." \
           "Set JUDGE_ALLOW_FALLBACK=1 only for local/debug fallback." >&2
      exit 1
    fi
  fi
fi

mkdir -p "$OUT"
PROMPT="$OUT/judge_prompt_$TAG.md"
{
  echo "You are a GROUNDED blind judge: first research the live markets per your PHASE 1"
  echo "instructions, then score ONLY the two answer texts below against the rubric +"
  echo "your observations, output the strict JSON, and stop."
  echo
  cat "$REPO/scripts/eval_sports_ab_judge.md"
  echo; echo "---"; echo; echo "THE QUESTION:"; echo "$QUESTION"
  echo; echo "---"; echo; echo "ANSWER A:"; echo; cat "$ANS_A"
  echo; echo "---"; echo; echo "ANSWER B:"; echo; cat "$ANS_B"
} > "$PROMPT"

LOG="$OUT/judge_$TAG.log"
run_judge() {
  if [ -n "$TIMEOUT" ]; then
    (cd "$REPO" && timeout "$TIMEOUT" "$OPENCODE" run --agent wayfinder-eval-judge \
      -m "$JUDGE_MODEL" "$(cat "$PROMPT")")
  else
    (cd "$REPO" && "$OPENCODE" run --agent wayfinder-eval-judge \
      -m "$JUDGE_MODEL" "$(cat "$PROMPT")")
  fi
}
for attempt in $(seq 1 "$JUDGE_ATTEMPTS"); do
  run_judge > "$LOG" 2>&1 && break
  echo "judge $TAG attempt $attempt failed — $( [ "$attempt" -lt "$JUDGE_ATTEMPTS" ] && echo retrying || echo giving up )" >&2
  [ "$attempt" -lt "$JUDGE_ATTEMPTS" ] && sleep 30
done

python3 - "$TAG" "$LOG" "$DB" "$OUT" <<'PY'
import json, re, sqlite3, sys, pathlib
tag, log_path, db, out = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]

def find_json(text):
    # last JSON object containing "verdict"
    candidates = re.findall(r"\{(?:[^{}]|\{(?:[^{}]|\{[^{}]*\})*\})*\}", text, re.S)
    for blob in reversed(candidates):
        if '"verdict"' in blob:
            try:
                return json.loads(blob)
            except ValueError:
                continue
    return None

verdict = find_json(pathlib.Path(log_path).read_text(errors="replace"))
if verdict is None:  # stdout truncation: pull the newest verdict text from the session DB
    con = sqlite3.connect(db)
    rows = con.execute(
        """SELECT json_extract(p.data,'$.text') FROM part p JOIN message m ON p.message_id=m.id
           WHERE json_extract(p.data,'$.type')='text' AND json_extract(p.data,'$.text') LIKE '%\"verdict\"%'
           ORDER BY m.time_created DESC LIMIT 3"""
    ).fetchall()
    for (text,) in rows:
        verdict = find_json(text or "")
        if verdict:
            break
if verdict is None:
    sys.exit(f"no verdict JSON found for {tag}")
path = pathlib.Path(out) / f"judge_{tag}.json"
path.write_text(json.dumps(verdict, indent=2))
s = verdict.get("scores", {})
print(f"{tag}: verdict={verdict.get('verdict')} ({verdict.get('margin')}) "
      f"A={s.get('A',{}).get('total')} B={s.get('B',{}).get('total')} -> {path}")
PY
