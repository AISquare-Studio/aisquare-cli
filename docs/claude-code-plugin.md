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

- macOS, Linux and WSL. On native Windows the hooks need Git Bash's `sh`; without it, keep the `settings.json` route (`aisquare agents connect claude-code`).
- Plugins belong to one Claude Code config directory. One installed in `~/.claude` does not reach the fleet's account directories (`~/.claude-c1` and so on), which keep their `settings.json` hooks.
- Claude Code does not update third-party marketplaces on its own by default. After an aisquare release, run `/plugin marketplace update aisquare-cli`, then `/plugin update aisquare@aisquare-cli`. You can also turn on auto-update for the marketplace under `/plugin`.
- The marketplace is this repository, so adding it clones the repository.

## Removing it

Run `/plugin uninstall aisquare@aisquare-cli`, and `/plugin marketplace remove aisquare-cli` if you no longer want the marketplace. Your memory stays in `~/.aisquare`.
