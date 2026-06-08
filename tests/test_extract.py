import base64
import plistlib
from pathlib import Path

import pytest

from extract_webarchive_images import (
    content_hash,
    ext_for_mime,
    is_image_mime,
    make_unique_path,
    parse_srcset_urls,
    process_webarchive,
    safe_stem,
)

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20  # fake but non-empty
_JPG = b"\xff\xd8\xff" + b"\x00" * 20


def _make_archive(tmp_path: Path, subresources: list[dict], subframes=None) -> Path:
    plist: dict = {
        "WebMainResource": {
            "WebResourceMIMEType": "text/html",
            "WebResourceURL": "https://example.com/",
            "WebResourceData": b"<html></html>",
        },
        "WebSubresources": subresources,
    }
    if subframes:
        plist["WebSubframeArchives"] = subframes
    path = tmp_path / "test.webarchive"
    path.write_bytes(plistlib.dumps(plist, fmt=plistlib.FMT_BINARY))
    return path


# ── is_image_mime ─────────────────────────────────────────────────────────────

class TestIsImageMime:
    def test_image_types(self):
        for mime in ("image/jpeg", "image/png", "image/webp", "image/svg+xml", "image/avif"):
            assert is_image_mime(mime)

    def test_non_image_types(self):
        for mime in ("text/html", "application/json", "video/mp4", ""):
            assert not is_image_mime(mime)

    def test_case_insensitive(self):
        assert is_image_mime("Image/JPEG")
        assert is_image_mime("IMAGE/PNG")


# ── ext_for_mime ──────────────────────────────────────────────────────────────

class TestExtForMime:
    def test_known_mimes(self):
        assert ext_for_mime("image/jpeg") == ".jpg"
        assert ext_for_mime("image/png") == ".png"
        assert ext_for_mime("image/webp") == ".webp"
        assert ext_for_mime("image/svg+xml") == ".svg"
        assert ext_for_mime("image/gif") == ".gif"

    def test_strips_mime_params(self):
        assert ext_for_mime("image/jpeg; charset=utf-8") == ".jpg"

    def test_unknown_mime_falls_back_to_url_extension(self):
        assert ext_for_mime("image/x-unknown", "https://example.com/photo.tiff") == ".tiff"

    def test_unknown_mime_no_url_returns_bin(self):
        assert ext_for_mime("image/x-unknown") == ".bin"


# ── safe_stem ─────────────────────────────────────────────────────────────────

class TestSafeStem:
    def test_simple_filename(self):
        assert safe_stem("https://example.com/photo.jpg") == "photo"

    def test_special_chars_replaced(self):
        stem = safe_stem("https://example.com/my image (1).jpg")
        assert " " not in stem
        assert "(" not in stem

    def test_empty_string(self):
        assert safe_stem("") == ""

    def test_long_stem_truncated_to_80(self):
        url = "https://example.com/" + "a" * 200 + ".png"
        assert len(safe_stem(url)) <= 80

    def test_returns_string(self):
        assert isinstance(safe_stem("https://example.com/"), str)


# ── content_hash ──────────────────────────────────────────────────────────────

class TestContentHash:
    def test_deterministic(self):
        assert content_hash(b"hello") == content_hash(b"hello")

    def test_different_inputs_differ(self):
        assert content_hash(b"hello") != content_hash(b"world")

    def test_length_is_8(self):
        assert len(content_hash(b"anything")) == 8


# ── make_unique_path ──────────────────────────────────────────────────────────

class TestMakeUniquePath:
    def test_no_collision(self, tmp_path):
        assert make_unique_path(tmp_path, "img", ".png") == tmp_path / "img.png"

    def test_single_collision(self, tmp_path):
        (tmp_path / "img.png").touch()
        assert make_unique_path(tmp_path, "img", ".png") == tmp_path / "img_2.png"

    def test_multiple_collisions(self, tmp_path):
        (tmp_path / "img.png").touch()
        (tmp_path / "img_2.png").touch()
        assert make_unique_path(tmp_path, "img", ".png") == tmp_path / "img_3.png"


# ── parse_srcset_urls ─────────────────────────────────────────────────────────

class TestParseSrcsetUrls:
    def test_single_absolute_url(self):
        html = '<img srcset="https://example.com/img.png 1x">'
        assert "https://example.com/img.png" in parse_srcset_urls(html, "https://example.com/")

    def test_multiple_urls(self):
        html = '<img srcset="https://example.com/a.png 1x, https://example.com/b.png 2x">'
        urls = parse_srcset_urls(html, "https://example.com/")
        assert len(urls) == 2

    def test_protocol_relative_url(self):
        html = '<img srcset="//example.com/img.png 1x">'
        urls = parse_srcset_urls(html, "https://example.com/")
        assert "https://example.com/img.png" in urls

    def test_root_relative_url(self):
        html = '<img srcset="/images/img.png 1x">'
        urls = parse_srcset_urls(html, "https://example.com/page")
        assert "https://example.com/images/img.png" in urls

    def test_deduplicates(self):
        url = "https://example.com/img.png"
        html = f'<img srcset="{url} 1x, {url} 2x">'
        urls = parse_srcset_urls(html, "https://example.com/")
        assert urls.count(url) == 1

    def test_empty_html(self):
        assert parse_srcset_urls("", "https://example.com/") == []


# ── process_webarchive (integration) ─────────────────────────────────────────

class TestProcessWebarchive:
    def test_extracts_image(self, tmp_path):
        archive = _make_archive(tmp_path, [
            {"WebResourceMIMEType": "image/png",
             "WebResourceURL": "https://example.com/img.png",
             "WebResourceData": _PNG},
        ])
        out = tmp_path / "out"
        result = process_webarchive(archive, out, dry_run=False, dedup=True, quiet=True, audit=False)
        assert len(result.saved) == 1
        assert result.saved[0].suffix == ".png"
        assert result.saved[0].exists()

    def test_extracts_multiple_image_types(self, tmp_path):
        archive = _make_archive(tmp_path, [
            {"WebResourceMIMEType": "image/png",
             "WebResourceURL": "https://example.com/a.png",
             "WebResourceData": _PNG},
            {"WebResourceMIMEType": "image/jpeg",
             "WebResourceURL": "https://example.com/b.jpg",
             "WebResourceData": _JPG},
        ])
        out = tmp_path / "out"
        result = process_webarchive(archive, out, dry_run=False, dedup=True, quiet=True, audit=False)
        assert len(result.saved) == 2
        assert {p.suffix for p in result.saved} == {".png", ".jpg"}

    def test_dedup_skips_identical_bytes(self, tmp_path):
        archive = _make_archive(tmp_path, [
            {"WebResourceMIMEType": "image/png",
             "WebResourceURL": "https://example.com/a.png",
             "WebResourceData": _PNG},
            {"WebResourceMIMEType": "image/png",
             "WebResourceURL": "https://example.com/b.png",
             "WebResourceData": _PNG},
        ])
        out = tmp_path / "out"
        result = process_webarchive(archive, out, dry_run=False, dedup=True, quiet=True, audit=False)
        assert len(result.saved) == 1
        assert result.skipped_duplicate == 1

    def test_no_dedup_saves_both(self, tmp_path):
        archive = _make_archive(tmp_path, [
            {"WebResourceMIMEType": "image/png",
             "WebResourceURL": "https://example.com/a.png",
             "WebResourceData": _PNG},
            {"WebResourceMIMEType": "image/png",
             "WebResourceURL": "https://example.com/b.png",
             "WebResourceData": _PNG},
        ])
        out = tmp_path / "out"
        result = process_webarchive(archive, out, dry_run=False, dedup=False, quiet=True, audit=False)
        assert len(result.saved) == 2
        assert result.skipped_duplicate == 0

    def test_dry_run_writes_no_files(self, tmp_path):
        archive = _make_archive(tmp_path, [
            {"WebResourceMIMEType": "image/png",
             "WebResourceURL": "https://example.com/img.png",
             "WebResourceData": _PNG},
        ])
        out = tmp_path / "out"
        result = process_webarchive(archive, out, dry_run=True, dedup=True, quiet=True, audit=False)
        assert len(result.saved) == 1
        assert not out.exists()

    def test_auto_output_dir(self, tmp_path):
        archive = _make_archive(tmp_path, [
            {"WebResourceMIMEType": "image/png",
             "WebResourceURL": "https://example.com/img.png",
             "WebResourceData": _PNG},
        ])
        result = process_webarchive(archive, None, dry_run=False, dedup=True, quiet=True, audit=False)
        assert result.saved[0].parent == tmp_path / "test_images"

    def test_data_uri_in_html(self, tmp_path):
        b64 = base64.b64encode(_PNG).decode()
        html = f'<img src="data:image/png;base64,{b64}">'.encode()
        plist = {
            "WebMainResource": {
                "WebResourceMIMEType": "text/html",
                "WebResourceURL": "https://example.com/",
                "WebResourceData": html,
            },
            "WebSubresources": [],
        }
        archive = tmp_path / "data_uri.webarchive"
        archive.write_bytes(plistlib.dumps(plist, fmt=plistlib.FMT_BINARY))
        out = tmp_path / "out"
        result = process_webarchive(archive, out, dry_run=False, dedup=True, quiet=True, audit=False)
        assert len(result.saved) == 1

    def test_subframe_images_extracted(self, tmp_path):
        subframe = {
            "WebMainResource": {
                "WebResourceMIMEType": "text/html",
                "WebResourceURL": "https://example.com/frame",
                "WebResourceData": b"<html></html>",
            },
            "WebSubresources": [
                {"WebResourceMIMEType": "image/png",
                 "WebResourceURL": "https://example.com/frame/img.png",
                 "WebResourceData": _PNG},
            ],
        }
        archive = _make_archive(tmp_path, [], subframes=[subframe])
        out = tmp_path / "out"
        result = process_webarchive(archive, out, dry_run=False, dedup=True, quiet=True, audit=False)
        assert len(result.saved) == 1

    def test_missing_file_returns_error(self, tmp_path):
        result = process_webarchive(
            tmp_path / "nonexistent.webarchive", None,
            dry_run=False, dedup=True, quiet=True, audit=False,
        )
        assert len(result.errors) > 0

    def test_non_image_resources_skipped(self, tmp_path):
        archive = _make_archive(tmp_path, [
            {"WebResourceMIMEType": "text/css",
             "WebResourceURL": "https://example.com/style.css",
             "WebResourceData": b"body { color: red; }"},
            {"WebResourceMIMEType": "application/javascript",
             "WebResourceURL": "https://example.com/app.js",
             "WebResourceData": b"console.log('hi')"},
        ])
        out = tmp_path / "out"
        result = process_webarchive(archive, out, dry_run=False, dedup=True, quiet=True, audit=False)
        assert len(result.saved) == 0
