import re
from html.parser import HTMLParser

from fastapi.testclient import TestClient

from bucketeer.main import STATIC_DIR, create_app


def client(settings):
    return TestClient(create_app(settings))


def test_static_security_headers(settings):
    r = client(settings).get("/")
    csp = r.headers["content-security-policy"]
    for part in ("default-src 'self'", "frame-ancestors 'none'", "script-src 'self'"):
        assert part in csp
    assert "unsafe-inline" not in csp
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["cache-control"] == "no-cache"


def test_assets_and_fonts_cache(settings):
    c = client(settings)
    f = c.get("/fonts/inter.woff2")
    assert f.status_code == 200 and "max-age" in f.headers["cache-control"]
    assert c.get("/js/app.js").headers["cache-control"] == "no-cache"
    assert c.get("/showcase.html").status_code == 200


def test_api_and_health_behind_static_mount(settings):
    c = client(settings)
    r = c.get("/api/me")
    assert r.status_code == 401 and r.headers["cache-control"] == "no-store"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert c.post("/api/logout").status_code == 403  # CSRF guard still applies
    h = c.get("/health")
    assert h.status_code == 200 and h.json() == {"status": "ok"}
    assert "cache-control" not in h.headers


class _Scan(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls, self.problems = [], []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        for k in ("src", "href"):
            if a.get(k) and tag != "a":
                self.urls.append(a[k])
        if tag == "script" and "src" not in a:
            self.problems.append("inline <script>")
        if tag == "style":
            self.problems.append("inline <style>")
        if "style" in a:
            self.problems.append(f"style attribute on <{tag}>")
        for k in a:
            if k.startswith("on"):
                self.problems.append(f"{k} handler on <{tag}>")


def _scan(path):
    scan = _Scan()
    scan.feed(path.read_text())
    return scan


def _html_files():
    files = sorted(STATIC_DIR.rglob("*.html"))
    assert files
    return files


def test_html_has_no_inline_code_and_only_same_origin_assets():
    for path in _html_files():
        scan = _scan(path)
        assert not scan.problems, (path.name, scan.problems)
        for url in scan.urls:
            assert not re.match(r"^([a-z]+:)?//", url), (path.name, url)
            assert not url.startswith(("javascript:", "data:")), (path.name, url)


def test_referenced_assets_exist():
    for path in _html_files():
        for url in _scan(path).urls:
            assert (path.parent / url).is_file(), (path.name, url)


def test_example_url_exemption_is_narrow():
    pattern = r"https://(?:[\w*-]+\.)*example\.(?:org|com)(?![\w.-])"
    for ok in ("https://example.org", "https://www.example.com/x", "https://*.example.org"):
        assert re.fullmatch(pattern + r"(?:/\S*)?", ok), ok
    for bad in ("https://evil-example.org/x.js", "https://example.com.evil.net/x.js",
                "https://notexample.org/a"):
        assert re.search(r"https?://", re.sub(pattern, "", bad)), bad


def test_js_and_css_have_no_external_urls_or_innerhtml():
    for path in [*STATIC_DIR.rglob("*.js"), *STATIC_DIR.rglob("*.css")]:
        text = path.read_text().replace("http://www.w3.org/2000/svg", "")  # SVG namespace
        text = text.replace("https://s3.example.org", "")  # showcase demo data
        # example URLs: example.org / example.com and their subdomains only
        text = re.sub(r"https://(?:[\w*-]+\.)*example\.(?:org|com)(?![\w.-])", "", text)
        assert not re.search(r"https?://", text), path
        for bad in ("innerHTML", "insertAdjacentHTML", "outerHTML =", "document.write"):
            assert bad not in text, (path.name, bad)


def test_dom_h_assigns_custom_element_props_as_properties():
    # h() must not turn string props on custom elements into attributes (secrets).
    src = (STATIC_DIR / "js" / "dom.js").read_text()
    assert 'el.localName.includes("-")' in src and "el[k] = v" in src
