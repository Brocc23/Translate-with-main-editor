#!/usr/bin/env python3
"""
review_flags.py - Aggregate, prioritize, inspect, and validate difficulty flags
for the Two-Stage Translator + Main Editor pipeline:
  - Combines Stage-1 Drafter `.review.json` flags (with certainty 1-3 & severity)
  - Runs `auto_flag.py` deterministic script heuristics (untranslated residue,
    glossary violations, length outliers, dialogue streaks, zero-pronoun POV risk,
    ruby/bouten wordplay, and chunk risk tiers)
  - Samples a deterministic 5% random spot-audit (`[AUDIT-5%]`) of unflagged lines
    and provides `audit-report` to measure the unflagged miss rate after editing.
  - Validates XML well-formedness across chunks (`validate`).
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
import xml.etree.ElementTree as ET
from pathlib import Path

# Allow importing sibling auto_flag module
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in _sys.path:
    _sys.path.insert(0, str(_SCRIPT_DIR))

from auto_flag import (
    load_config_langs,
    load_glossary_terms,
    scan_chunk_heuristics,
)

SEVERITY_RANK = {"high": 3, "medium": 2, "low": 1, "audit": 0}


def parse_chunk_spec(spec: str):
    """Parse chunk specification like '1-8,11-20,24' into sorted integer IDs."""
    ids = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start, end = part.split("-", 1)
            ids.update(range(int(start), int(end) + 1))
        else:
            ids.add(int(part.replace("chunk", "")))
    return sorted(ids)


def discover_chunks(temp_dir: Path, chunk_spec: str = None):
    if chunk_spec:
        return parse_chunk_spec(chunk_spec)
    nums = []
    for p in sorted(temp_dir.glob("chunk*.md")):
        m = re.match(r"^chunk(\d+)\.md$", p.name)
        if m:
            nums.append(int(m.group(1)))
    return nums


def normalize_severity(item: dict) -> str:
    sev = str(item.get("severity", "")).strip().lower()
    if sev in ("high", "medium", "low"):
        return sev
    cert = item.get("certainty")
    if cert == 1:
        return "high"
    if cert == 2:
        return "medium"
    return "medium"


def cmd_dump(args):
    temp_dir = Path(args.temp_dir).resolve()
    all_chunk_files = sorted(temp_dir.glob("chunk*.md"))
    total_book_chunks = len(all_chunk_files)
    chunks = discover_chunks(temp_dir, args.chunks)

    source_lang, target_lang = load_config_langs(temp_dir)
    glossary_terms = load_glossary_terms(temp_dir)
    min_sev_rank = SEVERITY_RANK.get(args.min_severity, 1)

    lines_out = []
    total_flags = 0
    total_model_flags = 0
    total_auto_flags = 0
    total_audit_flags = 0
    flagged_chunks = 0
    high_risk_chunks = []

    snap_path = temp_dir / "audit_snapshot.json"
    existing_snap = {}
    if snap_path.exists():
        try:
            existing_snap = json.loads(snap_path.read_text(encoding="utf-8"))
        except Exception:
            existing_snap = {}
    updated_snap = dict(existing_snap)

    for c in chunks:
        cid = f"chunk{c:04d}"
        rfile = temp_dir / f"output_{cid}.review.json"
        sfile = temp_dir / f"{cid}.md"
        ofile = temp_dir / f"output_{cid}.md"

        if not sfile.exists() or not ofile.exists():
            continue

        slines = sfile.read_text(encoding="utf-8").splitlines()
        olines = ofile.read_text(encoding="utf-8").splitlines()

        # 1. Read model-reported flags from .review.json
        model_items_by_line = {}
        if rfile.exists():
            try:
                data = json.loads(rfile.read_text(encoding="utf-8"))
                for item in data.get("flagged", []):
                    ln = int(item.get("line") or item.get("line_number") or 0)
                    if ln <= 0:
                        continue
                    sev = normalize_severity(item)
                    cert = item.get("certainty")
                    cert_tag = f" (certainty={cert}/3)" if cert is not None else ""
                    issue_text = f"{item.get('issue', '')}{cert_tag}".strip()
                    model_items_by_line[ln] = {
                        "line": ln,
                        "severity": sev,
                        "detector": "model",
                        "issue": issue_text,
                    }
            except Exception as e:
                lines_out.append(f"=== CHUNK {c:04d}: ERROR reading {rfile.name}: {e} ===")

        # 2. Run deterministic script-side heuristics unless disabled
        auto_res = {
            "risk_tier": "normal",
            "risk_reasons": [],
            "flags": [],
            "audit_lines": [],
        }
        if not args.no_auto_flag:
            auto_res = scan_chunk_heuristics(
                temp_dir=temp_dir,
                chunk_num=c,
                total_chunks=total_book_chunks,
                glossary_terms=glossary_terms,
                source_lang=source_lang,
                target_lang=target_lang,
                already_flagged_lines=set(model_items_by_line.keys()),
                audit_rate=args.audit_rate,
            )

        if auto_res["risk_tier"] == "high":
            high_risk_chunks.append((cid, auto_res["risk_reasons"]))

        # Record initial baseline snapshot for audited unflagged lines (do not overwrite if already snapshotted unless --reset-audit-snapshot)
        if auto_res["audit_lines"]:
            if cid not in updated_snap or args.reset_audit_snapshot:
                updated_snap[cid] = {
                    str(ln): (olines[ln - 1] if 1 <= ln <= len(olines) else "")
                    for ln in auto_res["audit_lines"]
                }

        # 3. Merge model flags + auto_flag flags by line number
        merged_by_line = dict(model_items_by_line)
        for af in auto_res["flags"]:
            ln = af["line"]
            if ln in merged_by_line:
                # Both model and script flagged this line -> upgrade to HIGH
                existing = merged_by_line[ln]
                existing["severity"] = "high"
                existing["detector"] = "model+auto_flag"
                existing["issue"] = f"{existing['issue']} || [auto_flag:{af['category']}] {af['issue']}"
            else:
                merged_by_line[ln] = {
                    "line": ln,
                    "severity": af["severity"],
                    "detector": f"auto_flag:{af['category']}",
                    "issue": af["issue"],
                }

        # Filter by --min-severity
        filtered_items = [
            item for item in merged_by_line.values()
            if SEVERITY_RANK.get(item["severity"], 1) >= min_sev_rank
        ]

        # Count stats before adding audit lines
        for item in filtered_items:
            if "model" in item["detector"]:
                total_model_flags += 1
            if "auto_flag" in item["detector"]:
                total_auto_flags += 1

        # Add 5% random spot-audit lines
        for aln in auto_res["audit_lines"]:
            filtered_items.append({
                "line": aln,
                "severity": "audit",
                "detector": "spot_audit_5pct",
                "issue": "Random 5% unflagged spot-audit line — verify no silent pronoun, attribution, or nuance error",
            })
            total_audit_flags += 1

        if not filtered_items and auto_res["risk_tier"] != "high":
            continue

        flagged_chunks += 1
        # Sort by severity (HIGH -> MEDIUM -> LOW -> AUDIT) then by line number
        filtered_items.sort(key=lambda x: (-SEVERITY_RANK.get(x["severity"], 0), x["line"]))

        risk_badge = f" [RISK TIER: HIGH — {'; '.join(auto_res['risk_reasons'])}]" if auto_res["risk_tier"] == "high" else ""
        lines_out.append(f"=== CHUNK {c:04d} ({len(filtered_items)} flagged items){risk_badge} ===")
        for item in filtered_items:
            total_flags += 1
            ln = item["line"]
            sev_label = "AUDIT-5%" if item["severity"] == "audit" else item["severity"].upper()
            src_line = slines[ln - 1] if 1 <= ln <= len(slines) else ""
            cur_line = olines[ln - 1] if 1 <= ln <= len(olines) else ""
            lines_out.append(f"[Line {ln}] [{sev_label}] ({item['detector']}) {item['issue']}")
            lines_out.append(f"  SRC: {src_line}")
            lines_out.append(f"  CUR: {cur_line}")
            if ln > 1 and (ln - 2) < len(olines):
                lines_out.append(f"  Prev (L{ln-1}): {olines[ln-2]}")
            if 1 <= ln < len(olines):
                lines_out.append(f"  Next (L{ln+1}): {olines[ln]}")

        if auto_res["risk_tier"] == "high" and args.high_risk_mode == "full":
            lines_out.append(f"--- FULL CHUNK REVIEW: {cid} ({len(slines)} lines, High-Risk Chunk) ---")
            max_ln = max(len(slines), len(olines))
            for idx in range(max_ln):
                ln = idx + 1
                s_l = slines[idx] if idx < len(slines) else ""
                o_l = olines[idx] if idx < len(olines) else ""
                flag_tag = ""
                if ln in merged_by_line:
                    finfo = merged_by_line[ln]
                    flag_tag = f"  <-- [{finfo['severity'].upper()}: {finfo['detector']}]"
                lines_out.append(f"  L{ln:03d} SRC: {s_l}")
                lines_out.append(f"  L{ln:03d} CUR: {o_l}{flag_tag}")
        lines_out.append("")

    if updated_snap and not args.no_auto_flag and args.audit_rate > 0:
        try:
            snap_path.write_text(json.dumps(updated_snap, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
        except Exception:
            pass

    summary_lines = [
        f"Total Review Queue Items: {total_flags} across {flagged_chunks} chunks (checked {len(chunks)} chunks)",
        f"  - Breakdown: {total_model_flags} model flags | {total_auto_flags} script auto-flags | {total_audit_flags} random spot-audit lines",
        f"  - High-Risk Chunks ({len(high_risk_chunks)}): {', '.join(cid for cid, _ in high_risk_chunks) if high_risk_chunks else 'None'}",
        "",
    ]
    report = "\n".join(summary_lines) + "\n" + "\n".join(lines_out)

    if args.out:
        out_path = Path(args.out).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(report, encoding="utf-8", newline="\n")
        print(f"Wrote {total_flags} review items ({total_model_flags} model, {total_auto_flags} auto-flag, {total_audit_flags} audit) across {flagged_chunks} chunks to {out_path}")
    else:
        print(report)


def cmd_audit_report(args):
    """Compare audit_snapshot.json against current output_chunkNNNN.md to measure unflagged miss rate."""
    temp_dir = Path(args.temp_dir).resolve()
    snap_path = temp_dir / "audit_snapshot.json"
    if not snap_path.exists():
        print(f"ERROR: No audit_snapshot.json found in {temp_dir}. Run `review_flags.py dump` before Stage-2 editing first.")
        _sys.exit(1)

    snap = json.loads(snap_path.read_text(encoding="utf-8"))
    filter_chunks = set(f"chunk{c:04d}" for c in parse_chunk_spec(args.chunks)) if args.chunks else None

    total_audited = 0
    total_changed = 0
    changed_details = []

    for cid in sorted(snap.keys()):
        if filter_chunks and cid not in filter_chunks:
            continue
        ofile = temp_dir / f"output_{cid}.md"
        if not ofile.exists():
            continue
        olines = ofile.read_text(encoding="utf-8").splitlines()
        line_map = snap[cid]
        for ln_str, before_text in sorted(line_map.items(), key=lambda kv: int(kv[0])):
            ln = int(ln_str)
            total_audited += 1
            after_text = olines[ln - 1] if 1 <= ln <= len(olines) else ""
            if after_text.strip() != before_text.strip():
                total_changed += 1
                changed_details.append((cid, ln, before_text.strip(), after_text.strip()))

    miss_rate = (total_changed / total_audited * 100.0) if total_audited > 0 else 0.0
    print(f"=== Random 5% Spot-Audit Report ===")
    print(f"Audited unflagged lines checked : {total_audited}")
    print(f"Unflagged lines modified by Editor: {total_changed}")
    print(f"Measured Unflagged Miss Rate    : {miss_rate:.1f}% (threshold: {args.max_miss_rate:.1f}%)")

    if changed_details:
        print("\nModified Audit Lines:")
        for cid, ln, bef, aft in changed_details:
            print(f"  {cid} L{ln}:")
            print(f"    BEFORE: {bef}")
            print(f"    AFTER : {aft}")

    if miss_rate > args.max_miss_rate:
        print(
            f"\nWARNING: Unflagged miss rate ({miss_rate:.1f}%) exceeds {args.max_miss_rate:.1f}%! "
            f"Recommend running a full-chunk Senior Reviewer pass on High-Risk chunks or lowering --min-severity to 'low'."
        )
    else:
        print(f"\nPASS: Unflagged miss rate ({miss_rate:.1f}%) is within quality bounds (<= {args.max_miss_rate:.1f}%).")


def cmd_validate(args):
    temp_dir = Path(args.temp_dir).resolve()
    chunks = discover_chunks(temp_dir, args.chunks)
    md_italic_pat = re.compile(r"\*([^*\n]+)\*")
    errors = 0
    fixed_italics = 0

    for c in chunks:
        sfile = temp_dir / f"chunk{c:04d}.md"
        ofile = temp_dir / f"output_chunk{c:04d}.md"
        if not sfile.exists() or not ofile.exists():
            print(f"ERROR chunk{c:04d}: Missing source or output file")
            errors += 1
            continue

        stext = sfile.read_text(encoding="utf-8")
        otext = ofile.read_text(encoding="utf-8")

        if args.fix_markdown_italics and "*" in otext:
            new_otext = md_italic_pat.sub(r"<em>\1</em>", otext)
            if new_otext != otext:
                ofile.write_text(new_otext, encoding="utf-8", newline="\n")
                otext = new_otext
                fixed_italics += 1
                print(f"Fixed markdown *italics* -> <em> in output_chunk{c:04d}.md")

        slines = stext.splitlines()
        olines = otext.splitlines()
        if len(slines) != len(olines):
            print(f"WARNING chunk{c:04d}: Line count differs (source={len(slines)}, output={len(olines)})")

        try:
            ET.fromstring("<root>\n" + otext + "\n</root>")
        except Exception as e:
            print(f"ERROR chunk{c:04d}: XML well-formedness error: {e}")
            errors += 1

    print(f"Validated {len(chunks)} chunks. XML errors: {errors}. Auto-fixed markdown italics in {fixed_italics} chunks.")
    if errors > 0:
        _sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Manage, prioritize, and inspect difficulty flags (model + deterministic auto_flag + 5% spot-audit) for the Translator + Main Editor pipeline."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_dump = sub.add_parser("dump", help="Dump merged model + auto_flag + 5%% spot-audit passages sorted by severity (plus full-chunk dump for High-Risk chunks)")
    p_dump.add_argument("temp_dir", help="Path to working chunk directory")
    p_dump.add_argument("--chunks", default=None, help="Optional chunk range filter, e.g. '1-8,11-20'")
    p_dump.add_argument("--min-severity", choices=["low", "medium", "high"], default="low", help="Minimum flag severity to include (default: low = full review)")
    p_dump.add_argument("--high-risk-mode", choices=["full", "flags"], default="full", help="For High-Risk chunks, dump the entire chunk ('full', default) or only flagged lines ('flags')")
    p_dump.add_argument("--audit-rate", type=float, default=0.05, help="Fraction of unflagged lines to sample for 5%% random spot-audit (default: 0.05, 0 to disable)")
    p_dump.add_argument("--no-auto-flag", action="store_true", help="Disable deterministic script-side heuristics and dump only model .review.json flags")
    p_dump.add_argument("--reset-audit-snapshot", action="store_true", help="Overwrite existing audit_snapshot.json baseline with current chunk lines")
    p_dump.add_argument("--out", default=None, help="Optional output file path")
    p_dump.set_defaults(func=cmd_dump)

    p_audit = sub.add_parser("audit-report", help="Measure what percentage of unflagged 5%% spot-audit lines were edited by the Main Editor")
    p_audit.add_argument("temp_dir", help="Path to working chunk directory")
    p_audit.add_argument("--chunks", default=None, help="Optional chunk range filter, e.g. '1-8,11-20'")
    p_audit.add_argument("--max-miss-rate", type=float, default=10.0, help="Alert threshold percentage for unflagged edits (default: 10.0%%)")
    p_audit.set_defaults(func=cmd_audit_report)

    p_val = sub.add_parser("validate", help="Validate XML well-formedness and optionally convert *italics* to <em>")
    p_val.add_argument("temp_dir", help="Path to working chunk directory")
    p_val.add_argument("--chunks", default=None, help="Optional chunk range filter, e.g. '1-8,11-20'")
    p_val.add_argument("--fix-markdown-italics", action="store_true", help="Convert *text* to <em>text</em> inside XHTML")
    p_val.set_defaults(func=cmd_validate)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
