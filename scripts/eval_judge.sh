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
# The judge defaults to GPT-5.5 (high reasoning) — a DIFFERENT model from the arms to
# avoid self-preference bias, and stronger for grounded scoring. Model names are not
# secrets, so defaulting here is safe. Override with JUDGE_MODEL=...
JUDGE_MODEL="${JUDGE_MODEL:-openai/gpt-5.5}"
TIMEOUT="${JUDGE_TIMEOUT:-900}"

# Resolve OpenAI creds from the wayfinder system config (`system.openai.*`, env fallback)
# into the environment so opencode's OpenAI provider can authenticate. Single source of
# truth in the config; the key never touches a tracked file or stdout.
if [[ "$JUDGE_MODEL" == openai/* ]]; then
  eval "$(cd "$REPO" && poetry run python - <<'PY'
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
  [ -n "${OPENAI_API_KEY:-}" ] || { echo "no OpenAI creds in system.openai.* or env" >&2; exit 1; }
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
for attempt in 1 2; do
  (cd "$REPO" && timeout "$TIMEOUT" "$OPENCODE" run --agent wayfinder-eval-judge \
    -m "$JUDGE_MODEL" "$(cat "$PROMPT")") > "$LOG" 2>&1 && break
  echo "judge $TAG attempt $attempt failed — $( [ "$attempt" = 1 ] && echo retrying || echo giving up )" >&2
  [ "$attempt" = 1 ] && sleep 30
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
