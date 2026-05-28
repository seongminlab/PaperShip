from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
TASK_PATH = ROOT / "projects" / "daily_paper_digest" / "task.py"
LISTENER_PATH = ROOT / "projects" / "daily_paper_digest" / "telegram_listener.py"


def load_task_module():
    spec = importlib.util.spec_from_file_location("daily_paper_digest_task", TASK_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {TASK_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


task = load_task_module()


def load_listener_module():
    spec = importlib.util.spec_from_file_location("daily_paper_digest_listener", LISTENER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {LISTENER_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(LISTENER_PATH.parent))
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


listener = load_listener_module()


class NatureDailyDigestTests(unittest.TestCase):
    def test_load_journal_sources_from_json(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "journals.json"
            path.write_text(
                """
                [
                  {
                    "name": "Example",
                    "listing_url": "https://example.test/research",
                    "discovery": "rss",
                    "base_url": "https://example.test",
                    "feed_url": "https://example.test/feed",
                    "allowed_item_types": ["Research Article"]
                  }
                ]
                """,
                encoding="utf-8",
            )

            sources = task.load_journal_sources(path)

        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0].name, "Example")
        self.assertEqual(sources[0].allowed_item_types, ("Research Article",))

    def test_load_journal_sources_rejects_rss_without_feed_url(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "journals.json"
            path.write_text(
                """
                [
                  {
                    "name": "Broken",
                    "listing_url": "https://example.test/research",
                    "discovery": "rss",
                    "base_url": "https://example.test"
                  }
                ]
                """,
                encoding="utf-8",
            )

            with self.assertRaises(ValueError):
                task.load_journal_sources(path)

    def test_link_parser_extracts_unique_article_urls(self) -> None:
        parser = task.LinkParser("https://www.nature.com")
        parser.feed(
            """
            <a href="/articles/s41586-026-00001-1">Paper</a>
            <a href="/articles/s41586-026-00001-1">Duplicate</a>
            <a href="/nature/research-articles">Listing</a>
            <a href="/articles/not a slug">Bad</a>
            """
        )

        urls = []
        seen = set()
        for url in parser.links:
            if url not in seen:
                urls.append(url)
                seen.add(url)

        self.assertEqual(urls, ["https://www.nature.com/articles/s41586-026-00001-1"])

    def test_pdf_url_prefers_citation_meta(self) -> None:
        pdf_url = task.build_pdf_url(
            {"citation_pdf_url": ["https://example.test/paper.pdf"]},
            "https://www.nature.com/articles/s41586-026-00001-1",
        )

        self.assertEqual(pdf_url, "https://example.test/paper.pdf")

    def test_pdf_url_falls_back_to_nature_article_pdf(self) -> None:
        pdf_url = task.build_pdf_url(
            {},
            "https://www.nature.com/articles/s41586-026-00001-1",
        )

        self.assertEqual(pdf_url, "https://www.nature.com/articles/s41586-026-00001-1.pdf")

    def test_pdf_url_falls_back_to_science_pdf(self) -> None:
        pdf_url = task.build_pdf_url(
            {},
            "https://www.science.org/doi/abs/10.1126/science.adp2397",
        )

        self.assertEqual(pdf_url, "https://www.science.org/doi/pdf/10.1126/science.adp2397")

    def test_pdf_url_falls_back_to_cell_pdf(self) -> None:
        pdf_url = task.build_pdf_url(
            {},
            "https://www.cell.com/cell/fulltext/S0092-8674(26)00394-6",
        )

        self.assertEqual(
            pdf_url,
            "https://www.cell.com/action/showPdf?pii=S0092-8674%2826%2900394-6",
        )

    def test_parse_publication_date_supports_common_formats(self) -> None:
        self.assertEqual(task.parse_publication_date("2026-05-27"), task.date(2026, 5, 27))
        self.assertEqual(task.parse_publication_date("2026-05-27T06:00:10Z"), task.date(2026, 5, 27))
        self.assertEqual(task.parse_publication_date("27 May 2026"), task.date(2026, 5, 27))

    def test_is_published_on_matches_run_date(self) -> None:
        article = task.Article(
            url="https://www.nature.com/articles/s41586-026-00001-1",
            title="A test paper",
            journal="Nature",
            authors=[],
            summary_source="A source abstract.",
            published="2026-05-27",
            pdf_url=None,
        )

        self.assertTrue(task.is_published_on(article, task.date(2026, 5, 27)))
        self.assertFalse(task.is_published_on(article, task.date(2026, 5, 28)))

    def test_target_publication_dates_include_run_date_and_previous_day(self) -> None:
        self.assertEqual(
            task.target_publication_dates(task.date(2026, 5, 27)),
            {task.date(2026, 5, 27), task.date(2026, 5, 26)},
        )

    def test_is_published_in_accepts_previous_day_window(self) -> None:
        article = task.Article(
            url="https://www.nature.com/articles/s41586-026-00001-1",
            title="A test paper",
            journal="Nature",
            authors=[],
            summary_source="A source abstract.",
            published="2026-05-26",
            pdf_url=None,
        )
        target_dates = task.target_publication_dates(task.date(2026, 5, 27))

        self.assertTrue(task.is_published_in(article, target_dates))
        self.assertFalse(task.is_published_in(article, {task.date(2026, 5, 25)}))

    def test_rss_discovery_filters_allowed_article_types(self) -> None:
        source = task.JournalSource(
            name="Science",
            listing_url="https://www.science.org/journal/science/research",
            discovery="rss",
            base_url="https://www.science.org",
            feed_url="https://example.test/feed",
            allowed_item_types=("Research Article",),
        )
        rss = """
        <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
          xmlns="http://purl.org/rss/1.0/"
          xmlns:dc="http://purl.org/dc/elements/1.1/">
          <item>
            <title>Research item</title>
            <link>https://www.science.org/doi/abs/10.1126/science.good?af=R</link>
            <dc:type>Research Article</dc:type>
          </item>
          <item>
            <title>Book item</title>
            <link>https://www.science.org/doi/abs/10.1126/science.skip?af=R</link>
            <dc:type>Books et al.</dc:type>
          </item>
        </rdf:RDF>
        """

        original_fetch_text = task.fetch_text
        try:
            task.fetch_text = lambda _url: rss
            self.assertEqual(
                task.discover_rss_article_urls(source),
                ["https://www.science.org/doi/abs/10.1126/science.good"],
            )
        finally:
            task.fetch_text = original_fetch_text

    def test_summary_is_normalized_to_exactly_eight_sentences(self) -> None:
        summary = task.normalize_to_eight(["첫 문장입니다.", "  ", "둘째 문장입니다."])

        self.assertEqual(len(summary), 8)
        self.assertEqual(summary[0], "첫 문장입니다.")
        self.assertEqual(summary[1], "둘째 문장입니다.")

    def test_article_message_contains_required_fields(self) -> None:
        article = task.Article(
            url="https://www.nature.com/articles/s41586-026-00001-1",
            title="A test paper",
            journal="Nature",
            authors=["Ada Lovelace", "Grace Hopper"],
            summary_source="A source abstract.",
            published="2026-05-26",
            pdf_url="https://www.nature.com/articles/s41586-026-00001-1.pdf",
        )
        summary = [f"요약 문장 {index}입니다." for index in range(1, 9)]

        message = task.article_message(article, summary)

        self.assertIn("<b>A test paper</b>", message)
        self.assertIn("<b>저널</b>: Nature", message)
        self.assertIn("<b>저자</b>: Ada Lovelace, Grace Hopper", message)
        self.assertIn("8. 요약 문장 8입니다.", message)
        self.assertIn("논문 원문 보기", message)

    def test_article_buttons_include_interest_callback(self) -> None:
        article = task.Article(
            url="https://www.nature.com/articles/s41586-026-00001-1",
            title="A test paper",
            journal="Nature",
            authors=[],
            summary_source="A source abstract.",
            published="2026-05-26",
            pdf_url=None,
        )

        buttons = task.article_buttons(article)

        self.assertEqual(buttons[-1][0]["text"], "관심 저장")
        self.assertTrue(buttons[-1][0]["callback_data"].startswith("save:"))

    def test_keyword_counts_are_rebuilt_from_articles(self) -> None:
        data = {
            "articles": {
                "https://example.test/a": {"keywords": ["CRISPR", "gene editing"]},
                "https://example.test/b": {"keywords": ["crispr", "delivery"]},
            }
        }

        rebuilt = task.rebuild_keyword_counts(data)

        self.assertEqual(rebuilt["keywords"]["crispr"]["count"], 2)
        self.assertEqual(rebuilt["keywords"]["gene editing"]["count"], 1)

    def test_listener_pdf_filename_uses_requested_pattern(self) -> None:
        article = task.Article(
            url="https://www.nature.com/articles/s41586-026-00001-1",
            title="Very low carbohydrate ketogenic diet in treatment",
            journal="Nature Communications",
            authors=["Min Kim", "Ada Lovelace"],
            summary_source="A source abstract.",
            published="2026-05-27",
            pdf_url=None,
        )

        self.assertEqual(
            listener.paper_filename(article),
            "kim-2026-very-low-carbohydrate-ketogenic-diet.pdf",
        )

    def test_no_new_articles_message_uses_requested_text(self) -> None:
        calls = []
        original_telegram_request = task.telegram_request
        original_chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        try:
            os.environ["TELEGRAM_CHAT_ID"] = "123"
            task.telegram_request = lambda method, payload: calls.append((method, payload))

            task.send_no_new_articles_message()
        finally:
            task.telegram_request = original_telegram_request
            if original_chat_id is None:
                os.environ.pop("TELEGRAM_CHAT_ID", None)
            else:
                os.environ["TELEGRAM_CHAT_ID"] = original_chat_id

        self.assertEqual(calls[0][0], "sendMessage")
        self.assertEqual(calls[0][1]["text"], "오늘 업로드된 신규 논문이 없습니다")

    def test_callback_interest_message_omits_summary(self) -> None:
        article = task.Article(
            url="https://www.nature.com/articles/s41586-026-00001-1",
            title="A test paper",
            journal="Nature",
            authors=[],
            summary_source="A source abstract.",
            published="2026-05-27",
            pdf_url=None,
        )

        message = listener.success_or_failure_message(
            article,
            ["요약 문장입니다."],
            ["crispr", "delivery"],
            Path("/tmp/paper.pdf"),
            None,
            include_summary=False,
        )

        self.assertIn("<b>A test paper</b>", message)
        self.assertIn("<b>키워드</b>: crispr, delivery", message)
        self.assertIn("PDF 저장 완료", message)
        self.assertNotIn("<b>요약</b>", message)
        self.assertNotIn("요약 문장입니다.", message)

    def test_save_command_interest_message_includes_summary(self) -> None:
        article = task.Article(
            url="https://www.nature.com/articles/s41586-026-00001-1",
            title="A test paper",
            journal="Nature",
            authors=[],
            summary_source="A source abstract.",
            published="2026-05-27",
            pdf_url=None,
        )

        message = listener.success_or_failure_message(
            article,
            ["요약 문장입니다."],
            ["crispr"],
            None,
            "PDF URL을 찾지 못했습니다.",
            include_summary=True,
        )

        self.assertIn("PDF 다운로드 요청 실패: PDF URL을 찾지 못했습니다.", message)
        self.assertIn("<b>요약</b>", message)
        self.assertIn("요약 문장입니다.", message)

    def test_existing_interested_article_is_not_downloaded_again(self) -> None:
        article = task.Article(
            url="https://www.nature.com/articles/s41586-026-00001-1",
            title="A test paper",
            journal="Nature",
            authors=[],
            summary_source="A source abstract.",
            published="2026-05-27",
            pdf_url="https://example.test/paper.pdf",
        )
        sent_messages = []
        original_keywords_path = task.KEYWORDS_PATH
        original_listener_keywords_path = listener.task.KEYWORDS_PATH
        original_download_pdf = listener.download_pdf
        original_send_message = listener.send_message
        try:
            with TemporaryDirectory() as temp_dir:
                task.KEYWORDS_PATH = Path(temp_dir) / "keywords.json"
                task.save_interested_article(
                    article,
                    ["이미 저장된 요약입니다."],
                    ["crispr"],
                    Path("/tmp/existing.pdf"),
                    None,
                )

                def fail_download(_article):
                    raise AssertionError("download_pdf should not be called for an existing interested article")

                listener.download_pdf = fail_download
                listener.send_message = lambda chat_id, text, buttons=None: sent_messages.append(text)

                listener.task.KEYWORDS_PATH = task.KEYWORDS_PATH
                listener.process_interest(article, 123, include_summary=False)
        finally:
            task.KEYWORDS_PATH = original_keywords_path
            listener.task.KEYWORDS_PATH = original_listener_keywords_path
            listener.download_pdf = original_download_pdf
            listener.send_message = original_send_message

        self.assertEqual(len(sent_messages), 1)
        self.assertIn("PDF 저장 완료", sent_messages[0])
        self.assertNotIn("이미 저장된 요약입니다.", sent_messages[0])

    def test_url_only_message_runs_save_flow(self) -> None:
        article = task.Article(
            url="https://www.nature.com/articles/s41586-026-00001-1",
            title="A test paper",
            journal="Nature",
            authors=[],
            summary_source="A source abstract.",
            published="2026-05-27",
            pdf_url=None,
        )
        processed = []
        original_article_from_url = listener.article_from_url
        original_process_interest = listener.process_interest
        original_send_message = listener.send_message
        try:
            listener.article_from_url = lambda url: article
            listener.process_interest = lambda article_arg, chat_id, include_summary: processed.append(
                (article_arg.url, chat_id, include_summary)
            )
            listener.send_message = lambda chat_id, text, buttons=None: None

            listener.handle_message(
                {
                    "chat": {"id": 123},
                    "text": "https://www.nature.com/articles/s41586-026-00001-1",
                }
            )
        finally:
            listener.article_from_url = original_article_from_url
            listener.process_interest = original_process_interest
            listener.send_message = original_send_message

        self.assertEqual(processed, [(article.url, 123, True)])

    def test_uploaded_pdf_without_caption_uses_filename_fallback(self) -> None:
        document = {
            "file_id": "file-123",
            "file_name": "My Interesting Paper.pdf",
            "mime_type": "application/pdf",
        }
        article = listener.uploaded_pdf_article(document)

        self.assertEqual(
            listener.paper_filename(article),
            f"unknown-{listener.datetime.now().year}-my-interesting-paper.pdf",
        )

    def test_pdf_upload_is_saved_and_confirmed(self) -> None:
        article = task.Article(
            url="https://www.nature.com/articles/s41586-026-00001-1",
            title="A test paper",
            journal="Nature",
            authors=["Ada Lovelace"],
            summary_source="A source abstract.",
            published="2026-05-27",
            pdf_url=None,
        )
        sent_messages = []
        original_article_from_url = listener.article_from_url
        original_download_uploaded_pdf = listener.download_uploaded_pdf
        original_korean_summary = listener.task.korean_summary
        original_article_keywords = listener.task.article_keywords
        original_save_interested_article = listener.task.save_interested_article
        original_send_message = listener.send_message
        try:
            listener.article_from_url = lambda url: article
            listener.download_uploaded_pdf = lambda document, article_arg: Path("/tmp/lovelace-2026-a-test-paper.pdf")
            listener.task.korean_summary = lambda article_arg: ["요약 문장입니다."]
            listener.task.article_keywords = lambda article_arg: ["crispr"]
            listener.task.save_interested_article = lambda *args: None
            listener.send_message = lambda chat_id, text, buttons=None: sent_messages.append((chat_id, text, buttons))

            listener.handle_message(
                {
                    "chat": {"id": 123},
                    "caption": "https://www.nature.com/articles/s41586-026-00001-1",
                    "document": {
                        "file_id": "file-123",
                        "file_name": "paper.pdf",
                        "mime_type": "application/pdf",
                    },
                }
            )
        finally:
            listener.article_from_url = original_article_from_url
            listener.download_uploaded_pdf = original_download_uploaded_pdf
            listener.task.korean_summary = original_korean_summary
            listener.task.article_keywords = original_article_keywords
            listener.task.save_interested_article = original_save_interested_article
            listener.send_message = original_send_message

        self.assertEqual(sent_messages[0][0], 123)
        self.assertIn("<b>PDF 파일 저장</b>", sent_messages[0][1])
        self.assertIn("<b>A test paper</b>", sent_messages[0][1])
        self.assertIn("<b>키워드</b>: crispr", sent_messages[0][1])
        self.assertIn("PDF 저장 완료", sent_messages[0][1])


if __name__ == "__main__":
    unittest.main()
