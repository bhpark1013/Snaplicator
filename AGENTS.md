# AGENTS.md

Guidance for AI agents working in this repository.

## Writing copy

Applies to everything a person reads: the landing page (`docs/index.html`),
the admin UI (`frontend/`), CLI output, `README.md`, and error messages.

**Say the essential thing, plainly, and stop.**

1. **One idea per sentence, and only the ideas that change what the reader
   does.** If a sentence could be deleted and the reader would still act
   correctly, delete it.

2. **Cut TMI.** Background, history, restated benefits, and "here's why this
   is nice" are noise. The reader wants to know what a thing *is*, what it
   *costs*, and what to *do next* — nothing else.

3. **Name the essence, not the symptoms.** When explaining why something works
   a certain way, give the constraint that actually forces the choice, not a
   list of pleasant properties any alternative would also have.

4. **Let structure carry meaning that words would otherwise repeat.** A
   diagram that shows a long link and a short link does not need a sentence
   saying one is continuous and the other is instant.

5. **Never invent numbers, quotes, or claims.** Every figure must be measured
   and every quotation checked against the source. If a claim cannot be
   verified, do not make it. Say what is true, including when it is a
   limitation.

6. **Plain Korean, not translationese.** Prefer ordinary words to Sino-Korean
   compounds. Write what a Korean engineer would say aloud, not a
   word-for-word rendering of the English. The same applies to Japanese.

7. **One name per referent.** Pick one term for a thing and keep it. Two words
   for one concept reads as two concepts.

Before/after, from a real edit in this repo:

```
before  A clone shares every block with the snapshot it came from and
        allocates only what it changes. Add a few and watch what the disk does.
after   A clone shares every block with its snapshot and allocates only what
        it changes.
```

The second sentence was an instruction to do something the interactive widget
right below it already invites. It went.

## Line breaking

No line may end in the middle of a word. Three separate mechanisms are in play
and they do not overlap, so all three have to hold:

- **Korean** — `:root:lang(ko) body { word-break: keep-all }`. Without it the
  browser ends a line between any two syllables and the h1 breaks as
  망가뜨 / 려도. Scoped to Korean deliberately: Japanese is *meant* to wrap
  between kana, and `keep-all` there makes every paragraph ragged. Do not
  widen the selector.
- **Hyphens** — an explicit hyphen is a break opportunity that no CSS property
  governs. Any hyphenated term in prose (`copy-on-write`, `postgresql-client`,
  `non-commercial`) must be wrapped in `<span class="nb">`. When you add copy
  with a hyphen in it, wrap it.
- **Code and URLs** — `pre.term` opts out with `word-break: normal;
  overflow-wrap: anywhere`, because a URL is one unbreakable token and has to
  break somewhere. Leave that exception alone.

Verify by measuring, not by looking. Walk the text nodes with a `Range`, find
where the rects change line, and assert that no break lands between two
non-space characters. Run it for `en` and `ko` across every view at 320, 375,
414, 768 and 1440.

## Translations

English lives in the markup; `ko` and `ja` are overlays in the `DICT` object.
When copy changes, change all three, and keep the key sets identical — the
`nav.*` keys are deliberately untranslated, and nothing else should be missing.

## Licensing

Third-party assets carry their notices in the file that ships them, because
several of the licences require it (SIL OFL for the bundled font, ISC for the
lucide icon path data). Do not strip those comments, and add one whenever you
bring in someone else's work.
