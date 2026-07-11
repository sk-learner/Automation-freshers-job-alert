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
NTFY_NOTIFICATION_PATH = OUTPUT_DIR / "ntfy-notification.json"
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


def previous_jobs_snapshot() -> list[dict[str, str]]:
    for path in (DOCS_JSON_PATH, JSON_PATH):
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        jobs = payload.get("jobs", [])
        if isinstance(jobs, list):
            return [job for job in jobs if isinstance(job, dict)]
    return []


def job_key(job: Job | dict[str, str]) -> tuple[str, str, str, str]:
    if isinstance(job, Job):
        return (job.company.strip(), job.role.strip(), job.location.strip(), job.url.strip())
    return (
        str(job.get("company", "")).strip(),
        str(job.get("role", "")).strip(),
        str(job.get("location", "")).strip(),
        str(job.get("url", "")).strip(),
    )


def build_ntfy_notification(jobs: list[Job], previous_jobs: list[dict[str, str]]) -> dict[str, object] | None:
    previous_keys = {
        job_key(job)
        for job in previous_jobs
        if str(job.get("location", "")).strip() != "Watchlist"
    }
    verified_jobs = [job for job in jobs if job.location != "Watchlist"]
    new_jobs = [job for job in verified_jobs if job_key(job) not in previous_keys]
    if not new_jobs:
        return None

    board_url = "https://sk-learner.github.io/Automation-freshers-job-alert/"
    headline = f"{len(new_jobs)} new fresher IT job" + ("" if len(new_jobs) == 1 else "s")
    lines = [f"**{headline} found**", ""]
    for job in new_jobs[:6]:
        lines.append(f"- **{job.role}** at **{job.company}** ({job.location})")
    if len(new_jobs) > 6:
        lines.append(f"- plus {len(new_jobs) - 6} more on the board")
    lines.extend(["", f"[Open live board]({board_url})"])
    priority = "high" if any(job.location == "Kochi" for job in new_jobs) else "default"
    return {
        "newJobCount": len(new_jobs),
        "title": "Freshers IT board updated",
        "message": "\n".join(lines),
        "click": board_url,
        "tags": "briefcase,computer",
        "priority": priority,
    }


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
      --ink: #132238;
      --ink-soft: #4d5b6a;
      --panel: rgba(255, 255, 255, .82);
      --panel-strong: #ffffff;
      --line: rgba(19, 34, 56, .10);
      --teal: #0d766e;
      --teal-soft: #d8f3ef;
      --navy: #24568c;
      --navy-soft: #dceafa;
      --amber: #996515;
      --amber-soft: #f9edc9;
      --rose: #b24c63;
      --rose-soft: #f8dce3;
      --shadow: 0 24px 80px rgba(19, 34, 56, .12);
      --shadow-soft: 0 16px 40px rgba(19, 34, 56, .08);
      --radius-xl: 28px;
      --radius-lg: 20px;
      --radius-md: 14px;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "Plus Jakarta Sans", "Inter", ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(13, 118, 110, .18), transparent 28rem),
        radial-gradient(circle at top right, rgba(36, 86, 140, .16), transparent 26rem),
        linear-gradient(180deg, #f3f8fb 0%, #eff4f8 44%, #fbfcfe 100%);
      min-height: 100vh;
    }}
    .shell {{ width: min(1380px, calc(100vw - 32px)); margin: 0 auto; padding: 30px 0 56px; }}
    .hero {{
      position: relative;
      overflow: hidden;
      padding: 26px;
      border: 1px solid rgba(255, 255, 255, .7);
      border-radius: var(--radius-xl);
      background:
        linear-gradient(135deg, rgba(8, 40, 73, .95) 0%, rgba(16, 68, 87, .94) 52%, rgba(13, 118, 110, .92) 100%);
      box-shadow: var(--shadow);
      color: #f3fbff;
    }}
    .hero::before {{
      content: "";
      position: absolute;
      inset: auto -6rem -7rem auto;
      width: 22rem;
      height: 22rem;
      border-radius: 50%;
      background: rgba(255, 255, 255, .08);
      filter: blur(10px);
    }}
    header {{ display: grid; grid-template-columns: minmax(0, 1.5fr) minmax(280px, .9fr); gap: 24px; align-items: stretch; }}
    .brand {{ display: flex; gap: 18px; position: relative; z-index: 1; }}
    .app-icon {{
      width: 64px;
      height: 64px;
      flex: 0 0 auto;
      border-radius: 18px;
      box-shadow: 0 18px 40px rgba(0, 0, 0, .18);
      background: rgba(255, 255, 255, .14);
      backdrop-filter: blur(10px);
    }}
    h1 {{
      margin: 0;
      font-size: clamp(34px, 5vw, 66px);
      line-height: .94;
      letter-spacing: -.05em;
    }}
    .eyebrow {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      margin-bottom: 14px;
      padding: 7px 12px;
      border: 1px solid rgba(255, 255, 255, .18);
      border-radius: 999px;
      background: rgba(255, 255, 255, .10);
      font-size: 12px;
      font-weight: 700;
      letter-spacing: .08em;
      text-transform: uppercase;
    }}
    .hero-copy {{
      margin-top: 18px;
      max-width: 700px;
      color: rgba(243, 251, 255, .78);
      font-size: 15px;
      line-height: 1.6;
    }}
    .hero-meta {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 18px;
    }}
    .hero-chip {{
      padding: 10px 14px;
      border-radius: 999px;
      background: rgba(255, 255, 255, .12);
      border: 1px solid rgba(255, 255, 255, .14);
      font-size: 13px;
      font-weight: 600;
      color: rgba(243, 251, 255, .92);
    }}
    .stamp {{
      position: relative;
      z-index: 1;
      display: grid;
      gap: 16px;
      align-content: start;
      padding: 22px;
      border: 1px solid rgba(255, 255, 255, .16);
      border-radius: 22px;
      background: rgba(255, 255, 255, .09);
      backdrop-filter: blur(12px);
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, .1);
    }}
    .stamp-label {{
      color: rgba(243, 251, 255, .68);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: .1em;
      font-weight: 700;
    }}
    .stamp strong {{
      display: block;
      font-size: 34px;
      line-height: .95;
      letter-spacing: -.04em;
    }}
    .stamp-list {{
      display: grid;
      gap: 10px;
      color: rgba(243, 251, 255, .84);
      font-size: 13px;
      line-height: 1.5;
    }}
    .toolbar {{
      display: grid;
      grid-template-columns: minmax(220px, 1fr) 220px auto;
      gap: 12px;
      margin: 22px 0 18px;
      align-items: center;
    }}
    input, select, button {{
      height: 50px;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: rgba(255,255,255,.9);
      color: var(--ink);
      font: inherit;
      padding: 0 16px;
      box-shadow: var(--shadow-soft);
    }}
    input:focus, select:focus, button:focus {{
      outline: 2px solid rgba(13, 118, 110, .18);
      outline-offset: 2px;
    }}
    button {{
      cursor: pointer;
      background: linear-gradient(135deg, #132238 0%, #24568c 100%);
      color: #fff;
      border-color: transparent;
      font-weight: 800;
    }}
    .metrics {{
      display: grid;
      grid-template-columns: repeat(5, minmax(130px, 1fr));
      gap: 14px;
      margin-bottom: 22px;
    }}
    .metric {{
      padding: 18px 18px 16px;
      border: 1px solid var(--line);
      border-radius: var(--radius-lg);
      background: var(--panel);
      box-shadow: var(--shadow-soft);
      backdrop-filter: blur(12px);
    }}
    .metric span {{
      display: block;
      color: var(--ink-soft);
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: .12em;
      font-weight: 800;
    }}
    .metric strong {{
      display: block;
      margin-top: 10px;
      font-size: clamp(28px, 3vw, 40px);
      line-height: 1;
      letter-spacing: -.04em;
    }}
    .board {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 18px;
      align-items: start;
    }}
    .lane {{
      padding: 18px;
      border: 1px solid var(--line);
      border-radius: 24px;
      background: rgba(255, 255, 255, .56);
      box-shadow: var(--shadow-soft);
      backdrop-filter: blur(10px);
    }}
    .lane-head {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 16px;
      gap: 10px;
    }}
    .lane-title {{
      margin: 0;
      font-size: 20px;
      line-height: 1;
      letter-spacing: -.03em;
    }}
    .lane-kicker {{
      margin-top: 6px;
      color: var(--ink-soft);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: .1em;
      font-weight: 800;
    }}
    .lane-count {{
      min-width: 44px;
      height: 44px;
      display: grid;
      place-items: center;
      border-radius: 14px;
      font-size: 16px;
      font-weight: 900;
      color: var(--ink);
      background: rgba(255,255,255,.9);
      border: 1px solid var(--line);
    }}
    .stack {{
      display: grid;
      gap: 12px;
    }}
    .card {{
      position: relative;
      overflow: hidden;
      padding: 18px;
      border: 1px solid var(--line);
      border-radius: 20px;
      background: var(--panel-strong);
      box-shadow: var(--shadow-soft);
      transition: transform .18s ease, box-shadow .18s ease, border-color .18s ease;
    }}
    .card:hover {{
      transform: translateY(-2px);
      box-shadow: 0 22px 44px rgba(19, 34, 56, .12);
      border-color: rgba(19, 34, 56, .16);
    }}
    .card::before {{
      content: "";
      position: absolute;
      inset: 0 auto 0 0;
      width: 5px;
      background: linear-gradient(180deg, #0d766e 0%, #24568c 100%);
    }}
    .card-top {{
      display: flex;
      align-items: start;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 10px;
    }}
    .company {{
      margin: 0;
      font-size: 18px;
      line-height: 1.15;
      letter-spacing: -.02em;
    }}
    .role {{
      margin: 0;
      color: var(--ink-soft);
      font-size: 14px;
      line-height: 1.5;
    }}
    .badge {{
      flex: 0 0 auto;
      padding: 8px 10px;
      border-radius: 12px;
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: .08em;
      font-weight: 800;
      border: 1px solid transparent;
    }}
    .Kochi .badge {{ background: var(--teal-soft); color: var(--teal); border-color: rgba(13, 118, 110, .16); }}
    .Thiruvananthapuram .badge {{ background: var(--rose-soft); color: var(--rose); border-color: rgba(178, 76, 99, .16); }}
    .Bengaluru .badge {{ background: var(--navy-soft); color: var(--navy); border-color: rgba(36, 86, 140, .16); }}
    .Watchlist .badge {{ background: var(--amber-soft); color: var(--amber); border-color: rgba(153, 101, 21, .16); }}
    .meta-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
      margin: 14px 0;
    }}
    .meta-item {{
      padding: 10px 12px;
      border-radius: 14px;
      background: #f6f9fc;
      border: 1px solid rgba(19, 34, 56, .06);
    }}
    .meta-item span {{
      display: block;
      margin-bottom: 4px;
      color: var(--ink-soft);
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: .08em;
      font-weight: 800;
    }}
    .meta-item strong {{
      font-size: 13px;
      line-height: 1.35;
    }}
    .note {{
      margin: 0 0 14px;
      color: var(--ink-soft);
      font-size: 13px;
      line-height: 1.55;
    }}
    .card a {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      color: var(--ink);
      text-decoration: none;
      font-weight: 800;
      font-size: 13px;
    }}
    .card a::after {{
      content: "↗";
      font-size: 14px;
    }}
    .empty {{
      padding: 22px 18px;
      border: 1px dashed rgba(19, 34, 56, .18);
      border-radius: 18px;
      background: rgba(255,255,255,.46);
      color: var(--ink-soft);
      font-size: 14px;
      line-height: 1.55;
    }}
    .footnote {{
      margin-top: 18px;
      color: var(--ink-soft);
      font-size: 12px;
      line-height: 1.6;
    }}
    @media (max-width: 900px) {{
      .shell {{ width: min(100vw - 18px, 1380px); padding: 18px 0 40px; }}
      .hero {{ padding: 18px; border-radius: 24px; }}
      header, .toolbar, .metrics, .board, .meta-grid {{ grid-template-columns: 1fr; }}
      .stamp strong {{ font-size: 28px; }}
      .toolbar {{ margin-top: 18px; }}
    }}
  </style>
</head>
<body>
  <main class="shell">
    <section class="hero">
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
            <div class="eyebrow">Auto-refreshing every 30 minutes</div>
            <h1>Freshers IT Job Board</h1>
            <div class="hero-copy">Verified IT fresher roles from public sources, ranked for Kochi first, then Thiruvananthapuram, then Bengaluru. Clean signal only: direct listings, tracked source coverage, fast filtering.</div>
            <div class="hero-meta">
              <div class="hero-chip">Software</div>
              <div class="hero-chip">Data</div>
              <div class="hero-chip">QA</div>
              <div class="hero-chip">Trainee</div>
              <div class="hero-chip">Intern</div>
            </div>
          </div>
        </div>
        <div class="stamp">
          <div>
            <div class="stamp-label">Last refreshed</div>
            <strong>{html.escape(datetime.fromisoformat(refreshed_at.replace("Z", "+00:00")).strftime("%d %b %Y"))}</strong>
          </div>
          <div class="stamp-list">
            <div>Priority: Kochi -> Thiruvananthapuram -> Bengaluru</div>
            <div>Sources: {html.escape(source_summary)}</div>
          </div>
        </div>
      </header>
    </section>
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
        <div class="card-top">
          <div>
            <h3 class="company">${{job.company}}</h3>
            <p class="role">${{job.role}}</p>
          </div>
          <div class="badge">${{job.location}}</div>
        </div>
        <div class="meta-grid">
          <div class="meta-item"><span>Posted</span><strong>${{job.posted}}</strong></div>
          <div class="meta-item"><span>Apply by</span><strong>${{job.apply_by}}</strong></div>
          <div class="meta-item"><span>Level</span><strong>${{job.level}}</strong></div>
          <div class="meta-item"><span>Source</span><strong>${{job.source}}</strong></div>
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
          <div class="lane-head">
            <div>
              <h2 class="lane-title">${{lane}}</h2>
              <div class="lane-kicker">${{label}}</div>
            </div>
            <div class="lane-count">${{laneJobs.length}}</div>
          </div>
          <div class="stack">
            ${{laneJobs.map(card).join("") || `<div class="empty">No verified public IT fresher posting was captured for this lane in the current refresh.</div>`}}
          </div>
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
    previous_jobs = previous_jobs_snapshot()
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
    notification = build_ntfy_notification(jobs, previous_jobs)
    if notification is None:
        NTFY_NOTIFICATION_PATH.unlink(missing_ok=True)
    else:
        NTFY_NOTIFICATION_PATH.write_text(json.dumps(notification, indent=2), encoding="utf-8")

    location_counts = {lane: metric_value(jobs, lane) for lane in LOCATION_PRIORITY}
    print(json.dumps({
        "verified_cards": sum(job.location != "Watchlist" for job in jobs),
        "location_counts": location_counts,
        "sources": source_notes,
        "new_jobs": 0 if notification is None else notification["newJobCount"],
        "html": str(HTML_PATH),
    }, indent=2))


if __name__ == "__main__":
    main()
