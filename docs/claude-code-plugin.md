# The Claude Code plugin

The aisquare plugin gives Claude Code the six hooks that `aisquare agents connect claude-code` writes into `settings.json`, without an installer and without touching that file. Inside Claude Code:

```text
/plugin marketplace add AISquare-Studio/aisquare-cli
/plugin install aisquare@aisquare-cli
```

Then start a new session, or run `/reload-plugins` if Claude Code asks for it. The same steps from a shell:

```sh
claude plugin marketplace add AISquare-Studio/aisquare-cli
claude plugin install aisquare@aisquare-cli
```

## What runs

The hooks are SessionStart, UserPromptSubmit, SessionEnd, Stop, Notification and StopFailure. Each one runs `aisquare hook <event>`, the same entry point the `settings.json` hooks use:

- the aisquare CLI on your `PATH`, or in `~/.local/bin` where `uv tool install` puts it;
- otherwise the aisquare release the plugin pins, through `uvx`. A machine with only [uv](https://docs.astral.sh/uv/) gets memory with nothing else installed. The first session start downloads it, which takes a few seconds; after that it starts in well under a second;
- with neither, the session starts as usual and says once that aisquare is not available.

A hook that fails never blocks a prompt or a session.

## Saving memory on purpose

The hooks hand your saved context to every new session. To save something, use the CLI. Install it once, and the plugin runs that same install from then on:

```sh
uv tool install aisquare-cli
aisquare remember --user "I prefer pytest"
```

## The plugin and the settings.json hooks together

The one-line installer and `aisquare agents connect claude-code` install the hooks into `settings.json`. If you also install the plugin, its hooks stand down for every event `settings.json` already runs, so nothing fires twice. `aisquare doctor` still warns until you keep one route:

```sh
aisquare doctor
# keep the plugin, and remove the settings.json hooks:
aisquare agents disconnect claude-code
```

To keep the `settings.json` hooks instead, run `/plugin uninstall aisquare@aisquare-cli` inside Claude Code.

## Limits

- macOS, Linux and WSL. Native Windows keeps the `settings.json` route (`aisquare agents connect claude-code`): the hooks run `sh`, and `aisquare doctor` there reads only that route.
- Plugins belong to one Claude Code config directory. One installed in `~/.claude` does not reach the fleet's account directories (`~/.aisquare/claude-accounts/<slot>`), which keep their `settings.json` hooks.
- Claude Code does not update third-party marketplaces on its own by default. After an aisquare release, run `claude plugin marketplace update aisquare-cli` and then `claude plugin update aisquare@aisquare-cli` from a shell, and restart Claude Code. To have it update on its own instead, open `/plugin`, go to the Marketplaces tab, select `aisquare-cli` and choose Enable auto-update.
- The marketplace is this repository, so adding it clones the repository.

## Removing it

Run `/plugin uninstall aisquare@aisquare-cli`, and `/plugin marketplace remove aisquare-cli` if you no longer want the marketplace. Your memory stays in `~/.aisquare`.

`aisquare uninstall` leaves the plugin to you, because the plugin is Claude Code's: it names the command that removes it, and `--purge` waits until it is gone, since the plugin would run aisquare again and make `~/.aisquare` anew.
