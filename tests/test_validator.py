from safco_agent.agents.validator import Validator
from safco_agent.schema import Product, Variant


def _p(**kw):
    base = {"name": "X", "product_url": "https://x.test/p/1"}
    base.update(kw)
    return Product(**base)


def _v(**kw):
    base = {"parent_dedup_key": "sku:abc", "safco_item_number": "1001", "name": "X"}
    base.update(kw)
    return Variant(**base)


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


def test_variant_dedup_by_parent_plus_item_number() -> None:
    v = Validator()
    a = _v(parent_dedup_key="sku:drcdk", safco_item_number="4681214")
    b = _v(parent_dedup_key="sku:drcdk", safco_item_number="4681214")  # same combo
    c = _v(parent_dedup_key="sku:drcdk", safco_item_number="4681216")  # new item#
    d = _v(parent_dedup_key="sku:other", safco_item_number="4681214")  # new parent
    assert v.validate_variant(a)[0]
    ok, reason = v.validate_variant(b)
    assert not ok and reason == "duplicate"
    assert v.validate_variant(c)[0]  # different item# under same parent
    assert v.validate_variant(d)[0]  # same item# under different parent
    assert v.variants_accepted == 3
    assert v.variants_duplicates == 1


def test_variant_rejected_when_no_id_and_no_name() -> None:
    v = Validator()
    bare = _v(safco_item_number=None, name=None)
    ok, reason = v.validate_variant(bare)
    assert not ok and reason == "missing_item_number_and_name"
    assert v.variants_rejected == 1
