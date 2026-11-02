#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from importlib import metadata as importlib_metadata
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_MANIFEST_PATH = SKILL_ROOT / 'runtime' / 'manifest.json'
DEFAULT_RUNTIME_CONFIG_PATH = SKILL_ROOT / '.runtime' / 'config.json'
RUNTIME_FLOORS = {'wayfinder-paths': '0.11.1'}


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
    base_runs = str(env.get('WAYFINDER_RUNS_DIR') or '').strip()
    slug = str(manifest.get('slug') or '').strip()
    if base_runs and slug:
        if Path(slug).name != slug or slug in {'.', '..'}:
            raise SystemExit(f'Invalid path slug in runtime manifest: {slug}')
        root = Path(base_runs).expanduser()
        if not root.is_absolute():
            root = (Path.cwd() / root).resolve()
        path_runs = root / 'paths' / slug
        path_runs.mkdir(parents=True, exist_ok=True)
        env['WAYFINDER_RUNS_DIR'] = str(path_runs)
        local_runs = SKILL_ROOT / 'path' / '.wayfinder_runs'
        if not local_runs.exists() and not local_runs.is_symlink():
            try:
                local_runs.symlink_to(path_runs, target_is_directory=True)
            except OSError as exc:
                print(f'Warning: could not link path state directory: {exc}', file=sys.stderr)
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


def _runtime_version_is_compatible(installed: str, required: str) -> bool:
    if installed == required:
        return True
    try:
        installed_release = tuple(int(part) for part in installed.split('.'))
        required_release = tuple(int(part) for part in required.split('.'))
    except ValueError:
        return False
    if len(installed_release) != 3 or len(required_release) != 3:
        return False
    return (
        installed_release[:2] == required_release[:2]
        and installed_release[2] >= required_release[2]
    )


def _python_version_is_compatible(spec: str) -> bool:
    if not spec:
        return True
    current = sys.version_info[:3]
    try:
        from packaging.specifiers import SpecifierSet
        from packaging.version import Version
        return Version('.'.join(str(p) for p in current)) in SpecifierSet(spec)
    except ImportError:
        pass
    except ValueError:
        return False
    # Standalone hosts may not have packaging until the runtime is installed.
    # Support numeric Python releases here; reject unsupported syntax safely.
    for clause in spec.split(','):
        clause = clause.strip()
        if not clause:
            continue
        match = re.fullmatch(r'(>=|<=|==|!=|~=|>|<)\s*([0-9]+(?:\.[0-9]+){0,2})(\.\*)?', clause)
        if not match:
            return False
        op, raw, wildcard = match.groups()
        wanted = tuple(int(p) for p in raw.split('.'))
        padded = wanted + (0,) * (3 - len(wanted))
        if wildcard:
            if op not in {'==', '!='}:
                return False
            matches = current[:len(wanted)] == wanted
            ok = matches if op == '==' else not matches
        elif op == '~=':
            if len(wanted) < 2:
                return False
            ok = current >= padded and current[:len(wanted) - 1] == wanted[:-1]
        else:
            ok = {
                '>=': current >= padded, '<=': current <= padded,
                '>': current > padded, '<': current < padded,
                '==': current == padded, '!=': current != padded,
            }[op]
        if not ok:
            return False
    return True


def _runtime_requirement(manifest: dict[str, object]) -> tuple[str, str]:
    package = str(manifest.get('package') or 'wayfinder-paths')
    version = str(manifest.get('version') or '').strip()
    floor = RUNTIME_FLOORS.get(package)
    try:
        below_floor = bool(floor) and tuple(int(p) for p in version.split('.')) < tuple(
            int(p) for p in floor.split('.')
        )
    except ValueError:
        below_floor = False
    # Pins older than the floor can't bootstrap a working runtime from PyPI.
    return package, floor if below_floor else version


def _runtime_package_spec(manifest: dict[str, object]) -> str:
    package, version = _runtime_requirement(manifest)
    if not version:
        return package
    try:
        release = tuple(int(part) for part in version.split('.'))
    except ValueError:
        return f'{package}=={version}'
    operator = '~=' if len(release) == 3 else '=='
    return f'{package}{operator}{version}'


def _current_runtime_is_compatible(manifest: dict[str, object]) -> bool:
    package, required = _runtime_requirement(manifest)
    python = str(manifest.get('python') or '').strip()
    if not required or not _python_version_is_compatible(python):
        return False
    try:
        installed = importlib_metadata.version(package)
    except importlib_metadata.PackageNotFoundError:
        return False
    return _runtime_version_is_compatible(installed, required)


def _compatible_wayfinder_binary(manifest: dict[str, object]) -> str | None:
    binary = shutil.which('wayfinder')
    _, required = _runtime_requirement(manifest)
    if not binary or not required:
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
    if _runtime_version_is_compatible(resolved, required):
        return binary
    return None


def _wayfinder_exec_args(manifest: dict[str, object], args: list[str]) -> list[str]:
    path_dir = SKILL_ROOT / 'path'
    component = str(manifest.get('component') or 'main')
    command = [
        'path',
        'exec',
        '--path-dir',
        str(path_dir),
        '--component',
        component,
    ]
    passthrough = _normalized_passthrough(args)
    if passthrough:
        command.extend(['--args-json', json.dumps(passthrough)])
    return command


def _bootstrap_with_uv(manifest: dict[str, object], env: dict[str, str], args: list[str]) -> int:
    binary = shutil.which('uv')
    if not binary:
        raise FileNotFoundError('uv not found')
    spec = _runtime_package_spec(manifest)
    python = str(manifest.get('python') or '').strip()
    cmd = [binary, 'run', '--isolated', '--no-project']
    if python:
        cmd.extend(['--python', python])
    cmd.extend(['--with', spec, 'wayfinder', *_wayfinder_exec_args(manifest, args)])
    return _call_cli(cmd, env)


def _bootstrap_with_pipx(manifest: dict[str, object], env: dict[str, str], args: list[str]) -> int:
    binary = shutil.which('pipx')
    if not binary:
        raise FileNotFoundError('pipx not found')
    python = str(manifest.get('python') or '').strip()
    if not _python_version_is_compatible(python):
        raise RuntimeError(f'Current Python does not satisfy {python}')
    spec = _runtime_package_spec(manifest)
    cmd = [binary, 'run', '--python', sys.executable, '--spec', spec, 'wayfinder', *_wayfinder_exec_args(manifest, args)]
    return _call_cli(cmd, env)


def _venv_python(venv_dir: Path) -> Path:
    if os.name == 'nt':
        return venv_dir / 'Scripts' / 'python.exe'
    return venv_dir / 'bin' / 'python'


def _venv_matches(python_bin: Path, manifest: dict[str, object]) -> bool:
    package, required = _runtime_requirement(manifest)
    if not python_bin.exists() or not required:
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
    return _runtime_version_is_compatible(proc.stdout.strip(), required)


def _bootstrap_with_local_venv(manifest: dict[str, object], env: dict[str, str], args: list[str]) -> int:
    python = str(manifest.get('python') or '').strip()
    if not _python_version_is_compatible(python):
        raise RuntimeError(f'Current Python does not satisfy {python}')
    runtime_dir = SKILL_ROOT / '.runtime'
    venv_dir = runtime_dir / 'venv'
    python_bin = _venv_python(venv_dir)
    if not _venv_matches(python_bin, manifest):
        runtime_dir.mkdir(parents=True, exist_ok=True)
        subprocess.check_call([sys.executable, '-m', 'venv', str(venv_dir)])
        python_bin = _venv_python(venv_dir)
        subprocess.check_call([str(python_bin), '-m', 'pip', 'install', '--upgrade', 'pip'])
        spec = _runtime_package_spec(manifest)
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

    if _current_runtime_is_compatible(manifest):
        cmd = [
            sys.executable,
            '-m',
            'wayfinder_paths.mcp.cli',
            *_wayfinder_exec_args(manifest, args),
        ]
        return _call_cli(cmd, env)

    binary = _compatible_wayfinder_binary(manifest)
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
        except RuntimeError as exc:
            errors.append(str(exc))

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
