import pytest
from resources.lib.epgstation.api import normalize_url


@pytest.mark.parametrize("typed, url", [
    ("", ""),
    ("   ", ""),
    ("epgstation.lan:8888", "http://epgstation.lan:8888"),
    (" http://epgs.lan:8888/ ", "http://epgs.lan:8888"),
    ("https://epgs.example/sub/", "https://epgs.example/sub"),
])
def test_normalize_url(typed, url):
    assert normalize_url(typed) == url
