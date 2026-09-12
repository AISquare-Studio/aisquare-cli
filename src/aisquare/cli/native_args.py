"""Protect native arguments from Click's short-option cluster parser."""

import re

from typer._click.core import Context
from typer.core import TyperCommand, TyperOption


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
        tokens = iter(args)
        for token in tokens:
            if token == "--":
                rewritten.extend([token, *tokens])
                break
            if token == "-c" and "-c" in valued:
                value = next(tokens, None)
                if value is not None and re.match(r"^[A-Za-z_][\w.-]*=", value):
                    rewritten.append("--config=" + value)
                else:
                    rewritten.append(token)
                    if value is not None:
                        rewritten.append(value)
            elif (
                "-c" in valued
                and token.startswith("-c")
                and re.match(r"^[A-Za-z_][\w.-]*=", token[2:])
            ):
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
                if token in valued:
                    value = next(tokens, None)
                    if value is not None:
                        rewritten.append(value)
        remaining = super().parse_args(ctx, rewritten)
        remaining[:] = [protected.get(token, token) for token in remaining]
        return remaining
