# macOS launchd Setup

PaperShip includes launchd templates for:

- Daily 7 AM digest: `launchd/com.papership.daily-automation.plist.example`
- Always-on Telegram listener: `launchd/com.papership.journal-listener.plist.example`

The template files use placeholders so personal paths are not committed.

## Prepare Local Plists

From the repository root:

```bash
mkdir -p ~/Library/Logs/PaperShip
sed "s#__PAPERSHIP_ROOT__#$(pwd)#g; s#__HOME__#$HOME#g" \
  launchd/com.papership.daily-automation.plist.example \
  > launchd/com.papership.daily-automation.local.plist
sed "s#__PAPERSHIP_ROOT__#$(pwd)#g; s#__HOME__#$HOME#g" \
  launchd/com.papership.journal-listener.plist.example \
  > launchd/com.papership.journal-listener.local.plist
```

The generated `*.local.plist` files are ignored by git.

## Install

```bash
cp launchd/com.papership.daily-automation.local.plist ~/Library/LaunchAgents/com.papership.daily-automation.plist
cp launchd/com.papership.journal-listener.local.plist ~/Library/LaunchAgents/com.papership.journal-listener.plist
launchctl load ~/Library/LaunchAgents/com.papership.daily-automation.plist
launchctl load ~/Library/LaunchAgents/com.papership.journal-listener.plist
```

## Run Once Manually

```bash
launchctl start com.papership.daily-automation
launchctl kickstart -k gui/$(id -u)/com.papership.journal-listener
```

## Confirm Status

```bash
launchctl print gui/$(id -u)/com.papership.daily-automation
launchctl print gui/$(id -u)/com.papership.journal-listener
```

## Uninstall

```bash
launchctl unload ~/Library/LaunchAgents/com.papership.daily-automation.plist
launchctl unload ~/Library/LaunchAgents/com.papership.journal-listener.plist
```

The daily schedule is set to 7:00 AM in `launchd/com.papership.daily-automation.plist.example`.
