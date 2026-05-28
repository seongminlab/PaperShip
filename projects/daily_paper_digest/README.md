# 01 Journal Daily Digest

Every morning, this project checks selected journal article sources and sends papers published on the run date or the previous day to Telegram.

Currently monitored sources:

- Nature
- Nature Communications
- Nature Methods
- Nature Biotechnology
- Cell
- Science
- Science Advances
- Science Translational Medicine
- JAMA
- New England Journal of Medicine
- The Lancet
- The BMJ
- Annals of Internal Medicine

The source list lives in:

`config/journals.json`

Add or remove journals there, then run the tests before relying on the morning automation. In normal use, journal source changes should only require edits inside `config/`.

Each Telegram message includes:

- Original paper title
- Journal name
- Authors
- 8 Korean summary sentences
- Article link
- PDF download button when a PDF URL can be inferred or exposed
- `관심 저장` button for saving a paper locally

## Interested Papers

The Telegram listener handles two ways to save an interested paper:

- Tap the `관심 저장` button under a daily digest message.
- Send `/save 논문원문링크` to the bot.
- Send only a paper URL to the bot.
- Upload a PDF file to the bot. If the PDF message caption contains a paper URL, PaperShip uses the paper metadata for the saved filename; otherwise it falls back to the uploaded PDF filename.

When saving succeeds, the listener:

- Downloads the PDF into `papers/`.
- Uses this filename pattern: `{first-author-lastname}-{year}-{first-5-title-words}.pdf`.
- Generates a Korean 8-sentence summary.
- Extracts keywords.
- Updates `data/daily_paper_digest/keywords.json`.
- Sends a Telegram confirmation message.

Button saves send a short confirmation with title, keywords, and PDF status. `/save 논문원문링크` sends the same save status plus the Korean summary.

If PDF download fails, the listener still stores the paper metadata and keywords, then sends a Telegram message with the paper title, the failure reason, and buttons for `논문 원문` and `PDF 다운로드`.

The listener is intended to run continuously through:

`launchd/com.papership.journal-listener.plist.example`

If the `관심 저장` button does not react, restart the listener:

```bash
launchctl kickstart -k gui/$(id -u)/com.papership.journal-listener
```

If you use a custom local launchd label, set `PAPERSHIP_LISTENER_RESTART_COMMAND` in your local environment so Telegram error messages show the right restart command.

If polling fails because a Telegram webhook is active, the listener automatically calls `deleteWebhook` before polling.

## Keyword File

Interested-paper keywords are stored in:

`data/daily_paper_digest/keywords.json`

The file has two main sections:

- `articles`: saved papers and each paper's keyword list.
- `keywords`: keyword frequency counts and the article URLs where each keyword appears.

To manually add a keyword, edit the relevant article entry in `articles` and add the keyword to its `keywords` array. Then rebuild keyword frequency counts:

```bash
python3 projects/daily_paper_digest/telegram_listener.py --rebuild-keywords
```

The `keywords` frequency section is regenerated from the article keyword arrays.

## Required Settings

Copy `config/secrets.env.example` to `config/secrets.env`, then fill in:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

You can also set these as environment variables if you prefer.

## Optional Settings

- `OPENAI_API_KEY`
- `OPENAI_MODEL`
- `JOURNAL_MAX_ARTICLES_PER_SOURCE`
- `JOURNAL_REQUEST_TIMEOUT`

If `OPENAI_API_KEY` is set, summaries are generated as natural Korean. Without it, the project still runs and creates a structured Korean digest from publisher metadata.

The project sends unseen articles whose publication date matches the local date when the code runs or the previous day. For example, a run on `2026-05-27` can send articles published on `2026-05-27` or `2026-05-26`, but articles already recorded in `data/daily_paper_digest/seen_articles.json` are skipped.

Nature-family journals are discovered from their research article listing pages. Cell, Science-family, and most medical journals are discovered from official RSS feeds because their article listing or article pages can reject automated HTML requests. The BMJ uses a configured HTML link pattern for its research listing page.

After daily article messages, the project sends `오늘의 관심 후보 TOP 3` with paper title, journal, and three short keywords.

## Add A Journal

For a Nature-family research article page:

```json
{
  "name": "Nature Example",
  "listing_url": "https://www.nature.com/example/research-articles",
  "discovery": "nature_html",
  "base_url": "https://www.nature.com"
}
```

For a publisher RSS feed:

```json
{
  "name": "Example Journal",
  "listing_url": "https://example.test/research",
  "discovery": "rss",
  "base_url": "https://example.test",
  "feed_url": "https://example.test/action/showFeed?type=etoc&feed=rss&jc=example",
  "allowed_item_types": ["Research Article"]
}
```

For a publisher listing page that exposes article links in HTML:

```json
{
  "name": "Example HTML Journal",
  "listing_url": "https://example.test/research",
  "discovery": "html_links",
  "base_url": "https://example.test",
  "article_url_patterns": ["^/content/[0-9]+/example-[0-9]+$"]
}
```

## Manual Run

```bash
python3 scripts/run_all.py
```

## Tests

```bash
python3 -m unittest discover -s tests
```

## State

Seen article URLs are stored in:

`data/daily_paper_digest/seen_articles.json`

To re-send old papers, remove that file.
