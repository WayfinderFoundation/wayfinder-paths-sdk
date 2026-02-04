#!/usr/bin/env python3

import argparse
import re
import shutil
from pathlib import Path


def sanitize_name(name: str) -> str:
    name = re.sub(r"[^a-zA-Z0-9_-]", "_", name)
    name = re.sub(r"_+", "_", name)
    return name.strip("_").lower()


ADAPTER_PY = '''from typing import Any

from wayfinder_paths.core.adapters.BaseAdapter import BaseAdapter


class {class_name}(BaseAdapter):
    adapter_type: str = "{adapter_type}"

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__("{dir_name}", config)
'''

MANIFEST_YAML = '''schema_version: "0.1"
entrypoint: "wayfinder_paths.adapters.{dir_name}.adapter.{class_name}"
capabilities: []
dependencies: []
'''

TEST_PY = '''import pytest

from wayfinder_paths.adapters.{dir_name}.adapter import {class_name}


class Test{class_name}:
    @pytest.fixture
    def adapter(self):
        return {class_name}()

    def test_init(self, adapter):
        assert adapter.adapter_type == "{adapter_type}"
        assert adapter.name == "{dir_name}"
'''

README_MD = '''# {class_name}

TODO: Brief description of what this adapter does.

- **Type**: `{adapter_type}`
- **Module**: `wayfinder_paths.adapters.{dir_name}.adapter.{class_name}`

## Overview

TODO: Describe the adapter's purpose and capabilities.

## Usage

```python
from wayfinder_paths.adapters.{dir_name}.adapter import {class_name}

adapter = {class_name}()
```

## Methods

TODO: Document available methods.

## Dependencies

TODO: List any clients or external dependencies.

## Testing

```bash
poetry run pytest wayfinder_paths/adapters/{dir_name}/ -v
```
'''


def main():
    parser = argparse.ArgumentParser(description="Create a new adapter")
    parser.add_argument("name", help="Adapter name (e.g., 'my_protocol')")
    parser.add_argument("--adapters-dir", type=Path,
                        default=Path(__file__).parent.parent / "wayfinder_paths" / "adapters")
    parser.add_argument("--override", action="store_true")
    args = parser.parse_args()

    dir_name = sanitize_name(args.name)
    if not dir_name.endswith("_adapter"):
        dir_name += "_adapter"

    class_name = "".join(word.capitalize() for word in dir_name.split("_"))
    adapter_type = dir_name.replace("_adapter", "").upper()

    adapter_dir = args.adapters_dir / dir_name
    if adapter_dir.exists() and not args.override:
        raise SystemExit(f"Adapter exists: {adapter_dir}\nUse --override to replace")
    if adapter_dir.exists():
        shutil.rmtree(adapter_dir)
    adapter_dir.mkdir(parents=True, exist_ok=True)

    fmt = {"class_name": class_name, "dir_name": dir_name, "adapter_type": adapter_type}
    (adapter_dir / "adapter.py").write_text(ADAPTER_PY.format(**fmt))
    (adapter_dir / "manifest.yaml").write_text(MANIFEST_YAML.format(**fmt))
    (adapter_dir / "test_adapter.py").write_text(TEST_PY.format(**fmt))
    (adapter_dir / "examples.json").write_text("{}\n")
    (adapter_dir / "README.md").write_text(README_MD.format(**fmt))

    print(f"Created {adapter_dir}")
    print(f"  Class: {class_name}")
    print(f"  Type: {adapter_type}")


if __name__ == "__main__":
    main()
