#!/usr/bin/env python3
"""
epub_merge.py - Validate translated chunks, reconstruct XHTML files in place
inside the unzipped EPUB directory, update TOC/OPF metadata for ANY target
language code, and optionally package a ready-to-read .epub archive.
"""
import sys as _sys
if hasattr(_sys.stdout, 'reconfigure'):
    _sys.stdout.reconfigure(encoding='utf-8')
if hasattr(_sys.stderr, 'reconfigure'):
    _sys.stderr.reconfigure(encoding='utf-8')

import argparse
import json
import os
import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

from manifest import validate_for_merge, read_output_text

WATERMARK_RE = re.compile(
    r'\s*<p>[^<]*https?://link3\.cc/acg123\s*</p>\s*<p>[^<]*仅供个人学习交流使用[^<]*</p>\s*<hr\s*/?>\s*',
    re.DOTALL,
)

BARE_AMP_RE = re.compile(r'&(?!(?:[a-zA-Z][a-zA-Z0-9]*|#\d+|#x[0-9a-fA-F]+);)')
BARE_ALT_RE = re.compile(r'(<img\b[^>]*?)\balt(?=\s*/?>|\s+[a-zA-Z])', re.IGNORECASE)
DOCTYPE_RE = re.compile(r'<!DOCTYPE[^>]*>', re.IGNORECASE | re.DOTALL)
HTML_TAG_RE = re.compile(r'<html\b[^>]*>', re.IGNORECASE)
TITLE_RE = re.compile(r'(<title[^>]*>)([\s\S]*?)(</title>)', re.IGNORECASE)


def sanitize_bare_amps(xhtml_fragment: str) -> str:
    s = BARE_AMP_RE.sub('&amp;', xhtml_fragment)
    return BARE_ALT_RE.sub(r'\1alt=""', s)


def normalize_html_root_tag(xhtml_text: str, target_lang: str = "en", keep_vertical: bool = False) -> str:
    """Set xml:lang='<target_lang>', lang='<target_lang>' (if present), and switch class='vrtl' to class='hltr' unless keep_vertical is True."""
    def _repl(m):
        tag = m.group(0)
        tag = re.sub(r'\bxml:lang="[^"]*"', f'xml:lang="{target_lang}"', tag)
        tag = re.sub(r'\blang="[^"]*"', f'lang="{target_lang}"', tag)
        if not keep_vertical:
            tag = re.sub(r'\bclass="vrtl"', 'class="hltr"', tag)
        return tag
    return HTML_TAG_RE.sub(_repl, xhtml_text, count=1)


def validate_xhtml_wellformed(xhtml_text: str, label: str):
    """Parse XHTML via ElementTree (ignoring DOCTYPE) to guarantee valid XML."""
    xml_to_check = DOCTYPE_RE.sub('', xhtml_text)
    try:
        ET.fromstring(xml_to_check.encode('utf-8'))
    except ET.ParseError as e:
        raise ValueError(f"Invalid XHTML in {label}: {e}")


def apply_toc_map(text: str, toc_map: dict) -> str:
    if not toc_map:
        return text
    for src, tgt in sorted(toc_map.items(), key=lambda kv: -len(kv[0])):
        if src and tgt:
            text = text.replace(src, sanitize_bare_amps(tgt))
    return text


def pack_epub(epub_dir: Path, out_epub_path: Path):
    """Create a spec-compliant EPUB zip archive with uncompressed mimetype first."""
    out_epub_path.parent.mkdir(parents=True, exist_ok=True)
    mimetype_path = epub_dir / "mimetype"
    if not mimetype_path.exists():
        raise FileNotFoundError(f"Missing mimetype file in {epub_dir}")

    with zipfile.ZipFile(out_epub_path, 'w') as zf:
        zf.write(mimetype_path, "mimetype", compress_type=zipfile.ZIP_STORED)
        for root, _, files in os.walk(epub_dir):
            for fname in sorted(files):
                full_path = Path(root) / fname
                rel_path = full_path.relative_to(epub_dir).as_posix()
                if rel_path == "mimetype":
                    continue
                zf.write(full_path, rel_path, compress_type=zipfile.ZIP_DEFLATED)

    print(f"Packed EPUB archive: {out_epub_path} ({out_epub_path.stat().st_size:,} bytes)")


def main():
    parser = argparse.ArgumentParser(
        description="Merge translated chunks back into the unzipped EPUB folder and optionally pack .epub"
    )
    parser.add_argument("--temp-dir", required=True, help="Path to working chunk directory")
    parser.add_argument("--epub-dir", default=None, help="Override target unzipped EPUB directory")
    parser.add_argument("--target-lang", default=None, help="Target BCP-47 language code (defaults to epub_map.json target_lang or 'en')")
    parser.add_argument("--keep-vertical", action="store_true", help="Keep vertical text layout (vrtl) for CJK target languages")
    parser.add_argument("--title", default=None, help="Translated book title for OPF/NCX metadata")
    parser.add_argument("--out-epub", default=None, help="Optional output .epub file path to build")
    args = parser.parse_args()

    temp_dir = Path(args.temp_dir).resolve()
    epub_map_path = temp_dir / "epub_map.json"
    if not epub_map_path.exists():
        print(f"ERROR: Missing {epub_map_path}", file=_sys.stderr)
        _sys.exit(1)

    ok, _, _ = validate_for_merge(str(temp_dir))
    if not ok:
        print("ERROR: Manifest validation failed. Cannot merge until all chunks are valid.", file=_sys.stderr)
        _sys.exit(1)

    epub_map = json.loads(epub_map_path.read_text(encoding="utf-8"))
    epub_dir = Path(args.epub_dir).resolve() if args.epub_dir else Path(epub_map["epub_dir"]).resolve()
    target_lang = args.target_lang or epub_map.get("target_lang") or "en"

    toc_map = {}
    toc_map_path = temp_dir / "toc_translations.json"
    if toc_map_path.exists():
        toc_map = json.loads(toc_map_path.read_text(encoding="utf-8"))

    staged_writes = []

    for f_entry in epub_map["files"]:
        rel_path = f_entry["rel_path"]
        target_path = epub_dir / rel_path

        if f_entry.get("translate_body"):
            chunk_texts = []
            for cid in f_entry["chunks"]:
                out_file = temp_dir / f"output_{cid}.md"
                txt = read_output_text(str(out_file))
                if txt is None:
                    raise RuntimeError(f"Unreadable output chunk: {out_file}")
                txt = sanitize_bare_amps(txt.strip())
                try:
                    validate_xhtml_wellformed(f"<root>{txt}</root>", f"output_{cid}.md ({rel_path})")
                except ValueError as e:
                    print(f"ERROR: {e}", file=_sys.stderr)
                    _sys.exit(1)
                chunk_texts.append(txt)

            merged_body = "\n".join(chunk_texts)
            reconstructed = f"{f_entry['prefix']}\n{merged_body}\n{f_entry['suffix']}"
        else:
            raw_html = target_path.read_text(encoding="utf-8")
            reconstructed = WATERMARK_RE.sub("\n", raw_html)

        reconstructed = normalize_html_root_tag(reconstructed, target_lang=target_lang, keep_vertical=args.keep_vertical)
        if toc_map:
            reconstructed = TITLE_RE.sub(
                lambda m: m.group(1) + apply_toc_map(m.group(2), toc_map) + m.group(3),
                reconstructed,
                count=1,
            )
        if args.title and f_entry.get("original_title"):
            reconstructed = TITLE_RE.sub(
                lambda m: m.group(1)
                + (sanitize_bare_amps(args.title) if m.group(2).strip() == epub_map.get("original_title") else m.group(2))
                + m.group(3),
                reconstructed,
                count=1,
            )

        try:
            validate_xhtml_wellformed(reconstructed, rel_path)
        except ValueError as e:
            print(f"ERROR: {e}", file=_sys.stderr)
            _sys.exit(1)

        staged_writes.append((target_path, reconstructed))

    opf_path = epub_dir / epub_map["opf_rel_path"]
    opf_dir = opf_path.parent

    for nav_href in epub_map.get("nav_hrefs", []):
        nav_path = (opf_dir / nav_href).resolve()
        if nav_path.exists():
            nav_text = WATERMARK_RE.sub("\n", nav_path.read_text(encoding="utf-8"))
            nav_text = normalize_html_root_tag(nav_text, target_lang=target_lang, keep_vertical=args.keep_vertical)
            nav_text = apply_toc_map(nav_text, toc_map)
            validate_xhtml_wellformed(nav_text, nav_href)
            staged_writes.append((nav_path, nav_text))

    ncx_href = epub_map.get("ncx_href")
    if ncx_href:
        ncx_path = (opf_dir / ncx_href).resolve()
        if ncx_path.exists():
            ncx_text = ncx_path.read_text(encoding="utf-8")
            ncx_text = apply_toc_map(ncx_text, toc_map)
            if args.title:
                ncx_text = re.sub(
                    r'(<docTitle>\s*<text>)(.*?)(</text>\s*</docTitle>)',
                    lambda m: m.group(1) + sanitize_bare_amps(args.title) + m.group(3),
                    ncx_text,
                    flags=re.DOTALL,
                )
            validate_xhtml_wellformed(ncx_text, ncx_href)
            staged_writes.append((ncx_path, ncx_text))

    if opf_path.exists():
        opf_text = opf_path.read_text(encoding="utf-8")
        opf_text = re.sub(r'<dc:language>[^<]*</dc:language>', f'<dc:language>{target_lang}</dc:language>', opf_text)
        opf_text = re.sub(r'\bxml:lang="[^"]*"', f'xml:lang="{target_lang}"', opf_text)
        if not args.keep_vertical:
            opf_text = re.sub(r'\bpage-progression-direction="rtl"', 'page-progression-direction="ltr"', opf_text)
        if args.title:
            opf_text = re.sub(
                r'(<dc:title[^>]*>)(.*?)(</dc:title>)',
                lambda m: m.group(1) + sanitize_bare_amps(args.title) + m.group(3),
                opf_text,
                count=1,
                flags=re.DOTALL,
            )
        validate_xhtml_wellformed(opf_text, epub_map["opf_rel_path"])
        staged_writes.append((opf_path, opf_text))

    for path, content in staged_writes:
        path.write_text(content, encoding="utf-8", newline="\n")

    for css_rel in ["OEBPS/Styles/style.css", "item/style/book-style.css"]:
        css_path = epub_dir / css_rel
        if css_path.exists():
            css_text = css_path.read_text(encoding="utf-8")
            if "img.fit" not in css_text:
                extra_css = (
                    "\n/* Responsive illustration & page-break protection */\n"
                    ".illust, figure.illust {\n"
                    "  page-break-inside: avoid;\n"
                    "  break-inside: avoid;\n"
                    "  text-align: center;\n"
                    "  margin: 0.5em auto;\n"
                    "}\n"
                    "img.fit, .illust img {\n"
                    "  max-width: 100%;\n"
                    "  max-height: 92vh;\n"
                    "  height: auto;\n"
                    "  display: block;\n"
                    "  margin: 0.5em auto;\n"
                    "  page-break-inside: avoid;\n"
                    "  break-inside: avoid;\n"
                    "}\n"
                )
                css_path.write_text(css_text + extra_css, encoding="utf-8", newline="\n")

    print(f"Successfully updated {len(staged_writes)} files in {epub_dir} (target_lang={target_lang})")

    if args.out_epub:
        pack_epub(epub_dir, Path(args.out_epub).resolve())


if __name__ == "__main__":
    main()
