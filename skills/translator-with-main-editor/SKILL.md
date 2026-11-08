---
name: translator-with-main-editor
description: >-
  Translate any EPUB book or unzipped EPUB directory between ANY source and target
  language using a Translator + Main Editor Pipeline: parallel Fast Translator subagents
  (e.g. Flash) that preserve 100% of XHTML tags, apply a live glossary, and flag
  hard-to-interpret passages in `.review.json`, followed by a Frontier Main Editor
  (e.g. Argon / Pro / Opus) that resolves every flagged nuance, idiom, omitted pronoun,
  and cross-chapter consistency issue before repackaging a valid `.epub`.
---

# Translator with Main Editor (`translator-with-main-editor`)

Translate `.epub` files or unzipped EPUB directories from **any source language** (`ja`, `zh`, `ko`, `en`, `es`, `fr`, `de`, `ru`, etc.) into **any target language** (`en`, `id`, `es`, `pt`, `fr`, `zh`, `ja`, etc.) while preserving 100% of the book's original XHTML structure, illustrations, Ruby/bouten spans, CSS, and table of contents.

---

## Credits & Acknowledgments

This skill builds upon the foundational book-translation pipeline created by **[`deusyu/translate-book`](https://github.com/deusyu/translate-book)** (`manifest.py`, `glossary.py`, `chunk_context.py`, `merge_meta.py`, and `run_state.py`), extending it with:
1. **Lossless XHTML-Preserving EPUB Split & Merge (`epub_split.py`, `epub_merge.py`)**: Operates directly on `.epub` archives or unzipped EPUB folders without flattening HTML to plain Markdown, preserving every illustration, `<svg>`, CSS class, TOC anchor, and layout rule while automatically converting vertical RTL (`vrtl`) to horizontal LTR (`hltr`) when appropriate.
2. **Two-Stage Hybrid Speculative Translation Architecture (`review_flags.py` + `.review.json`)**:
   - **Stage 1 — Parallel Fast Drafters (`Model: "flash"`)**: Rapidly translate chunks in parallel with strict 1-to-1 XHTML tag parity, glossary injection, and neighbor context—while acting as scouts that flag every ambiguous pronoun, omitted subject, pun, cultural allusion, or source-text defect into `output_chunkNNNN.review.json`.
   - **Stage 2 — Frontier Senior Reviewer (Parent Agent on Argon / Pro / Opus)**: Reviews all flagged passages across the entire volume in a single unified context, refining `output_chunkNNNN.md` where deep literary and contextual reasoning matters most.

---

## Directory Structure

- `scripts/epub_split.py` — Extracts `.epub` (or reads an unzipped EPUB directory), splits translatable `.xhtml` spine files along balanced DOM block boundaries into `chunkNNNN.md`, writes `manifest.json` & `epub_map.json`, seeds `glossary.json`, and counts term frequencies across any Unicode script.
- `scripts/prep_batch.py` — Prints per-chunk glossary term tables and read-only `±300`-char neighbor context for a batch of chunks in a single call.
- `scripts/review_flags.py` — Aggregates all flagged difficult passages from `output_chunkNNNN.review.json` (`dump`) for the Senior Reviewer and validates 100% XML well-formedness (`validate --fix-markdown-italics`).
- `scripts/glossary.py` — Manages v2 `glossary.json` (`count-frequencies`, `print-terms-for-chunk`, `compute-hash`). *(From `deusyu/translate-book`)*
- `scripts/chunk_context.py` — Extracts read-only previous/next chunk excerpts. *(From `deusyu/translate-book`)*
- `scripts/run_state.py` — Tracks per-chunk translation state and plans selective re-translation (`plan`, `record`, `record-all`, `status`). *(From `deusyu/translate-book`)*
- `scripts/merge_meta.py` — Merges subagent `output_chunkNNNN.meta.json` observations into `glossary.json` (`prepare-merge`, `apply-merge`, `status`). *(From `deusyu/translate-book`)*
- `scripts/epub_merge.py` — Validates all translated chunks and XML well-formedness, updates `.xhtml`, TOC, and `.opf` metadata (`<dc:language>`, `xml:lang`, `hltr`), and packs the final `.epub` archive.

---

## Step-by-Step Workflow

### Step 1: Split the EPUB into XHTML Chunks

Run `epub_split.py` on either a `.epub` file or an unzipped EPUB directory:

```bash
python "<skill_dir>/scripts/epub_split.py" "<path/to/book.epub_or_dir>" \
  --temp-dir "<work_dir>/book_temp" \
  --source-lang "<src_lang>" \
  --target-lang "<tgt_lang>" \
  --glossary-seed "<optional_seed_glossary.json>"
```

### Step 2: Plan Selective Translation / Resume

Check which chunks need translation:

```bash
python "<skill_dir>/scripts/run_state.py" plan "<work_dir>/book_temp"
```

*(Note: If the user specifies certain chunks that were already translated by the Frontier model and must not be altered, exclude those chunk IDs from all subsequent modifications.)*

### Step 3: Stage 1 — Parallel Fast Drafters + Difficulty Flagging (`Model: "flash"`)

Process `translation_chunk_ids` in batches (e.g., 5–10 parallel subagents per batch).

1. **Prepare glossary term tables and neighbor context** for the batch:
   ```bash
   python "<skill_dir>/scripts/prep_batch.py" "<work_dir>/book_temp" chunk0001 chunk0002 chunk0003 chunk0004 chunk0005
   ```
2. **Spawn parallel subagents (`invoke_subagent` with `TypeName: "self"`, `Model: "flash"`)**.
   Give each subagent the prompt below:

#### Stage-1 Subagent Prompt Template (Draft + Flag Difficult Passages)

```markdown
Translate the XHTML fragment file `<temp_dir>/chunk<NNNN>.md` from <SOURCE_LANG> into natural, polished <TARGET_LANG> prose, and write the translated XHTML fragment to `<temp_dir>/output_chunk<NNNN>.md`.

### 1. Strict XHTML & Formatting Rules:
- **Preserve XHTML Structure 100%**: Keep every HTML/XHTML tag (`<p>`, `<h1>`, `<div>`, `<span>`, `<em>`, `<br/>`, `<hr/>`, `<figure>`, `<svg>`, `<image>`, `<img>`, `<a>`, `<b>`) and all attributes intact and in the exact same order. Match line count 1-to-1 with `chunk<NNNN>.md`.
- **Use `<em>` instead of Markdown `*italics*`**: Inside XHTML tags, always use `<em>...</em>` for italics, never raw asterisks `*...*`.
- **Valid XML**: Never leave unescaped `<` or bare `&` inside text nodes (use `&amp;`). Never wrap the file in markdown code fences (` ```html `).
- **Term Consistency Table (Mandatory)**:
{TERM_TABLE}

- **Neighbor Context (Read-Only — DO NOT translate or include in output)**:
{NEIGHBOR_CONTEXT}

### 2. Difficulty Flagging File (`<temp_dir>/output_chunk<NNNN>.review.json`):
Whenever you encounter ANY passage that is difficult to interpret—such as:
- Omitted subjects or ambiguous pronouns (e.g. Japanese/Chinese zero-pronouns or double-translation errors where the source text misattributes "I" vs. "he/she")
- Wordplay, puns, literary/cultural allusions, or idioms
- Unattributed rapid-fire dialogue or sudden narrative POV switches
- Complex worldbuilding/lore mechanics or source-text typos/OCR defects

Flag each such line in `<temp_dir>/output_chunk<NNNN>.review.json` so the Frontier Senior Reviewer can inspect and refine it:
```json
{
  "chunk_id": "chunk<NNNN>",
  "has_difficult_passages": true,
  "flagged": [
    {
      "line": 42,
      "source": "<exact source line>",
      "current_tl": "<your best draft translation>",
      "issue": "<detailed explanation of the ambiguity, pun, omitted subject, or lore nuance for the Senior Reviewer>"
    }
  ]
}
```

### 3. Meta Feedback File (`<temp_dir>/output_chunk<NNNN>.meta.json`):
Also write `<temp_dir>/output_chunk<NNNN>.meta.json`:
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
```

3. **Merge Glossary Meta Observations** after batches complete:
   ```bash
   python "<skill_dir>/scripts/merge_meta.py" prepare-merge "<work_dir>/book_temp"
   ```

### Step 4: Stage 2 — Frontier Senior Reviewer Pass (Argon / Pro / Opus)

Once Stage 1 finishes across the volume (or batch), **the main parent agent (running on the Frontier model: Argon, Pro, or Opus)** performs the Senior Editorial Review:

1. **Dump all flagged passages across the volume**:
   ```bash
   python "<skill_dir>/scripts/review_flags.py" dump "<work_dir>/book_temp" --out "<work_dir>/book_temp/all_flags.txt"
   ```
2. **Inspect & Refine**:
   - Read `all_flags.txt` (and surrounding lines in `chunkNNNN.md` / `output_chunkNNNN.md` when deeper context is needed).
   - Resolve every omitted pronoun, dialogue attribution ambiguity, pun, literary reference, TOC/header symbol alignment, and cross-chapter terminology discrepancy by directly editing `output_chunkNNNN.md`.
3. **Validate XML Well-Formedness & Auto-Fix Any Stray Markdown Italics**:
   ```bash
   python "<skill_dir>/scripts/review_flags.py" validate "<work_dir>/book_temp" --fix-markdown-italics
   ```
4. **Record All Completed Chunks**:
   ```bash
   python "<skill_dir>/scripts/run_state.py" record-all "<work_dir>/book_temp"
   ```

### Step 5: Merge In-Place & Package the Final `.epub`

1. *(Optional)* Create `<work_dir>/book_temp/toc_translations.json` mapping source chapter titles in `nav.xhtml` / `toc.ncx` to target-language chapter titles.
2. Run `epub_merge.py` to reconstruct all `.xhtml` files, update `.opf` / TOC metadata, and package the `.epub`:

```bash
python "<skill_dir>/scripts/epub_merge.py" \
  --temp-dir "<work_dir>/book_temp" \
  --target-lang "<tgt_lang>" \
  --title "<Translated Book Title>" \
  --out-epub "<output_path/Book Title (Translated).epub>"
```
