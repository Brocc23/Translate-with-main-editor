#!/usr/bin/env python3
"""
prep_batch.py - Prepare per-chunk glossary term tables and neighbor context
excerpts for a batch of chunks in a single command call.
"""
import sys as _sys
if hasattr(_sys.stdout, 'reconfigure'):
    _sys.stdout.reconfigure(encoding='utf-8')
if hasattr(_sys.stderr, 'reconfigure'):
    _sys.stderr.reconfigure(encoding='utf-8')

import argparse
import os
from pathlib import Path

import glossary as glossary_mod
import chunk_context as context_mod


def main():
    parser = argparse.ArgumentParser(
        description="Print per-chunk term table and neighbor context for one or more chunks."
    )
    parser.add_argument("temp_dir", help="Path to working chunk directory")
    parser.add_argument("chunks", nargs="+", help="Chunk IDs or filenames (e.g. chunk0001 chunk0002)")
    args = parser.parse_args()

    temp_dir = Path(args.temp_dir).resolve()
    glossary_path = temp_dir / "glossary.json"
    glossary = glossary_mod.load_glossary(str(glossary_path)) if glossary_path.exists() else None

    for c in args.chunks:
        fname = c if c.endswith(".md") else f"{c}.md"
        cid = os.path.splitext(fname)[0]
        cpath = temp_dir / fname
        if not cpath.exists():
            print(f"=== {cid}: MISSING ({cpath}) ===")
            continue

        chunk_text = cpath.read_text(encoding="utf-8")
        print(f"==================== {cid} ({fname}) ====================")
        if glossary:
            terms = glossary_mod.select_terms_for_chunk(glossary, chunk_text)
            table = glossary_mod.format_terms_for_prompt(terms)
            if table:
                print("--- TERM TABLE ---")
                print(table)
        ctx = context_mod.get_neighbor_context(str(temp_dir), fname)
        ctx_block = context_mod.format_for_prompt(ctx)
        if ctx_block:
            print("--- NEIGHBOR CONTEXT ---")
            print(ctx_block)
        print()


if __name__ == "__main__":
    main()
