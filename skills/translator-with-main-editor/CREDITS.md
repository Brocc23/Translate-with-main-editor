# Credits & Acknowledgments

This skill builds on the book-translation pipeline created by **[`deusyu/translate-book`](https://github.com/deusyu/translate-book)**. The following scripts come from that project:

- `manifest.py`
- `glossary.py`
- `chunk_context.py`
- `merge_meta.py`
- `run_state.py`

## Extensions in this skill

1. **Lossless XHTML-preserving EPUB split and merge** (`epub_split.py`, `epub_merge.py`). They work directly on `.epub` archives or unzipped EPUB folders without flattening HTML to Markdown. Every illustration, `<svg>`, CSS class, TOC anchor, and layout rule is preserved, and vertical RTL (`vrtl`) can be converted to horizontal LTR (`hltr`) when appropriate.
2. **Two-stage hybrid translation** (`review_flags.py` and `.review.json`).
   - **Stage 1, fast drafters**: translate chunks in parallel with strict 1-to-1 tag parity, glossary injection, and neighbor context. They also flag ambiguous pronouns, omitted subjects, puns, cultural allusions, and source defects.
   - **Stage 2, senior reviewer**: the strongest available model reviews all flagged passages in one unified context and refines the translation where deep literary and contextual reasoning matters most.
