from __future__ import annotations

from unittest.mock import patch

from wayfinder_paths.core.utils.solidity import collect_sources


def test_collect_sources_resolves_oz_relative_imports():
    source = """
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";

contract Foo {}
"""

    deps = {
        "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol": """
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import "../IERC20.sol";

library SafeERC20 {}
""",
        "@openzeppelin/contracts/token/ERC20/IERC20.sol": """
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

interface IERC20 {}
""",
    }

    def _mock_load_dependency_source(*, node_modules, key: str) -> str:
        return deps[key]

    with patch(
        "wayfinder_paths.core.utils.solidity.ensure_oz_installed",
        return_value="/tmp/node_modules",
    ), patch(
        "wayfinder_paths.core.utils.solidity._load_dependency_source",
        side_effect=_mock_load_dependency_source,
    ):
        sources = collect_sources(source)

    assert "Contract.sol" in sources
    assert "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol" in sources
    assert "@openzeppelin/contracts/token/ERC20/IERC20.sol" in sources
