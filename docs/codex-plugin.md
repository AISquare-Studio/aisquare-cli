# The Codex plugin

The aisquare plugin gives Codex the hooks that `aisquare agents connect codex` writes into `$CODEX_HOME/hooks.json`, without an installer and without writing hooks into that file. Installing it writes to Codex's `config.toml`: Codex records there the marketplace, that the plugin is enabled (`[plugins."aisquare@aisquare-cli"]`), and, once you trust them, its hooks (`[hooks.state]`). From a shell:

```sh
codex plugin marketplace add AISquare-Studio/aisquare-cli
codex plugin add aisquare@aisquare-cli
```

Or add the marketplace as above and install the plugin from the plugin browser: type `/plugins` inside Codex.

## Trust its hooks

Installing a plugin trusts none of its hooks, and Codex runs none of them until you do. Start a new Codex session: it says "Hooks need review". Choose **Review hooks** to read them (each runs `sh ".../scripts/aisquare-hook" codex <event>`, from source "Plugin - aisquare@aisquare-cli"), or **Trust all and continue**. You can come back to them at any time with `/hooks`, where `t` trusts them all.

Codex keeps that trust across plugin updates whose hooks are unchanged, and asks again when they change. `aisquare doctor` says when the plugin's hooks are waiting for it.

## What runs

The hooks are SessionStart, UserPromptSubmit, Stop, SessionEnd, Interrupt, PermissionRequest, PreToolUse (for `request_user_input`) and PostToolUse. Each one runs `aisquare hook codex`, the same entry point the `hooks.json` hooks use, told which Codex home the plugin was installed in:

- the aisquare CLI on your `PATH`, or in `~/.local/bin` where `uv tool install` puts it;
- otherwise the aisquare release the plugin pins, through `uvx`. On a machine with only [uv](https://docs.astral.sh/uv/), the hooks run with nothing else installed: they capture your prompts and hand each new session what is saved. The first session start downloads the release, which takes a few seconds;
- with neither, the session starts as usual and says once that aisquare is not available.

A hook that fails never blocks a prompt or a session. Codex runs hooks from its background app server, in the session's folder, so the `PATH` they see is the one Codex was first started with.

The plugin's launcher is the Claude Code plugin's, the same file in both plugins ([The Claude Code plugin](claude-code-plugin.md) has the details of the `uvx` route, including the answers about uv's Pythons it keeps in `~/.cache/aisquare`).

## Saving memory on purpose

The hooks hand your saved context to every new session. To save something, use the CLI. Install it once, and the plugin runs that same install from then on:

```sh
uv tool install aisquare-cli
aisquare remember --user "I prefer pytest"
```

## The plugin and the hooks.json hooks together

The one-line installer and `aisquare agents connect codex` install the hooks into `$CODEX_HOME/hooks.json`. Codex runs a plugin's hooks and that file's side by side, so if you also install the plugin, its hooks stand down while that file runs aisquare's, and nothing fires twice. `hooks.json` gives every event the same command, so the plugin stands down for all of them; if that file lacks some, `aisquare doctor` asks for `aisquare agents connect codex`, which writes the whole set. `aisquare doctor` warns until you keep one route:

```sh
aisquare doctor
# keep the plugin, and remove the hooks.json hooks:
aisquare agents disconnect codex
# or keep the hooks.json hooks, and remove the plugin:
codex plugin remove aisquare@aisquare-cli
```

Each route's hooks need trusting on their own: the plugin also stands down beside `hooks.json` hooks you have not trusted yet, which Codex skips. Trust both in `/hooks`, or keep one. Beside `hooks.json` hooks whose aisquare is gone (uninstalled after `agents connect`), the plugin runs in their place, and `aisquare doctor` says to remove them.

`aisquare agents status codex` names the route the Codex home a session from your shell reads takes (`"route": "plugin"`, `"hooks"` or `"both"` under `--json`), and `aisquare doctor` grades each Codex home's.

## Limits

- macOS, Linux and WSL. Native Windows keeps the `hooks.json` route (`aisquare agents connect codex`): the hooks run `sh`, and `aisquare doctor` there reads only that route.
- Plugins belong to one Codex home. One installed in `~/.codex` does not reach another `CODEX_HOME`, nor the fleet's account homes (`~/.aisquare/codex-accounts/<slot>`), which keep their `hooks.json` hooks.
- After an aisquare release, refresh the marketplace and install the new version, then start a new Codex session:

  ```sh
  codex plugin marketplace upgrade aisquare-cli
  codex plugin add aisquare@aisquare-cli
  ```

- The marketplace is this repository, so adding it clones the repository. Its Codex manifest is `.agents/plugins/marketplace.json`, which Codex reads before the Claude Code plugin's `.claude-plugin/marketplace.json`.

## Removing it

Run `codex plugin remove aisquare@aisquare-cli`, and `codex plugin marketplace remove aisquare-cli` if you no longer want the marketplace. For a home other than the one your shell's `codex` reads, set `CODEX_HOME` for them. Your memory stays in `~/.aisquare`.

`aisquare uninstall` leaves the plugin to you, because the plugin is Codex's: it names the command that removes it from each Codex home that enables it, and `--purge` waits until it is gone, since the plugin would run aisquare again and make `~/.aisquare` anew.

## Sources

Measured on Codex 0.162.1 (`codex plugin --help`, a local marketplace, `/hooks`). The manifest and marketplace formats are Codex's: [Build plugins](https://developers.openai.com/codex/plugins/build/) (`.codex-plugin/plugin.json`, `.agents/plugins/marketplace.json`, the plugin cache and `config.toml`), and [Hooks](https://learn.chatgpt.com/docs/hooks) (plugin hooks in `hooks/hooks.json`, `PLUGIN_ROOT`, and trust).
