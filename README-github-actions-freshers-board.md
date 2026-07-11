# Freshers IT Board GitHub Actions Pipeline

This workspace now includes a code-driven refresh pipeline for the freshers IT board.

Files:

- `.github/workflows/freshers-it-board.yml`
- `scripts/refresh_freshers_it_board.py`
- `requirements.txt`

What it does:

- runs every 30 minutes in GitHub Actions
- scrapes public job sources
- includes direct public employer pages and Naukri search/result discovery, but only keeps roles whose final job pages are still directly verifiable
- filters to IT-only fresher and entry-style roles
- prioritizes Kochi, then Thiruvananthapuram, then Bengaluru
- regenerates:
  - `outputs/freshers-job-board.html`
  - `outputs/freshers-job-board.csv`
  - `outputs/freshers-job-board.json`
  - `docs/index.html`
  - `docs/freshers-job-board.csv`
  - `docs/freshers-job-board.json`
- commits the updated outputs back to the repository

Notes:

- The current collector is strongest on `Infopark` and has a best-effort `Technopark` parser.
- No extra API keys are required for the current source set.
- It intentionally keeps source-gap watchlist cards instead of inventing postings when a source is not verifiable.
- `docs/index.html` is GitHub Pages-ready if you enable Pages for the repository and point it at the `docs` folder on the default branch.
