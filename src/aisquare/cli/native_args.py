"""Protect native arguments from Click's short-option cluster parser."""

from typer._click.core import Context
from typer._click.exceptions import UsageError
from typer.core import TyperCommand, TyperOption

from aisquare.core.agent_adapters import adapters
from aisquare.core.agent_adapters.types import is_config_assignment


class NativeForwardingCommand(TyperCommand):
    def parse_args(self, ctx: Context, args: list[str]) -> list[str]:
        """Keep legacy -c BINARY while allowing Codex's -c KEY=VALUE overrides.

        Normalize the native alias to its long spelling before Click sees it;
        otherwise it is consumed as --command (or split as a short-option cluster).
        Known AISquare option values and everything after -- remain untouched.
        """
        rewritten: list[str] = []
        protected: dict[str, str] = {}
        valued = {
            option
            for param in self.get_params(ctx)
            if isinstance(param, TyperOption) and not param.is_flag
            for option in param.opts
        }
        # Native option values are not positional commands/prompts. Adapters
        # supply their arity; unknown flags require an explicit boundary if
        # later AISquare options would make ownership ambiguous.
        native_values = {
            option for adapter in adapters() for option in adapter.capabilities.value_options
        }
        native_switches = {
            option for adapter in adapters() for option in adapter.capabilities.switch_options
        }
        owned_options = {
            option
            for param in self.get_params(ctx)
            if isinstance(param, TyperOption)
            for option in (*param.opts, *param.secondary_opts)
        }
        role_seen = False
        tokens = iter(enumerate(args))
        for index, token in tokens:
            if token == "--":
                rewritten.extend(args[index:])
                break
            if not token.startswith("-"):
                if role_seen:
                    # Insert our boundary; a subsequent user -- now belongs
                    # to the native CLI and survives Click unchanged.
                    rewritten.extend(["--", *args[index:]])
                    break
                role_seen = True
                rewritten.append(token)
                continue
            if token == "-c" and "-c" in valued:
                following = next(tokens, None)
                value = following[1] if following is not None else None
                if value is not None and is_config_assignment(value):
                    rewritten.append("--config=" + value)
                else:
                    rewritten.append(token)
                    if value is not None:
                        rewritten.append(value)
            elif "-c" in valued and token.startswith("-c") and is_config_assignment(token[2:]):
                rewritten.append("--config=" + token[2:])
            elif (
                token.startswith("-")
                and not token.startswith("--")
                and len(token) > 2
                and token[:2] not in valued
            ):
                # Click otherwise parses known letters *inside* an unknown
                # short option: -mexample used to become --env xample.
                marker = f"--__aisquare_native_arg_{len(protected)}={token}"
                while marker in args:
                    marker = "-" + marker
                protected[marker] = token
                rewritten.append(marker)
            else:
                rewritten.append(token)
                if token in valued or (
                    token in native_values
                    and index + 1 < len(args)
                    and not args[index + 1].startswith("-")
                ):
                    following = next(tokens, None)
                    if following is not None:
                        rewritten.append(following[1])
                elif (
                    token not in native_switches | native_values
                    and not (len(token) > 2 and token[:2] in valued)
                    and token.split("=")[0] not in owned_options
                    and "=" not in token
                ):
                    tail = args[index + 1 :]
                    until_separator = tail[: tail.index("--")] if "--" in tail else tail
                    if any(part.split("=")[0] in valued for part in until_separator):
                        raise UsageError(
                            f"Unknown native option {token!r}: put AISquare options before it "
                            "and use -- before the native arguments.",
                            ctx,
                        )
                    rewritten.extend(["--", *tail])
                    break
        remaining = super().parse_args(ctx, rewritten)
        remaining[:] = [protected.get(token, token) for token in remaining]
        return remaining
