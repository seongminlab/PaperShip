# PaperShip

PaperShip is a local-first journal monitoring workspace. It checks selected research journals every morning, summarizes newly published papers in Korean, sends the digest to Telegram, and can save papers you mark as interesting.

## What It Does

- Checks configured journal sources once a day.
- Sends only unseen papers published today or yesterday.
- Generates Korean 8-sentence summaries with the OpenAI API when configured.
- Sends each paper to Telegram with source/PDF links and a `관심 저장` button.
- Saves interested papers into `papers/` and tracks keywords in local JSON data.
- Sends `오늘 업로드된 신규 논문이 없습니다` when there are no new papers.

## Requirements

PaperShip needs three values in `config/secrets.env`:

- `TELEGRAM_BOT_TOKEN`: Telegram Bot API token from BotFather.
- `TELEGRAM_CHAT_ID`: Telegram chat ID where PaperShip should send messages.
- `OPENAI_API_KEY`: OpenAI API token for Korean summaries, keyword extraction, and ranking.

Create the local secrets file:

```bash
cp config/secrets.env.example config/secrets.env
```

Then edit `config/secrets.env`:

```bash
TELEGRAM_BOT_TOKEN=<telegram-bot-api-token>
TELEGRAM_CHAT_ID=<telegram-chat-id>
OPENAI_API_KEY=<openai-api-key>
OPENAI_MODEL=gpt-5-mini
```

`config/secrets.env` is ignored by git. Do not commit real tokens.

## Find Your Telegram Chat ID

1. Create a bot with Telegram BotFather and copy the bot token.
2. Send any message to your bot in Telegram.
3. Open this URL in a browser, replacing the token:

```text
https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/getUpdates
```

4. Find `message.chat.id` in the JSON response and copy that number into `TELEGRAM_CHAT_ID`.

If `getUpdates` returns a webhook conflict, clear the webhook once:

```text
https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/deleteWebhook
```

Then send your bot another message and open the `getUpdates` URL again.

## Repository Layout

- `projects/daily_paper_digest/`: journal digest project.
- `projects/daily_paper_digest/journals.json`: monitored journal source list.
- `projects/_template/`: starter template for additional automation projects.
- `scripts/run_all.py`: runs every enabled project.
- `config/secrets.env.example`: safe environment variable example.
- `launchd/*.plist.example`: macOS launchd templates.
- `docs/`: operating notes.
- `tests/`: regression tests.

## Private Files

The following local files are intentionally ignored and should not be committed:

- `config/*.env`
- `data/`
- `logs/`
- `papers/*`, except `papers/.gitkeep`
- `downloads/`
- `launchd/*.local.plist`

Before publishing, run:

```bash
git status --short
git grep -n -I -E "sk-|ghp_|github_pat|TELEGRAM_BOT_TOKEN=|OPENAI_API_KEY=|/Users/"
```

Only placeholders in examples should appear.

## Run Locally

After filling `config/secrets.env`, run once:

```bash
python3 scripts/run_all.py
```

Run tests:

```bash
python3 -m unittest discover -s tests
```

## macOS Scheduling

PaperShip uses launchd on macOS. See [docs/macos_launchd.md](docs/macos_launchd.md) for daily digest and Telegram listener setup.

## Current Journal Sources

- Nature
- Nature Communications
- Nature Methods
- Nature Biotechnology
- Cell
- Science
- Science Advances
- Science Translational Medicine

Edit [journals.json](projects/daily_paper_digest/journals.json) to add or remove journals.

## Add Journal Sources

Journal sources are configured in [journals.json](projects/daily_paper_digest/journals.json).

For Nature-family research article pages, add an item like:

```json
{
  "name": "Nature Example",
  "listing_url": "https://www.nature.com/example/research-articles",
  "discovery": "nature_html",
  "base_url": "https://www.nature.com"
}
```

For journals with RSS feeds, add an item like:

```json
{
  "name": "Example Journal",
  "listing_url": "https://example.com/journal/research",
  "discovery": "rss",
  "base_url": "https://example.com",
  "feed_url": "https://example.com/rss",
  "allowed_item_types": ["Research Article"]
}
```

After editing the file, run:

```bash
python3 -m unittest discover -s tests
python3 scripts/run_all.py
```
