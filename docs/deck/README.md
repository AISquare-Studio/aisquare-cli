# The decks

Client-facing collateral for `aisquare` / `asq`, in three lengths. Each deck is
**one HTML file with no external reference of any kind** — the CSS, the diagrams,
the terminal captures and the three typefaces are all inside it — so it opens
from `file://`, from a locked-down laptop, or in three years, and looks the same.
Open one in a browser; there is nothing to build and nothing to serve.

| | Read it | Send it | What it is |
| --- | --- | --- | --- |
| **One-pager** | [`aisquare-one-pager.html`](aisquare-one-pager.html) | [`.pdf`](aisquare-one-pager.pdf) — 1 page, A3 landscape | The leave-behind. Claim, the loop, one capture, eight assurances, roadmap, the ask. |
| **Short deck** | [`aisquare-short-deck.html`](aisquare-short-deck.html) | [`.pdf`](aisquare-short-deck.pdf) — 5 pages, A4 | The fifteen-minute version, on a numbered page rail. |
| **Pitch deck** | [`aisquare-pitch-deck.html`](aisquare-pitch-deck.html) | [`.pdf`](aisquare-pitch-deck.pdf) — 15 pages, 16:9 | For presenting. Arrow keys, space, or the prev/next control; a progress bar and a slide counter that names the current slide. |

## Printing them

Each HTML carries its own print stylesheet, so the browser's Print dialog is not
paginating the scroll layout — it is following a layout authored for paper. For
the short deck and the pitch deck, printing the HTML reproduces the committed PDF
exactly: one leaf or one slide per page, no element straddling a fold.

**The one-pager is the exception, and deliberately.** Its web page is ~3,700 px
tall at full measure, which is four A-series pages — no honest single sheet holds
it. So `aisquare-one-pager.pdf` is a *separate* document: a single A3 sheet built
for print, carrying the same spine with a wide, short variant of the loop diagram
in place of the tall one, and without the full-bleed capture. Printing
`aisquare-one-pager.html` gives you a clean four-page A4 document instead. Both
are correct; they are just not the same artefact.

## What is real in the screenshots, and what is not

This matters if you reuse a capture, or if a client asks.

- **The board capture is real data.** The shipping CLI was driven through an
  actual sequence — five contract-carrying tasks written, two claimed, one sent
  to review, one reopened by the tester with its reason, one question routed to
  the planner, one signal set — and that is what the board rendered.
- **The doctor capture uses the real check names** from
  [`../../src/aisquare/services/diagnostics.py`](../../src/aisquare/services/diagnostics.py),
  with plausible details: eighteen green and one honest amber for an optional
  component that is not on the machine.
- **The project rows and the two session panes are a worked example.** Three
  fictional repositories and a scripted manager and coder transcript — spawning
  live agents for a screenshot would have cost tokens and touched a real
  repository. Every caption in every deck says so on the page.

Every capture is a real render of the real UI: they were produced by driving
[`src/aisquare/cli/ui/`](../../src/aisquare/cli/ui/) and the board TUI headless
through Textual's pilot and exporting SVG, so the layout, the role icons, the
state chips and the theme are the shipping code's, not a mockup's.

## How they are kept honest

The decks are built from templates by a small generator that lives outside this
repository, alongside four checks that run against the built files:

- every page element is measured against its page box, so a page break is
  verified rather than hoped for — the tightest page in the pitch deck closes
  with 13 px to spare;
- every `pre`/`nowrap` block is checked for horizontal overflow, because such a
  block scrolls on screen and **crops silently on paper** — this found the
  one-line installer losing `.sh | sh` off the right edge of two pages;
- every label in a hand-drawn figure is checked against its own `viewBox`;
- every character the decks use is checked against the embedded faces, so no
  word can render part of itself in a fallback font.

If you edit a deck by hand, the thing to watch is that last one: the files embed
each typeface subset to the glyphs the decks actually use, so a character you add
may have no glyph. Box drawing and the role emoji are the known and accepted
exceptions — no text face carries them, they come from the reader's system fonts
exactly as they would in a terminal, and their advance inside a capture is pinned
by `textLength`, so a substituted face cannot shift that layout.

Martian Mono, IBM Plex Sans and JetBrains Mono are embedded under the
[SIL Open Font License 1.1](https://openfontlicense.org/).
