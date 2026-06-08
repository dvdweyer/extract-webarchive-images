#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations
"""
extract_webarchive_images.py
────────────────────────────
Extract all embedded images from one or more macOS Safari .webarchive files.

Usage
─────
  # Single archive → output dir auto-named after the archive
  python3 extract_webarchive_images.py page.webarchive

  # Multiple archives
  python3 extract_webarchive_images.py *.webarchive

  # Explicit output directory
  python3 extract_webarchive_images.py page.webarchive -o ~/Desktop/images

  # Also download images referenced in srcset but not saved in the archive
  python3 extract_webarchive_images.py page.webarchive --fetch-missing

  # Audit: list every resource and why it was kept/skipped (no files written)
  python3 extract_webarchive_images.py page.webarchive --audit

  # Dry-run: show what would be extracted without writing anything
  python3 extract_webarchive_images.py page.webarchive --dry-run

  # Keep duplicate images (same bytes, different URLs) as separate files
  python3 extract_webarchive_images.py page.webarchive --no-dedup

  # Quiet mode (only the summary line)
  python3 extract_webarchive_images.py page.webarchive -q

Requirements
────────────
  Python 3.6+, no third-party dependencies.
  Works on macOS and Linux (plistlib handles both binary and XML plists).
"""

import argparse
import base64
import hashlib
import plistlib
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

# ── MIME → extension mapping ──────────────────────────────────────────────────

MIME_TO_EXT: dict[str, str] = {
    "image/jpeg":               ".jpg",
    "image/jpg":                ".jpg",
    "image/png":                ".png",
    "image/gif":                ".gif",
    "image/webp":               ".webp",
    "image/svg+xml":            ".svg",
    "image/bmp":                ".bmp",
    "image/tiff":               ".tiff",
    "image/tif":                ".tif",
    "image/avif":               ".avif",
    "image/heic":               ".heic",
    "image/heif":               ".heif",
    "image/ico":                ".ico",
    "image/x-icon":             ".ico",
    "image/vnd.microsoft.icon": ".ico",
    "image/jxl":                ".jxl",
}

IMAGE_MIME_PREFIX = "image/"

# data:image/... URI pattern
DATA_URI_RE = re.compile(
    r'data:(image/[^;,\s"\']+);base64,([A-Za-z0-9+/=]+)',
    re.IGNORECASE,
)

# srcset attribute pattern — each <source srcset="..."> may hold one or more
# space/comma-separated "url [descriptor]" entries.
SRCSET_ATTR_RE = re.compile(r'srcset=["\']([^"\']+)["\']', re.IGNORECASE)

# Extension → MIME fallback for URLs whose server returns a generic type
_EXT_TO_MIME: dict[str, str] = {
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png":  "image/png",
    ".webp": "image/webp",
    ".gif":  "image/gif",
    ".avif": "image/avif",
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".svg":  "image/svg+xml",
    ".bmp":  "image/bmp",
    ".tiff": "image/tiff",
    ".tif":  "image/tif",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def is_image_mime(mime: str) -> bool:
    return mime.strip().lower().startswith(IMAGE_MIME_PREFIX)


def ext_for_mime(mime: str, url: str = "") -> str:
    mime_lc = mime.strip().lower().split(";")[0].strip()
    if mime_lc in MIME_TO_EXT:
        return MIME_TO_EXT[mime_lc]
    if url:
        suffix = Path(urlparse(url).path).suffix.lower()
        if suffix and re.match(r"\.[a-z0-9]{2,5}$", suffix):
            return suffix
    return ".bin"


def safe_stem(url: str) -> str:
    try:
        path = urlparse(url).path
        stem = Path(path).stem
        stem = re.sub(r"[^\w\-.]", "_", stem).strip("_.")
        return stem[:80]
    except Exception:
        return ""


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:8]


def make_unique_path(out_dir: Path, stem: str, ext: str) -> Path:
    candidate = out_dir / f"{stem}{ext}"
    if not candidate.exists():
        return candidate
    counter = 2
    while True:
        candidate = out_dir / f"{stem}_{counter}{ext}"
        if not candidate.exists():
            return candidate
        counter += 1


def parse_srcset_urls(html: str, base_url: str) -> list[str]:
    """Return deduplicated absolute image URLs from all srcset attributes."""
    parsed_base = urlparse(base_url) if base_url else None
    seen: set[str] = set()
    urls: list[str] = []

    for raw_val in SRCSET_ATTR_RE.findall(html):
        # Each comma-separated part is "url [width/density descriptor]"
        for part in raw_val.split(","):
            token = part.strip().split()[0] if part.strip() else ""
            if not token or token.startswith("data:"):
                continue
            if token.startswith("//"):
                abs_url = "https:" + token
            elif token.startswith("/") and parsed_base:
                abs_url = f"{parsed_base.scheme}://{parsed_base.netloc}{token}"
            elif token.startswith("http://") or token.startswith("https://"):
                abs_url = token
            else:
                continue
            if abs_url not in seen:
                seen.add(abs_url)
                urls.append(abs_url)

    return urls


def fetch_url(url: str, timeout: int = 15) -> tuple[bytes, str]:
    """Download url; return (body, mime). Raises urllib.error on failure."""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                                "Version/17.0 Safari/605.1.15"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        mime = (resp.headers.get_content_type() or "").strip()
        return resp.read(), mime


# ── Result accumulator ────────────────────────────────────────────────────────

class ExtractionResult:
    def __init__(self, archive_path: Path):
        self.archive_path = archive_path
        self.saved: list[Path] = []
        self.skipped_duplicate: int = 0
        self.fetched_missing: int = 0   # images downloaded from the network
        self.errors: list[str] = []
        self.audit_log: list[dict] = []


# ── Core extraction ───────────────────────────────────────────────────────────

def write_image(
    data: bytes,
    mime: str,
    url: str,
    label: str,
    out_dir: Path,
    seen_hashes: set[str],
    result: ExtractionResult,
    dry_run: bool,
    dedup: bool,
    quiet: bool,
    audit: bool,
) -> None:
    """Attempt to save one image blob. Shared by subresource and data-URI paths."""
    if not data:
        entry = {"url": url, "mime": mime, "status": "SKIP", "reason": "empty data"}
        result.audit_log.append(entry)
        if audit:
            print(f"  SKIP  [empty data]  {label}")
        return

    h = content_hash(data)

    if dedup and h in seen_hashes:
        result.skipped_duplicate += 1
        entry = {"url": url, "mime": mime, "status": "SKIP", "reason": f"duplicate hash {h}"}
        result.audit_log.append(entry)
        if audit:
            print(f"  SKIP  [duplicate {h}]  {label}")
        return

    seen_hashes.add(h)

    ext  = ext_for_mime(mime, url)
    stem = safe_stem(url) or f"image_{h}"
    dest = make_unique_path(out_dir, stem, ext)

    entry = {"url": url, "mime": mime, "bytes": len(data), "hash": h,
             "dest": str(dest.name), "status": ""}

    if dry_run or audit:
        entry["status"] = "DRY-RUN" if dry_run else "WOULD-SAVE"
        result.audit_log.append(entry)
        result.saved.append(dest)
        if not quiet or audit:
            tag = "DRY-RUN" if dry_run else "✓"
            print(f"  {tag}  [{mime}]  {label}  →  {dest.name}  ({len(data):,} B)")
    else:
        try:
            dest.write_bytes(data)
            entry["status"] = "SAVED"
            result.audit_log.append(entry)
            result.saved.append(dest)
            if not quiet:
                print(f"  ✓  [{mime}]  {label}  →  {dest.name}  ({len(data):,} B)")
        except OSError as exc:
            msg = f"  ✗  Could not write {dest}: {exc}"
            entry["status"] = "ERROR"
            entry["reason"] = str(exc)
            result.audit_log.append(entry)
            result.errors.append(msg)
            if not quiet or audit:
                print(msg)


def _fetch_missing_srcset(
    html_resources: list[tuple[str, str]],
    archived_urls: set[str],
    out_dir: Path,
    seen_hashes: set[str],
    result: ExtractionResult,
    dry_run: bool,
    dedup: bool,
    quiet: bool,
    audit: bool,
) -> None:
    """Download images found in srcset attributes but absent from the archive."""
    # Collect unique candidates across all HTML resources
    seen_candidates: set[str] = set()
    candidates: list[str] = []
    for html_text, base_url in html_resources:
        for url in parse_srcset_urls(html_text, base_url):
            if url not in seen_candidates and url not in archived_urls:
                seen_candidates.add(url)
                candidates.append(url)

    if not candidates:
        return

    if not quiet:
        action = "Would fetch" if (dry_run or audit) else "Fetching"
        print(f"\n  {action} {len(candidates)} srcset image(s) not in archive…")

    for url in candidates:
        label = url

        if dry_run or audit:
            tag = "DRY-RUN" if dry_run else "WOULD-FETCH"
            ext  = Path(urlparse(url).path).suffix.lower() or ".bin"
            stem = safe_stem(url) or f"image_{content_hash(url.encode())}"
            dest = make_unique_path(out_dir, stem, ext)
            entry = {"url": url, "status": tag, "reason": "not in archive; would fetch"}
            result.audit_log.append(entry)
            result.saved.append(dest)
            if not quiet or audit:
                print(f"  {tag}  {url}  →  {dest.name}")
            continue

        try:
            data, mime = fetch_url(url)
        except Exception as exc:
            msg = f"  ✗  {url}: {exc}"
            result.errors.append(msg)
            if not quiet:
                print(msg)
            continue

        # Some servers return a generic MIME; fall back to the URL extension
        if not is_image_mime(mime):
            ext = Path(urlparse(url).path).suffix.lower()
            mime = _EXT_TO_MIME.get(ext, "")
            if not mime:
                if not quiet:
                    print(f"  SKIP  [not an image: {mime or '?'}]  {url}")
                continue

        before = len(result.saved)
        write_image(data, mime, url, label, out_dir, seen_hashes,
                    result, dry_run, dedup, quiet, audit)
        if len(result.saved) > before:
            result.fetched_missing += 1


def extract_resources(
    archive_dict: dict,
    out_dir: Path,
    seen_hashes: set[str],
    result: ExtractionResult,
    dry_run: bool,
    dedup: bool,
    quiet: bool,
    audit: bool,
    depth: int = 0,
    fetch_missing: bool = False,
) -> None:
    """Recursively walk a WebArchive plist dict."""

    # ── Gather all resources at this level ────────────────────────────────────
    resources: list[dict] = []
    main = archive_dict.get("WebMainResource")
    if isinstance(main, dict):
        resources.append(main)

    subs = archive_dict.get("WebSubresources", [])
    if isinstance(subs, list):
        resources.extend(r for r in subs if isinstance(r, dict))

    # URLs already present in this archive level (used to avoid re-fetching)
    archived_urls: set[str] = {r.get("WebResourceURL", "") for r in resources}
    # HTML bodies to mine for srcset URLs
    html_resources: list[tuple[str, str]] = []  # (decoded_text, base_url)

    for resource in resources:
        mime = resource.get("WebResourceMIMEType", "").strip()
        url  = resource.get("WebResourceURL", "")
        data = resource.get("WebResourceData")

        # ── Non-image subresources: scan for data: URIs if HTML/CSS ──────────
        if not is_image_mime(mime):
            if audit:
                short_mime = mime or "(no mime)"
                print(f"  ·  [{short_mime}]  {url or '(no url)'}  — not an image")
            if isinstance(data, bytes):
                try:
                    text = data.decode("utf-8", errors="replace")
                except Exception:
                    text = ""
                # Collect HTML for --fetch-missing srcset parsing
                if fetch_missing and mime.startswith("text/html"):
                    html_resources.append((text, url))
                # Extract data-URI images embedded in HTML/CSS/JS source
                matches = DATA_URI_RE.findall(text)
                for i, (uri_mime, b64) in enumerate(matches):
                    try:
                        img_bytes = base64.b64decode(b64)
                    except Exception:
                        continue
                    uri_label = f"data-uri #{i+1} in {url or '(embedded)'}"
                    write_image(
                        img_bytes, uri_mime, f"data_uri_{i}",
                        uri_label, out_dir, seen_hashes,
                        result, dry_run, dedup, quiet, audit,
                    )
            continue

        # ── Image subresource ─────────────────────────────────────────────────
        if not isinstance(data, bytes):
            entry = {"url": url, "mime": mime, "status": "SKIP",
                     "reason": "WebResourceData missing or wrong type"}
            result.audit_log.append(entry)
            result.errors.append(f"  ⚠  Missing data for: {url or '(no url)'}")
            if audit:
                print(f"  SKIP  [no data]  {url or '(no url)'}")
            continue

        label = url or "(no url)"
        write_image(data, mime, url, label, out_dir, seen_hashes,
                    result, dry_run, dedup, quiet, audit)

    # ── Fetch srcset images not captured in the archive ───────────────────────
    if fetch_missing and html_resources:
        _fetch_missing_srcset(
            html_resources, archived_urls, out_dir, seen_hashes,
            result, dry_run, dedup, quiet, audit,
        )

    # ── Recurse into sub-frame archives (iframes) ─────────────────────────────
    subframes = archive_dict.get("WebSubframeArchives", [])
    if isinstance(subframes, list):
        for frame in subframes:
            if isinstance(frame, dict):
                if audit:
                    frame_url = (frame.get("WebMainResource") or {}).get("WebResourceURL", "")
                    print(f"\n  ┌─ sub-frame: {frame_url}")
                extract_resources(
                    frame, out_dir, seen_hashes, result,
                    dry_run, dedup, quiet, audit, depth + 1,
                    fetch_missing=fetch_missing,
                )
                if audit:
                    print(f"  └─ end sub-frame")


def process_webarchive(
    archive_path: Path,
    out_dir: Path | None,
    dry_run: bool,
    dedup: bool,
    quiet: bool,
    audit: bool,
    fetch_missing: bool = False,
) -> ExtractionResult:
    result = ExtractionResult(archive_path)

    try:
        with archive_path.open("rb") as fh:
            plist = plistlib.load(fh)
    except Exception as exc:
        result.errors.append(f"Failed to parse plist: {exc}")
        return result

    if not isinstance(plist, dict):
        result.errors.append("Unexpected plist structure (root is not a dict).")
        return result

    if out_dir is None:
        out_dir = archive_path.parent / f"{archive_path.stem}_images"

    if not dry_run and not audit:
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            result.errors.append(f"Cannot create output directory {out_dir}: {exc}")
            return result

    seen_hashes: set[str] = set()
    extract_resources(plist, out_dir, seen_hashes, result,
                      dry_run, dedup, quiet, audit,
                      fetch_missing=fetch_missing)
    return result


# ── CLI ───────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="extract_webarchive_images",
        description="Extract all images from Safari .webarchive files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("archives", nargs="*", metavar="FILE.webarchive")
    p.add_argument("-o", "--output", metavar="DIR",
                   help="Output directory (default: <archive_stem>_images/ next to archive).")
    p.add_argument("--fetch-missing", action="store_true",
                   help="Download images referenced in srcset attributes but not "
                        "saved in the archive (requires network access).")
    p.add_argument("--dry-run", action="store_true",
                   help="Show what would be extracted without writing files.")
    p.add_argument("--audit", action="store_true",
                   help="List every resource with its disposition. Implies --dry-run.")
    p.add_argument("--no-dedup", action="store_true",
                   help="Save duplicate images (same bytes, different URLs) as separate files.")
    p.add_argument("-q", "--quiet", action="store_true",
                   help="Suppress per-file output; only print the summary.")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.archives:
        parser.print_help()
        return 0

    archives  = [Path(p) for p in args.archives]
    base_out  = Path(args.output).expanduser() if args.output else None
    multiple  = len(archives) > 1
    audit     = args.audit
    dry_run   = args.dry_run or audit   # --audit implies no writes
    dedup     = not args.no_dedup

    total_saved   = 0
    total_fetched = 0
    total_dupes   = 0
    total_errors  = 0
    fatal         = False

    for archive_path in archives:
        if not archive_path.exists():
            print(f"❌  {archive_path}: file not found", file=sys.stderr)
            fatal = True
            continue
        if not archive_path.is_file():
            print(f"❌  {archive_path}: not a file", file=sys.stderr)
            fatal = True
            continue

        if base_out is not None and multiple:
            out_dir: Path | None = base_out / f"{archive_path.stem}_images"
        else:
            out_dir = base_out

        print(f"\n📂  {archive_path.name}")
        if audit:
            print(f"{'─' * 60}")

        result = process_webarchive(
            archive_path, out_dir, dry_run, dedup, args.quiet, audit,
            fetch_missing=args.fetch_missing,
        )

        for err in result.errors:
            print(err, file=sys.stderr)

        if audit:
            print(f"{'─' * 60}")

        action = "Would extract" if dry_run else "Extracted"
        dest_dir = (
            result.saved[0].parent if result.saved
            else out_dir or (archive_path.parent / f"{archive_path.stem}_images")
        )

        fetched_note = ""
        if args.fetch_missing and result.fetched_missing:
            fetched_note = f" ({result.fetched_missing} fetched from network)"
        elif dry_run and args.fetch_missing:
            # In dry-run we don't know what would succeed, so note the intent
            fetched_note = " (includes srcset URLs that would be fetched)"

        print(
            f"  → {action} {len(result.saved)} image(s){fetched_note}"
            + (f", {result.skipped_duplicate} duplicate(s) skipped" if result.skipped_duplicate else "")
            + (f", {len(result.errors)} error(s)" if result.errors else "")
            + (f"  [{dest_dir}]" if result.saved and not dry_run else "")
        )

        total_saved   += len(result.saved)
        total_fetched += result.fetched_missing
        total_dupes   += result.skipped_duplicate
        total_errors  += len(result.errors)

        if result.errors and any(
            e.startswith("Failed to parse") or e.startswith("Unexpected") or e.startswith("Cannot create")
            for e in result.errors
        ):
            fatal = True

    if multiple or fatal:
        fetched_total = f" ({total_fetched} fetched from network)" if total_fetched else ""
        print(
            f"\n{'─' * 50}\n"
            f"Total: {total_saved} image(s){fetched_total}"
            + (f", {total_dupes} duplicate(s) skipped" if total_dupes else "")
            + (f", {total_errors} error(s)" if total_errors else "")
        )

    return 1 if fatal or total_errors else 0


if __name__ == "__main__":
    sys.exit(main())
