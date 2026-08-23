# assets

Images referenced from the documentation.

| File | Used by | Notes |
|---|---|---|
| `logo.svg` | `README.md` header | preferred — scales, and one file works at any size |
| `logo-dark.svg` | `README.md` header | only if the main logo is unreadable on a dark background |
| `social-preview.png` | GitHub link cards | 1280×640, uploaded through GitHub's settings, not from here |

GitHub renders README files on both light and dark backgrounds, and does not
let CSS decide between them. A logo with dark ink on a transparent
background disappears entirely in dark mode — which is the default for a
large share of readers. Either use a mark that reads on both, or ship two
files and let the `<picture>` element in the README pick.
