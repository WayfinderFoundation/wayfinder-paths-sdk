#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from importlib import metadata as importlib_metadata
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_MANIFEST_PATH = SKILL_ROOT / 'runtime' / 'manifest.json'
DEFAULT_RUNTIME_CONFIG_PATH = SKILL_ROOT / '.runtime' / 'config.json'


def _load_manifest() -> dict[str, object]:
    return json.loads(RUNTIME_MANIFEST_PATH.read_text(encoding='utf-8'))


def _normalized_passthrough(args: list[str]) -> list[str]:
    if args and args[0] == '--':
        return args[1:]
    return args


def _runtime_env(manifest: dict[str, object]) -> dict[str, str]:
    env = os.environ.copy()
    cfg_env = str(manifest.get('config_path_env') or 'WAYFINDER_CONFIG_PATH')
    if not env.get(cfg_env) and DEFAULT_RUNTIME_CONFIG_PATH.exists():
        env[cfg_env] = str(DEFAULT_RUNTIME_CONFIG_PATH)
    return env


def _config_has_api_key(path_value: str | None) -> bool:
    if not path_value:
        return False
    try:
        payload = json.loads(Path(path_value).expanduser().read_text(encoding='utf-8'))
    except Exception:
        return False
    if not isinstance(payload, dict):
        return False
    system = payload.get('system')
    if not isinstance(system, dict):
        return False
    return bool(str(system.get('api_key') or '').strip())


def _ensure_api_key(manifest: dict[str, object], env: dict[str, str]) -> None:
    if not bool(manifest.get('require_api_key')):
        return
    api_env = str(manifest.get('api_key_env') or 'WAYFINDER_API_KEY')
    cfg_env = str(manifest.get('config_path_env') or 'WAYFINDER_CONFIG_PATH')
    if env.get(api_env):
        return
    if _config_has_api_key(env.get(cfg_env)):
        return
    raise SystemExit(
        f'Missing API key. Set {api_env} or configure {cfg_env} before running this skill.'
    )


def _call_cli(command: list[str], env: dict[str, str]) -> int:
    return subprocess.call(command, env=env)


def _current_runtime_matches(manifest: dict[str, object]) -> bool:
    package = str(manifest.get('package') or 'wayfinder-paths')
    version = str(manifest.get('version') or '').strip()
    if not version:
        return False
    try:
        installed = importlib_metadata.version(package)
    except importlib_metadata.PackageNotFoundError:
        return False
    return installed == version


def _wayfinder_binary_matches(manifest: dict[str, object]) -> str | None:
    binary = shutil.which('wayfinder')
    version = str(manifest.get('version') or '').strip()
    if not binary or not version:
        return None
    try:
        proc = subprocess.run(
            [binary, 'path', 'version'],
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception:
        return None
    resolved = proc.stdout.strip()
    if resolved == version:
        return binary
    return None


def _wayfinder_exec_args(manifest: dict[str, object], args: list[str]) -> list[str]:
    path_dir = SKILL_ROOT / 'path'
    component = str(manifest.get('component') or 'main')
    return [
        'path',
        'exec',
        '--path-dir',
        str(path_dir),
        '--component',
        component,
        '--',
        *_normalized_passthrough(args),
    ]


def _run_with_existing_runtime(manifest: dict[str, object], env: dict[str, str]) -> int | None:
    if not bool(manifest.get('prefer_existing_runtime', True)):
        return None
    exec_args = _wayfinder_exec_args(manifest, [])
    if _current_runtime_matches(manifest):
        return None
    binary = _wayfinder_binary_matches(manifest)
    if binary:
        return _call_cli([binary, *_wayfinder_exec_args(manifest, sys.argv[2:])], env)
    return None


def _bootstrap_with_uv(manifest: dict[str, object], env: dict[str, str], args: list[str]) -> int:
    binary = shutil.which('uv')
    if not binary:
        raise FileNotFoundError('uv not found')
    package = str(manifest.get('package') or 'wayfinder-paths')
    version = str(manifest.get('version') or '').strip()
    spec = f'{package}=={version}' if version else package
    cmd = [binary, 'run', '--with', spec, 'wayfinder', *_wayfinder_exec_args(manifest, args)]
    return _call_cli(cmd, env)


def _bootstrap_with_pipx(manifest: dict[str, object], env: dict[str, str], args: list[str]) -> int:
    binary = shutil.which('pipx')
    if not binary:
        raise FileNotFoundError('pipx not found')
    package = str(manifest.get('package') or 'wayfinder-paths')
    version = str(manifest.get('version') or '').strip()
    spec = f'{package}=={version}' if version else package
    cmd = [binary, 'run', '--spec', spec, 'wayfinder', *_wayfinder_exec_args(manifest, args)]
    return _call_cli(cmd, env)


def _venv_python(venv_dir: Path) -> Path:
    if os.name == 'nt':
        return venv_dir / 'Scripts' / 'python.exe'
    return venv_dir / 'bin' / 'python'


def _venv_matches(python_bin: Path, manifest: dict[str, object]) -> bool:
    package = str(manifest.get('package') or 'wayfinder-paths')
    version = str(manifest.get('version') or '').strip()
    if not python_bin.exists() or not version:
        return False
    try:
        proc = subprocess.run(
            [
                str(python_bin),
                '-c',
                (
                    'from importlib import metadata as m; '
                    f'print(m.version({package!r}))'
                ),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception:
        return False
    return proc.stdout.strip() == version


def _bootstrap_with_local_venv(manifest: dict[str, object], env: dict[str, str], args: list[str]) -> int:
    runtime_dir = SKILL_ROOT / '.runtime'
    venv_dir = runtime_dir / 'venv'
    python_bin = _venv_python(venv_dir)
    if not _venv_matches(python_bin, manifest):
        runtime_dir.mkdir(parents=True, exist_ok=True)
        subprocess.check_call([sys.executable, '-m', 'venv', str(venv_dir)])
        python_bin = _venv_python(venv_dir)
        subprocess.check_call([str(python_bin), '-m', 'pip', 'install', '--upgrade', 'pip'])
        package = str(manifest.get('package') or 'wayfinder-paths')
        version = str(manifest.get('version') or '').strip()
        spec = f'{package}=={version}' if version else package
        subprocess.check_call([str(python_bin), '-m', 'pip', 'install', spec])
    cmd = [
        str(python_bin),
        '-m',
        'wayfinder_paths.mcp.cli',
        *_wayfinder_exec_args(manifest, args),
    ]
    return _call_cli(cmd, env)


def run(component: str | None, args: list[str]) -> int:
    manifest = _load_manifest()
    if component:
        manifest['component'] = component
    env = _runtime_env(manifest)
    _ensure_api_key(manifest, env)

    if _current_runtime_matches(manifest):
        cmd = [
            sys.executable,
            '-m',
            'wayfinder_paths.mcp.cli',
            *_wayfinder_exec_args(manifest, args),
        ]
        return _call_cli(cmd, env)

    binary = _wayfinder_binary_matches(manifest)
    if binary:
        return _call_cli([binary, *_wayfinder_exec_args(manifest, args)], env)

    bootstrap_order = [str(manifest.get('bootstrap') or 'uv')]
    fallback = str(manifest.get('fallback_bootstrap') or 'pipx')
    if fallback and fallback not in bootstrap_order:
        bootstrap_order.append(fallback)
    if 'venv' not in bootstrap_order:
        bootstrap_order.append('venv')

    errors: list[str] = []
    for method in bootstrap_order:
        try:
            if method == 'uv':
                return _bootstrap_with_uv(manifest, env, args)
            if method == 'pipx':
                return _bootstrap_with_pipx(manifest, env, args)
            if method == 'venv':
                return _bootstrap_with_local_venv(manifest, env, args)
        except FileNotFoundError as exc:
            errors.append(str(exc))
        except subprocess.CalledProcessError as exc:
            errors.append(f'{method} failed with exit code {exc.returncode}')

    raise SystemExit('Failed to bootstrap runtime: ' + '; '.join(errors))


def configure(api_key: str, config_path: str | None) -> int:
    path = Path(config_path).expanduser() if config_path else DEFAULT_RUNTIME_CONFIG_PATH
    payload: dict[str, object] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding='utf-8'))
            if isinstance(loaded, dict):
                payload = loaded
        except Exception:
            payload = {}
    system = payload.get('system') if isinstance(payload.get('system'), dict) else {}
    system['api_key'] = api_key
    payload['system'] = system
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'ok': True, 'result': {'config_path': str(path)}}))
    return 0


def main() -> int:
    argv = sys.argv[1:]
    command = 'run'
    if argv and argv[0] in {'run', 'configure'}:
        command = argv.pop(0)

    if command == 'configure':
        parser = argparse.ArgumentParser(description='Write a local Wayfinder config for this skill.')
        parser.add_argument('--api-key', required=True)
        parser.add_argument('--config-path', default=None)
        parsed = parser.parse_args(argv)
        return configure(parsed.api_key, parsed.config_path)

    parser = argparse.ArgumentParser(description='Run the exported Wayfinder skill.')
    parser.add_argument('--component', default=None)
    parser.add_argument('args', nargs=argparse.REMAINDER)
    parsed = parser.parse_args(argv)
    return run(parsed.component, parsed.args)


if __name__ == '__main__':
    raise SystemExit(main())
