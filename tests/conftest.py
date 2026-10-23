import pytest

from wecom_excel_mcp import decoder


@pytest.fixture(autouse=True, params=["native", "python"])
def decoder_impl(request, monkeypatch):
    """Run every test against both decoders: the Rust one must match the Python reference."""
    if request.param == "native" and decoder._native is None:
        pytest.skip("Rust extension not built")
    monkeypatch.setattr(decoder, "USE_NATIVE", request.param == "native")
    return request.param


@pytest.fixture(autouse=True)
def fresh_rate_limits():
    from wecom_excel_mcp import server

    server._buckets.clear()
