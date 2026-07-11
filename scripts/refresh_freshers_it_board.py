from __future__ import annotations

import csv
import html
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup


IST_OFFSET = timezone.utc
ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "outputs"
HTML_PATH = OUTPUT_DIR / "freshers-job-board.html"
CSV_PATH = OUTPUT_DIR / "freshers-job-board.csv"
JSON_PATH = OUTPUT_DIR / "freshers-job-board.json"
DOCS_DIR = ROOT / "docs"
DOCS_HTML_PATH = DOCS_DIR / "index.html"
DOCS_CSV_PATH = DOCS_DIR / "freshers-job-board.csv"
DOCS_JSON_PATH = DOCS_DIR / "freshers-job-board.json"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

LOCATION_PRIORITY = ["Kochi", "Thiruvananthapuram", "Bengaluru"]
IT_KEYWORDS = {
    "software",
    "developer",
    "engineer",
    "python",
    "data",
    "wordpress",
    "web",
    "system engineer",
    "qa",
    "test",
    "testing",
    "implementation",
    "analyst",
    "ai",
    "ml",
    "cloud",
    "apprentice",
    "apprenticeship",
    "trainee",
    "intern",
    "internship",
    "it qa",
    "system validation",
}
NON_IT_KEYWORDS = {
    "sales",
    "recruiter",
    "business development",
    "seo",
    "compliance",
    "marketing",
    "hr",
    "lead generation",
    "customer support",
    "telecaller",
    "accounts",
    "accounts executive",
    "clinical",
    "auditor",
    "office assistant",
    "founder's office",
    "founders office",
    "transaction",
    "3d",
    "designer",
    "design coordinator",
    "visual designer",
    "graphic",
}
SENIORITY_EXCLUSIONS = {
    "senior",
    "sr.",
    "sr ",
    "lead ",
    "manager",
    "architect",
    "principal",
    "staff ",
}
CITY_ALIASES = {
    "kochi": "Kochi",
    "cochin": "Kochi",
    "thiruvananthapuram": "Thiruvananthapuram",
    "trivandrum": "Thiruvananthapuram",
    "bengaluru": "Bengaluru",
    "bangalore": "Bengaluru",
}
TRUSTED_JOB_HOSTS = (
    "jobs.lever.co",
    "boards.greenhouse.io",
    "myworkdayjobs.com",
    "smartrecruiters.com",
    "jobs.ashbyhq.com",
    "naukri.com",
)
NAUKRI_SEARCH_PAGES = [
    ("Kochi", "https://www.naukri.com/data-analyst-jobs-in-kochi"),
    ("Kochi", "https://www.naukri.com/software-engineer-jobs-in-kochi"),
    ("Thiruvananthapuram", "https://www.naukri.com/fresher-it-jobs-in-trivandrum"),
    ("Thiruvananthapuram", "https://www.naukri.com/software-engineer-jobs-in-trivandrum"),
    ("Bengaluru", "https://www.naukri.com/software-engineer-trainee-jobs-in-bangalore"),
    ("Bengaluru", "https://www.naukri.com/data-analyst-jobs-in-bangalore"),
]


@dataclass
class Job:
    company: str
    role: str
    location: str
    posted: str
    apply_by: str
    level: str
    source: str
    note: str
    url: str


class SourceError(RuntimeError):
    pass


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def fetch(url: str) -> str:
    response = requests.get(
        url,
        timeout=30,
        headers={"User-Agent": USER_AGENT},
    )
    response.raise_for_status()
    return response.text


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def normalize_location(text: str) -> str | None:
    lower = text.lower()
    for needle, city in CITY_ALIASES.items():
        if needle in lower:
            return city
    return None


def classify_level(role: str) -> str:
    lower = role.lower()
    if "apprent" in lower:
        return "Apprenticeship"
    if "intern" in lower:
        return "Internship"
    if "trainee" in lower:
        return "Trainee"
    if "junior" in lower:
        return "Junior"
    if any(token in lower for token in ("engineer", "developer", "analyst", "tester", "qa", "implementation", "ai")):
        return "Entry level"
    return "Early career"


def is_it_role(role: str, company: str = "", note: str = "") -> bool:
    haystack = f"{role} {company}".lower()
    if any(term in haystack for term in NON_IT_KEYWORDS):
        return False
    return any(term in haystack for term in IT_KEYWORDS)


def is_entry_friendly(role: str) -> bool:
    lower = role.lower()
    return not any(term in lower for term in SENIORITY_EXCLUSIONS)


def parse_date(text: str) -> str:
    text = clean_text(text)
    patterns = [
        ("%d %b %Y", r"\b\d{1,2} [A-Za-z]{3} \d{4}\b"),
        ("%d %B %Y", r"\b\d{1,2} [A-Za-z]+ \d{4}\b"),
        ("%Y-%m-%d", r"\b\d{4}-\d{2}-\d{2}\b"),
    ]
    for fmt, pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        try:
            return datetime.strptime(match.group(0), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return text or "n/a"


def infer_company_from_url(url: str) -> str:
    host = urlparse(url).netloc.lower()
    host = host.removeprefix("www.")
    if "naukri.com" in host:
        return "Naukri listing"
    parts = [part for part in host.split(".") if part not in {"com", "in", "co", "io", "jobs", "careers"}]
    if not parts:
        return host
    return clean_text(parts[0].replace("-", " ").title())


def trusted_job_host(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return any(domain in host for domain in TRUSTED_JOB_HOSTS)


def verify_discovered_job(url: str, location_hint: str, source: str, fallback_title: str = "") -> Job | None:
    if not trusted_job_host(url):
        return None
    text = clean_text(BeautifulSoup(fetch(url), "lxml").get_text(" ", strip=True))
    location = normalize_location(text) or location_hint
    if location not in {location_hint, "Kochi", "Thiruvananthapuram", "Bengaluru"}:
        return None
    title = fallback_title
    title_match = re.search(r"<title>(.*?)</title>", fetch(url), re.I | re.S)
    if title_match:
        title = clean_text(BeautifulSoup(title_match.group(1), "lxml").get_text(" ", strip=True))
    if not title:
        title = fallback_title or infer_company_from_url(url)
    role = clean_text(title.split("|")[0].split(" - ")[0])
    if not is_it_role(role, infer_company_from_url(url), text):
        return None
    if not is_entry_friendly(role):
        return None
    return Job(
        company=infer_company_from_url(url),
        role=role,
        location=location,
        posted="n/a",
        apply_by="n/a",
        level=classify_level(role),
        source=source,
        note="Discovered via search and verified against a live public job page.",
        url=url,
    )


def parse_naukri_search_page(url: str, location_hint: str) -> list[Job]:
    text = fetch(url)
    jobs: list[Job] = []
    # Naukri search pages ship enough text in SSR HTML for simple extraction.
    pattern = re.compile(
        r'"title":"(?P<title>[^"]+?)".+?"companyName":"(?P<company>[^"]+?)".+?"jdURL":"(?P<url>https:[^"]+?)"',
        re.S,
    )
    seen: set[str] = set()
    for match in pattern.finditer(text):
        role = clean_text(match.group("title"))
        company = clean_text(match.group("company"))
        job_url = match.group("url").replace("\\u002F", "/")
        if job_url in seen:
            continue
        seen.add(job_url)
        if not is_it_role(role, company):
            continue
        if not is_entry_friendly(role):
            continue
        try:
            verified = verify_discovered_job(
                url=job_url,
                location_hint=location_hint,
                source="Naukri verified page",
                fallback_title=role,
            )
        except Exception:
            continue
        if verified:
            verified.company = company if company and company != "Naukri listing" else verified.company
            jobs.append(verified)
    return dedupe_jobs(jobs)


def scrape_infopark_page(page: int) -> list[Job]:
    url = "https://infopark.in/companies-job"
    if page > 1:
        url = f"{url}?page={page}"

    document = fetch(url)
    soup = BeautifulSoup(document, "lxml")
    text = soup.get_text("\n", strip=True)
    if "companies-job" not in url and not text:
        raise SourceError("Infopark page returned empty content")

    jobs: list[Job] = []
    rows = soup.select("tr")
    for row in rows:
        cells = row.find_all("td")
        if len(cells) < 5:
            continue
        posted = parse_date(cells[0].get_text(" ", strip=True))
        role = clean_text(cells[1].get_text(" ", strip=True))
        company = clean_text(cells[2].get_text(" ", strip=True))
        apply_by = parse_date(cells[3].get_text(" ", strip=True))
        link = row.find("a", href=True)
        href = requests.compat.urljoin(url, link["href"]) if link else url
        snippet = clean_text(row.get_text(" ", strip=True))
        if not role or not company:
            continue
        if not is_it_role(role, company, snippet):
            continue
        if not is_entry_friendly(role):
            continue
        jobs.append(
            Job(
                company=company,
                role=role,
                location="Kochi",
                posted=posted,
                apply_by=apply_by,
                level=classify_level(role),
                source=f"Infopark page {page}",
                note=f"Verified from Infopark public jobs page {page}.",
                url=href,
            )
        )

    return dedupe_jobs(jobs)


def scrape_technopark() -> list[Job]:
    url = "https://technopark.in/job-search"
    document = fetch(url)
    soup = BeautifulSoup(document, "lxml")
    cards = soup.select(".job-item, .views-row, article, .card")
    jobs: list[Job] = []
    for card in cards:
        snippet = clean_text(card.get_text(" ", strip=True))
        if len(snippet) < 40:
            continue
        links = [a for a in card.find_all("a", href=True) if clean_text(a.get_text(" ", strip=True))]
        if not links:
            continue
        role = clean_text(links[0].get_text(" ", strip=True))
        if not is_it_role(role, note=snippet):
            continue
        company = clean_text(links[1].get_text(" ", strip=True)) if len(links) > 1 else "Technopark employer"
        href = requests.compat.urljoin(url, links[0]["href"])
        jobs.append(
            Job(
                company=company,
                role=role,
                location="Thiruvananthapuram",
                posted="n/a",
                apply_by="n/a",
                level=classify_level(role),
                source="Technopark",
                note="Verified from Technopark public careers page.",
                url=href,
            )
        )
    return dedupe_jobs(jobs)


def scrape_bengaluru_direct_pages() -> list[Job]:
    """Keep only roles whose live public employer pages still expose title + city."""
    candidates = [
        ("Sprinto", "AI Implementation Intern", "https://jobs.lever.co/Sprinto/c325db0c-2182-4751-85c2-4adfee69a6ce"),
        ("Fermi AI", "Product QA Intern", "https://jobs.ashbyhq.com/Fermi%20AI/05d389f1-e57d-4383-92a3-8d452a378815"),
        ("Sarvam", "Intern - Developer Relations", "https://jobs.ashbyhq.com/sarvam/6a152939-8576-41af-8604-a5d82f66975c"),
    ]
    jobs: list[Job] = []
    for company, role, url in candidates:
        text = clean_text(BeautifulSoup(fetch(url), "lxml").get_text(" ", strip=True))
        if role.lower() not in text.lower() or not normalize_location(text):
            continue
        if not is_it_role(role, company, text):
            continue
        jobs.append(
            Job(
                company=company,
                role=role,
                location="Bengaluru",
                posted="n/a",
                apply_by="n/a",
                level=classify_level(role),
                source="Direct public employer page",
                note="Verified from a live public employer listing page.",
                url=url,
            )
        )
    return dedupe_jobs(jobs)


def scrape_naukri_discovery() -> list[Job]:
    jobs: list[Job] = []
    for location, url in NAUKRI_SEARCH_PAGES:
        try:
            jobs.extend(parse_naukri_search_page(url, location))
        except Exception:
            continue
    return dedupe_jobs(jobs)


def dedupe_jobs(jobs: Iterable[Job]) -> list[Job]:
    seen: dict[tuple[str, str, str], Job] = {}
    for job in jobs:
        key = (
            clean_text(job.company).lower(),
            clean_text(job.role).lower(),
            clean_text(job.location).lower(),
        )
        current = seen.get(key)
        if current is None or (job.posted != "n/a" and current.posted == "n/a"):
            seen[key] = job
    return list(seen.values())


def sort_jobs(jobs: list[Job]) -> list[Job]:
    priority = {city: index for index, city in enumerate(LOCATION_PRIORITY)}

    def sort_key(job: Job) -> tuple[int, str, str, str]:
        return (
            priority.get(job.location, 999),
            "9999-99-99" if job.posted == "n/a" else job.posted,
            job.apply_by,
            job.company.lower(),
        )

    return sorted(jobs, key=sort_key, reverse=False)


def add_watchlist_cards(jobs: list[Job], source_notes: list[str]) -> list[Job]:
    locations_present = {job.location for job in jobs}
    cards = list(jobs)
    if "Thiruvananthapuram" not in locations_present:
        cards.append(
            Job(
                company="Technopark public careers",
                role="Coverage check only",
                location="Watchlist",
                posted=now_utc().strftime("%Y-%m-%d"),
                apply_by="n/a",
                level="Source status",
                source="Technopark",
                note="No verified IT listing rows were captured for Trivandrum in this run.",
                url="https://technopark.in/job-search",
            )
        )
    if "Bengaluru" not in locations_present:
        cards.append(
            Job(
                company="Broader web search",
                role="Coverage check only",
                location="Watchlist",
                posted=now_utc().strftime("%Y-%m-%d"),
                apply_by="n/a",
                level="Source status",
                source="Web search",
                note="No direct publicly verifiable Bengaluru IT fresher page was captured in this run.",
                url="https://www.google.com/search?q=bengaluru+entry+level+software+engineer",
            )
        )
    if source_notes:
        cards.append(
            Job(
                company="Run notes",
                role="Source coverage summary",
                location="Watchlist",
                posted=now_utc().strftime("%Y-%m-%d"),
                apply_by="n/a",
                level="Source status",
                source="Automation",
                note=" | ".join(source_notes),
                url="https://infopark.in/companies-job",
            )
        )
    return cards


def csv_rows(jobs: list[Job]) -> list[list[str]]:
    return [
        [
            job.company,
            job.role,
            job.location,
            job.posted,
            job.apply_by,
            job.level,
            job.source,
            job.note,
            job.url,
        ]
        for job in jobs
    ]


def metric_value(jobs: list[Job], location: str) -> int:
    return sum(1 for job in jobs if job.location == location)


def render_html(jobs: list[Job], refreshed_at: str, source_summary: str) -> str:
    serialized_jobs = json.dumps([asdict(job) for job in jobs], ensure_ascii=True)
    refreshed_day = refreshed_at[:10]
    script_jobs = serialized_jobs.replace("</script>", "<\\/script>")
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="board-scope" content="IT-only entry level and fresher-friendly public job postings">
  <meta name="last-refresh-iso" content="{refreshed_day}">
  <meta name="last-refresh-at" content="{refreshed_at}">
  <meta name="location-priority" content="Kochi,Thiruvananthapuram,Bengaluru">
  <meta name="sources-checked" content="{html.escape(source_summary)}">
  <title>Freshers IT Job Board</title>
  <link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='14' fill='%23246b51'/%3E%3Cpath d='M23 22v-3.5A4.5 4.5 0 0 1 27.5 14h9A4.5 4.5 0 0 1 41 18.5V22' fill='none' stroke='%23ffffff' stroke-width='4' stroke-linecap='round'/%3E%3Crect x='15' y='22' width='34' height='26' rx='5' fill='%23f7f8ef'/%3E%3Cpath d='M15 31h34' stroke='%23246b51' stroke-width='4'/%3E%3Cpath d='M26 39.5 30.2 44 39 34' fill='none' stroke='%23246b51' stroke-width='4' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E">
  <style>
    :root {{
      --ink: #17211b;
      --muted: #637067;
      --panel: #ffffff;
      --green: #246b51;
      --mint: #dcefe4;
      --blue: #305f8f;
      --sky: #dbe9f5;
      --gold: #8d6422;
      --sand: #f2e8cf;
      --violet: #604b8a;
      --lav: #e7e1f1;
      --shadow: 0 18px 42px rgba(32, 42, 34, .11);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: var(--ink);
      background:
        radial-gradient(circle at 12% 8%, rgba(36, 107, 81, .13), transparent 24rem),
        radial-gradient(circle at 88% 2%, rgba(48, 95, 143, .14), transparent 22rem),
        linear-gradient(180deg, #f7f8ef 0%, #eef3eb 52%, #f7f5ef 100%);
      min-height: 100vh;
    }}
    .shell {{ width: min(1480px, calc(100vw - 32px)); margin: 0 auto; padding: 28px 0 42px; }}
    header {{ display: grid; grid-template-columns: 1fr auto; gap: 24px; align-items: end; padding: 10px 0 24px; }}
    .brand {{ display: flex; align-items: center; gap: 14px; }}
    .app-icon {{ width: 54px; height: 54px; flex: 0 0 auto; border-radius: 12px; box-shadow: 0 14px 32px rgba(36, 107, 81, .22); }}
    h1 {{ margin: 0 0 8px; font-size: clamp(32px, 5vw, 58px); line-height: .98; }}
    .sub {{ max-width: 820px; color: var(--muted); font-size: 15px; line-height: 1.55; }}
    .stamp {{ background: rgba(255,255,255,.74); border: 1px solid rgba(23,33,27,.12); border-radius: 8px; box-shadow: var(--shadow); padding: 14px 16px; min-width: 290px; }}
    .stamp strong {{ display: block; font-size: 28px; }}
    .stamp span {{ color: var(--muted); font-size: 13px; display: block; }}
    .toolbar {{ display: grid; grid-template-columns: minmax(220px, 1fr) auto auto; gap: 12px; margin-bottom: 18px; align-items: center; }}
    input, select, button {{
      height: 42px; border: 1px solid rgba(23,33,27,.16); border-radius: 8px; background: rgba(255,255,255,.82);
      color: var(--ink); font: inherit; padding: 0 12px; box-shadow: 0 8px 22px rgba(32, 42, 34, .06);
    }}
    button {{ cursor: pointer; background: var(--ink); color: #fff; border-color: var(--ink); font-weight: 700; }}
    .metrics {{ display: grid; grid-template-columns: repeat(5, minmax(150px, 1fr)); gap: 12px; margin-bottom: 18px; }}
    .metric {{ background: rgba(255,255,255,.76); border: 1px solid rgba(23,33,27,.12); border-radius: 8px; padding: 14px; box-shadow: 0 14px 36px rgba(32, 42, 34, .08); }}
    .metric span {{ display: block; color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .08em; }}
    .metric strong {{ display: block; margin-top: 6px; font-size: 30px; line-height: 1; }}
    .board {{ display: grid; grid-template-columns: repeat(4, minmax(260px, 1fr)); gap: 14px; align-items: start; overflow-x: auto; padding-bottom: 8px; }}
    .lane {{ min-width: 260px; background: rgba(255,255,255,.54); border: 1px solid rgba(23,33,27,.13); border-radius: 8px; box-shadow: 0 18px 40px rgba(32, 42, 34, .09); padding: 12px; }}
    .lane-title {{ display: flex; justify-content: space-between; align-items: center; margin: 0 0 12px; font-weight: 800; }}
    .lane-title span {{ color: var(--muted); font-size: 12px; font-weight: 700; }}
    .card {{ background: var(--panel); border: 1px solid rgba(23,33,27,.12); border-radius: 8px; padding: 13px; margin-bottom: 10px; box-shadow: 0 10px 24px rgba(32, 42, 34, .08); }}
    .card h3 {{ margin: 0 0 4px; font-size: 17px; line-height: 1.2; }}
    .role {{ color: var(--muted); font-size: 13px; min-height: 34px; line-height: 1.35; }}
    .meta {{ display: flex; flex-wrap: wrap; gap: 7px; margin: 12px 0; }}
    .pill {{ border-radius: 999px; padding: 5px 8px; font-size: 12px; font-weight: 750; border: 1px solid rgba(23,33,27,.1); background: #f4f6f1; }}
    .Kochi .pill.status {{ background: var(--mint); color: var(--green); }}
    .Thiruvananthapuram .pill.status {{ background: var(--lav); color: var(--violet); }}
    .Bengaluru .pill.status {{ background: var(--sky); color: var(--blue); }}
    .Watchlist .pill.status {{ background: var(--sand); color: var(--gold); }}
    .note {{ color: #354039; font-size: 13px; line-height: 1.45; margin: 0 0 12px; }}
    .card a {{ color: var(--green); text-decoration: none; font-weight: 800; font-size: 13px; }}
    .footnote {{ margin-top: 18px; color: var(--muted); font-size: 12px; line-height: 1.55; }}
    @media (max-width: 900px) {{
      header, .toolbar, .metrics {{ grid-template-columns: 1fr; }}
      .stamp {{ min-width: 0; }}
      .board {{ grid-template-columns: 1fr; overflow-x: visible; }}
      .lane {{ min-width: 0; }}
    }}
  </style>
</head>
<body>
  <main class="shell">
    <header>
      <div class="brand">
        <svg class="app-icon" viewBox="0 0 64 64" aria-hidden="true">
          <rect width="64" height="64" rx="14" fill="#246b51"></rect>
          <path d="M23 22v-3.5A4.5 4.5 0 0 1 27.5 14h9A4.5 4.5 0 0 1 41 18.5V22" fill="none" stroke="#fff" stroke-width="4" stroke-linecap="round"></path>
          <rect x="15" y="22" width="34" height="26" rx="5" fill="#f7f8ef"></rect>
          <path d="M15 31h34" stroke="#246b51" stroke-width="4"></path>
          <path d="M26 39.5 30.2 44 39 34" fill="none" stroke="#246b51" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"></path>
        </svg>
        <div>
          <h1>Freshers IT Job Board</h1>
          <div class="sub">Latest verified public IT openings for software, data, QA, implementation, and trainee-style entry roles, sorted with location priority: Kochi first, then Thiruvananthapuram, then Bengaluru.</div>
        </div>
      </div>
      <div class="stamp">
        <span>Last refreshed</span>
        <strong>{html.escape(datetime.fromisoformat(refreshed_at.replace("Z", "+00:00")).strftime("%d %b %Y"))}</strong>
        <span>Priority order: Kochi -> Thiruvananthapuram -> Bengaluru</span>
        <span>Sources checked: {html.escape(source_summary)}</span>
      </div>
    </header>
    <section class="toolbar">
      <input id="search" type="search" placeholder="Search company, IT role, city, source">
      <select id="location">
        <option value="">All locations</option>
        <option>Kochi</option>
        <option>Thiruvananthapuram</option>
        <option>Bengaluru</option>
        <option>Watchlist</option>
      </select>
      <button id="download">Download CSV</button>
    </section>
    <section class="metrics" id="metrics"></section>
    <section class="board" id="board"></section>
    <p class="footnote">This refresh keeps only IT-focused postings that were publicly visible and verifiable in the latest run. If a source is inaccessible or does not expose direct listing rows, the board keeps the gap explicit instead of fabricating cards.</p>
  </main>
  <script>
    const jobs = {script_jobs};
    const lanes = [["Kochi","Priority 1"],["Thiruvananthapuram","Priority 2"],["Bengaluru","Priority 3"],["Watchlist","Source gaps"]];
    const board = document.querySelector("#board");
    const metrics = document.querySelector("#metrics");
    const search = document.querySelector("#search");
    const locationFilter = document.querySelector("#location");
    function card(job) {{
      return `<article class="card ${{job.location}}">
        <h3>${{job.company}}</h3>
        <div class="role">${{job.role}}</div>
        <div class="meta">
          <span class="pill status">${{job.location}}</span>
          <span class="pill">${{job.posted}}</span>
          <span class="pill">Apply by ${{job.apply_by}}</span>
          <span class="pill">${{job.level}}</span>
        </div>
        <p class="note">${{job.note}}</p>
        <a href="${{job.url}}" target="_blank" rel="noreferrer">Open source listing</a>
      </article>`;
    }}
    function render() {{
      const q = search.value.trim().toLowerCase();
      const selected = locationFilter.value;
      const filtered = jobs.filter(job => {{
        const text = `${{job.company}} ${{job.role}} ${{job.location}} ${{job.level}} ${{job.source}} ${{job.note}}`.toLowerCase();
        return (!q || text.includes(q)) && (!selected || job.location === selected);
      }});
      metrics.innerHTML = [
        ["Verified cards", filtered.length],
        ["Kochi", filtered.filter(j => j.location === "Kochi").length],
        ["Trivandrum", filtered.filter(j => j.location === "Thiruvananthapuram").length],
        ["Bengaluru", filtered.filter(j => j.location === "Bengaluru").length],
        ["Apply soon", filtered.filter(j => j.apply_by !== "n/a").length]
      ].map(([label, value]) => `<div class="metric"><span>${{label}}</span><strong>${{value}}</strong></div>`).join("");
      board.innerHTML = lanes.map(([lane, label]) => {{
        const laneJobs = filtered.filter(job => job.location === lane);
        return `<section class="lane">
          <div class="lane-title">${{lane}}<span>${{label}} / ${{laneJobs.length}}</span></div>
          ${{laneJobs.map(card).join("") || `<div class="card"><p class="note">No verified public IT fresher posting was captured for this lane in the current refresh.</p></div>`}}
        </section>`;
      }}).join("");
    }}
    function csvEscape(value) {{
      return `"${{String(value).replaceAll('"', '""')}}"`;
    }}
    document.querySelector("#download").addEventListener("click", () => {{
      const header = ["Company", "Role", "Location", "Posted", "Apply By", "Level", "Source", "Note", "URL"];
      const rows = jobs.map(job => [job.company, job.role, job.location, job.posted, job.apply_by, job.level, job.source, job.note, job.url]);
      const csv = [header, ...rows].map(row => row.map(csvEscape).join(",")).join("\\n");
      const blob = new Blob([csv], {{ type: "text/csv" }});
      const link = document.createElement("a");
      link.href = URL.createObjectURL(blob);
      link.download = "freshers-job-board.csv";
      link.click();
      URL.revokeObjectURL(link.href);
    }});
    search.addEventListener("input", render);
    locationFilter.addEventListener("change", render);
    render();
  </script>
</body>
</html>
"""


def write_csv(jobs: list[Job]) -> None:
    with CSV_PATH.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Company", "Role", "Location", "Posted", "Apply By", "Level", "Source", "Note", "URL"])
        writer.writerows(csv_rows(jobs))


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    collected: list[Job] = []
    source_notes: list[str] = []

    for page in (1, 2):
        try:
            page_jobs = scrape_infopark_page(page)
            collected.extend(page_jobs)
            source_notes.append(f"Infopark page {page}: {len(page_jobs)} IT roles")
        except Exception as exc:  # noqa: BLE001
            source_notes.append(f"Infopark page {page} failed: {exc}")

    try:
        technopark_jobs = scrape_technopark()
        collected.extend(technopark_jobs)
        source_notes.append(f"Technopark: {len(technopark_jobs)} IT roles")
    except Exception as exc:  # noqa: BLE001
        source_notes.append(f"Technopark failed: {exc}")

    try:
        bengaluru_jobs = scrape_bengaluru_direct_pages()
        collected.extend(bengaluru_jobs)
        source_notes.append(f"Bengaluru direct employer pages: {len(bengaluru_jobs)} IT roles")
    except Exception as exc:  # noqa: BLE001
        source_notes.append(f"Bengaluru direct employer pages failed: {exc}")

    try:
        naukri_jobs = scrape_naukri_discovery()
        collected.extend(naukri_jobs)
        source_notes.append(f"Naukri verified pages: {len(naukri_jobs)} IT roles")
    except Exception as exc:  # noqa: BLE001
        source_notes.append(f"Naukri discovery failed: {exc}")

    jobs = sort_jobs(dedupe_jobs(collected))
    jobs = add_watchlist_cards(jobs, source_notes)
    refreshed_at = now_utc().replace(microsecond=0).isoformat().replace("+00:00", "Z")
    source_summary = ", ".join(source_notes)

    JSON_PATH.write_text(
        json.dumps(
            {
                "refreshedAt": refreshed_at,
                "locationPriority": LOCATION_PRIORITY,
                "sourcesChecked": source_notes,
                "jobs": [asdict(job) for job in jobs],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    write_csv(jobs)
    HTML_PATH.write_text(render_html(jobs, refreshed_at, source_summary), encoding="utf-8")
    DOCS_HTML_PATH.write_text(HTML_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    DOCS_CSV_PATH.write_text(CSV_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    DOCS_JSON_PATH.write_text(JSON_PATH.read_text(encoding="utf-8"), encoding="utf-8")

    location_counts = {lane: metric_value(jobs, lane) for lane in LOCATION_PRIORITY}
    print(json.dumps({
        "verified_cards": sum(job.location != "Watchlist" for job in jobs),
        "location_counts": location_counts,
        "sources": source_notes,
        "html": str(HTML_PATH),
    }, indent=2))


if __name__ == "__main__":
    main()
