#!/usr/bin/env python3
"""
epub_split.py - Split an already-unzipped EPUB folder OR a .epub archive file
into XHTML-preserving chunks compatible with manifest.py, glossary.py,
chunk_context.py, merge_meta.py, review_flags.py, and run_state.py.

Works across ANY source language (Japanese, Chinese, Korean, English, Spanish,
French, German, Russian, etc.).
"""
import sys as _sys
if hasattr(_sys.stdout, 'reconfigure'):
    _sys.stdout.reconfigure(encoding='utf-8')
if hasattr(_sys.stderr, 'reconfigure'):
    _sys.stderr.reconfigure(encoding='utf-8')

import argparse
import hashlib
import json
import os
import re
import shutil
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

from manifest import create_manifest
import glossary as glossary_mod

WATERMARK_RE = re.compile(
    r'\s*<p>[^<]*https?://link3\.cc/acg123\s*</p>\s*<p>[^<]*仅供个人学习交流使用[^<]*</p>\s*<hr\s*/?>\s*',
    re.DOTALL,
)

# Normalize HTML5 boolean `alt` on <img ... alt /> into strict XHTML `alt=""`
BARE_ALT_RE = re.compile(r'(<img\b[^>]*?)\balt(?=\s*/?>|\s+[a-zA-Z])', re.IGNORECASE)

# Matches any letter/ideograph in any Unicode script (Latin, Cyrillic, Arabic, CJK, Hangul, Kana, etc.)
ANY_TEXT_RE = re.compile(r'[^\W\d_]', re.UNICODE)

# Matches inner wrapper inside <body> if present (<section>...</section> or <div class="...">...</div>)
INNER_WRAPPER_RE = re.compile(
    r'^(.*?<body[^>]*>\s*(?:<(?:section|div)[^>]*>\s*)?)(.*?)(\s*(?:</(?:section|div)>\s*)?</body>.*)$',
    re.DOTALL | re.IGNORECASE,
)

TITLE_RE = re.compile(r'(<title[^>]*>)(.*?)(</title>)', re.DOTALL | re.IGNORECASE)


def has_translatable_text(text: str) -> bool:
    return bool(ANY_TEXT_RE.search(text))


def strip_tags(text: str) -> str:
    return re.sub(r'<[^>]+>', '', text)


def find_opf_path(epub_dir: Path) -> Path:
    container_xml = epub_dir / "META-INF" / "container.xml"
    if not container_xml.exists():
        raise FileNotFoundError(f"Missing META-INF/container.xml in {epub_dir}")
    tree = ET.parse(container_xml)
    root = tree.getroot()
    for elem in root.iter():
        if elem.tag.endswith("rootfile"):
            full_path = elem.attrib.get("full-path")
            if full_path:
                return epub_dir / full_path
    raise ValueError(f"Could not find rootfile in {container_xml}")


def get_ordered_xhtml_files(epub_dir: Path, opf_path: Path):
    tree = ET.parse(opf_path)
    root = tree.getroot()
    opf_dir = opf_path.parent

    manifest_items = {}
    nav_hrefs = []
    ncx_href = None
    orig_title = ""

    for elem in root.iter():
        if elem.tag.endswith("title") and not orig_title and elem.text:
            orig_title = elem.text.strip()
        elif elem.tag.endswith("item"):
            item_id = elem.attrib.get("id")
            href = elem.attrib.get("href")
            media_type = elem.attrib.get("media-type", "")
            props = elem.attrib.get("properties", "")
            if item_id and href:
                manifest_items[item_id] = (href, media_type, props)
                if "nav" in props.split():
                    nav_hrefs.append(href)
                if media_type == "application/x-dtbncx+xml":
                    ncx_href = href

    spine_hrefs = []
    for elem in root.iter():
        if elem.tag.endswith("itemref"):
            idref = elem.attrib.get("idref")
            if idref in manifest_items:
                href, media_type, _ = manifest_items[idref]
                if media_type == "application/xhtml+xml" or href.endswith((".xhtml", ".html", ".htm")):
                    spine_hrefs.append(href)

    return orig_title, opf_dir, spine_hrefs, nav_hrefs, ncx_href


def split_into_blocks(inner_html: str):
    """Split inner HTML into balanced top-level blocks (or line-by-line paragraphs)
    so no HTML element is ever split across two chunks."""
    lines = inner_html.splitlines(keepends=True)
    blocks = []
    current = []
    depth = 0

    open_tag_re = re.compile(r'<([a-zA-Z0-9:_-]+)(?:\s[^>]*)?(?<!/)>')
    close_tag_re = re.compile(r'</([a-zA-Z0-9:_-]+)\s*>')
    void_tags = {"br", "hr", "img", "meta", "link", "image", "input"}

    for line in lines:
        current.append(line)
        for m in open_tag_re.finditer(line):
            tag = m.group(1).lower()
            if tag not in void_tags:
                depth += 1
        for m in close_tag_re.finditer(line):
            tag = m.group(1).lower()
            if tag not in void_tags:
                depth -= 1
        if depth <= 0:
            depth = 0
            block_str = "".join(current)
            if block_str.strip():
                blocks.append(block_str)
            current = []

    if current:
        block_str = "".join(current)
        if block_str.strip():
            blocks.append(block_str)

    return blocks


def group_blocks_into_chunks(blocks, max_chars: int):
    chunks = []
    cur_blocks = []
    cur_len = 0

    for b in blocks:
        b_len = len(re.sub(r'\s+', ' ', b))
        if cur_blocks and (cur_len + b_len > max_chars):
            chunks.append("".join(cur_blocks).strip() + "\n")
            cur_blocks = [b]
            cur_len = b_len
        else:
            cur_blocks.append(b)
            cur_len += b_len

    if cur_blocks:
        chunks.append("".join(cur_blocks).strip() + "\n")

    return chunks


def main():
    parser = argparse.ArgumentParser(
        description="Split an unzipped EPUB directory or .epub archive into XHTML-preserving chunks."
    )
    parser.add_argument("epub_input", help="Path to unzipped EPUB directory OR .epub file")
    parser.add_argument("--temp-dir", required=True, help="Working directory for chunks and state")
    parser.add_argument("--unzip-dir", default=None, help="Where to extract if epub_input is a .epub file (defaults to <temp-dir>/unzipped_epub)")
    parser.add_argument("--source-lang", default="auto", help="Source language code or name (e.g. zh, ja, ko, es, fr)")
    parser.add_argument("--target-lang", default="en", help="Target language code (e.g. en, id, es, ja, zh)")
    parser.add_argument("--max-chars", type=int, default=3800, help="Target max normalized chars per chunk (default: 3800)")
    parser.add_argument(
        "--glossary-seed",
        default=str(Path(__file__).resolve().parent.parent / "resources" / "glossary_template.json"),
        help="Path to seed glossary.json if temp-dir does not have one yet",
    )
    args = parser.parse_args()

    input_path = Path(args.epub_input).resolve()
    temp_dir = Path(args.temp_dir).resolve()
    temp_dir.mkdir(parents=True, exist_ok=True)

    if input_path.is_file() and input_path.suffix.lower() == ".epub":
        epub_dir = Path(args.unzip_dir).resolve() if args.unzip_dir else (temp_dir / "unzipped_epub")
        if not (epub_dir / "META-INF" / "container.xml").exists():
            epub_dir.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(input_path, 'r') as zf:
                zf.extractall(epub_dir)
            print(f"Extracted {input_path.name} -> {epub_dir}")
    else:
        epub_dir = input_path

    opf_path = find_opf_path(epub_dir)
    orig_title, opf_dir, spine_hrefs, nav_hrefs, ncx_href = get_ordered_xhtml_files(epub_dir, opf_path)

    all_xhtml_map = []
    chunk_files = []
    chunk_counter = 0
    combined_source_parts = []

    for href in spine_hrefs:
        xhtml_path = (opf_dir / href).resolve()
        rel_to_epub = xhtml_path.relative_to(epub_dir).as_posix()
        raw_html = xhtml_path.read_text(encoding="utf-8")

        cleaned_html = WATERMARK_RE.sub("\n", raw_html)
        cleaned_html = BARE_ALT_RE.sub(r'\1alt=""', cleaned_html)
        watermark_stripped = cleaned_html != raw_html

        title_match = TITLE_RE.search(cleaned_html)
        doc_title = title_match.group(2).strip() if title_match else ""

        body_match = INNER_WRAPPER_RE.match(cleaned_html)
        if not body_match:
            all_xhtml_map.append({
                "rel_path": rel_to_epub,
                "href_in_opf": href,
                "original_title": doc_title,
                "translate_body": False,
                "watermark_stripped": watermark_stripped,
                "chunks": [],
            })
            continue

        prefix, inner_body, suffix = body_match.group(1), body_match.group(2), body_match.group(3)
        body_text_only = strip_tags(inner_body)

        if not has_translatable_text(body_text_only):
            all_xhtml_map.append({
                "rel_path": rel_to_epub,
                "href_in_opf": href,
                "original_title": doc_title,
                "translate_body": False,
                "watermark_stripped": watermark_stripped,
                "prefix": prefix,
                "inner_body": inner_body,
                "suffix": suffix,
                "chunks": [],
            })
            continue

        blocks = split_into_blocks(inner_body)
        chunk_texts = group_blocks_into_chunks(blocks, args.max_chars)
        file_chunk_ids = []

        for c_text in chunk_texts:
            chunk_counter += 1
            c_name = f"chunk{chunk_counter:04d}.md"
            c_id = f"chunk{chunk_counter:04d}"
            c_path = temp_dir / c_name
            c_path.write_text(c_text, encoding="utf-8", newline="\n")
            chunk_files.append(c_name)
            file_chunk_ids.append(c_id)
            combined_source_parts.append(c_text)

        all_xhtml_map.append({
            "rel_path": rel_to_epub,
            "href_in_opf": href,
            "original_title": doc_title,
            "translate_body": True,
            "watermark_stripped": watermark_stripped,
            "prefix": prefix,
            "suffix": suffix,
            "chunks": file_chunk_ids,
        })

    input_md_path = temp_dir / "input.md"
    input_md_path.write_text("\n".join(combined_source_parts), encoding="utf-8", newline="\n")

    create_manifest(str(temp_dir), chunk_files, str(input_md_path))

    epub_map = {
        "epub_dir": str(epub_dir),
        "source_lang": args.source_lang,
        "target_lang": args.target_lang,
        "opf_rel_path": opf_path.relative_to(epub_dir).as_posix(),
        "original_title": orig_title,
        "nav_hrefs": nav_hrefs,
        "ncx_href": ncx_href,
        "files": all_xhtml_map,
    }
    (temp_dir / "epub_map.json").write_text(
        json.dumps(epub_map, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
    )

    config_lines = [
        f"source_file={epub_dir}",
        f"source_lang={args.source_lang}",
        f"output_lang={args.target_lang}",
        f"original_title={orig_title}",
        f"chunk_count={len(chunk_files)}",
    ]
    (temp_dir / "config.txt").write_text("\n".join(config_lines) + "\n", encoding="utf-8", newline="\n")

    glossary_path = temp_dir / "glossary.json"
    seed_path = Path(args.glossary_seed)
    if not glossary_path.exists() and seed_path.exists():
        shutil.copyfile(seed_path, glossary_path)
        print(f"Seeded glossary.json from {seed_path.name}")

    if glossary_path.exists():
        glossary_mod.count_frequencies(str(glossary_path), str(temp_dir))

    print(
        f"Split complete: {len(chunk_files)} chunks across "
        f"{sum(1 for f in all_xhtml_map if f['translate_body'])} translatable XHTML files "
        f"(total spine files: {len(all_xhtml_map)})."
    )


if __name__ == "__main__":
    main()
