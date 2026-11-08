#!/usr/bin/env python3
"""
auto_flag.py - Deterministic, zero-token script-side difficulty & error heuristics
for the Translator + Main Editor pipeline.

Detects objective risk signals that a fast drafting model (e.g. Flash / Haiku)
often misses because models are frequently overconfident when resolving omitted
pronouns, speaker turns, or glossary terms:
  1. Untranslated source-script residue in the target output (`high`)
     - Exempts translator notes (`TL Note:`) and censored blocks (`■■ェ■■■`).
  2. Glossary term violations where source/alias appears in source line but canonical
     target (including plural/inflected forms like Enemy -> Enemies) is missing (`high`)
  3. Source-to-output length-ratio outliers on source-script lines (`high`/`medium`)
     - Skips lines that are already predominantly Latin/English in the source.
  4. Unattributed consecutive dialogue runs (>= 6 quote-only lines with no recent
     character name attribution) (`medium`)
  5. Zero-pronoun / POV-shift ambiguity in pro-drop languages (ja/zh/ko) where output
     injects pronouns without explicit source pronouns (`medium`)
  6. Ruby (<ruby>/<rt>), bouten (em-sesame), or special bracketed wordplay (`low`)
  7. Chunk-level Risk Tiers (opening/closing chunks, heavy section transitions, glossary conflicts)
  8. Deterministic 5% Random Spot-Audit sampling of unflagged lines to measure miss rate.
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
import statistics
from pathlib import Path

TAG_RE = re.compile(r'<[^>]+>')
WHITESPACE_RE = re.compile(r'\s+')

# Script character ranges
CJK_UNIFIED_RE = re.compile(r'[\u3400-\u4dbf\u4e00-\u9fff]')
HIRAGANA_KATAKANA_RE = re.compile(r'[\u3040-\u309f\u30a0-\u30ff]')
HANGUL_RE = re.compile(r'[\uac00-\ud7af\u1100-\u11ff]')
CYRILLIC_RE = re.compile(r'[\u0400-\u04ff]')
ARABIC_RE = re.compile(r'[\u0600-\u06ff]')
LATIN_ALPHA_RE = re.compile(r'[A-Za-z]')

# Intentional untranslated exemptions (TL notes & censored blocks like ■■ェ■■■)
TL_NOTE_RE = re.compile(r'(?:\bTL\s*Note\b|\bTranslator\'?s?\s*Note\b|\bT/N\b)', re.IGNORECASE)
CENSOR_BLOCK_RE = re.compile(r'[■□▲△●○×✕◆◇]{1,}[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]*[■□▲△●○×✕◆◇]{1,}')

# Dialogue & structural patterns
DIALOGUE_OPEN_RE = re.compile(r'^\s*(?:「|『|“|"|\'|—|―|–)')
DIALOGUE_CLOSE_SRC_RE = re.compile(r'(?:」|』|”|")\s*$')

# Explicit source pronouns in ZH / JA / KO (Korean uses particle/boundary anchors to avoid matching inside words)
ZH_PRONOUN_RE = re.compile(r'(?:我|你|您|他|她|它|咱们|我们|你们|他们|她们|自己|本人|那家伙|这家伙)')
JA_PRONOUN_RE = re.compile(r'(?:私|僕|俺|あたし|わたし|ぼく|おれ|彼|彼女|あなた|君|きみ|あんた|自分|彼ら)')
KO_PRONOUN_RE = re.compile(
    r'(?:^|[\s「『“"\'(\[])'
    r'(?:(?:나|저|너|그|그녀|우리|저희|그들|자신)(?:는|은|가|이|도|만|를|을|의|에게|한테|께|랑|와|과)(?=$|[\s,.!?」』”"\')\]]))'
    r'|(?:^|[\s「『“"\'(\[])(?:내|제|네)\s+'
)

# English / Latin target pronouns that often get hallucinated or misattributed from zero-pronoun sources
EN_THIRD_OR_FIRST_PRONOUN_RE = re.compile(r'\b(?:I|my|me|myself|he|him|his|himself|she|her|hers|herself|they|them|their)\b', re.IGNORECASE)
EN_GENDERED_PRONOUN_RE = re.compile(r'\b(?:he|him|his|himself|she|her|hers|herself)\b', re.IGNORECASE)

# Wordplay / special emphasis markup in source
RUBY_OR_BOUTEN_RE = re.compile(r'(?:<ruby\b|<rt\b|class="[^"]*em-sesame[^"]*"|「　+」|『　+』)', re.IGNORECASE)
SCENE_BREAK_OR_HEADER_RE = re.compile(r'(?:id="toc-[^"]+"|<h[1-6]\b|<hr\b|[◆◇■□●○★☆]{1,2})', re.IGNORECASE)


def strip_tags(line: str) -> str:
    return TAG_RE.sub('', line).strip()


def load_config_langs(temp_dir: Path):
    source_lang = "auto"
    target_lang = "en"
    epub_map_path = temp_dir / "epub_map.json"
    if epub_map_path.exists():
        try:
            data = json.loads(epub_map_path.read_text(encoding="utf-8"))
            source_lang = data.get("source_lang", source_lang)
            target_lang = data.get("target_lang", target_lang)
        except Exception as e:
            print(f"WARNING: Failed to read {epub_map_path}: {e}", file=_sys.stderr)
    cfg_path = temp_dir / "config.txt"
    if cfg_path.exists():
        for line in cfg_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("source_lang="):
                source_lang = line.split("=", 1)[1].strip()
            elif line.startswith("output_lang="):
                target_lang = line.split("=", 1)[1].strip()
    return source_lang.lower(), target_lang.lower()


def load_glossary_terms(temp_dir: Path):
    gpath = temp_dir / "glossary.json"
    if not gpath.exists():
        return []
    try:
        data = json.loads(gpath.read_text(encoding="utf-8"))
        terms = data.get("terms", [])
        if isinstance(terms, dict):
            return list(terms.values())
        if isinstance(terms, list):
            return terms
    except Exception as e:
        print(f"WARNING: Failed to parse glossary at {gpath}: {e}", file=_sys.stderr)
    return []


def _normalize_en_word(w: str) -> str:
    """Normalize an English word to its singular/base stem for plural-tolerant glossary matching."""
    w = re.sub(r"(?:'s|s')$", "", w.lower().strip(".,!?;:\"'()[]{}—–-"))
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 4 and w.endswith("ves"):
        return w[:-3] + "f"
    if len(w) > 4 and (w.endswith("ses") or w.endswith("xes") or w.endswith("zes") or w.endswith("ches") or w.endswith("shes")):
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss") and not w.endswith("us"):
        return w[:-1]
    return w


def glossary_target_matches(tgt_term: str, oplain: str) -> bool:
    """Check if `tgt_term` (or its plural/inflected form, e.g. 'Enemies of the World' for 'Enemy of the World') appears in `oplain`."""
    oplain_low = oplain.lower()
    if tgt_term.lower() in oplain_low:
        return True
    core_tgt = re.sub(r'^(?:the|a|an)\s+', '', tgt_term, flags=re.IGNORECASE).strip()
    if core_tgt.lower() in oplain_low:
        return True

    # Build plural/possessive regex for core_tgt (handles multi-word phrases like "Enemy of the World" -> "Enemies of the World")
    tgt_words = [w for w in re.split(r'\W+', core_tgt) if len(w) >= 3 and w.lower() not in ("the", "and", "for", "from", "with")]
    if not tgt_words:
        return True
    out_words_norm = {_normalize_en_word(w) for w in re.split(r'\W+', oplain) if w}
    if all(_normalize_en_word(tw) in out_words_norm for tw in tgt_words):
        return True
    return False


def count_source_script_chars(text: str, source_lang: str) -> int:
    """Count characters belonging to the non-Latin source script (or non-ASCII if auto)."""
    if source_lang.startswith(("zh", "ja", "auto")):
        cjk_kana = len(CJK_UNIFIED_RE.findall(text)) + len(HIRAGANA_KATAKANA_RE.findall(text))
        if cjk_kana > 0 or source_lang.startswith(("zh", "ja")):
            return cjk_kana
    if source_lang.startswith("ko"):
        return len(HANGUL_RE.findall(text))
    if source_lang.startswith(("ru", "uk", "bg")):
        return len(CYRILLIC_RE.findall(text))
    if source_lang.startswith(("ar", "fa", "ur")):
        return len(ARABIC_RE.findall(text))
    return len(text)


def detect_untranslated_residue(text: str, source_lang: str, target_lang: str):
    """Return a description of untranslated source script characters if found in target text."""
    if not text:
        return None
    # Exempt translator notes
    if TL_NOTE_RE.search(text):
        return None
    # Strip censored blocks such as ■■ェ■■■ before checking residue
    cleaned = CENSOR_BLOCK_RE.sub('', text)

    tgt_is_cjk = target_lang.startswith(("zh", "ja"))
    tgt_is_ko = target_lang.startswith("ko")
    tgt_is_cyr = target_lang.startswith(("ru", "uk", "bg"))
    tgt_is_ar = target_lang.startswith(("ar", "fa", "ur"))

    if not tgt_is_cjk:
        cjk_matches = CJK_UNIFIED_RE.findall(cleaned)
        kana_matches = HIRAGANA_KATAKANA_RE.findall(cleaned)
        if cjk_matches or kana_matches:
            sample = "".join((cjk_matches + kana_matches)[:6])
            return f"Untranslated CJK/Kana characters left in output: '{sample}'"
    elif target_lang.startswith("zh"):
        kana_matches = HIRAGANA_KATAKANA_RE.findall(cleaned)
        if len(kana_matches) >= 2:
            sample = "".join(kana_matches[:6])
            return f"Untranslated Japanese Kana left in Chinese output: '{sample}'"

    if not tgt_is_ko:
        ko_matches = HANGUL_RE.findall(cleaned)
        if ko_matches:
            sample = "".join(ko_matches[:6])
            return f"Untranslated Korean Hangul characters left in output: '{sample}'"

    if not tgt_is_cyr and source_lang.startswith(("ru", "uk", "bg")):
        cyr_matches = CYRILLIC_RE.findall(cleaned)
        if cyr_matches:
            sample = "".join(cyr_matches[:6])
            return f"Untranslated Cyrillic characters left in output: '{sample}'"

    if not tgt_is_ar and source_lang.startswith(("ar", "fa", "ur")):
        ar_matches = ARABIC_RE.findall(cleaned)
        if ar_matches:
            sample = "".join(ar_matches[:6])
            return f"Untranslated Arabic script characters left in output: '{sample}'"

    return None


def is_pure_dialogue_line(src_plain: str, tgt_plain: str) -> bool:
    """True if the line is purely spoken dialogue without narrative attribution outside quotes."""
    if not src_plain or not tgt_plain:
        return False
    if DIALOGUE_OPEN_RE.match(src_plain) and DIALOGUE_CLOSE_SRC_RE.search(src_plain):
        inner = src_plain.strip()[1:-1]
        if "」" not in inner and "』" not in inner:
            return True
    if tgt_plain.startswith('"') and tgt_plain.endswith('"') and tgt_plain.count('"') == 2:
        return True
    return False


def has_source_pronoun(src_plain: str, source_lang: str) -> bool:
    if source_lang.startswith("ja"):
        return bool(JA_PRONOUN_RE.search(src_plain))
    if source_lang.startswith("ko"):
        return bool(KO_PRONOUN_RE.search(src_plain))
    return bool(ZH_PRONOUN_RE.search(src_plain) or JA_PRONOUN_RE.search(src_plain))


def compute_chunk_risk_tier(chunk_num: int, total_chunks: int, slines: list, meta_path: Path):
    """Determine if a chunk is High-Risk (opening/closing chunk, heavy multi-scene transitions, or glossary conflicts)."""
    reasons = []
    is_opening_or_closing = False
    if chunk_num == 1:
        reasons.append("Opening chunk of volume")
        is_opening_or_closing = True
    if total_chunks > 2 and chunk_num == total_chunks:
        reasons.append("Closing chunk of volume")
        is_opening_or_closing = True

    header_count = sum(1 for l in slines if SCENE_BREAK_OR_HEADER_RE.search(l))
    if header_count >= 4:
        reasons.append(f"Contains {header_count} section/chapter transitions (high POV-switch density)")

    if meta_path.exists():
        try:
            mdata = json.loads(meta_path.read_text(encoding="utf-8"))
            new_ents = len(mdata.get("new_entities", []))
            conflicts = len(mdata.get("conflicts", []))
            if new_ents >= 6:
                reasons.append(f"Introduces {new_ents} new entities")
            if conflicts > 0:
                reasons.append(f"Reports {conflicts} glossary conflicts")
        except Exception:
            pass

    return ("high" if reasons else "normal"), reasons, is_opening_or_closing


def scan_chunk_heuristics(
    temp_dir: Path,
    chunk_num: int,
    total_chunks: int = 0,
    glossary_terms: list = None,
    source_lang: str = "auto",
    target_lang: str = "en",
    already_flagged_lines: set = None,
    audit_rate: float = 0.05,
):
    """Scan a single chunk (chunkNNNN.md + output_chunkNNNN.md) and return heuristic flags + audit sample."""
    cid = f"chunk{chunk_num:04d}"
    sfile = temp_dir / f"{cid}.md"
    ofile = temp_dir / f"output_{cid}.md"
    mfile = temp_dir / f"output_{cid}.meta.json"

    if not sfile.exists() or not ofile.exists():
        return {
            "chunk_id": cid,
            "risk_tier": "high",
            "is_opening_or_closing": False,
            "risk_reasons": ["Missing source or output"],
            "flags": [],
            "audit_lines": [],
        }

    slines = sfile.read_text(encoding="utf-8").splitlines()
    olines = ofile.read_text(encoding="utf-8").splitlines()
    if glossary_terms is None:
        glossary_terms = load_glossary_terms(temp_dir)
    if already_flagged_lines is None:
        already_flagged_lines = set()

    risk_tier, risk_reasons, is_opening_or_closing = compute_chunk_risk_tier(chunk_num, total_chunks, slines, mfile)

    # Pre-filter glossary terms that actually appear anywhere in this chunk
    full_src_text = "\n".join(slines)
    active_terms = []
    character_target_names = set()
    for t in glossary_terms:
        if not isinstance(t, dict):
            continue
        src_term = t.get("source", "").strip()
        tgt_term = t.get("target", "").strip()
        if not src_term or not tgt_term:
            continue
        if src_term in full_src_text:
            active_terms.append(t)
            first_word = tgt_term.split()[0].strip(".,!?;:\"'()")
            if len(first_word) >= 3 and first_word[0].isupper():
                character_target_names.add(first_word.lower())

    # Compute chunk median length ratio on lines that actually have substantial source-script characters
    ratios = []
    max_lines = min(len(slines), len(olines))
    for i in range(max_lines):
        sp = strip_tags(slines[i])
        tp = strip_tags(olines[i])
        src_script_cnt = count_source_script_chars(sp, source_lang)
        if len(sp) >= 15 and src_script_cnt >= 10 and len(tp) >= 5:
            ratios.append(len(tp) / len(sp))
    median_ratio = statistics.median(ratios) if len(ratios) >= 5 else 2.2

    flags_by_line = {}

    def add_flag(ln: int, severity: str, category: str, reason: str):
        sev_rank = {"high": 3, "medium": 2, "low": 1, "audit": 0}
        if ln not in flags_by_line:
            flags_by_line[ln] = {
                "line": ln,
                "severity": severity,
                "category": category,
                "source_detector": "auto_flag",
                "reasons": [reason],
            }
        else:
            existing = flags_by_line[ln]
            if reason not in existing["reasons"]:
                existing["reasons"].append(reason)
            if sev_rank.get(severity, 0) > sev_rank.get(existing["severity"], 0):
                existing["severity"] = severity
                existing["category"] = category

    dialogue_streak = 0
    recent_scene_break = True

    for idx in range(max_lines):
        ln = idx + 1
        sraw = slines[idx]
        oraw = olines[idx]
        splain = strip_tags(sraw)
        oplain = strip_tags(oraw)

        if SCENE_BREAK_OR_HEADER_RE.search(sraw):
            recent_scene_break = True
            dialogue_streak = 0

        if not splain and not oplain:
            continue

        # 1. Untranslated source-script residue (HIGH)
        residue = detect_untranslated_residue(oplain, source_lang, target_lang)
        if residue:
            add_flag(ln, "high", "untranslated_residue", residue)

        # 2. Glossary violation check (HIGH, plural/inflection tolerant)
        for t in active_terms:
            src_term = t.get("source", "")
            tgt_term = t.get("target", "")
            if src_term in splain:
                if not glossary_target_matches(tgt_term, oplain):
                    # Check if any approved alias/note variant (e.g. 'door' when 门扉 is used as a physical door rather than 《门扉》)
                    if src_term == "门扉" and f"《{src_term}》" not in splain and re.search(r'\bdoors?\b', oplain, re.IGNORECASE):
                        continue
                    add_flag(
                        ln,
                        "high",
                        "glossary_violation",
                        f"Glossary term '{src_term}' -> expected '{tgt_term}', not found in translated line",
                    )

        # 3. Length-ratio outlier (HIGH / MEDIUM) — skip lines already in English/Latin in the source
        src_script_cnt = count_source_script_chars(splain, source_lang)
        latin_cnt = len(LATIN_ALPHA_RE.findall(splain))
        if len(splain) >= 20 and src_script_cnt >= 12 and latin_cnt < src_script_cnt:
            ratio = len(oplain) / max(len(splain), 1)
            if ratio < median_ratio * 0.35:
                add_flag(
                    ln,
                    "high",
                    "length_outlier",
                    f"Abnormally short translation (ratio={ratio:.2f} vs chunk median={median_ratio:.2f}) — possible dropped clause",
                )
            elif ratio > median_ratio * 2.8 and len(oplain) > 120 and not TL_NOTE_RE.search(oplain):
                add_flag(
                    ln,
                    "medium",
                    "length_outlier",
                    f"Abnormally long translation (ratio={ratio:.2f} vs chunk median={median_ratio:.2f}) — possible padding or injected note",
                )

        # 4. Unattributed consecutive dialogue runs (MEDIUM) — raised threshold (>= 6) & checks if recent lines name a speaker
        if is_pure_dialogue_line(splain, oplain):
            dialogue_streak += 1
            if dialogue_streak == 6 or (dialogue_streak > 6 and dialogue_streak % 5 == 1):
                # Check if any of the previous 3 lines inside the streak mentioned a character name to anchor turn-taking
                recent_window = " ".join(strip_tags(olines[max(0, idx - 3):idx + 1][j]) for j in range(len(olines[max(0, idx - 3):idx + 1]))).lower()
                has_name_anchor = any(cname in recent_window for cname in character_target_names)
                if not has_name_anchor:
                    add_flag(
                        ln,
                        "medium",
                        "dialogue_run",
                        f"Line {dialogue_streak} of a long unattributed dialogue streak with no character names — verify speaker turn-taking",
                    )
        else:
            dialogue_streak = 0

        # 5. Zero-pronoun / POV-shift risk in CJK sources (MEDIUM)
        is_cjk_src = source_lang in ("auto", "zh", "ja", "ko") or bool(CJK_UNIFIED_RE.search(splain))
        if is_cjk_src and len(splain) >= 12 and src_script_cnt >= 8:
            if not has_source_pronoun(splain, source_lang):
                if recent_scene_break and EN_THIRD_OR_FIRST_PRONOUN_RE.search(oplain):
                    add_flag(
                        ln,
                        "medium",
                        "zero_pronoun_pov",
                        "Zero-pronoun source right after chunk/section boundary where translation supplies subject pronoun — verify narrator/POV",
                    )
                    recent_scene_break = False
                elif is_pure_dialogue_line(splain, oplain) and EN_GENDERED_PRONOUN_RE.search(oplain):
                    add_flag(
                        ln,
                        "medium",
                        "zero_pronoun_dialogue",
                        "Zero-pronoun spoken line where translation supplies gendered pronoun (he/she/him/her) — verify referent",
                    )
            else:
                if len(splain) >= 15 and not SCENE_BREAK_OR_HEADER_RE.search(sraw):
                    recent_scene_break = False

        # 6. Ruby, bouten emphasis, or blank-quote wordplay markup (LOW)
        if RUBY_OR_BOUTEN_RE.search(sraw):
            add_flag(
                ln,
                "low",
                "markup_nuance",
                "Contains <ruby>, bouten (em-sesame), or special bracketed wordplay — verify emphasis/reading nuance",
            )

    # 8. Deterministic 5% Random Spot-Audit of unflagged non-trivial lines
    all_flagged_set = set(already_flagged_lines) | set(flags_by_line.keys())
    eligible_for_audit = []
    for idx in range(max_lines):
        ln = idx + 1
        if ln in all_flagged_set:
            continue
        splain = strip_tags(slines[idx])
        oplain = strip_tags(olines[idx])
        if len(splain) >= 15 and len(oplain) >= 15:
            eligible_for_audit.append(ln)

    audit_lines = []
    if audit_rate > 0 and eligible_for_audit:
        target_audit_count = max(1, round(len(eligible_for_audit) * audit_rate))
        scored = sorted(
            eligible_for_audit,
            key=lambda ln: hashlib.sha256(f"{cid}:{ln}".encode("utf-8")).hexdigest(),
        )
        audit_lines = sorted(scored[:target_audit_count])

    heuristic_list = []
    for ln in sorted(flags_by_line.keys()):
        item = flags_by_line[ln]
        heuristic_list.append({
            "line": ln,
            "severity": item["severity"],
            "category": item["category"],
            "source_detector": "auto_flag",
            "source": slines[ln - 1] if 1 <= ln <= len(slines) else "",
            "current_tl": olines[ln - 1] if 1 <= ln <= len(olines) else "",
            "issue": " | ".join(item["reasons"]),
        })

    return {
        "chunk_id": cid,
        "risk_tier": risk_tier,
        "is_opening_or_closing": is_opening_or_closing,
        "risk_reasons": risk_reasons,
        "median_length_ratio": round(median_ratio, 2),
        "flags": heuristic_list,
        "audit_lines": audit_lines,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Run deterministic script-side difficulty and error heuristics on translated chunks."
    )
    parser.add_argument("temp_dir", help="Path to working chunk directory")
    parser.add_argument("--chunks", default=None, help="Optional chunk range, e.g. '1-8,11-20'")
    parser.add_argument("--audit-rate", type=float, default=0.05, help="Fraction of unflagged lines to sample for spot-audit (default: 0.05)")
    parser.add_argument("--write-audit-snapshot", action="store_true", help="Save snapshot of audited lines to audit_snapshot.json for later miss-rate calculation")
    parser.add_argument("--json", action="store_true", help="Emit JSON output")
    args = parser.parse_args()

    temp_dir = Path(args.temp_dir).resolve()
    source_lang, target_lang = load_config_langs(temp_dir)
    glossary_terms = load_glossary_terms(temp_dir)

    all_chunk_files = sorted(temp_dir.glob("chunk*.md"))
    total_chunks = len(all_chunk_files)

    if args.chunks:
        chunk_nums = []
        for part in args.chunks.split(","):
            part = part.strip()
            if "-" in part:
                s, e = part.split("-", 1)
                chunk_nums.extend(range(int(s), int(e) + 1))
            elif part:
                chunk_nums.append(int(part.replace("chunk", "")))
        chunk_nums = sorted(set(chunk_nums))
    else:
        chunk_nums = [int(re.match(r"^chunk(\d+)\.md$", p.name).group(1)) for p in all_chunk_files if re.match(r"^chunk(\d+)\.md$", p.name)]

    results = []
    total_flags = 0
    total_audit = 0

    for c in chunk_nums:
        rfile = temp_dir / f"output_chunk{c:04d}.review.json"
        model_flagged = set()
        if rfile.exists():
            try:
                rdata = json.loads(rfile.read_text(encoding="utf-8"))
                for item in rdata.get("flagged", []):
                    ln = item.get("line") or item.get("line_number")
                    if ln:
                        model_flagged.add(int(ln))
            except Exception:
                pass

        res = scan_chunk_heuristics(
            temp_dir=temp_dir,
            chunk_num=c,
            total_chunks=total_chunks,
            glossary_terms=glossary_terms,
            source_lang=source_lang,
            target_lang=target_lang,
            already_flagged_lines=model_flagged,
            audit_rate=args.audit_rate,
        )
        results.append(res)
        total_flags += len(res["flags"])
        total_audit += len(res["audit_lines"])

    if args.write_audit_snapshot:
        snap = {}
        for r in results:
            cid = r["chunk_id"]
            ofile = temp_dir / f"output_{cid}.md"
            olines = ofile.read_text(encoding="utf-8").splitlines() if ofile.exists() else []
            snap[cid] = {
                str(ln): (olines[ln - 1] if 1 <= ln <= len(olines) else "")
                for ln in r["audit_lines"]
            }
        snap_path = temp_dir / "audit_snapshot.json"
        snap_path.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")

    if args.json:
        print(json.dumps({"total_auto_flags": total_flags, "total_audit_lines": total_audit, "chunks": results}, ensure_ascii=False, indent=2))
    else:
        print(f"Auto-Flag Scan Complete: {total_flags} heuristic flags + {total_audit} spot-audit lines across {len(results)} chunks.")
        for r in results:
            if r["flags"] or r["risk_tier"] == "high":
                print(f"  {r['chunk_id']} [risk={r['risk_tier'].upper()}]: {len(r['flags'])} auto-flags, {len(r['audit_lines'])} audit lines")


if __name__ == "__main__":
    main()
