# Offline interface assets

The application serves these files from FastAPI at `/static`; they must remain in the distribution alongside `main.py`.

- Tailwind CSS 3.4.17 is compiled from `templates/**/*.html` and `modules/**/*.py` with `pnpm build:offline`.
- Phosphor Icons Web 2.1.2 regular, bold, and light font styles are stored in `phosphor/`.
- TinyMCE 6.8.3 and html2pdf.js 0.10.1 support the report-template editor and PDF export.
- TinyMCE retains its upstream license file in the copied distribution. Tailwind, Phosphor, TinyMCE, and html2pdf.js are MIT licensed.

The runtime does not need Node.js or Internet access to use these assets. Node.js and network access are only needed to rebuild the vendored frontend assets after changing dependencies or Tailwind classes.
