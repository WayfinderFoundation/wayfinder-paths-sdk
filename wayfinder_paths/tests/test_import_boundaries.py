from __future__ import annotations

import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    "imports",
    [
        (
            "from wayfinder_paths.core.utils.transaction import send_transaction; "
            "from wayfinder_paths.core.clients.TokenClient import TokenClient"
        ),
        (
            "from wayfinder_paths.core.clients.TokenClient import TokenClient; "
            "from wayfinder_paths.core.utils.transaction import send_transaction"
        ),
    ],
)
def test_transaction_and_token_client_import_in_either_order(imports: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", imports],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
