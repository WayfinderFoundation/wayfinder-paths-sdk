from wayfinder_paths.core.clients.BRAPClient import normalize_brap_quote_response


def test_normalize_brap_quote_response_accepts_current_shape():
    payload = {
        "quotes": [{"provider": "lifi"}],
        "best_quote": {"provider": "lifi", "output_amount": "100"},
        "errors": [{"provider": "enso", "error": "no route"}],
    }

    normalized = normalize_brap_quote_response(payload)

    assert normalized["quotes"] == [{"provider": "lifi"}]
    assert normalized["best_quote"] == {"provider": "lifi", "output_amount": "100"}
    assert normalized["quote_count"] == 1
    assert normalized["errors"] == [{"provider": "enso", "error": "no route"}]


def test_normalize_brap_quote_response_accepts_legacy_nested_shape():
    payload = {
        "quotes": {
            "quote_count": 3,
            "all_quotes": [{"provider": "lifi"}, {"provider": "enso"}],
            "best_quote": {"provider": "enso", "output_amount": "200"},
        }
    }

    normalized = normalize_brap_quote_response(payload)

    assert normalized["quotes"] == [{"provider": "lifi"}, {"provider": "enso"}]
    assert normalized["best_quote"] == {"provider": "enso", "output_amount": "200"}
    assert normalized["quote_count"] == 3


def test_normalize_brap_quote_response_accepts_data_envelope():
    payload = {
        "data": {
            "quotes": [{"provider": "lifi"}],
            "best_quote": {"provider": "lifi"},
        }
    }

    normalized = normalize_brap_quote_response(payload)

    assert normalized["quotes"] == [{"provider": "lifi"}]
    assert normalized["best_quote"] == {"provider": "lifi"}


def test_normalize_brap_quote_response_accepts_legacy_alias():
    payload = {
        "legacy_quote_response": {
            "quotes": {
                "quote_count": 1,
                "all_quotes": [{"provider": "debridge"}],
                "best_quote": {"provider": "debridge"},
            }
        }
    }

    normalized = normalize_brap_quote_response(payload)

    assert normalized["quotes"] == [{"provider": "debridge"}]
    assert normalized["best_quote"] == {"provider": "debridge"}
    assert normalized["quote_count"] == 1
