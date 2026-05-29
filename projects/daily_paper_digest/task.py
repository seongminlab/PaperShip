from __future__ import annotations

import html
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlsplit, urlunsplit
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from xml.etree import ElementTree


ROOT = Path(__file__).resolve().parents[2]
STATE_DIR = ROOT / "data" / "daily_paper_digest"
SEEN_PATH = STATE_DIR / "seen_articles.json"
SENT_ARTICLES_PATH = STATE_DIR / "sent_articles.json"
KEYWORDS_PATH = STATE_DIR / "keywords.json"
PAPERS_DIR = ROOT / "papers"
CONFIG_DIR = ROOT / "config"
SECRETS_PATH = CONFIG_DIR / "secrets.env"
JOURNALS_PATH = CONFIG_DIR / "journals.json"


@dataclass(frozen=True)
class JournalSource:
    name: str
    listing_url: str
    discovery: str
    base_url: str
    feed_url: str | None = None
    allowed_item_types: tuple[str, ...] = ()
    article_url_patterns: tuple[str, ...] = ()


@dataclass(frozen=True)
class Article:
    url: str
    title: str
    journal: str
    authors: list[str]
    summary_source: str
    published: str
    pdf_url: str | None


@dataclass(frozen=True)
class ArticleCandidate:
    source: JournalSource
    url: str
    title: str = ""
    journal: str = ""
    authors: tuple[str, ...] = ()
    summary_source: str = ""
    published: str = ""
    pdf_url: str | None = None


def load_journal_sources(path: Path = JOURNALS_PATH) -> list[JournalSource]:
    if not path.exists():
        logging.warning("Journal source config does not exist: %s", path)
        return []

    raw_sources = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw_sources, list):
        raise ValueError(f"{path} must contain a JSON array")

    sources: list[JournalSource] = []
    for index, raw_source in enumerate(raw_sources, start=1):
        if not isinstance(raw_source, dict):
            raise ValueError(f"{path} item {index} must be an object")

        name = required_string(raw_source, "name", path, index)
        listing_url = required_string(raw_source, "listing_url", path, index)
        discovery = required_string(raw_source, "discovery", path, index)
        base_url = required_string(raw_source, "base_url", path, index)
        feed_url = optional_string(raw_source, "feed_url")
        allowed_item_types = tuple(optional_string_list(raw_source, "allowed_item_types"))
        article_url_patterns = tuple(optional_string_list(raw_source, "article_url_patterns"))

        if discovery not in {"nature_html", "rss", "html_links"}:
            raise ValueError(f"{path} item {index} has unsupported discovery: {discovery}")
        if discovery == "rss" and not feed_url:
            raise ValueError(f"{path} item {index} uses rss discovery but has no feed_url")
        if discovery == "html_links" and not article_url_patterns:
            raise ValueError(f"{path} item {index} uses html_links discovery but has no article_url_patterns")

        sources.append(
            JournalSource(
                name=name,
                listing_url=listing_url,
                discovery=discovery,
                base_url=base_url,
                feed_url=feed_url,
                allowed_item_types=allowed_item_types,
                article_url_patterns=article_url_patterns,
            )
        )

    return sources


def required_string(raw_source: dict[str, Any], key: str, path: Path, index: int) -> str:
    value = raw_source.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path} item {index} requires a non-empty {key}")
    return value.strip()


def optional_string(raw_source: dict[str, Any], key: str) -> str | None:
    value = raw_source.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string when set")
    return value.strip() or None


def optional_string_list(raw_source: dict[str, Any], key: str) -> list[str]:
    value = raw_source.get(key, [])
    if not isinstance(value, list):
        raise ValueError(f"{key} must be an array when set")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{key} must contain only non-empty strings")
        result.append(item.strip())
    return result


class LinkParser(HTMLParser):
    def __init__(self, base_url: str, patterns: tuple[str, ...] = (r"^/articles/[a-z0-9-]+$",)) -> None:
        super().__init__()
        self.base_url = base_url
        self.patterns = tuple(re.compile(pattern) for pattern in patterns)
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        href = dict(attrs).get("href")
        if not href:
            return
        absolute_url = urljoin(self.base_url, href)
        if any(pattern.search(href) or pattern.search(absolute_url) for pattern in self.patterns):
            self.links.append(absolute_url)


class HtmlArticleParser(HTMLParser):
    def __init__(self, source: JournalSource) -> None:
        super().__init__()
        self.source = source
        self.patterns = tuple(re.compile(pattern) for pattern in source.article_url_patterns)
        self.entries: dict[str, dict[str, str]] = {}
        self.current_link_url: str | None = None
        self.current_link_depth = 0
        self.current_date_url: str | None = None
        self.current_date_depth = 0
        self.last_url: str | None = None

    def matches_article_url(self, href: str, absolute_url: str) -> bool:
        return any(pattern.search(href) or pattern.search(absolute_url) for pattern in self.patterns)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        href = values.get("href")
        if tag == "a" and href:
            absolute_url = urljoin(self.source.base_url, href)
            if self.matches_article_url(href, absolute_url):
                url = canonical_url(absolute_url)
                self.entries.setdefault(url, {"url": url})
                self.current_link_url = url
                self.current_link_depth = 1
                self.last_url = url
                return

        if self.current_link_url:
            self.current_link_depth += 1

        if values.get("data-testid") == "article-entry-date" and self.last_url:
            self.current_date_url = self.last_url
            self.current_date_depth = 1
        elif self.current_date_url:
            self.current_date_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if self.current_link_url:
            self.current_link_depth -= 1
            if self.current_link_depth <= 0:
                self.current_link_url = None
                self.current_link_depth = 0

        if self.current_date_url:
            self.current_date_depth -= 1
            if self.current_date_depth <= 0:
                self.current_date_url = None
                self.current_date_depth = 0

    def handle_data(self, data: str) -> None:
        text = re.sub(r"\s+", " ", data).strip()
        if not text:
            return
        if self.current_link_url:
            entry = self.entries.setdefault(self.current_link_url, {"url": self.current_link_url})
            entry["title"] = " ".join(part for part in (entry.get("title"), text) if part).strip()
        if self.current_date_url:
            entry = self.entries.setdefault(self.current_date_url, {"url": self.current_date_url})
            entry["published"] = " ".join(part for part in (entry.get("published"), text) if part).strip()

    def article_candidates(self) -> list[ArticleCandidate]:
        candidates: list[ArticleCandidate] = []
        for entry in self.entries.values():
            url = entry["url"]
            title = entry.get("title", "")
            candidates.append(
                ArticleCandidate(
                    source=self.source,
                    url=url,
                    title=title,
                    journal=self.source.name,
                    summary_source=title or "No abstract available.",
                    published=entry.get("published", ""),
                    pdf_url=build_pdf_url({}, url),
                )
            )
        return candidates


class MetaParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.meta: dict[str, list[str]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "meta":
            return
        values = dict(attrs)
        key = values.get("name") or values.get("property")
        content = values.get("content")
        if key and content:
            self.meta.setdefault(key, []).append(html.unescape(content.strip()))


class PlainTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if text:
            self.parts.append(text)


def html_to_text(value: str) -> str:
    parser = PlainTextParser()
    parser.feed(value)
    return html.unescape(re.sub(r"\s+", " ", " ".join(parser.parts)).strip())


def fetch_text(url: str) -> str:
    timeout = int(os.getenv("JOURNAL_REQUEST_TIMEOUT", os.getenv("NATURE_REQUEST_TIMEOUT", "25")))
    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 PaperShip journal-digest/1.0",
            "Accept": "text/html,application/xhtml+xml,application/rss+xml,application/xml,text/xml,*/*",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def load_env_file(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key and key not in os.environ:
            os.environ[key] = value


def load_seen() -> set[str]:
    if not SEEN_PATH.exists():
        return set()
    return set(json.loads(SEEN_PATH.read_text(encoding="utf-8")))


def save_seen(seen: set[str]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    SEEN_PATH.write_text(
        json.dumps(sorted(seen), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def save_json_object(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def canonical_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def article_id(url: str) -> str:
    return hashlib.sha256(canonical_url(url).encode("utf-8")).hexdigest()[:16]


def article_to_record(article: Article, summary: list[str] | None = None) -> dict[str, Any]:
    record: dict[str, Any] = {
        "id": article_id(article.url),
        "url": article.url,
        "title": article.title,
        "journal": article.journal,
        "authors": article.authors,
        "summary_source": article.summary_source,
        "published": article.published,
        "pdf_url": article.pdf_url,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if summary is not None:
        record["summary"] = summary
    return record


def article_from_record(record: dict[str, Any]) -> Article:
    return Article(
        url=str(record.get("url") or ""),
        title=str(record.get("title") or "Untitled"),
        journal=str(record.get("journal") or "Unknown"),
        authors=[str(author) for author in record.get("authors", []) if str(author).strip()],
        summary_source=str(record.get("summary_source") or record.get("title") or ""),
        published=str(record.get("published") or ""),
        pdf_url=str(record["pdf_url"]) if record.get("pdf_url") else None,
    )


def save_sent_article(article: Article, summary: list[str]) -> str:
    data = load_json_object(SENT_ARTICLES_PATH)
    articles = data.setdefault("articles", {})
    if not isinstance(articles, dict):
        articles = {}
        data["articles"] = articles

    identifier = article_id(article.url)
    articles[identifier] = article_to_record(article, summary)
    save_json_object(SENT_ARTICLES_PATH, data)
    return identifier


def load_sent_article(identifier: str) -> Article | None:
    data = load_json_object(SENT_ARTICLES_PATH)
    articles = data.get("articles", {})
    if not isinstance(articles, dict):
        return None
    record = articles.get(identifier)
    if not isinstance(record, dict):
        return None
    return article_from_record(record)


def unique_urls(urls: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for url in urls:
        canonical = canonical_url(url)
        if canonical not in seen:
            result.append(canonical)
            seen.add(canonical)
    return result


def discover_nature_article_urls(source: JournalSource) -> list[str]:
    parser = LinkParser(source.base_url)
    parser.feed(fetch_text(source.listing_url))
    return unique_urls(parser.links)


def discover_nature_article_candidates(source: JournalSource) -> list[ArticleCandidate]:
    return [ArticleCandidate(source=source, url=url) for url in discover_nature_article_urls(source)]


def discover_html_link_article_candidates(source: JournalSource) -> list[ArticleCandidate]:
    parser = HtmlArticleParser(source)
    parser.feed(fetch_text(source.listing_url))
    return parser.article_candidates()


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def item_fields(item: ElementTree.Element) -> dict[str, list[str]]:
    fields: dict[str, list[str]] = {}
    for child in item:
        text = (child.text or "").strip()
        if text:
            fields.setdefault(local_name(child.tag), []).append(html.unescape(text))
    return fields


def rss_items(root: ElementTree.Element) -> list[ElementTree.Element]:
    rss1_items = root.findall(".//{http://purl.org/rss/1.0/}item")
    if rss1_items:
        return rss1_items
    return root.findall(".//item")


def rss_field(fields: dict[str, list[str]], *names: str) -> str:
    for name in names:
        values = fields.get(name)
        if values:
            return values[0]
    return ""


def rss_fields(fields: dict[str, list[str]], *names: str) -> list[str]:
    values: list[str] = []
    for name in names:
        values.extend(fields.get(name, []))
    return values


def rss_authors(fields: dict[str, list[str]]) -> tuple[str, ...]:
    authors: list[str] = []
    for creator in rss_fields(fields, "creator", "author"):
        parts = re.split(r",|;|\band\b", creator)
        for part in parts:
            author = re.sub(r"\s+", " ", part).strip()
            if author and author not in authors:
                authors.append(author)
    return tuple(authors)


def discover_rss_article_urls(source: JournalSource) -> list[str]:
    return [candidate.url for candidate in discover_rss_article_candidates(source)]


def discover_rss_article_candidates(source: JournalSource) -> list[ArticleCandidate]:
    if not source.feed_url:
        return []

    root = ElementTree.fromstring(fetch_text(source.feed_url))
    candidates: list[ArticleCandidate] = []
    seen_urls: set[str] = set()
    for item in rss_items(root):
        fields = item_fields(item)
        item_type = rss_field(fields, "type", "section")
        if source.allowed_item_types and item_type not in source.allowed_item_types:
            continue

        link = rss_field(fields, "url", "link")
        if link:
            url = canonical_url(link)
            if url in seen_urls:
                continue
            seen_urls.add(url)
            description = html_to_text(rss_field(fields, "description", "encoded"))
            candidates.append(
                ArticleCandidate(
                    source=source,
                    url=url,
                    title=rss_field(fields, "title") or "Untitled",
                    journal=rss_field(fields, "source", "publicationName") or source.name,
                    authors=rss_authors(fields),
                    summary_source=description or rss_field(fields, "title") or "No abstract available.",
                    published=rss_field(fields, "publicationDate", "date", "pubDate", "coverDate"),
                    pdf_url=build_pdf_url({}, url),
                )
            )

    return candidates


def discover_article_urls(source: JournalSource) -> list[str]:
    return [candidate.url for candidate in discover_article_candidates(source)]


def discover_article_candidates(source: JournalSource) -> list[ArticleCandidate]:
    max_articles = int(
        os.getenv(
            "JOURNAL_MAX_ARTICLES_PER_SOURCE",
            os.getenv("NATURE_MAX_ARTICLES_PER_RUN", "30"),
        )
    )
    if source.discovery == "rss":
        return discover_rss_article_candidates(source)[:max_articles]
    if source.discovery == "html_links":
        return discover_html_link_article_candidates(source)[:max_articles]
    return discover_nature_article_candidates(source)[:max_articles]


def discover_all_article_candidates(
    sources: list[JournalSource] | None = None,
) -> dict[JournalSource, list[ArticleCandidate]]:
    discovered: dict[JournalSource, list[ArticleCandidate]] = {}
    for source in sources or load_journal_sources():
        try:
            candidates = discover_article_candidates(source)
        except (ElementTree.ParseError, HTTPError, URLError, TimeoutError):
            logging.exception("Failed to discover articles for %s", source.name)
            candidates = []
        discovered[source] = candidates
    return discovered


def first(meta: dict[str, list[str]], *keys: str) -> str:
    for key in keys:
        values = meta.get(key)
        if values:
            return values[0]
    return ""


def build_pdf_url(meta: dict[str, list[str]], article_url: str) -> str | None:
    explicit = first(meta, "citation_pdf_url")
    if explicit:
        return explicit

    nature_match = re.search(r"nature\.com/articles/([^/?#]+)", article_url)
    if nature_match:
        return f"https://www.nature.com/articles/{nature_match.group(1)}.pdf"

    science_match = re.search(r"science\.org/doi/(?:abs|full)/([^?#]+)", article_url)
    if science_match:
        return f"https://www.science.org/doi/pdf/{science_match.group(1)}"

    cell_match = re.search(r"cell\.com/[^/]+/fulltext/([^?#]+)", article_url)
    if cell_match:
        return f"https://www.cell.com/action/showPdf?pii={quote(cell_match.group(1), safe='')}"

    lancet_match = re.search(r"thelancet\.com/[^?#]+/article/([^/?#]+)/fulltext", article_url)
    if lancet_match:
        return f"https://www.thelancet.com/action/showPdf?pii={quote(lancet_match.group(1), safe='')}"

    doi_match = re.search(r"(?:nejm\.org|acpjournals\.org)/doi/(?:abs|full)/([^?#]+)", article_url)
    if doi_match:
        return f"https://{urlsplit(article_url).netloc}/doi/pdf/{doi_match.group(1)}"

    bmj_match = re.search(r"bmj\.com/content/([^?#]+)", article_url)
    if bmj_match:
        return f"https://www.bmj.com/content/{bmj_match.group(1)}.full.pdf"

    jama_match = re.search(r"jamanetwork\.com/journals/[^/]+/fullarticle/(\d+)", article_url)
    if jama_match:
        return f"https://jamanetwork.com/journals/jama/articlepdf/{jama_match.group(1)}"

    oup_match = re.search(r"academic\.oup\.com/([^/]+)/article/([^/]+)/([^/]+)/([^/?#]+)/(\d+)", article_url)
    if oup_match:
        journal, volume, issue, slug, article_id_value = oup_match.groups()
        return f"https://academic.oup.com/{journal}/article-pdf/{volume}/{issue}/{slug}/{article_id_value}/{slug}.pdf"

    return None


def infer_doi_from_url(url: str) -> str | None:
    doi_match = re.search(r"(10\.\d{4,9}/[^?#\s]+)", url)
    if doi_match:
        return doi_match.group(1).rstrip("/")

    oup_match = re.search(r"academic\.oup\.com/([^/]+)/article/[^/]+/[^/]+/([^/?#]+)/\d+", url)
    if oup_match:
        journal, slug = oup_match.groups()
        if is_oup_page_like_slug(slug):
            return None
        return f"10.1093/{journal}/{slug}"

    return None


def crossref_article(url: str) -> Article | None:
    doi = infer_doi_from_url(url)
    if not doi:
        return crossref_article_by_oup_page(url)

    message = crossref_work_message(doi)
    if message is None:
        return crossref_article_by_oup_page(url)

    return article_from_crossref_message(url, message, doi)


def crossref_work_message(doi: str) -> dict[str, Any] | None:
    request = Request(
        f"https://api.crossref.org/works/{quote(doi, safe='')}",
        headers={
            "User-Agent": "PaperShip journal-digest/1.0",
            "Accept": "application/json",
        },
    )
    timeout = int(os.getenv("JOURNAL_REQUEST_TIMEOUT", os.getenv("NATURE_REQUEST_TIMEOUT", "25")))
    try:
        with urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8", errors="replace"))
    except HTTPError as exc:
        if exc.code == 404:
            return None
        raise

    message = body.get("message", {})
    if not isinstance(message, dict):
        return None
    return message


def article_from_crossref_message(url: str, message: dict[str, Any], doi: str | None = None) -> Article:
    title = first_crossref_string(message, "title") or doi or "Untitled"
    journal = first_crossref_string(message, "container-title", "short-container-title") or "Unknown"
    abstract = html_to_text(str(message.get("abstract") or ""))
    authors = crossref_authors(message.get("author"))
    published = crossref_date(message)
    return Article(
        url=canonical_url(url),
        title=title,
        journal=journal,
        authors=authors,
        summary_source=abstract or title,
        published=published,
        pdf_url=build_pdf_url({}, url) or build_oup_pdf_url(url, doi),
    )


def build_oup_pdf_url(url: str, doi: str | None) -> str | None:
    if not doi:
        return None
    oup_match = re.search(r"academic\.oup\.com/([^/]+)/article/([^/]+)/([^/]+)/([^/?#]+)/(\d+)", url)
    doi_match = re.search(r"10\.1093/[^/]+/([^/?#]+)", doi)
    if not oup_match or not doi_match:
        return None
    journal, volume, issue, _slug, article_id_value = oup_match.groups()
    article_code = doi_match.group(1)
    return f"https://academic.oup.com/{journal}/article-pdf/{volume}/{issue}/{article_code}/{article_id_value}/{article_code}.pdf"


def crossref_article_by_oup_page(url: str) -> Article | None:
    oup_match = re.search(r"academic\.oup\.com/([^/]+)/article/([^/]+)/([^/]+)/([^/?#]+)/(\d+)", url)
    if not oup_match:
        return None
    journal, volume, issue, page_or_slug, _article_id_value = oup_match.groups()
    if not is_oup_page_like_slug(page_or_slug):
        return None

    message = crossref_search_oup_page(journal, volume, issue, page_or_slug)
    if not message:
        return None
    doi = str(message.get("DOI") or "")
    return article_from_crossref_message(url, message, doi or None)


def crossref_search_oup_page(journal: str, volume: str, issue: str, page: str) -> dict[str, Any] | None:
    queries = [
        f"{journal} {volume} {issue} {page}",
        f"{oup_journal_title(journal)} {volume} {issue} {page}",
    ]
    timeout = int(os.getenv("JOURNAL_REQUEST_TIMEOUT", os.getenv("NATURE_REQUEST_TIMEOUT", "25")))
    for query in queries:
        request = Request(
            f"https://api.crossref.org/works?query.bibliographic={quote(query)}&rows=10",
            headers={
                "User-Agent": "PaperShip journal-digest/1.0",
                "Accept": "application/json",
            },
        )
        with urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8", errors="replace"))
        items = body.get("message", {}).get("items", [])
        if not isinstance(items, list):
            continue
        match = best_crossref_page_match(items, journal, volume, issue, page)
        if match:
            return match
    return None


def best_crossref_page_match(
    items: list[Any],
    journal: str,
    volume: str,
    issue: str,
    page: str,
) -> dict[str, Any] | None:
    expected_title = oup_journal_title(journal).casefold()
    expected_doi_segment = oup_doi_journal_segment(journal)
    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("volume") or "") != volume:
            continue
        if str(item.get("issue") or "") != issue:
            continue
        page_value = str(item.get("page") or "")
        if not (page_value == page or page_value.startswith(f"{page}-")):
            continue
        doi = str(item.get("DOI") or "").casefold()
        container = " ".join(str(part) for part in item.get("container-title", [])).casefold()
        if expected_doi_segment and f"10.1093/{expected_doi_segment}/" in doi:
            return item
        if expected_title and expected_title in container:
            return item
    return None


def oup_journal_title(journal: str) -> str:
    titles = {
        "mbe": "Molecular Biology and Evolution",
        "gbe": "Genome Biology and Evolution",
        "nar": "Nucleic Acids Research",
    }
    return titles.get(journal, journal)


def oup_doi_journal_segment(journal: str) -> str:
    segments = {
        "mbe": "molbev",
        "gbe": "gbe",
        "nar": "nar",
    }
    return segments.get(journal, journal)


def is_oup_page_like_slug(slug: str) -> bool:
    return bool(re.fullmatch(r"\d+|e\d+", slug, flags=re.IGNORECASE))


def first_crossref_string(message: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = message.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str) and item.strip():
                    return item.strip()
        elif isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def crossref_authors(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    authors: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        given = str(item.get("given") or "").strip()
        family = str(item.get("family") or "").strip()
        name = " ".join(part for part in (given, family) if part).strip()
        if name:
            authors.append(name)
    return authors


def crossref_date(message: dict[str, Any]) -> str:
    for key in ("published-online", "published-print", "published", "created"):
        value = message.get(key)
        if not isinstance(value, dict):
            continue
        date_parts = value.get("date-parts")
        if not isinstance(date_parts, list) or not date_parts:
            continue
        first_part = date_parts[0]
        if not isinstance(first_part, list) or not first_part:
            continue
        year = int(first_part[0])
        month = int(first_part[1]) if len(first_part) > 1 else 1
        day = int(first_part[2]) if len(first_part) > 2 else 1
        return date(year, month, day).isoformat()
    return ""


def fallback_article_from_url(url: str) -> Article:
    crossref = crossref_article(url)
    if crossref:
        return crossref

    path_parts = [part for part in urlsplit(url).path.split("/") if part]
    title = path_parts[-2] if len(path_parts) >= 2 and path_parts[-1].isdigit() else (path_parts[-1] if path_parts else url)
    title = re.sub(r"[-_]+", " ", title).strip() or url
    return Article(
        url=canonical_url(url),
        title=title,
        journal=urlsplit(url).netloc or "Unknown",
        authors=[],
        summary_source=title,
        published="",
        pdf_url=build_pdf_url({}, url),
    )


def article_from_candidate(candidate: ArticleCandidate) -> Article:
    return Article(
        url=candidate.url,
        title=candidate.title or "Untitled",
        journal=candidate.journal or candidate.source.name,
        authors=list(candidate.authors),
        summary_source=candidate.summary_source or candidate.title or "No abstract available.",
        published=candidate.published,
        pdf_url=candidate.pdf_url or build_pdf_url({}, candidate.url),
    )


def fetch_article(candidate: ArticleCandidate) -> Article:
    if candidate.title and candidate.published:
        return article_from_candidate(candidate)

    url = candidate.url
    source = candidate.source
    parser = MetaParser()
    try:
        parser.feed(fetch_text(url))
    except (HTTPError, URLError, TimeoutError):
        if candidate.title:
            logging.exception("Falling back to feed metadata for %s", url)
            return article_from_candidate(candidate)
        raise
    meta = parser.meta

    title = first(meta, "citation_title", "dc.title", "og:title") or "Untitled"
    journal = first(meta, "citation_journal_title", "prism.publicationName") or source.name
    authors = meta.get("citation_author", [])
    description = first(meta, "dc.description", "description", "og:description")
    abstract = first(meta, "citation_abstract")
    published = first(
        meta,
        "citation_publication_date",
        "prism.publicationDate",
        "article:published_time",
        "dc.date",
        "dc.Date",
    )

    return Article(
        url=url,
        title=title,
        journal=journal,
        authors=authors,
        summary_source=abstract or description or title,
        published=published,
        pdf_url=build_pdf_url(meta, url),
    )


def parse_publication_date(value: str) -> date | None:
    if not value:
        return None

    cleaned = value.strip()
    iso_match = re.match(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})", cleaned)
    if iso_match:
        year, month, day = (int(part) for part in iso_match.groups())
        return date(year, month, day)

    try:
        return datetime.fromisoformat(cleaned.replace("Z", "+00:00")).date()
    except ValueError:
        pass

    try:
        return parsedate_to_datetime(cleaned).date()
    except (TypeError, ValueError, IndexError):
        pass

    for fmt in ("%d %B %Y", "%d %b %Y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue

    return None


def target_publication_dates(run_date: date) -> set[date]:
    return {run_date, run_date - timedelta(days=1)}


def is_published_on(article: Article, target_date: date) -> bool:
    return is_published_in(article, {target_date})


def is_published_in(article: Article, target_dates: set[date]) -> bool:
    published = parse_publication_date(article.published)
    if not published:
        logging.info("Skipped article with unknown publication date: %s", article.url)
        return False
    return published in target_dates


def split_sentences(text: str) -> list[str]:
    normalized = re.sub(r"\s+", " ", text).strip()
    return [part.strip() for part in re.split(r"(?<=[.!?])\s+", normalized) if part.strip()]


def fallback_korean_summary(article: Article) -> list[str]:
    authors = ", ".join(article.authors[:5]) if article.authors else "저자 정보 미확인"
    source_sentences = split_sentences(article.summary_source)
    core = source_sentences[:4] or [article.summary_source]

    sentences = [
        f"이 논문은 '{article.title}'라는 제목으로 {article.journal}에 게재된 연구입니다.",
        f"주요 저자는 {authors}입니다.",
        "출판사가 제공한 공개 요약과 논문 메타데이터를 바탕으로 핵심 내용을 정리했습니다.",
    ]

    for index, sentence in enumerate(core, start=1):
        sentences.append(f"핵심 내용 {index}: {sentence}")

    sentences.extend(
        [
            "연구의 중요성은 관찰된 현상이나 제안된 방법이 해당 분야의 기존 이해를 넓힌다는 점에 있습니다.",
            "자세한 실험 조건, 데이터 해석, 한계는 원문과 PDF에서 확인하는 것이 좋습니다.",
            "관심이 있다면 아래 원문 링크나 PDF 버튼을 눌러 논문 전문을 확인할 수 있습니다.",
        ]
    )
    return sentences[:8]


def openai_korean_summary(article: Article) -> list[str] | None:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None

    model = os.getenv("OPENAI_MODEL", "gpt-5-mini")
    prompt = (
        "다음 학술 논문 정보를 바탕으로 한국어 요약문을 정확히 8문장으로 작성해줘. "
        "각 문장은 독립적인 완결문이어야 하고, 과장 없이 연구의 목적, 방법, 발견, 의미, 한계를 균형 있게 담아줘.\n\n"
        f"제목: {article.title}\n"
        f"저널: {article.journal}\n"
        f"저자: {', '.join(article.authors)}\n"
        f"발행일: {article.published}\n"
        f"원문 요약/초록: {article.summary_source}"
    )
    payload = {
        "model": model,
        "input": prompt,
    }
    request = Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        timeout = int(os.getenv("JOURNAL_REQUEST_TIMEOUT", os.getenv("NATURE_REQUEST_TIMEOUT", "25")))
        with urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError):
        logging.exception("OpenAI summary failed; falling back to metadata summary")
        return None

    text = extract_openai_text(body)
    sentences = split_korean_numbered_sentences(text)
    return normalize_to_eight(sentences) if sentences else None


def extract_openai_text(body: dict[str, Any]) -> str:
    if isinstance(body.get("output_text"), str):
        return body["output_text"]

    chunks: list[str] = []
    for item in body.get("output", []):
        for content in item.get("content", []):
            text = content.get("text")
            if isinstance(text, str):
                chunks.append(text)
    return "\n".join(chunks).strip()


def split_korean_numbered_sentences(text: str) -> list[str]:
    cleaned = re.sub(r"^\s*\d+[.)]\s*", "", text.strip(), flags=re.MULTILINE)
    parts = [part.strip() for part in re.split(r"(?<=[.!?。])\s+|\n+", cleaned) if part.strip()]
    return [re.sub(r"^\d+[.)]\s*", "", part).strip() for part in parts]


def normalize_to_eight(sentences: list[str]) -> list[str]:
    result = [sentence.strip() for sentence in sentences[:8] if sentence.strip()]
    while len(result) < 8:
        result.append("자세한 내용은 원문을 통해 추가로 확인할 수 있습니다.")
    return result


def korean_summary(article: Article) -> list[str]:
    return openai_korean_summary(article) or fallback_korean_summary(article)


def fallback_keywords(article: Article, limit: int = 6) -> list[str]:
    stopwords = {
        "and",
        "the",
        "for",
        "with",
        "from",
        "using",
        "into",
        "that",
        "this",
        "a",
        "an",
        "of",
        "in",
        "to",
        "by",
        "on",
    }
    words = re.findall(r"[A-Za-z][A-Za-z0-9-]{2,}", article.title.lower())
    keywords: list[str] = []
    seen: set[str] = set()
    for word in words:
        if word in stopwords or word in seen:
            continue
        keywords.append(word)
        seen.add(word)
        if len(keywords) >= limit:
            break
    return keywords or [article.journal.lower()]


def openai_keywords(article: Article, limit: int = 6) -> list[str] | None:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None

    prompt = (
        "다음 논문을 대표하는 영어 키워드를 JSON 배열로만 출력해줘. "
        f"키워드는 최대 {limit}개이고, 각 키워드는 1-4단어의 짧은 명사구여야 해.\n\n"
        f"제목: {article.title}\n"
        f"저널: {article.journal}\n"
        f"초록/설명: {article.summary_source}"
    )
    request = Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps({"model": os.getenv("OPENAI_MODEL", "gpt-5-mini"), "input": prompt}).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        timeout = int(os.getenv("JOURNAL_REQUEST_TIMEOUT", os.getenv("NATURE_REQUEST_TIMEOUT", "25")))
        with urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError):
        logging.exception("OpenAI keyword extraction failed; falling back to title keywords")
        return None

    text = extract_openai_text(body).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\[[\s\S]*\]", text)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None

    if not isinstance(parsed, list):
        return None

    keywords: list[str] = []
    seen: set[str] = set()
    for item in parsed:
        keyword = re.sub(r"\s+", " ", str(item).strip().lower())
        if keyword and keyword not in seen:
            keywords.append(keyword)
            seen.add(keyword)
        if len(keywords) >= limit:
            break
    return keywords or None


def article_keywords(article: Article, limit: int = 6) -> list[str]:
    return openai_keywords(article, limit) or fallback_keywords(article, limit)


def rebuild_keyword_counts(data: dict[str, Any]) -> dict[str, Any]:
    articles = data.setdefault("articles", {})
    if not isinstance(articles, dict):
        articles = {}
        data["articles"] = articles

    counts: dict[str, dict[str, Any]] = {}
    for url, record in articles.items():
        if not isinstance(record, dict):
            continue
        keywords = record.get("keywords", [])
        if not isinstance(keywords, list):
            continue
        for raw_keyword in keywords:
            keyword = re.sub(r"\s+", " ", str(raw_keyword).strip().lower())
            if not keyword:
                continue
            entry = counts.setdefault(keyword, {"count": 0, "article_urls": []})
            entry["count"] += 1
            entry["article_urls"].append(url)

    data["keywords"] = counts
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    return data


def save_interested_article(
    article: Article,
    summary: list[str],
    keywords: list[str],
    pdf_path: Path | None,
    pdf_error: str | None,
) -> None:
    data = load_json_object(KEYWORDS_PATH)
    articles = data.setdefault("articles", {})
    if not isinstance(articles, dict):
        articles = {}
        data["articles"] = articles

    articles[canonical_url(article.url)] = {
        **article_to_record(article, summary),
        "keywords": keywords,
        "pdf_path": str(pdf_path) if pdf_path else None,
        "pdf_error": pdf_error,
        "interested_at": datetime.now(timezone.utc).isoformat(),
    }
    save_json_object(KEYWORDS_PATH, rebuild_keyword_counts(data))


def load_interested_article_record(article_url: str) -> dict[str, Any] | None:
    data = load_json_object(KEYWORDS_PATH)
    articles = data.get("articles", {})
    if not isinstance(articles, dict):
        return None
    record = articles.get(canonical_url(article_url))
    return record if isinstance(record, dict) else None


def telegram_request(method: str, payload: dict[str, Any]) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

    request = Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    timeout = int(os.getenv("JOURNAL_REQUEST_TIMEOUT", os.getenv("NATURE_REQUEST_TIMEOUT", "25")))
    with urlopen(request, timeout=timeout) as response:
        response.read()


def article_message(article: Article, summary: list[str]) -> str:
    authors = ", ".join(article.authors[:10]) if article.authors else "Unknown"
    if len(article.authors) > 10:
        authors += f" 외 {len(article.authors) - 10}명"

    summary_text = "\n".join(f"{idx}. {sentence}" for idx, sentence in enumerate(summary, start=1))
    return (
        f"<b>{html.escape(article.title)}</b>\n\n"
        f"<b>저널</b>: {html.escape(article.journal)}\n"
        f"<b>저자</b>: {html.escape(authors)}\n"
        f"<b>발행일</b>: {html.escape(article.published or 'Unknown')}\n\n"
        f"<b>요약</b>\n{html.escape(summary_text)}\n\n"
        f"<a href=\"{html.escape(article.url)}\">논문 원문 보기</a>"
    )


def article_buttons(article: Article, include_interest: bool = True) -> list[list[dict[str, str]]]:
    buttons: list[list[dict[str, str]]] = [[{"text": "논문 원문", "url": article.url}]]
    if article.pdf_url:
        buttons[0].append({"text": "PDF 다운로드", "url": article.pdf_url})
    if include_interest:
        buttons.append([{"text": "관심 저장", "callback_data": f"save:{article_id(article.url)}"}])
    return buttons


def send_article(article: Article) -> None:
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not chat_id:
        raise RuntimeError("TELEGRAM_CHAT_ID is not set")

    summary = korean_summary(article)
    save_sent_article(article, summary)

    telegram_request(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": article_message(article, summary),
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
            "reply_markup": {"inline_keyboard": article_buttons(article)},
        },
    )


def openai_interest_candidates(articles: list[Article]) -> list[dict[str, Any]] | None:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key or not articles:
        return None

    compact_articles = [
        {
            "title": article.title,
            "journal": article.journal,
            "keywords_hint": fallback_keywords(article, 5),
            "summary_source": article.summary_source[:800],
        }
        for article in articles[:40]
    ]
    prompt = (
        "아래 오늘 출판 논문 중 사용자가 관심있어 할 만한 TOP 3를 골라줘. "
        "응답은 JSON 배열만 출력하고, 각 항목은 title, journal, keywords 필드를 가져야 해. "
        "keywords는 영어 키워드 3개 배열이어야 해.\n\n"
        f"{json.dumps(compact_articles, ensure_ascii=False)}"
    )
    request = Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps({"model": os.getenv("OPENAI_MODEL", "gpt-5-mini"), "input": prompt}).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        timeout = int(os.getenv("JOURNAL_REQUEST_TIMEOUT", os.getenv("NATURE_REQUEST_TIMEOUT", "25")))
        with urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError):
        logging.exception("OpenAI interest candidate ranking failed; falling back to first articles")
        return None

    text = extract_openai_text(body).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\[[\s\S]*\]", text)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return parsed if isinstance(parsed, list) else None


def fallback_interest_candidates(articles: list[Article]) -> list[dict[str, Any]]:
    return [
        {
            "title": article.title,
            "journal": article.journal,
            "keywords": fallback_keywords(article, 3),
        }
        for article in articles[:3]
    ]


def interest_candidates_message(candidates: list[dict[str, Any]]) -> str:
    lines = ["<b>오늘의 관심 후보 TOP 3</b>"]
    for index, candidate in enumerate(candidates[:3], start=1):
        title = html.escape(str(candidate.get("title") or "Untitled"))
        journal = html.escape(str(candidate.get("journal") or "Unknown"))
        raw_keywords = candidate.get("keywords", [])
        if not isinstance(raw_keywords, list):
            raw_keywords = []
        keywords = ", ".join(html.escape(str(keyword)) for keyword in raw_keywords[:3])
        lines.append(f"\n{index}. <b>{title}</b>\n저널: {journal}\n키워드: {keywords or 'N/A'}")
    return "\n".join(lines)


def send_interest_candidates(articles: list[Article]) -> None:
    if not articles:
        return
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not chat_id:
        raise RuntimeError("TELEGRAM_CHAT_ID is not set")

    candidates = openai_interest_candidates(articles) or fallback_interest_candidates(articles)
    telegram_request(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": interest_candidates_message(candidates),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
    )


def send_no_new_articles_message() -> None:
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not chat_id:
        raise RuntimeError("TELEGRAM_CHAT_ID is not set")

    telegram_request(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": "오늘 업로드된 신규 논문이 없습니다",
            "disable_web_page_preview": True,
        },
    )


def run() -> None:
    load_env_file(SECRETS_PATH)
    if not os.getenv("TELEGRAM_BOT_TOKEN") or not os.getenv("TELEGRAM_CHAT_ID"):
        logging.warning(
            "Journal daily digest skipped: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in %s",
            SECRETS_PATH,
        )
        return

    started = datetime.now(timezone.utc).isoformat()
    run_date = datetime.now().date()
    target_dates = target_publication_dates(run_date)
    logging.info(
        "Journal daily digest started at %s; target publication dates are %s",
        started,
        ", ".join(str(day) for day in sorted(target_dates)),
    )

    seen = load_seen()
    sources = load_journal_sources()
    logging.info("Loaded %s journal source(s) from %s", len(sources), JOURNALS_PATH)

    discovered = discover_all_article_candidates(sources)
    total_found = sum(len(candidates) for candidates in discovered.values())
    total_new = sum(
        1
        for candidates in discovered.values()
        for candidate in candidates
        if candidate.url not in seen
    )
    logging.info("Found %s candidate article(s), %s unseen", total_found, total_new)

    sent = 0
    sent_articles: list[Article] = []
    for source, candidates in discovered.items():
        logging.info("Checking %s: %s candidate(s)", source.name, len(candidates))
        for candidate in candidates:
            if candidate.url in seen:
                continue

            article = fetch_article(candidate)
            if not is_published_in(article, target_dates):
                logging.info(
                    "Skipped %s because publication date %r is not in %s",
                    article.url,
                    article.published,
                    ", ".join(str(day) for day in sorted(target_dates)),
                )
                continue

            send_article(article)
            sent_articles.append(article)
            seen.add(candidate.url)
            save_seen(seen)
            sent += 1
            time.sleep(1)

    if sent_articles:
        send_interest_candidates(sent_articles)
    else:
        send_no_new_articles_message()
    logging.info("Journal daily digest finished; sent %s article(s)", sent)


if __name__ == "__main__":
    run()
