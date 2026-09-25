"""End-to-end tests for collect.py against local mock sites.

Run with:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import csv
import http.server
import json
import os
import shutil
import struct
import sys
import tempfile
import threading
import unittest
import zlib
from functools import partial
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import collect  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "elementor-site"
PROXY_VARS = ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]

EXISTING_IMAGES = [
    "2024/01/og.jpg", "2024/01/cropped-icon.png", "2024/01/logo.png", "2024/01/logo-300x100.png",
    "2024/01/logo-600x200.png", "2024/01/hero-1.jpg", "2024/01/hero-2.jpg", "2024/01/about-bg.jpg",
    "2024/01/team.jpg", "2024/01/team-300x200.jpg", "2024/01/team-1024x683.jpg", "2024/01/lazy.jpg",
    "2024/01/noscript.jpg", "2024/01/only-resized-768x512.jpg", "2024/01/gallery-full.jpg",
    "2024/01/gallery-full-150x150.jpg", "2024/01/services-bg.jpg", "2024/01/services-bg-mobile.jpg",
    "2024/01/header-texture.png", "2024/01/cta-bg.jpg", "2024/01/footer-bg.png", "2024/01/popup.jpg",
    "2024/02/team.jpg",
]

MAINTENANCE_HTML = b"""<!DOCTYPE html><html><head><title>Coming Soon</title></head>
<body class="cmp-coming-soon-page"><h1>We're launching soon!</h1></body></html>"""

PASSWORD_HTML = b"""<!DOCTYPE html><html><head><title>Protected: Home</title>
<link rel="stylesheet" href="/wp-content/themes/x/style.css"></head><body>
<form action="/wp-login.php?action=postpass" class="post-password-form" method="post">
<p>This content is password protected.</p><input name="post_password" type="password"></form></body></html>"""

JS_APP_HTML = b"""<!DOCTYPE html><html><head><title>JS site</title></head><body><div id="root"></div>
<script>
document.getElementById('root').innerHTML =
  '<section class="elementor-section elementor-top-section elementor-element elementor-element-js00001" '
  + 'data-id="js00001" data-element_type="section" id="about"><h2>Rendered heading</h2><p>'
  + 'Lots of rendered text. '.repeat(30) + '</p><div id="tall" style="height:3000px"></div>'
  + '<img id="late" alt=""></section>';
document.getElementById('tall').style.backgroundImage = 'url(/wp-content/uploads/js-bg.png)';
window.addEventListener('scroll', function () {
  if (window.scrollY > 1200) { document.getElementById('late').src = '/wp-content/uploads/late.png'; }
});
</script></body></html>"""


def png_bytes(seed: int) -> bytes:
    """A tiny valid PNG (the colour varies so files differ)."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    raw = b"\x00" + bytes([seed % 256, 80, 160])
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class Handler(http.server.SimpleHTTPRequestHandler):
    mode = "static"
    redirect_to = ""

    def log_message(self, *args):  # keep test output quiet
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8", headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.mode == "maintenance":
            return self._send(503, MAINTENANCE_HTML)
        if self.mode == "password":
            return self._send(200, PASSWORD_HTML)
        if self.mode == "redirect":
            return self._send(301, b"", headers={"Location": self.redirect_to})
        if self.mode == "jsapp":
            if self.path == "/":
                return self._send(200, JS_APP_HTML)
            if self.path.endswith(".png"):
                return self._send(200, png_bytes(len(self.path)), "image/png")
            return self._send(404, b"not found")
        if self.path.endswith("/fake.jpg"):  # soft 404: HTML with a 200
            return self._send(200, b"<!DOCTYPE html><html><body>Page not found</body></html>")
        return super().do_GET()


def start_server(mode: str, directory: str | None = None, redirect_to: str = ""):
    handler = type(f"{mode}Handler", (Handler,), {"mode": mode, "redirect_to": redirect_to})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), partial(handler, directory=directory))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class CollectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.saved_env = {k: os.environ.pop(k) for k in PROXY_VARS if k in os.environ}
        cls.tmp = Path(tempfile.mkdtemp(prefix="collect-test-"))
        webroot = cls.tmp / "webroot"
        shutil.copytree(FIXTURE, webroot)

        cls.servers = [start_server("static", str(webroot))]
        port = cls.servers[0].server_address[1]
        cls.main_domain = f"127.0.0.1:{port}"
        origin = f"http://{cls.main_domain}"
        for path in [webroot / "index.html", *webroot.rglob("*.css")]:
            text = path.read_text().replace("{{ORIGIN_JSON}}", origin.replace("/", "\\/")).replace("{{ORIGIN}}", origin)
            path.write_text(text)
        cls.served_index = (webroot / "index.html").read_bytes()
        for i, rel in enumerate(EXISTING_IMAGES):
            dest = webroot / "wp-content" / "uploads" / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(png_bytes(i))
        (webroot / "wp-content/uploads/2024/01/icon.svg").write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"><rect width="1" height="1"/></svg>')

        maint = start_server("maintenance")
        password = start_server("password")
        jsapp = start_server("jsapp")
        redirect = start_server("redirect", redirect_to=f"{origin}/")
        cls.servers += [maint, password, jsapp, redirect]
        cls.maint_domain = f"127.0.0.1:{maint.server_address[1]}"
        cls.password_domain = f"127.0.0.1:{password.server_address[1]}"
        cls.js_domain = f"127.0.0.1:{jsapp.server_address[1]}"
        cls.redirect_domain = f"localhost:{redirect.server_address[1]}"
        cls.dns_domain = "no-such-site.invalid"

        cls.sites = cls.tmp / "sites.txt"
        cls.sites.write_text("\n".join([
            "# test sites", "", cls.main_domain, cls.maint_domain, cls.password_domain,
            cls.redirect_domain, cls.dns_domain, cls.js_domain,
        ]) + "\n")
        cls.out = cls.tmp / "sites"
        cls.report = cls.tmp / "report.csv"
        cls.run_collect()
        cls.m = cls.manifest(cls.main_domain)

    @classmethod
    def tearDownClass(cls):
        for s in cls.servers:
            s.shutdown()
            s.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)
        os.environ.update(cls.saved_env)

    @classmethod
    def run_collect(cls, *extra):
        argv = ["--sites", str(cls.sites), "--out", str(cls.out), "--report", str(cls.report),
                "--scheme", "http", "--min-delay", "0", "--max-delay", "0", *extra]
        return collect.main(argv)

    @classmethod
    def manifest(cls, domain):
        return json.loads((cls.out / collect.dir_name(domain) / "manifest.json").read_text())

    def report_rows(self):
        with open(self.report, newline="") as fh:
            return {r["domain"]: r for r in csv.DictReader(fh)}

    def image(self, name):
        return next(i for i in self.m["images"] if i["source_url"].endswith("/" + name))

    # ---- report / page states

    def test_report_has_every_domain_with_expected_status(self):
        rows = self.report_rows()
        self.assertEqual(list(rows), [self.main_domain, self.maint_domain, self.password_domain,
                                      self.redirect_domain, self.dns_domain, self.js_domain])
        self.assertEqual(rows[self.main_domain]["status"], "ok")
        self.assertEqual(rows[self.maint_domain]["status"], "maintenance")
        self.assertEqual(rows[self.password_domain]["status"], "password_protected")
        self.assertEqual(rows[self.redirect_domain]["status"], "redirect_offsite")
        self.assertEqual(rows[self.dns_domain]["status"], "dns_error")
        with open(self.report, newline="") as fh:
            self.assertEqual(next(csv.reader(fh)), collect.REPORT_FIELDS)

    def test_low_content_site_flagged(self):
        m = self.manifest(self.js_domain)
        self.assertTrue(any(w.startswith("low_content") for w in m["warnings"]))

    # ---- raw HTML and metadata

    def test_index_html_saved_byte_for_byte(self):
        saved = (self.out / collect.dir_name(self.main_domain) / "index.html").read_bytes()
        self.assertEqual(saved, self.served_index)

    def test_metadata(self):
        self.assertEqual(self.m["status"], "ok")
        self.assertEqual(self.m["title"], "Acme Plumbing – Local plumbers you can trust")
        self.assertEqual(self.m["meta_description"], "Family-run plumbing and heating engineers.")
        self.assertEqual(self.m["og"]["title"], "Acme Plumbing")
        self.assertEqual(self.m["theme"], ["hello-elementor"])
        self.assertEqual(self.m["elementor"]["page_id"], "10")

    def test_nav(self):
        self.assertEqual([(n["label"], n["target"]) for n in self.m["nav"]], [
            ("Home", "home"), ("About", "about"), ("Services", "services"),
            ("Contact", "contact"), ("Reviews", "reviews"),
        ])
        self.assertIn("nav_target_missing: #reviews has no matching element", self.m["warnings"])

    # ---- sections

    def test_sections(self):
        s = self.m["sections"]
        self.assertEqual([x["element_id"] for x in s],
                         ["hdr0001", "a1b2c3d", "abt0001", "svc1234", "anc0001", "con0001", "ftr0001", "pop0001"])
        self.assertEqual([x["role"] for x in s],
                         ["header", "content", "content", "content", "content", "content", "footer", "popup"])
        self.assertEqual([x["anchor"] for x in s], [None, "home", "about", "services", "contact", None, None, None])
        self.assertEqual(s[3]["element_type"], "container")
        self.assertTrue(s[4]["anchor_only"])
        self.assertEqual(s[5]["hidden_on"], ["mobile"])
        self.assertTrue(s[0]["has_nav"])
        self.assertEqual(s[2]["widget_types"], ["heading", "text-editor", "image"])
        self.assertEqual(s[2]["headings"], ["About us"])
        self.assertTrue(s[2]["text_preview"].startswith("About us We have been fixing leaks"))
        self.assertLessEqual(len(s[2]["text_preview"]), 200)

    def test_forms(self):
        forms = {f["type"]: f for f in self.m["forms"]}
        self.assertEqual(set(forms), {"elementor-form", "contact-form-7"})
        self.assertEqual(forms["elementor-form"]["fields"], ["name", "email", "message"])
        self.assertEqual(forms["elementor-form"]["name"], "Quote request")
        self.assertEqual(forms["contact-form-7"]["fields"], ["your-name", "your-phone"])
        self.assertEqual(forms["elementor-form"]["section"], 6)

    def test_embeds(self):
        kinds = {e["type"] for e in self.m["embeds"]}
        self.assertEqual(kinds, {"google-maps", "youtube"})

    # ---- CSS & images

    def test_css_files(self):
        css = {c["local_path"] for c in self.m["stylesheets"]}
        self.assertEqual(css, {"css/post-10.css", "css/post-20.css"})
        self.assertTrue((self.out / collect.dir_name(self.main_domain) / "css" / "post-10.css").exists())

    def test_image_files(self):
        images_dir = self.out / collect.dir_name(self.main_domain) / "images"
        files = sorted(p.name for p in images_dir.iterdir())
        self.assertEqual(files, sorted([
            "about-bg.jpg", "cropped-icon.png", "cta-bg.jpg", "footer-bg.png", "gallery-full.jpg",
            "header-texture.png", "hero-1.jpg", "hero-2.jpg", "icon.svg", "lazy.jpg", "logo.png",
            "noscript.jpg", "og.jpg", "only-resized-768x512.jpg", "popup.jpg", "services-bg-mobile.jpg",
            "services-bg.jpg", "team-2.jpg", "team.jpg",
        ]))
        stats = self.m["stats"]
        self.assertEqual((stats["images_found"], stats["images_downloaded"], stats["images_failed"]), (20, 19, 1))

    def test_image_sources(self):
        self.assertEqual(self.image("team-300x200.jpg")["local_path"], "images/team.jpg")
        self.assertEqual(self.image("team-1024x683.jpg")["found_in"], "srcset")
        self.assertEqual(self.image("logo-300x100.png")["found_in"], "logo")
        self.assertEqual(self.image("hero-1.jpg")["found_in"], "data-settings")
        self.assertEqual(self.image("footer-bg.png")["found_in"], "data-settings")
        self.assertEqual(self.image("about-bg.jpg")["found_in"], "style")
        self.assertEqual(self.image("cta-bg.jpg")["found_in"], "css-inline")
        self.assertEqual(self.image("services-bg.jpg")["found_in"], "css")
        self.assertEqual(self.image("services-bg.jpg")["css_files"], ["css/post-10.css"])
        self.assertEqual(self.image("lazy.jpg")["found_in"], "data-src")
        self.assertEqual(self.image("gallery-full.jpg")["found_in"], "link")
        self.assertEqual(self.image("og.jpg")["found_in"], "og")
        self.assertEqual(self.image("cropped-icon-32x32.png")["local_path"], "images/cropped-icon.png")
        self.assertEqual(self.image("only-resized-768x512.jpg")["status"], "downloaded")
        self.assertEqual(self.image("2024/02/team.jpg")["local_path"], "images/team-2.jpg")
        fake = self.image("fake.jpg")
        self.assertEqual(fake["status"], "failed")
        self.assertIn("not an image", fake["error"])
        urls = " ".join(i["source_url"] for i in self.m["images"])
        self.assertNotIn("facebook.com", urls)
        self.assertNotIn("data:", urls)
        self.assertNotIn("commented-out", urls)
        self.assertNotIn(".woff", urls)

    def test_images_mapped_to_sections(self):
        s = {x["element_id"]: x for x in self.m["sections"]}
        self.assertEqual(s["svc1234"]["background_images"], ["images/services-bg.jpg", "images/services-bg-mobile.jpg"])
        self.assertEqual(s["a1b2c3d"]["background_images"], ["images/hero-1.jpg", "images/hero-2.jpg"])
        self.assertIn("images/header-texture.png", s["hdr0001"]["background_images"])
        self.assertIn("images/logo.png", s["hdr0001"]["images"])
        self.assertIn("images/cta-bg.jpg", s["con0001"]["background_images"])
        self.assertIn("images/footer-bg.png", s["ftr0001"]["background_images"])
        self.assertIn("images/team.jpg", s["abt0001"]["images"])

    # ---- re-running

    def test_rerun_skips_then_force_and_only(self):
        mtime = (self.out / collect.dir_name(self.main_domain) / "manifest.json").stat().st_mtime_ns
        self.run_collect("--only", self.main_domain)
        self.assertEqual(mtime, (self.out / collect.dir_name(self.main_domain) / "manifest.json").stat().st_mtime_ns)
        self.run_collect("--only", self.main_domain, "--force")
        self.assertNotEqual(mtime, (self.out / collect.dir_name(self.main_domain) / "manifest.json").stat().st_mtime_ns)
        self.assertEqual(self.manifest(self.main_domain)["stats"]["images_downloaded"], 19)
        self.assertEqual(len(self.report_rows()), 6)

    def test_render(self):
        try:
            import playwright  # noqa: F401
        except ImportError:
            self.skipTest("playwright not installed")
        self.run_collect("--only", self.js_domain, "--render")
        site = self.out / collect.dir_name(self.js_domain)
        self.assertTrue((site / "rendered.html").exists())
        m = self.manifest(self.js_domain)
        self.assertEqual(m["parsed_from"], "rendered.html")
        self.assertEqual([s["anchor"] for s in m["sections"]], ["about"])
        self.assertEqual(m["sections"][0]["headings"], ["Rendered heading"])
        names = {i["local_path"] for i in m["images"]}
        self.assertIn("images/late.png", names)  # only appears after scrolling
        self.assertIn("images/js-bg.png", names)  # background set by JavaScript
        self.assertFalse(any(w.startswith("low_content") for w in m["warnings"]))


if __name__ == "__main__":
    unittest.main()
