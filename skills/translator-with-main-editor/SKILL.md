---
name: translator-with-main-editor
description: >-
  Translate any EPUB book or unzipped EPUB directory between any source and target
  language using a Translator + Main Editor pipeline: parallel fast-model subagents
  draft each chunk while preserving 100% of the XHTML tags, apply a live glossary, and
  score per-line translation certainty & severity in `.review.json`; deterministic
  script heuristics (`auto_flag.py`) and a 5% random spot-audit catch overconfident
  drafting errors (untranslated residue, glossary mismatches, length outliers,
  zero-pronoun POV shifts, and unattributed dialogue streaks) before the strongest
  available model resolves every flagged nuance and packages a valid `.epub`.
---

# Translator with Main Editor (`translator-with-main-editor`)

Translate `.epub` files or unzipped EPUB directories from **any source language** into **any target language** while preserving the book's original XHTML structure, illustrations, Ruby/bouten spans, CSS, and table of contents.

Credits for the upstream project are in [CREDITS.md](CREDITS.md).

## Prerequisites

- **Python 3.9+**. All scripts use the standard library only; no `pip install` is needed.
- A **DRM-free** `.epub` file or an unzipped EPUB directory. DRM-protected books cannot be processed; ask the user for a DRM-free copy.
- Ability to spawn subagents (`invoke_subagent`). If unavailable, translate chunks sequentially yourself, following the same prompt template.
- **Model choice**: use the *fastest/cheapest available model* for Stage 1 and the *strongest available model* (the parent agent) for Stage 2. Exact names depend on the user's setup. Examples:
  - **Stage 1 (fast drafter)**: Gemini Flash, Claude Haiku, GPT mini-class models.
  - **Stage 2 (frontier editor)**: Argon, Gemini Pro, Claude Opus, or the strongest GPT model.

## Directory Structure

- `scripts/epub_split.py`: extracts the `.epub` (or reads an unzipped directory), splits translatable `.xhtml` spine files along balanced DOM block boundaries into `chunkNNNN.md`, writes `manifest.json` and `epub_map.json`, seeds `glossary.json`, and counts term frequencies in any Unicode script.
- `scripts/prep_batch.py`: prints per-chunk glossary term tables and read-only ±300-char neighbor context for a batch of chunks in one call.
- `scripts/auto_flag.py`: deterministic, zero-token script-side difficulty and error detector. Scans source + draft output for:
  1. Untranslated source-script residue (`high`)
  2. Glossary violations where a glossary source term appears on line $L$ of `chunkNNNN.md` but its canonical target is missing on line $L$ of `output_chunkNNNN.md` (`high`)
  3. Source-to-output length-ratio outliers indicating dropped clauses or hallucinated padding (`high` / `medium`)
  4. Unattributed consecutive dialogue streaks ($\ge 4$ quote-only lines in a row) (`medium`)
  5. Zero-pronoun / POV-shift ambiguity in pro-drop languages (`ja`/`zh`/`ko`) where the source has no explicit pronoun (`私/僕/俺/我/你/他/她`) but the translation injects `I/he/she` across a section boundary or gendered pronouns in dialogue (`medium`)
  6. `<ruby>`/`<rt>`, bouten (`em-sesame`), or blank-quote wordplay markup (`low`)
  7. Chunk-level **Risk Tiers** (`HIGH` for opening/closing chunks, multi-section transitions, or high new-entity/conflict counts)
  8. Deterministic **5% Random Spot-Audit** (`[AUDIT-5%]`) of unflagged lines.
- `scripts/review_flags.py`:
  - `dump`: merges Stage-1 model `.review.json` flags + `auto_flag.py` script heuristics + `5%` spot-audit lines, sorts by severity (`HIGH` $\rightarrow$ `MEDIUM` $\rightarrow$ `LOW` $\rightarrow$ `AUDIT-5%`), supports `--min-severity {low,medium,high}` and `--audit-rate`, and snapshots audited lines to `audit_snapshot.json`.
  - `audit-report`: compares `audit_snapshot.json` against edited `output_chunkNNNN.md` files after Stage 2 to compute the **Measured Unflagged Miss Rate (%)** and alerts if $>10\%$.
  - `validate`: checks XML well-formedness (`--fix-markdown-italics` repairs stray `*italics*`).
- `scripts/glossary.py`: manages the v2 `glossary.json` (`count-frequencies`, `print-terms-for-chunk`, `compute-hash`).
- `scripts/chunk_context.py`: extracts read-only previous/next chunk excerpts.
- `scripts/run_state.py`: tracks per-chunk state and plans selective re-translation (`plan`, `record`, `record-all`, `status`).
- `scripts/merge_meta.py`: merges subagent `output_chunkNNNN.meta.json` observations into `glossary.json` (`prepare-merge`, `apply-merge`, `status`).
- `scripts/epub_merge.py`: validates translated chunks, rewrites `.xhtml`, TOC, and `.opf` metadata (`<dc:language>`, `xml:lang`, `hltr`), and packs the final `.epub`.
- `resources/glossary_template.json`: example seed glossary for `--glossary-seed`.
- `resources/toc_translations_example.json`: example `toc_translations.json`.

## Step-by-Step Workflow

### Step 1: Split the EPUB into XHTML Chunks

```bash
python "<skill_dir>/scripts/epub_split.py" "<path/to/book.epub_or_dir>" \
  --temp-dir "<work_dir>/book_temp" \
  --source-lang "<src_lang>" \
  --target-lang "<tgt_lang>" \
  --glossary-seed "<optional_seed_glossary.json>"
```

If the split fails (corrupt zip, missing OPF, DRM), report the error to the user and stop; do not guess a workaround.

### Step 2: Plan Selective Translation / Resume

```bash
python "<skill_dir>/scripts/run_state.py" plan "<work_dir>/book_temp"
```

This lists the chunks that still need work, so an interrupted run resumes where it stopped. If the user says certain chunks were already translated by a strong model and must not be altered, exclude those IDs from every later step.

### Step 3: Stage 1, Parallel Fast Drafters + Structured Certainty Scoring

Process `translation_chunk_ids` in batches of 5-10 parallel subagents.

1. **Prepare glossary term tables and neighbor context** for the batch:
   ```bash
   python "<skill_dir>/scripts/prep_batch.py" "<work_dir>/book_temp" chunk0001 chunk0002 chunk0003 chunk0004 chunk0005
   ```
   The output has one section per chunk. Paste that chunk's term table into `{TERM_TABLE}` and its neighbor excerpts into `{NEIGHBOR_CONTEXT}` in the prompt below.
2. **Spawn one subagent per chunk** (`invoke_subagent`, `TypeName: "self"`, fastest available `Model`, e.g. `flash` or Haiku) with the template below.
3. **Check each result** before moving on (see *Quality gate*). Re-spawn any chunk that fails, at most twice. If it still fails, translate that chunk yourself in Stage 2.
4. **Merge glossary observations** after each batch:
   ```bash
   python "<skill_dir>/scripts/merge_meta.py" prepare-merge "<work_dir>/book_temp"
   ```
   Review the JSON proposal, then pipe your decisions into `apply-merge`:
   ```bash
   python "<skill_dir>/scripts/merge_meta.py" apply-merge "<work_dir>/book_temp" < decisions.json
   ```

#### Stage-1 Subagent Prompt Template

````markdown
Translate the XHTML fragment file `<temp_dir>/chunk<NNNN>.md` from <SOURCE_LANG> into natural, polished <TARGET_LANG> prose, and write the translated XHTML fragment to `<temp_dir>/output_chunk<NNNN>.md`.

### 1. Strict XHTML & Formatting Rules
- **Preserve XHTML structure 100%**: keep every tag (`<p>`, `<h1>`, `<div>`, `<span>`, `<em>`, `<br/>`, `<hr/>`, `<figure>`, `<svg>`, `<image>`, `<img>`, `<a>`, `<b>`) and all attributes intact and in the same order. Match the line count of `chunk<NNNN>.md` exactly, one output line per input line.
- **Use `<em>` instead of Markdown `*italics*`**. Never emit raw asterisks for emphasis.
- **Valid XML**: never leave unescaped `<` or a bare `&` in text nodes (use `&amp;`). Never wrap the file in markdown code fences.
- **Term Consistency Table (mandatory)**:
{TERM_TABLE}

- **Neighbor Context (read-only; do NOT translate or include in output)**:
{NEIGHBOR_CONTEXT}

### 2. Structured Per-Line Certainty Scoring (`<temp_dir>/output_chunk<NNNN>.review.json`)
As you translate each non-trivial line, rate your translation certainty from `1` to `3`:
- `3` = 100% certain (explicit subject/speaker, straightforward syntax, unambiguous meaning).
- `2` = Context-dependent (inferred zero-pronoun subject/object, inferred speaker turn in dialogue, idiomatic phrasing, or tone/register choice).
- `1` = Guessing / high ambiguity (wordplay/pun, cryptic lore mechanics, sudden unmarked POV shift, cultural allusion, or suspected source typo).

**Mandatory rule**: Flag **EVERY** line rated `1` or `2`. Over-flagging is strongly encouraged: a false positive costs the Main Editor only a second to verify, whereas an unflagged wrong pronoun or missed nuance ships to the final book.
Assign `"severity"`:
- `"high"`: certainty `1`, or any line where picking the wrong pronoun/speaker/term flips the factual meaning of the scene.
- `"medium"`: certainty `2` on narrative voice, dialogue turn-taking, or idiom adaptation.
- `"low"`: minor stylistic polish or emphasis/punctuation preference.

Format:
```json
{
  "chunk_id": "chunk<NNNN>",
  "has_difficult_passages": true,
  "flagged": [
    {
      "line": 42,
      "certainty": 1,
      "severity": "high",
      "source": "<exact source line>",
      "current_tl": "<your best draft translation>",
      "issue": "<why certainty is <= 2 and what the Main Editor should verify>"
    }
  ]
}
```
If every line in the chunk is `certainty: 3`, write `"has_difficult_passages": false` and `"flagged": []`.

### 3. Meta Feedback File (`<temp_dir>/output_chunk<NNNN>.meta.json`)
```json
{
  "schema_version": 1,
  "new_entities": [],
  "alias_hypotheses": [],
  "attribute_hypotheses": [],
  "used_term_sources": [],
  "conflicts": []
}
```
````

#### Quality gate (per chunk, before accepting a Stage-1 result)

- `output_chunk<NNNN>.md`, `.review.json`, and `.meta.json` all exist and the JSON parses.
- The output line count equals the input line count.
- The tag sequence matches the source (run `review_flags.py validate "<work_dir>/book_temp" --chunks N`; it also catches malformed XML).

### Step 4: Stage 2, Senior Reviewer Pass (strongest available model, e.g. Argon, Pro, or Opus)

The parent agent performs the editorial review once Stage 1 has finished. Rather than relying solely on the fast model's self-reported confusion, `review_flags.py dump` automatically runs `auto_flag.py` to combine **model flags** + **deterministic script flags** + **chunk risk tiers** + a **5% random spot-audit** (`[AUDIT-5%]`).

1. **Dump prioritized review queue.** For large books, review in volume or chapter slices to avoid overflowing context:
   ```bash
   python "<skill_dir>/scripts/review_flags.py" dump "<work_dir>/book_temp" \
     --chunks "1-20" \
     --min-severity low \
     --high-risk-mode opening-closing \
     --out "<work_dir>/book_temp/all_flags.txt"
   ```
   - Items inside each chunk are sorted by priority: `[HIGH]` $\rightarrow$ `[MEDIUM]` $\rightarrow$ `[LOW]` $\rightarrow$ `[AUDIT-5%]`.
   - By default (`--high-risk-mode opening-closing` and `--min-severity low`), the opening and closing chunks of the book include a `--- FULL CHUNK REVIEW ---` block with every line (`SRC` + `CUR`) so the Main Editor sets and closes the book's narrative voice end-to-end without drowning in full-text dumps of middle chapters (pass `--high-risk-mode full` to dump all high-risk chunks in full, or `--high-risk-mode flags` for flags only).
   - If token quota is tight, pass `--min-severity medium --high-risk-mode flags` so critical errors are resolved first.
2. **Inspect and refine.** Read the dump (and surrounding lines of `chunkNNNN.md` / `output_chunkNNNN.md` when more context is needed). Resolve every omitted pronoun, attribution ambiguity, glossary violation, pun, header/TOC mismatch, and `[AUDIT-5%]` line by editing `output_chunkNNNN.md` directly. Keep tags and line counts unchanged.
3. **Check the Measured Spot-Audit Miss Rate**:
   ```bash
   python "<skill_dir>/scripts/review_flags.py" audit-report "<work_dir>/book_temp"
   ```
   This reports what percentage of the 5% random unflagged spot-audit lines required edits from the Main Editor. If the miss rate exceeds `10.0%`, perform a full-chunk read-through of `HIGH` risk-tier chunks.
4. **Validate and auto-fix stray Markdown italics**:
   ```bash
   python "<skill_dir>/scripts/review_flags.py" validate "<work_dir>/book_temp" --fix-markdown-italics
   ```
   If validation still reports errors, open the named chunk, fix the offending line (usually an unescaped `&` or `<`, or a dropped closing tag), and re-run until clean.
5. **Record completed chunks**:
   ```bash
   python "<skill_dir>/scripts/run_state.py" record-all "<work_dir>/book_temp"
   ```

### Step 5: Merge In-Place and Package the Final `.epub`

1. *(Optional)* Create `<work_dir>/book_temp/toc_translations.json` mapping source chapter titles in `nav.xhtml` / `toc.ncx` to target-language titles (see `resources/toc_translations_example.json`).
2. Rebuild and package:
   ```bash
   python "<skill_dir>/scripts/epub_merge.py" \
     --temp-dir "<work_dir>/book_temp" \
     --target-lang "<tgt_lang>" \
     --title "<Translated Book Title>" \
     --out-epub "<output_path/Book Title (Translated).epub>"
   ```
3. **Final check**: confirm the output `.epub` exists, run `run_state.py status` and `merge_meta.py status` to confirm no chunk is pending, and, if the user has an EPUB validator such as `epubcheck` installed, run it on the result.

## Example

User: "Translate `Novel Vol1.epub` from Japanese to English."

1. Split with `--source-lang ja --target-lang en`.
2. Draft 40 chunks in batches of 8 with fast-model subagents (`certainty: 1|2` + `severity` in `.review.json`).
3. The reviewer dumps merged model + `auto_flag.py` + `5%` spot-audit flags for chunks 1-20 and 21-40, fixes pronoun/glossary/pun issues, checks `audit-report`, and validates XML.
4. Merge, writing `Novel Vol1 (Translated).epub` and reporting the final audit miss rate.

## Troubleshooting

| Symptom | Action |
|---|---|
| Line count mismatch in a chunk | Re-spawn that subagent, stressing the 1-to-1 line rule |
| `validate` reports XML errors | Fix the unescaped `&`/`<` or the missing closing tag, then re-run |
| `audit-report` miss rate > 10% | Review `[RISK TIER: HIGH]` chunks in full or re-run `dump` with `--min-severity low` |
| Glossary conflicts in `prepare-merge` | Decide the canonical term, then record it via `apply-merge` |
| `epub_merge.py` refuses to run | Run `run_state.py status`, finish or re-translate the listed chunks |
| Context window filling up | Use `--chunks` ranges and `--min-severity medium` for `dump` |
