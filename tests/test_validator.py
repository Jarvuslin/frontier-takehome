from safco_agent.agents.validator import Validator
from safco_agent.schema import Product


def _p(**kw):
    base = {"name": "X", "product_url": "https://x.test/p/1"}
    base.update(kw)
    return Product(**base)


def test_accepts_minimal() -> None:
    v = Validator()
    ok, _ = v.validate(_p())
    assert ok
    assert v.accepted == 1


def test_dedups_by_sku() -> None:
    v = Validator()
    a = _p(sku="ABC", product_url="https://x.test/p/1")
    b = _p(sku="abc", product_url="https://x.test/p/2")  # case-insensitive
    assert v.validate(a)[0]
    ok, reason = v.validate(b)
    assert not ok and reason == "duplicate"
    assert v.duplicates == 1


def test_dedups_by_url_when_no_sku() -> None:
    v = Validator()
    assert v.validate(_p(product_url="https://x.test/p/A"))[0]
    ok, reason = v.validate(_p(product_url="https://x.test/p/A"))
    assert not ok and reason == "duplicate"
