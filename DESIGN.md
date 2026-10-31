---
version: alpha
name: Scholar System
description: Assessment and standards-mastery platform for MindAccel University; it should feel like the exam room's own paperwork, exact and calm, never like a startup dashboard.
colors:
  paper: "#FFFFFF"      # the ground of every screen
  graphite: "#1F2123"   # body text, primary actions, filled bubbles
  muted: "#5A6157"      # secondary text (6.4:1 on paper)
  dropout: "#3B6620"    # form ink: rules, column headers, field labels, standard codes (6.8:1 on paper)
  band: "#EDF3E3"       # dropout tint: table header bands, selected rows, active tab
  rule: "#C6D6B0"       # light form rules
  redpen: "#A8231A"     # the grader's red pen: needs-grading counts, wrong answers, errors, destructive actions
  redpen-tint: "#FBE6E3"
  pencil: "#F9E3A0"     # #2-pencil yellow: "approaching" mastery fill (text #5A4300)
  mastered: "#D5E8C0"   # mastered fill (text #2C5216)
  notyet: "#F5D3CE"     # not-yet fill (text #8A1C14)
  chart-mastered: "#4F8A2B"     # chart marks for mastery levels (validated as a 3-hue set on paper,
  chart-approaching: "#C99A06"  # CVD ΔE ≥ 9; approaching is < 3:1, so segments always carry
  chart-notyet: "#A8231A"       # their count and every chart has a table beside it)
typography:
  ui:
    fontFamily: Public Sans (variable, self-hosted, OFL)
    fontSize: 15px
    lineHeight: 1.5
  exam:
    fontFamily: STIX Two Text (variable, self-hosted, OFL; latin, latin-ext, greek)
    fontSize: 18px
    lineHeight: 1.55
  title:
    fontFamily: Public Sans
    fontSize: 30px
    fontWeight: 750
    letterSpacing: -0.01em
rounded:
  none: 0
  field: 2px
  bubble: 50%
spacing:
  1: 4px
  2: 8px
  3: 12px
  4: 16px
  5: 24px
  6: 40px
  7: 64px
components:
  button-primary:
    backgroundColor: "{colors.graphite}"
    textColor: "{colors.paper}"
    rounded: "{rounded.field}"
    padding: 8px 16px
  button-secondary:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.graphite}"
    border: 1.5px solid graphite
  bubble:
    size: 26px (choices), 30px (navigator), 10px (status marks)
    empty: 1.5px dropout ring, letter in dropout
    filled: graphite fill, paper letter
---

## Overview

Scholar is used by instructors building and grading tests and by students sitting them, at MindAccel University. Its look comes from the paperwork of the exam room: optical answer sheets printed in a pale *dropout* green that scanners ignore, the graphite of a #2 pencil, the red pen of the grader, and exam papers typeset in a scientific serif. Screens are laid out like printed forms, not like a stack of floating cards.

## Colors

- **Paper (#FFFFFF):** forms are white; so is every screen. No tinted page ground, no cards lifted off it.
- **Graphite (#1F2123):** all body text and the primary action, the way a pencil mark is the one thing that counts on a sheet (16:1).
- **Muted (#5A6157):** secondary text, a green-leaning grey from the form stock (6.4:1).
- **Dropout (#3B6620) and band (#EDF3E3):** the form's printing. Rules, column-header bands, field labels, standard codes. It is structure, never a call to action (6.8:1 on paper, 6.0:1 on band).
- **Red pen (#A8231A):** one job: something a grader must look at or that is wrong. Pending-grading counts, incorrect answers in review, errors, destructive buttons. Never decoration.
- **Mastery scale:** mastered (green fill), approaching (#2-pencil yellow), not yet (red-pen tint), no evidence (plain paper with a dot). Each text/fill pair is ≥ 6.6:1.

## Typography

- **Public Sans** (designed for US government forms and sites) for every piece of interface: navigation, tables, labels, buttons, numbers (tabular figures).
- **STIX Two Text** (designed for scientific publishing, with Greek for math) for anything that is *the exam itself*: question stems, answer choices, student answers, instructions, rubrics, and the printed booklet.
- Scale (root 15px): 13 small labels, 15 body, 18 exam text, 20 section titles, 30 page titles. Page titles are the only loud type: 750 weight, slightly tight.
- Sentence case everywhere. No all-caps labels, no letter-spaced eyebrows.

## Layout

- A form header block on every page: breadcrumb, a large title, one plain-language line of facts, actions on the right, then a ruled tab strip.
- Sections are not boxes. A section is a title on a 2px dropout rule; its content sits on the paper beneath. Tables are the main structure (rosters, gradebooks, results) with a green header band and light rules.
- The exam (take, review, print) reads like a printed paper: question numbers hang in the left margin, stems in the serif, choices as answer bubbles.
- Widths: full width for tables and reports; 760px for forms and single-column reading (login, account, start, review).

## Elevation & Depth

Flat. Paper does not float. The only raised element is the sticky bar while sitting a test, separated by a rule, not a shadow.

## Shapes

0 radius for sections and tables, 2px for fields and buttons, full circles for bubbles. Nothing else is rounded.

## Components

- **Answer bubble (signature):** a circle with the choice letter in dropout ink; selected = filled graphite with a paper letter. Used for answer choices, the question navigator (answered = filled), the answer key in review and print, and the 10px status marks (open = filled, upcoming/draft = empty, closed = crossed).
- **Scannable answer sheet:** one full page per student per printed page, laid out in millimetres by `scholar/omr.py` (the template only places what that module computes). Four 8 mm black corner squares and a row of 3 mm code squares are drawn in SVG so they print even with "background graphics" off; they are the only pure black on the sheet and must stay solid black. Bubbles are dropout-green rings with the letter in a pale green inside, so pencil is the darkest thing in a bubble. Don't resize, recolour or move these marks in CSS: the reader expects them exactly where `omr.py` says.
- **Tally:** a run of real numbers set inline (`7 submitted   0 in progress   7 awaiting grading`), not a row of identical stat boxes. A number turns red pen only when it asks for action.
- **Buttons:** primary graphite fill; secondary paper with a graphite border; danger in red pen. All have hover, active, `:focus-visible` (2px graphite outline, 2px offset) and disabled states.
- **Answer key (tests from a PDF):** the PDF page images on the left in a ruled, scrolling frame, one table row per question on the right with the same answer bubbles as the test (selected = graphite). A row with no answer yet is tinted red-pen. Students get the same layout reversed in the portal: pages left, the sticky answer column right.
- **Scanner:** a full-screen camera view (black stage, paper-coloured side panel). The sheet is outlined in dropout green when held steady (pencil yellow while moving), with a ring that fills as it holds still. Results are plain lines under the student's name; a doubtful answer is a picture of that one question with tappable bubbles (selected = graphite, the reader's guess = dashed pencil ring) and a "Looks right" button.
- **Charts (analytics):** inline SVG, no library. Marks are graphite (the pencil mark that counts), flagged marks red pen with a label; axes and guide lines are dropout. Flat bars with a 2px paper gap, 10px dots with a paper ring, a hover tip on every mark, and a table with the same numbers next to every chart. Mastery spreads use the chart-* tokens, always with counts in the segments.
- **Test portal:** the same paper and type, with a stripped header (no staff navigation). Locks and pauses replace the page with a plain centred notice on paper, never a modal over the questions. The student's name and ID repeat faintly (4.5% graphite) across the test as a watermark.
- **Notes:** an explanation is introduced by a dropout label; an instructor's feedback is set in red-pen serif italic, like a margin note. No coloured side stripes.

## Do's and Don'ts

- Do reuse the header block, tabs, tables, bubbles and tokens on every page, unchanged.
- Do add a token here before a page uses it.
- Don't use: card grids with shadows (K2), cards in cards (K11), pill chips on every row (K12), all-caps labels or `A · B · C` meta strings (SD4), arrow glyphs on links (CP3), emoji as icons (I2), coloured side-stripe boxes (K4), a dark theme by default (C7), cream with terracotta (SD1), one neon accent on near-black (SD2).
- Don't load fonts from a CDN: the app is self-hosted on YunoHost and the faces ship in `static/fonts/`.
