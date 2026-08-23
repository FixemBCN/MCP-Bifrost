# assets

Images referenced from the documentation.

| File | Used by | Notes |
|---|---|---|
| `Bifrost_Logo_transparentBackground.png` | `README.md`, light theme | black dots — reads on a light background |
| `Bifrost_Logo_DarkBackground.png` | `README.md`, dark theme | white dots — reads on a dark background |
| `Bifrost_Logo_wb.png` | spare | same mark with a baked-in white background |

The README picks between the first two with a `<picture>` element, so the
mark stays legible whichever theme the reader is using.

GitHub renders README files on both light and dark backgrounds, and does not
let CSS decide between them. A logo with dark ink on a transparent
background disappears entirely in dark mode — which is the default for a
large share of readers. Either use a mark that reads on both, or ship two
files and let the `<picture>` element in the README pick.
