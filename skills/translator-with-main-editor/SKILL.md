---
name: translator-with-main-editor
description: >-
  Translate any EPUB book or unzipped EPUB directory between any source and target
  language using a Translator + Main Editor pipeline: parallel fast-model subagents
  draft each chunk while preserving 100% of the XHTML tags, apply a live glossary, and
  flag hard-to-interpret passages in `.review.json`; the strongest available model then
  resolves every flagged nuance, idiom, omitted pronoun, and cross-chapter consistency
  issue before the book is repackaged as a valid `.epub`. Use when the user asks to
  translate an EPUB / light novel / ebook while keeping layout, images, and TOC intact.
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
- `scripts/review_flags.py`: `dump` aggregates flagged passages from `output_chunkNNNN.review.json`; `validate` checks XML well-formedness (`--fix-markdown-italics` repairs stray `*italics*`). Both accept `--chunks '1-8,11-20'`.
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

### Step 3: Stage 1, Parallel Fast Drafters + Difficulty Flagging

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

### 2. Difficulty Flagging File (`<temp_dir>/output_chunk<NNNN>.review.json`)
Flag every passage that is hard to interpret, for example:
- omitted subjects or ambiguous pronouns (zero-pronouns in Japanese/Chinese, "I" vs "he/she" misattribution)
- wordplay, puns, literary or cultural allusions, idioms
- unattributed rapid-fire dialogue or sudden POV switches
- complex worldbuilding or lore terms, source typos or OCR defects

Format:
```json
{
  "chunk_id": "chunk<NNNN>",
  "has_difficult_passages": true,
  "flagged": [
    {
      "line": 42,
      "source": "<exact source line>",
      "current_tl": "<your best draft translation>",
      "issue": "<why this is ambiguous, for the Senior Reviewer>"
    }
  ]
}
```
If nothing is difficult, write `"has_difficult_passages": false` and `"flagged": []`.

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

The parent agent performs the editorial review once Stage 1 has finished.

1. **Dump flagged passages.** For large books, review in volume or chapter slices to avoid overflowing context:
   ```bash
   python "<skill_dir>/scripts/review_flags.py" dump "<work_dir>/book_temp" --chunks "1-20" --out "<work_dir>/book_temp/all_flags.txt"
   ```
   Repeat with the next range until every chunk is covered.
2. **Inspect and refine.** Read the dump (and surrounding lines of `chunkNNNN.md` / `output_chunkNNNN.md` when more context is needed). Resolve every omitted pronoun, attribution ambiguity, pun, allusion, header/TOC mismatch, and cross-chapter terminology discrepancy by editing `output_chunkNNNN.md` directly. Keep tags and line counts unchanged.
3. **Validate and auto-fix stray Markdown italics**:
   ```bash
   python "<skill_dir>/scripts/review_flags.py" validate "<work_dir>/book_temp" --fix-markdown-italics
   ```
   If validation still reports errors, open the named chunk, fix the offending line (usually an unescaped `&` or `<`, or a dropped closing tag), and re-run until clean.
4. **Record completed chunks**:
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
2. Draft 40 chunks in batches of 8 with fast-model subagents.
3. The reviewer dumps flags for chunks 1-20 and 21-40, fixes pronoun and pun issues, then validates.
4. Merge, writing `Novel Vol1 (Translated).epub` and reporting any chunks that needed re-translation.

## Troubleshooting

| Symptom | Action |
|---|---|
| Line count mismatch in a chunk | Re-spawn that subagent, stressing the 1-to-1 line rule |
| `validate` reports XML errors | Fix the unescaped `&`/`<` or the missing closing tag, then re-run |
| Glossary conflicts in `prepare-merge` | Decide the canonical term, then record it via `apply-merge` |
| `epub_merge.py` refuses to run | Run `run_state.py status`, finish or re-translate the listed chunks |
| Context window filling up | Use `--chunks` ranges for `dump` and `validate` |
