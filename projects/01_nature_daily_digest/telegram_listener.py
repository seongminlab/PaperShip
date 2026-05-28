#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import task


LOGS_DIR = task.ROOT / "logs"
LISTENER_LOG_PATH = LOGS_DIR / "journal-listener.log"
URL_RE = re.compile(r"https?://\S+")
DEFAULT_LISTENER_RESTART_COMMAND = "launchctl kickstart -k gui/$(id -u)/com.papership.journal-listener"


def setup_logging() -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(LISTENER_LOG_PATH, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def telegram_request(method: str, payload: dict[str, Any]) -> dict[str, Any]:
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
        return json.loads(response.read().decode("utf-8"))


def delete_webhook() -> None:
    try:
        telegram_request("deleteWebhook", {"drop_pending_updates": False})
    except Exception:
        logging.exception("Could not delete Telegram webhook before polling")


def get_updates(offset: int | None) -> list[dict[str, Any]]:
    payload: dict[str, Any] = {
        "timeout": 50,
        "allowed_updates": ["message", "callback_query"],
    }
    if offset is not None:
        payload["offset"] = offset
    body = telegram_request("getUpdates", payload)
    updates = body.get("result", [])
    return updates if isinstance(updates, list) else []


def answer_callback(callback_query_id: str, text: str) -> None:
    telegram_request("answerCallbackQuery", {"callback_query_id": callback_query_id, "text": text})


def send_message(chat_id: int | str, text: str, buttons: list[list[dict[str, str]]] | None = None) -> None:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    telegram_request("sendMessage", payload)


def listener_restart_command() -> str:
    return os.getenv("PAPERSHIP_LISTENER_RESTART_COMMAND", DEFAULT_LISTENER_RESTART_COMMAND)


def slugify(value: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", value.lower())
    return "-".join(words)


def first_author_lastname(article: task.Article) -> str:
    if not article.authors:
        return "unknown"
    parts = re.findall(r"[A-Za-z]+", article.authors[0])
    return (parts[-1] if parts else "unknown").lower()


def publication_year(article: task.Article) -> str:
    parsed = task.parse_publication_date(article.published)
    if parsed:
        return str(parsed.year)
    match = re.search(r"\b(19|20)\d{2}\b", article.published)
    if match:
        return match.group(0)
    return str(datetime.now().year)


def paper_filename(article: task.Article) -> str:
    author = slugify(first_author_lastname(article)) or "unknown"
    year = publication_year(article)
    title_words = re.findall(r"[A-Za-z0-9]+", article.title.lower())[:5]
    title = "-".join(title_words) or "untitled"
    return f"{author}-{year}-{title}.pdf"


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for index in range(2, 1000):
        candidate = path.with_name(f"{stem}-{index}{suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Could not find unique path for {path}")


def download_pdf(article: task.Article) -> tuple[Path | None, str | None]:
    if not article.pdf_url:
        return None, "PDF URL을 찾지 못했습니다."

    request = Request(
        article.pdf_url,
        headers={
            "User-Agent": "Mozilla/5.0 daily-automation-journal-listener/1.0",
            "Accept": "application/pdf,*/*",
        },
    )
    try:
        timeout = int(os.getenv("JOURNAL_REQUEST_TIMEOUT", os.getenv("NATURE_REQUEST_TIMEOUT", "25")))
        with urlopen(request, timeout=timeout) as response:
            content = response.read()
            content_type = response.headers.get("Content-Type", "")
    except (HTTPError, URLError, TimeoutError) as exc:
        return None, f"PDF 다운로드 요청 실패: {exc}"

    if not content.startswith(b"%PDF") and "pdf" not in content_type.lower():
        return None, f"PDF가 아닌 응답을 받았습니다: {content_type or 'unknown content type'}"

    task.PAPERS_DIR.mkdir(parents=True, exist_ok=True)
    path = unique_path(task.PAPERS_DIR / paper_filename(article))
    path.write_bytes(content)
    return path, None


def source_for_url(url: str) -> task.JournalSource:
    host = task.urlsplit(url).netloc
    for source in task.load_journal_sources():
        if task.urlsplit(source.base_url).netloc == host:
            return source
    return task.JournalSource(
        name="Unknown",
        listing_url=url,
        discovery="nature_html",
        base_url=f"{task.urlsplit(url).scheme}://{host}",
    )


def candidate_from_known_feeds(url: str) -> task.ArticleCandidate | None:
    canonical = task.canonical_url(url)
    for source in task.load_journal_sources():
        if source.discovery != "rss":
            continue
        try:
            for candidate in task.discover_rss_article_candidates(source):
                if candidate.url == canonical:
                    return candidate
        except Exception:
            logging.exception("Could not scan RSS feed for %s", source.name)
    return None


def article_from_url(url: str) -> task.Article:
    canonical = task.canonical_url(url)
    feed_candidate = candidate_from_known_feeds(canonical)
    if feed_candidate:
        return task.fetch_article(feed_candidate)

    source = source_for_url(canonical)
    return task.fetch_article(task.ArticleCandidate(source=source, url=canonical))


def success_or_failure_message(
    article: task.Article,
    summary: list[str],
    keywords: list[str],
    pdf_path: Path | None,
    pdf_error: str | None,
    include_summary: bool,
) -> str:
    keyword_text = ", ".join(html.escape(keyword) for keyword in keywords)
    if pdf_path:
        status = "PDF 저장 완료"
    else:
        status = pdf_failure_status(pdf_error)

    message = (
        f"<b>관심 논문 저장</b>\n\n"
        f"<b>{html.escape(article.title)}</b>\n"
        f"<b>키워드</b>: {keyword_text or 'N/A'}\n"
        f"{status}"
    )
    if include_summary:
        summary_text = "\n".join(f"{idx}. {sentence}" for idx, sentence in enumerate(summary, start=1))
        message += f"\n\n<b>요약</b>\n{html.escape(summary_text)}"
    return message


def pdf_failure_status(pdf_error: str | None) -> str:
    reason = pdf_error or "알 수 없는 오류"
    if reason.startswith("PDF 다운로드 요청 실패:"):
        return html.escape(reason)
    return f"PDF 다운로드 요청 실패: {html.escape(reason)}"


def record_keywords(record: dict[str, Any]) -> list[str]:
    keywords = record.get("keywords", [])
    if not isinstance(keywords, list):
        return []
    return [str(keyword) for keyword in keywords if str(keyword).strip()]


def record_summary(record: dict[str, Any]) -> list[str]:
    summary = record.get("summary", [])
    if not isinstance(summary, list):
        return []
    return [str(sentence) for sentence in summary if str(sentence).strip()]


def record_pdf_path(record: dict[str, Any]) -> Path | None:
    pdf_path = record.get("pdf_path")
    if not pdf_path:
        return None
    return Path(str(pdf_path))


def process_interest(article: task.Article, chat_id: int | str, include_summary: bool) -> None:
    logging.info("Saving interested article: %s", article.url)
    existing_record = task.load_interested_article_record(article.url)
    if existing_record:
        summary = record_summary(existing_record)
        keywords = record_keywords(existing_record)
        pdf_path = record_pdf_path(existing_record)
        pdf_error = str(existing_record.get("pdf_error") or "") or None
    else:
        summary = task.korean_summary(article)
        keywords = task.article_keywords(article)
        pdf_path, pdf_error = download_pdf(article)
        task.save_interested_article(article, summary, keywords, pdf_path, pdf_error)

    buttons = task.article_buttons(article, include_interest=False)
    send_message(
        chat_id,
        success_or_failure_message(article, summary, keywords, pdf_path, pdf_error, include_summary),
        buttons,
    )


def handle_callback(callback_query: dict[str, Any]) -> None:
    callback_id = str(callback_query.get("id") or "")
    data = str(callback_query.get("data") or "")
    message = callback_query.get("message", {})
    chat = message.get("chat", {}) if isinstance(message, dict) else {}
    chat_id = chat.get("id")
    if not callback_id or not chat_id:
        return

    if not data.startswith("save:"):
        answer_callback(callback_id, "알 수 없는 버튼입니다.")
        return

    answer_callback(callback_id, "관심 논문 저장을 시작합니다.")
    identifier = data.split(":", 1)[1]
    article = task.load_sent_article(identifier)
    if article is None:
        send_message(
            chat_id,
            "관심 저장 버튼은 눌렸지만 이 논문의 로컬 기록을 찾지 못했습니다.\n"
            "같은 논문을 저장하려면 <code>/save 논문원문링크</code> 형식으로 보내주세요.\n\n"
            "버튼이 아예 반응하지 않는 경우에는 Mac에서 리스너를 다시 시작하세요:\n"
            f"<code>{html.escape(listener_restart_command())}</code>",
        )
        return

    try:
        process_interest(article, chat_id, include_summary=False)
    except Exception as exc:
        logging.exception("Could not process interested article from callback")
        send_message(
            chat_id,
            "관심 저장 처리 중 오류가 발생했습니다.\n"
            f"이유: {html.escape(str(exc))}\n\n"
            "버튼이 계속 반응하지 않는 경우에는 Mac에서 리스너를 다시 시작하세요:\n"
            f"<code>{html.escape(listener_restart_command())}</code>",
        )


def handle_message(message: dict[str, Any]) -> None:
    text = str(message.get("text") or "").strip()
    chat = message.get("chat", {})
    chat_id = chat.get("id") if isinstance(chat, dict) else None
    if not chat_id or not text.startswith("/save"):
        return

    match = URL_RE.search(text)
    if not match:
        send_message(chat_id, "사용법: <code>/save 논문원문링크</code>")
        return

    url = match.group(0).rstrip(".,)")
    send_message(chat_id, "논문 저장을 시작합니다. PDF와 키워드를 확인하는 동안 잠시 기다려주세요.")
    try:
        article = article_from_url(url)
    except Exception as exc:
        logging.exception("Could not fetch article from /save URL")
        send_message(chat_id, f"논문 정보를 읽지 못했습니다.\n이유: {html.escape(str(exc))}\n원문: {html.escape(url)}")
        return

    process_interest(article, chat_id, include_summary=True)


def rebuild_keywords_command() -> int:
    data = task.load_json_object(task.KEYWORDS_PATH)
    task.save_json_object(task.KEYWORDS_PATH, task.rebuild_keyword_counts(data))
    print(f"Rebuilt keyword counts in {task.KEYWORDS_PATH}")
    return 0


def run_listener() -> None:
    task.load_env_file(task.SECRETS_PATH)
    if not os.getenv("TELEGRAM_BOT_TOKEN"):
        raise RuntimeError(f"TELEGRAM_BOT_TOKEN is not set in {task.SECRETS_PATH}")

    delete_webhook()
    offset: int | None = None
    logging.info("Journal Telegram listener started")
    while True:
        try:
            updates = get_updates(offset)
            for update in updates:
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    offset = update_id + 1
                if isinstance(update.get("callback_query"), dict):
                    handle_callback(update["callback_query"])
                elif isinstance(update.get("message"), dict):
                    handle_message(update["message"])
        except Exception:
            logging.exception("Listener loop failed; retrying soon")
            time.sleep(10)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rebuild-keywords", action="store_true")
    args = parser.parse_args()

    setup_logging()
    if args.rebuild_keywords:
        return rebuild_keywords_command()
    run_listener()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
