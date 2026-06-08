# extract-webarchive-images

Extract all embedded images from macOS Safari `.webarchive` files.

## Features

- Extracts images saved as subresources inside the archive
- Finds `data:image/...` URIs embedded in HTML, CSS, and JS
- Recursively handles sub-frame archives (iframes)
- Deduplicates images by content hash (skips identical bytes)
- Optionally downloads images referenced in `srcset` attributes that weren't captured in the archive
- Dry-run and audit modes — inspect what would be extracted without writing files
- No third-party dependencies

## Requirements

- Python 3.6+
- macOS or Linux

## Usage

```sh
# Single archive → output saved to page_images/ next to the archive
python3 extract_webarchive_images.py page.webarchive

# Multiple archives
python3 extract_webarchive_images.py *.webarchive

# Explicit output directory
python3 extract_webarchive_images.py page.webarchive -o ~/Desktop/images

# Also download images referenced in srcset but not saved in the archive
python3 extract_webarchive_images.py page.webarchive --fetch-missing

# Audit: list every resource and its disposition without writing files
python3 extract_webarchive_images.py page.webarchive --audit

# Dry-run: show what would be extracted without writing anything
python3 extract_webarchive_images.py page.webarchive --dry-run

# Keep duplicate images (same bytes, different URLs) as separate files
python3 extract_webarchive_images.py page.webarchive --no-dedup

# Quiet: only print the summary line
python3 extract_webarchive_images.py page.webarchive -q
```

## Options

| Flag | Description |
|---|---|
| `-o DIR` / `--output DIR` | Output directory (default: `<archive_stem>_images/` next to the archive) |
| `--fetch-missing` | Download images in `srcset` attributes not saved in the archive |
| `--dry-run` | Show what would be extracted without writing files |
| `--audit` | List every resource with its disposition (implies `--dry-run`) |
| `--no-dedup` | Save duplicate images as separate files instead of skipping them |
| `-q` / `--quiet` | Suppress per-file output; only print the summary |
