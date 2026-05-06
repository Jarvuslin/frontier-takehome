from safco_agent.agents.classifier import classify, classify_url


def test_classify_url_product() -> None:
    assert classify_url("https://www.safcodental.com/product/aquasoft") == "product"


def test_classify_url_category() -> None:
    assert classify_url("https://www.safcodental.com/catalog/gloves") == "category"


def test_classify_url_subcategory() -> None:
    assert (
        classify_url("https://www.safcodental.com/catalog/gloves/nitrile-gloves")
        == "subcategory"
    )


def test_classify_url_unknown() -> None:
    assert classify_url("https://www.safcodental.com/about") == "unknown"


def test_classify_dom_fallback() -> None:
    html = '<html><body><div class="product-card"></div></body></html>'
    # URL is unknown but DOM hints subcategory
    assert classify("https://www.safcodental.com/something", html) == "subcategory"
