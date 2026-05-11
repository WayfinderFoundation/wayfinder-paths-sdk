from pathlib import Path

_SDK_ROOT = Path(__file__).resolve().parents[2]
_INSTRUCTION_DOCS = (
    _SDK_ROOT / "AGENTS.md",
    _SDK_ROOT / "CLAUDE.md",
)


def test_prediction_market_guidance_defaults_to_polymarket() -> None:
    for doc_path in _INSTRUCTION_DOCS:
        text = doc_path.read_text(encoding="utf-8")

        assert "prediction markets - Polymarket first" in text
        assert "start with **Polymarket**" in text
        assert "explicitly asks for Hyperliquid/HL/HIP-4" in text
        assert "Polymarket has no clear/liquid fit" in text
        assert "search both venues" not in text
