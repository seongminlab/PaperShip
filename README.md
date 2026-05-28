# PaperShip

PaperShip is a local-first journal monitoring workspace. It checks selected research journals every morning, summarizes newly published papers in Korean, sends the digest to Telegram, and can save papers you mark as interesting.

## What It Does

- Checks configured journal sources once a day.
- Sends only unseen papers published today or yesterday.
- Generates Korean 8-sentence summaries with the OpenAI API when configured.
- Sends each paper to Telegram with source/PDF links and a `관심 저장` button.
- Saves interested papers into `papers/` and tracks keywords in local JSON data.
- Sends `오늘 업로드된 신규 논문이 없습니다` when there are no new papers.

## Repository Layout

- `projects/01_nature_daily_digest/`: journal digest project.
- `projects/01_nature_daily_digest/journals.json`: monitored journal source list.
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

## Setup

```bash
cp config/secrets.env.example config/secrets.env
```

Fill in:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `OPENAI_API_KEY` and `OPENAI_MODEL` if you want OpenAI summaries

Run once:

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

Edit [journals.json](projects/01_nature_daily_digest/journals.json) to add or remove journals.
