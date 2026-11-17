---
name: report
description: Write a report, write-up, comparison, table, chart or any answer too long for the call overlay as an offline HTML page in indigoshell's look, save it under reports/, and open it for the user. Use when a reply would run past a dozen lines, or needs a table, a chart or pictures.
---

# Reports

The call overlay is a narrow panel for a few lines. Anything longer goes
on a page: one self-contained HTML file the user reads in Firefox, in the
same colours as their desktop.

## Steps

1. Read `template.html` and `palette.css` from this skill's folder.
   `palette.css` is generated from the shell's own theme on every call,
   so a report always matches the desktop.
2. Start from the template: paste palette.css where it says, keep the
   role variables and classes, replace the example content.
3. Save it as `reports/<YYYY-MM-DD>-<short-slug>.html` in your working
   directory (make the folder; never overwrite an earlier report).
   Pictures go in `reports/assets/`, referenced relatively. To put the
   screen in a report: `import -window root reports/assets/<name>.png`.
4. Open it with the shell's `open` tool. On the call, say one line about
   what the page shows; don't repeat the page.

## Rules

- Offline and self-contained: inline CSS only, no external scripts,
  fonts, stylesheets or CDNs. Charts are inline SVG. No JavaScript
  unless the page needs it.
- Lead with the answer: a title, a one-line summary, then sections.
  Tables for comparisons, code blocks for commands to copy.
- Only the palette's colours. Dark only.

When the user asks for a different style, change `template.html` (and
this file, if the steps change) so the next report follows it. Colours
come from their theme: for different colours, they change the theme.
