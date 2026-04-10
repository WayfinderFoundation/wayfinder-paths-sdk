#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit('usage: validate_artifact.py <agent-id> <path>')
    agent_id, path_value = sys.argv[1], sys.argv[2]
    artifact_path = Path(path_value)
    if not artifact_path.exists():
        raise SystemExit(f'missing artifact for {agent_id}: {artifact_path}')
    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit('artifact payload must be a JSON object')
    print(json.dumps({'ok': True, 'agent_id': agent_id, 'path': str(artifact_path)}))
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
