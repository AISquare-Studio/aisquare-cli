# The Claude Code plugin

The aisquare plugin gives Claude Code the six hooks that `aisquare agents connect claude-code` writes into `settings.json`, without an installer and without writing hooks into that file. Installing it still writes to `settings.json`, or to the repository's `.claude` settings for a project or local install: Claude Code records there that the plugin is enabled (`enabledPlugins`). Inside Claude Code:

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
- otherwise the aisquare release the plugin pins, through `uvx`. On a machine with only [uv](https://docs.astral.sh/uv/), the hooks run with nothing else installed: they capture your prompts and hand each new session what is saved. There is no `aisquare` command there, so session start tells the agent to read the prompts with `uvx --python <python> --from aisquare-cli==<version> aisquare log`, naming the Python the hooks ran on and the `uvx` they ran (by its path, where it is not on your `PATH`), and saving something needs the CLI ([below](#saving-memory-on-purpose)). The first session start downloads the release, which takes a few seconds; after that it starts in well under a second;
- with neither, the session starts as usual and says once that aisquare is not available.

A hook that fails never blocks a prompt or a session.

On the `uvx` route the plugin asks uv, at session start, whether it has a Python aisquare is tested on (3.11 to 3.13) or may fetch one, and runs aisquare on it if so. It keeps the answer in `${XDG_CACHE_HOME:-~/.cache}/aisquare/plugin-tested-python`, one line for each uv and the settings it was asked under, so the session's other events start no uv process of their own. An answer that uv has none is asked again after five minutes. While a session start's `uv` downloads that Python for the first time, which takes tens of seconds, the events of other sessions under the same `uv` and settings read "none" and run aisquare on uv's default Python, which may be outside that range: asking uv themselves instead would make each of them wait for the same download, or offline for the same timeout. It is the one file aisquare writes outside `~/.aisquare`.

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

A plugin installed at project or local scope is the exception: it runs only in its repository, and there it stands down beside the `settings.json` hooks every other repository runs on, so `aisquare doctor` reads them as connected and never suggests removing the hooks.

## Limits

- macOS, Linux and WSL. Native Windows keeps the `settings.json` route (`aisquare agents connect claude-code`): the hooks run `sh`, and `aisquare doctor` there reads only that route.
- Plugins belong to one Claude Code config directory. One installed in `~/.claude` does not reach the fleet's account directories (`~/.aisquare/claude-accounts/<slot>`), which keep their `settings.json` hooks. One installed at project or local scope runs only in the repository it was installed in.
- Claude Code does not update third-party marketplaces on its own by default. After an aisquare release, run `claude plugin marketplace update aisquare-cli` and then `claude plugin update aisquare@aisquare-cli` from a shell, and restart Claude Code. To have it update on its own instead, open `/plugin`, go to the Marketplaces tab, select `aisquare-cli` and choose Enable auto-update.
- The marketplace is this repository, so adding it clones the repository.

## Removing it

Run `/plugin uninstall aisquare@aisquare-cli`, and `/plugin marketplace remove aisquare-cli` if you no longer want the marketplace. Your memory stays in `~/.aisquare`, and the plugin's answers about uv stay in `~/.cache/aisquare` (delete that folder if you want them gone; `aisquare uninstall --purge` deletes them, with the home where it is still there). A project or local install is removed from inside its repository, with its scope: `claude plugin uninstall aisquare@aisquare-cli --scope project` (or `--scope local`).

`aisquare uninstall` leaves the plugin to you, because the plugin is Claude Code's: it names the command that removes each install, at every scope, and `--purge` waits until they are gone, since the plugin would run aisquare again and make `~/.aisquare` anew.
