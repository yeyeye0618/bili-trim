---
name: bili-trim
description: A proofreader's sheet for cutting sponsor segments out of Bilibili's Android cache, in place.
colors:
  paper: "#F3F2EC"
  paper-shade: "#E7E6DD"
  paper-crease: "#D2D1C6"
  paper-selected: "#DDDCD1"
  grid-green: "#5F8F6C"
  grid-green-soft: "#A8C4AE"
  ink: "#17140F"
  ink-muted: "#54544A"
  ink-disabled: "#969488"
  vermilion: "#B8231A"
  vermilion-deep: "#961C14"
  vermilion-pale: "#F2CFC9"
  ink-well: "#121010"
  on-well: "#D9D2C4"
  on-well-muted: "#9A9183"
  status-ok: "#2A7347"
  warn-ochre: "#7D5F0E"
typography:
  display:
    fontFamily: "Barlow Semi Condensed, Bahnschrift SemiCondensed, Microsoft JhengHei, sans-serif"
    fontSize: "46px"
    fontWeight: 600
    fontFeature: "tnum"
  mark:
    fontFamily: "Barlow Semi Condensed, Bahnschrift SemiCondensed, Microsoft JhengHei, sans-serif"
    fontSize: "28px"
    fontWeight: 600
    fontFeature: "tnum"
  headline:
    fontFamily: "Noto Serif TC, Microsoft JhengHei, serif"
    fontSize: "24px"
    fontWeight: 600
  action:
    fontFamily: "Noto Sans TC, Microsoft JhengHei, sans-serif"
    fontSize: "20px"
    fontWeight: 700
  title:
    fontFamily: "Noto Serif TC, Noto Sans TC, Microsoft JhengHei, serif"
    fontSize: "17px"
    fontWeight: 600
  lead:
    fontFamily: "Noto Sans TC, Microsoft JhengHei, sans-serif"
    fontSize: "18px"
    fontWeight: 400
  body:
    fontFamily: "Noto Sans TC, Microsoft JhengHei, sans-serif"
    fontSize: "17px"
    fontWeight: 400
  body-strong:
    fontFamily: "Noto Sans TC, Microsoft JhengHei, sans-serif"
    fontSize: "17px"
    fontWeight: 700
  small:
    fontFamily: "Noto Sans TC, Microsoft JhengHei, sans-serif"
    fontSize: "16px"
    fontWeight: 400
  label-strong:
    fontFamily: "Noto Sans TC, Microsoft JhengHei, sans-serif"
    fontSize: "16px"
    fontWeight: 700
  numeral:
    fontFamily: "Barlow Semi Condensed, Bahnschrift SemiCondensed, Microsoft JhengHei, sans-serif"
    fontSize: "17px"
    fontWeight: 600
    fontFeature: "tnum"
  numeral-small:
    fontFamily: "Barlow Semi Condensed, Bahnschrift SemiCondensed, Microsoft JhengHei, sans-serif"
    fontSize: "16px"
    fontWeight: 600
    fontFeature: "tnum"
  numeral-lead:
    fontFamily: "Barlow Semi Condensed, Bahnschrift SemiCondensed, Microsoft JhengHei, sans-serif"
    fontSize: "18px"
    fontWeight: 600
    fontFeature: "tnum"
  numeral-value:
    fontFamily: "Barlow Semi Condensed, Bahnschrift SemiCondensed, Microsoft JhengHei, sans-serif"
    fontSize: "17px"
    fontWeight: 400
    fontFeature: "tnum"
rounded:
  none: "0px"
spacing:
  hair: "1px"
  xs: "4px"
  sm: "7px"
  md: "12px"
  lg: "16px"
  xl: "20px"
components:
  button-ink:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.paper}"
    rounded: "{rounded.none}"
    padding: "6px 10px"
  button-ink-disabled:
    backgroundColor: "{colors.paper-shade}"
    textColor: "{colors.ink-disabled}"
  button-write:
    backgroundColor: "{colors.vermilion}"
    textColor: "#FFFFFF"
    typography: "{typography.action}"
    rounded: "{rounded.none}"
    padding: "6px 10px"
  button-write-hover:
    backgroundColor: "{colors.vermilion-deep}"
  button-write-disabled:
    backgroundColor: "{colors.paper-crease}"
    textColor: "{colors.ink-muted}"
  button-ghost:
    backgroundColor: "transparent"
    textColor: "{colors.ink}"
    rounded: "{rounded.none}"
    padding: "6px 10px"
  button-ghost-hover:
    backgroundColor: "{colors.paper-shade}"
  button-outline:
    backgroundColor: "transparent"
    textColor: "{colors.ink}"
    rounded: "{rounded.none}"
  button-step:
    backgroundColor: "{colors.paper-shade}"
    textColor: "{colors.ink}"
    rounded: "{rounded.none}"
    padding: "6px 10px"
  button-flat:
    backgroundColor: "transparent"
    textColor: "{colors.ink}"
    padding: "5px 8px"
  button-link:
    backgroundColor: "transparent"
    textColor: "{colors.ink}"
    padding: "1px 6px"
  segment-on:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.paper}"
    rounded: "{rounded.none}"
  segment-off:
    backgroundColor: "transparent"
    textColor: "{colors.ink-muted}"
    rounded: "{rounded.none}"
  chip-device:
    backgroundColor: "{colors.paper-shade}"
    textColor: "{colors.ink}"
    rounded: "{rounded.none}"
  input-search:
    backgroundColor: "{colors.paper-shade}"
    textColor: "{colors.ink}"
    rounded: "{rounded.none}"
    padding: "7px 10px"
  input-cell:
    backgroundColor: "transparent"
    textColor: "{colors.ink}"
    padding: "3px 4px"
  inspector:
    backgroundColor: "{colors.paper-shade}"
    textColor: "{colors.ink}"
    width: "324px"
  library:
    backgroundColor: "{colors.paper}"
    width: "298px"
  library-row-selected:
    backgroundColor: "{colors.paper-selected}"
  top-bar:
    backgroundColor: "{colors.paper}"
    height: "48px"
    padding: "8px 20px"
  footer:
    backgroundColor: "{colors.paper}"
    height: "30px"
    padding: "6px 20px"
  preview-well:
    backgroundColor: "{colors.ink-well}"
    textColor: "{colors.on-well}"
  timeline-ruler:
    textColor: "{colors.ink-muted}"
    typography: "{typography.numeral-small}"
    height: "28px"
  timeline-cells:
    backgroundColor: "{colors.paper}"
    height: "50px"
  timeline-band:
    height: "26px"
  timeline-foot:
    textColor: "{colors.ink-muted}"
    typography: "{typography.small}"
    height: "22px"
  strike-cells:
    backgroundColor: "{colors.vermilion-pale}"
  strike-label:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.vermilion}"
    typography: "{typography.numeral-small}"
    padding: "1px 5px"
  cut-list-header:
    backgroundColor: "{colors.paper-shade}"
    textColor: "{colors.ink-muted}"
    height: "30px"
  cut-list-row:
    textColor: "{colors.ink}"
    typography: "{typography.numeral}"
    height: "36px"
  progress:
    backgroundColor: "{colors.paper-crease}"
    textColor: "{colors.ink}"
---

# Design System: bili-trim

## Overview

**Creative North Star: "The Proofreader's Sheet"**

The whole window is a sheet of Chinese proofreading paper laid on a desk: cool white stock, a printed green grid, and one vermilion pen that only ever marks deletions. This is not an editing suite. It refuses the NLE cockpit (dark grey, orange accent, many tracks) and the light SaaS dashboard (rounded cards, soft shadows). Cutting a sponsor segment reads as striking a passage from a manuscript: the document stays the same document, a passage is marked out, and the margin note says how much is gone.

The video frame sits in an ink well, a black field with a dark border, so the eye's local surround stays dark while the rest of the desk stays paper. Density is that of a working document: three fixed columns (cache list, sheet, "this one" inspector), quiet hairlines, and numerals set in a condensed tabular face so running clocks never jitter. Every surface is flat; structure comes from tone (paper, shade, crease) and 1px lines, never from lift.

The system lives in one file, `bili_look.py`. Colours, the type scale, font roles and every theme are defined there; `bili_gui.py` only references them. Only the light paper theme exists. A dark theme is a deferred, open decision; if it comes, it swaps the palette in that file and nothing else.

**Key Characteristics:**
- Paper-toned flat surfaces; depth by tone and hairline only.
- One accent, vermilion, with exactly two meanings: will be removed, and irreversible.
- The 刪除號 (deletion mark) on the 稿紙 grid timeline is the signature.
- Serif headings, sans body, condensed tabular numerals for every time and byte count.
- Zero radius, zero shadow, zero gradient.
- Everything scales with the five-step user font size through `px()`; layout reflows rather than just enlarging text.

## Colors

A cool paper ground, a printed green grid, near-black ink, and a single vermilion that is rationed.

### Primary
- **Vermilion (Cinnabar Seal Red)** (`vermilion`): reserved for exactly two things. (1) What will be removed: the strike on the timeline, its 「刪 xx.x s」 label, the tick at the head of each committed cut-list row, and the inspector's changed figures (新長, 移除). (2) Irreversible actions: 備份並寫入手機, 還原到手機, and 備份並寫入 in the confirm dialog. `vermilion-deep` is its hover; a darker press value lives only inside the write button theme.
- **Vermilion Pale** (`vermilion-pale`): the tint of grid cells that will be removed, under the strike.

### Secondary
- **Printed Grid Green** (`grid-green`): the timeline's outer frame and the lines that bound the cell band, like the ruled border of 稿紙.
- **Soft Grid Green** (`grid-green-soft`): the fine verticals inside the cell band, one per fragment (or one per k fragments when zoomed out).

### Neutral
- **Paper** (`paper`): the whole working surface; cool white, deliberately not cream.
- **Paper Shade** (`paper-shade`): the inspector column, step buttons, the device chip, the search field, table headers, disabled button fill.
- **Paper Crease** (`paper-crease`): hairlines, separators, borders, the progress trough.
- **Paper Selected** (`paper-selected`): the selected row in the cache list.
- **Ink** (`ink`): primary text, the 2px rules that close the top bar and open the footer, the playhead, the ink button and the active segment.
- **Muted Ink** (`ink-muted`): secondary text, ruler numerals, field labels, unsupported list rows. A brown-grey drawn from the paper, not a neutral grey; above 6:1 on paper.
- **Disabled Ink** (`ink-disabled`): disabled control text only. It is not required to meet body contrast.
- **Ink Well** (`ink-well`) with **On Well** (`on-well`) and **On Well Muted** (`on-well-muted`): the preview frame and its small resolution tag (a translucent near-black tag in the well's lower right).
- **Status Green** (`status-ok`): the device-connected square and "ok" states; the grid green pressed darker.
- **Ochre** (`warn-ochre`): warnings and cautions. Never red.

### Named Rules
**The Two Meanings Rule.** Vermilion means "this will be removed" or "this cannot be undone". Nothing else is red: not errors, not warnings, not links, not focus. A missing dependency is shown in ink plus bold; a caution is ochre. Every extra red dilutes the deletion mark.

**The One Red Block Rule.** In the main window the only filled vermilion block is the write action in the inspector. Other vermilion is line, text or tint.

**The Not By Colour Alone Rule.** Every coloured state also carries text or shape: the strike has hooks and a 「刪」 label, the status square has a label, unsupported rows say why.

## Typography

**Display / Numeral Font:** Barlow Semi Condensed (SemiBold 600 for display numerals, Regular 400 for inline values), bundled in `fonts/` under the SIL OFL 1.1, with its tabular figures baked into the cached copy's cmap. Falls back to Bahnschrift SemiCondensed, then Microsoft JhengHei.
**Body Font:** Noto Sans TC 400 / 700, instanced from Windows 11's variable font, with merged CJK, Hangul, symbol and emoji fallbacks; falls back to Microsoft JhengHei.
**Heading Font:** Noto Serif TC 600.

**Character:** A manuscript's serif for section heads, a plain sans for the working text, and an engineering-condensed numeral face for every time, size and count, so the page reads like a marked-up document with a meter running beside it.

Sizes are logical px at the 「標準」 user font size and are scaled by `px()` (DPI and the user's size step). In Dear PyGui the size is the line height, not the em: at 17 the Noto CJK advance is about 14px.

### Hierarchy
- **Display** (Barlow Semi Condensed 600, 46px): the playback clock, 00:04:32, followed by a lead-size `.3 / 1:16:59` on the same baseline.
- **Mark** (Barlow Semi Condensed 600, 28px): the BILI-TRIM wordmark in the top bar.
- **Headline** (Noto Serif TC 600, 24px): section heads, 手機上的快取, 時間軸, 這一支.
- **Action** (Noto Sans TC 700, 20px): the label on the write action.
- **Title** (Noto Serif TC 600, 17px): the video title at the top of the inspector, used only when the serif can draw every glyph in that title; otherwise it falls back to the body font.
- **Lead** (Noto Sans TC 400, 18px): the text after the clock.
- **Body / Body Strong** (Noto Sans TC 400 / 700, 17px): body copy and buttons; bold for list titles of the selected row and status emphasis.
- **Small / Label Strong** (Noto Sans TC 400 / 700, 16px): captions, list second lines, messages, field labels; bold for 起 / 迄 marks and the 「刪」 glyph.
- **Numerals** (Barlow Semi Condensed 600 at 16, 17, 18px): ruler ticks, list durations, quality and sizes, cut-list times, the resolution note.
- **Numeral Value** (Barlow Semi Condensed 400, 17px): numbers inside inspector values. Barlow's digits are 0.60 of the line against Noto's ideographs at 0.69, so the same size sits level with the Chinese beside it. (Bahnschrift's digits are 0.72; if the fallback is ever in use, this role needs about 14px.)

### Named Rules
**The Steady Clock Rule.** Every number that changes while playing or that is compared in a column (times, bytes, counts) is set in the tabular numeral face. Proportional digits are a defect.

**The Mixed-Run Rule.** A Dear PyGui text item has one font, so mixed CJK and numeral runs are separate items aligned on a shared baseline computed from each font's hhea metrics. Do not fake alignment with manual nudges.

**Decided: the numeral family.** Barlow Semi Condensed, as the direction contract names it; the user approved bundling it. The Google Fonts build has no OpenType feature Dear PyGui can run, so `bili_font.make_face` bakes its `tnum` glyphs into the cached copy; the files in `fonts/` stay unmodified.

## Layout

Three fixed columns between a top bar and a footer, all in logical px through `px()`:

- **Top bar** (48px): wordmark and version, device chip with status square, app variant and count; on the right the backup path, 設定, 說明. Closed below by a 2px ink rule.
- **Cache list** (298px, left): heading with count, search field, rows, and a full-width outline button at the bottom (重新掃描手機). Each row has fixed furniture: a two-line title, then duration, quality, size in numerals; unsupported rows are set in muted ink and state the reason.
- **Work sheet** (centre, padding 19 x 15): the ink well preview, the transport row (play, step buttons, 原片 / 成品 segment, clock), the timeline, and the cut list.
- **Inspector** (324px, right, paper-shade fill): 這一支 heading, title, key-value rows (作者, cid, 畫質, 原長, 新長, 移除, 視訊, 音訊, fragment), 吸附方向 segment, the write action, a ghost action and the cost note.
- **Footer** (30px): tool versions and cache state on the left, the latest message and 訊息 on the right; opened by a 2px ink rule.

Columns are separated by 1px paper-crease rules. Timeline bands from top: ruler 28, cells 50, mark band 26, description 22. Cut list: header 30, rows 36, sized for header plus three rows before it scrolls.

Spacing rhythm: item spacing 7, cell padding 12 x 6, window padding 16 x 13, bar and footer padding 20; hairline 1.

**Responsive rules** (measured each frame, no fixed breakpoints):
- The transport row splits into two rows (buttons; segment and clock) when it does not fit.
- The cut list drops the 吸附挪動 column and tightens gaps when each time column would fall under 112px.
- Inspector actions follow the data directly; only when the data overflows do they pin to the bottom and let the data scroll.
- The timeline description truncates to one line rather than colliding with 全片 / 看接縫.
- The preview is fitted into the well and reports native, display and scale in its tag.

### Named Rules
**The Scale Everything Rule.** Every dimension goes through `px()` and the theme is rebuilt on a font-size change; the timeline geometry is recomputed too. A hard-coded physical pixel is a bug, except deliberate 1px hairlines.

## Elevation & Depth

Flat. There are no shadows anywhere (BorderShadow is transparent) and no gradients. Depth is tonal: paper for the sheet, paper-shade for the inspector and controls, paper-crease for lines, and the ink well as the one dark recess. Modals dim the window with a translucent ink-well veil rather than lifting the dialog.

### Named Rules
**The Paper Has No Shadows Rule.** Nothing floats. If an element needs separation, use a tone step or a 1px line.

## Shapes

Every corner is square: window, child, frame, popup, scrollbar, grab and tab rounding are all 0. Borders are 1px, in paper-crease for structure, ink-muted for ghost controls, ink for outline buttons. Heavy emphasis lines are 2px ink (top bar, footer, the in/out range bar) and 3px vermilion (the strike and cut-list ticks). Small marks are solid rectangles: the status square, the playhead head, the strike hooks.

Dear PyGui shaped this vocabulary: there are no dashed borders, so nothing is dashed; child windows are at least 4px tall, so hairline rules are 1x1 tinted images stretched to size; 1px drawn lines are placed on half-pixels (`crisp`) so they stay single.

## Components

### Buttons
Square, flat, honest about consequence.
- **Ink** (`button-ink`, plus a short `ink_s` variant at 2px vertical padding): primary but reversible actions such as 播放 and 加入. Hover lifts to a warm near-black, press goes full black.
- **Write** (`button-write`): irreversible actions only. Vermilion fill, white text, action type in the inspector. Disabled becomes paper-crease with muted ink, never pale red.
- **Ghost** (`button-ghost`, short `ghost_s`): secondary actions; transparent with a muted-ink border.
- **Outline** (`button-outline`): full-width list actions; ink border.
- **Step** (`button-step`): transport steps (開頭, −10s, −1s, −格, 格+, +1s, +10s); paper-shade fill.
- **Flat / Link**: text-only top-bar menu items and in-row actions (看, 取消, 清除), the height of the text.
- **Disabled:** written as its own theme component, because disabled items do not inherit border and padding; text goes to disabled ink.

### Segmented Toggle
- **Style:** two or more adjoining square buttons with muted-ink borders; the selected one is ink with paper text (`segment-on`), the others transparent with muted ink text.
- **Used for:** 原片 / 成品 and 外擴 / 內縮.

### Chips
- **Device chip:** paper-shade, borderless, with a status-green square before the device name.

### Inputs / Fields
- **Search:** paper-shade field with a paper-crease border; brightens toward near-white when active.
- **Cell input:** times in the cut list look like plain text until hovered (paper-shade) or active.

### Navigation
The top bar is the only navigation: flat text buttons, no tabs, no icons.

### Cache List Row
Fixed furniture per row: two-line title in body, then numerals for duration, quality and size; 1px crease between rows; selected row in paper-selected. Rows that cannot be cut are muted ink and end with 不能剪：and the reason.

### Inspector Key-Value
Muted-ink label left, value right-aligned; numbers in numeral-value. 新長 and 移除 turn vermilion once there is a cut, because they describe what will be removed.

### Ink Well Preview
The frame letterboxed in `ink-well` with an ink border; a translucent dark tag in the lower right reads 原生 / 顯示 / 縮放 in on-well colours.

### The 刪除號 on the 稿紙 Timeline (signature)
- **Grid:** the cell band is ruled in soft grid green; one cell is one fragment. When cells would fall under 9px, only every k-th boundary is drawn (k from 1, 2, 5, 10, 20, 50 ...), and the header says 一格＝k 個 fragment, with the approximate seconds.
- **Removed cells:** tinted vermilion-pale.
- **Strike:** a 3px vermilion bar across the middle of the cell band with vertical end hooks about 0.42 of the band height. Hovering or dragging an end turns that hook ink and doubles its width: that is where it can be dragged.
- **Narrow strike:** under about 1.2 times the hook height in width, the bar and hooks become one solid vermilion block; zoom in to see the strike.
- **Label:** 「刪 xx.x s」 in vermilion (刪 bold, the number in numerals) on a paper knockout, centred over the strike near the top of the band. It is drawn after the playhead so the playhead never strikes through it. Labels that would overlap the previous one are skipped.
- **Playhead:** a 2px ink line with a 12 x 8 ink head sitting on the bottom of the ruler band.
- **Marks:** 起 and 迄 as 1px ink verticals with bold labels in the mark band; a 2px ink bar spans a pending range.
- **Ruler:** major ticks 8px with numerals, minor ticks 4px at reduced alpha, growing upward from the cell band.
- **Foot line:** a one-line explanation in small muted ink (cut points land on grid lines, snap direction, audio ±23 ms), truncated to fit.

### Cut List
Hand-positioned rows, not a table, because Dear PyGui tables pad rows unpredictably. Columns: tick, 起, 迄, 實際刪, 吸附挪動, 看, 取消; committed rows start with a 3px vermilion tick, the draft row with a crease-coloured tick and ink / ghost short buttons.

### Named Rules
**The Snapped Truth Rule.** Every number about a cut is the snapped value, the one that will actually be removed: strike label, cut list 起 / 迄 / 實際刪, inspector, confirm dialog. Never show the requested value where the removed value belongs. Durations use `fmt_dur`, which rounds to the second, so 原長 minus 移除 equals 新長.

**The Flush Text Rule.** Dear PyGui pushes text down by FramePadding.y. Containers whose children are positioned by coordinate (bar, list pane, footer, key-value, cut list, log) zero it, so a coordinate is where the glyph actually sits.

### Brand Mark
The app icon and README logo are drawn in code by `assets/make_icon.py`; every file in `assets/` is its output, so change the script and rerun it rather than editing an image.

- **Seal** (印章, 白文: white carved into colour): a rounded square in seal pink `#E74C7A` with an inner white frame line, carved with 「bili」 over 「trim」 in Barlow Semi Condensed SemiBold. The divider between the two lines is the 刪除號 (bar plus end hooks), the same mark the timeline uses.
- **Small cut**: at 16–40 px two lines of lettering cannot be carved, so the same seal carries a single 「b」 (inner frame only from 32 px). The `.ico` stores separate art per size: 16, 20, 24, 32, 40, 48, 64, 128, 256.
- **Wordmark**: 「bili-trim」 in Barlow Semi Condensed SemiBold, with the hyphen replaced by the 刪除號 in seal pink. Ink `#18191C` on light, `#F0F0F0` on dark; the README switches between `logo-light.png` and `logo-dark.png` by colour scheme.
- **Social preview**: `assets/social-preview.png`, 1280×640, uploaded by hand in the repository settings (GitHub has no API for it).

**The Not Official Rule.** The mark says B站 through its pink and nothing else: no 小電視 logo, no bilibili wordmark lettering, no mascots. The tool is not affiliated with bilibili, and its mark must not read as if it were.

**The Two Pinks Rule.** Seal pink belongs to the brand mark only. Inside the app, removal and irreversible actions stay vermilion `#B8231A`; do not bring the seal pink into the interface.

## Do's and Don'ts

### Do:
- **Do** keep every colour, size, font role and theme in `bili_look.py`; a future dark theme swaps that palette only.
- **Do** use vermilion only for what will be removed and for irreversible actions (寫入手機, 還原到手機, 備份並寫入).
- **Do** show warnings in ochre and missing or failed states in ink plus bold.
- **Do** set every time, byte count and count in the tabular numeral face.
- **Do** show snapped, actually-removed values everywhere and round durations with `fmt_dur`.
- **Do** draw cut points on fragment grid lines and state the grid unit (一格＝k 個 fragment) whenever k > 1.
- **Do** pass every dimension through `px()` and verify at the largest user font size.
- **Do** draw hairlines as 1x1 tinted images or half-pixel lines.

### Don't:
- **Don't** use vermilion for errors, warnings, links, focus or decoration.
- **Don't** round a corner, cast a shadow or use a gradient.
- **Don't** use dashed borders or rely on letter-spacing; Dear PyGui has neither.
- **Don't** use proportional digits for anything that ticks or is compared in a column.
- **Don't** put the video frame on paper; it lives in the ink well.
- **Don't** replace the bundled Barlow with a system face to save a download; the fallback exists for a missing folder, not as an alternative.
- **Don't** add a second theme until the light / dark decision is made.
