"""Root help, one-line descriptions and the generated command reference.

The grouped root help and ``sprintctl usage`` are both derived from the Click
tree, so a new command cannot drift out of the reference.  Every top-level
command needs an entry in ``SHORT_HELP`` and ``GROUPS`` (tests enforce it).
"""

from __future__ import annotations

import difflib
import re
import textwrap

import click

PURPOSE = "Advisory sprint coordination for agent sessions and operators."

# Task-grouped order: orientation first, plumbing last.
GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Start here", ("status", "next-work", "item", "reservation", "sprint")),
    ("Coordinate", ("handoff", "takeup", "event", "message", "agent", "run")),
    ("Inspect", ("usage", "render", "git-context", "context-candidates", "effect", "result", "agent-protocol")),
    ("Change", ("sync", "export", "import")),
    ("Maintain", ("maintain", "doctor", "db", "session")),
    ("Plumbing", ("authority", "projection-reads", "remote-schema", "remote-backfill", "migrate-to-remote", "repo")),
)

SHORT_HELP = {
    "status": "what needs you, what is in flight, what is free",
    "next-work": "items ready to start (priority order)",
    "item": "work items: add, list, show, note, status",
    "reservation": "advisory reservations on items",
    "sprint": "sprints: create, show, list, status",
    "handoff": "write a handoff bundle (records an event)",
    "takeup": "sprint-level takeup events",
    "event": "events and offline observations",
    "message": "durable inbox: send, read, receipts",
    "agent": "agent directory: register, observe, retire",
    "run": "run continuation: predecessor context, notes",
    "usage": "command reference; --context for sprint context",
    "render": "render a plain-text sprint document",
    "git-context": "git branch, commit and worktree",
    "context-candidates": "ranked context-candidate packet",
    "effect": "read effect acceptance facts",
    "result": "inspect offline owner read artifacts",
    "agent-protocol": "print the reservation protocol",
    "sync": "synchronize local observations (writes)",
    "export": "export a sprint to a JSON file",
    "import": "import a sprint from an export file (writes)",
    "maintain": "stale-item checks, sweeps, carryover",
    "doctor": "diagnose install, backend and schema",
    "db": "local database vacuum and integrity",
    "session": "session lifecycle helpers",
    "authority": "remote authority command journal",
    "projection-reads": "guarded projection-backed reads",
    "remote-schema": "check or migrate the shared schema",
    "remote-backfill": "copy repository history between authorities",
    "migrate-to-remote": "migrate local sqlite to remote postgres",
    "repo": "repositories in remote postgres",
}

EXAMPLES = (
    "sprintctl status",
    "sprintctl next-work",
    "sprintctl reservation reserve --item-id 12 --actor me",
)


def derive_short_help(command: click.Command) -> str | None:
    """First sentence of the docstring, whole (Click would clip it at 45)."""
    text = (command.help or "").strip().split("\n\n")[0]
    text = re.sub(r"\s+", " ", text)
    text = re.split(r"(?<=[a-z)])\. ", text)[0].rstrip(".")
    return text or None


def apply_short_help(group: click.Group, *, top: bool = True) -> None:
    """Give every command an unclipped one-line description (recursive)."""
    for name, command in group.commands.items():
        if top and name in SHORT_HELP:
            command.short_help = SHORT_HELP[name]
        elif command.short_help is None:
            command.short_help = derive_short_help(command)
        if isinstance(command, click.Group):
            apply_short_help(command, top=False)


class RootGroup(click.Group):
    """Root group: grouped help, did-you-mean, and bare invocation prints help."""

    def format_help(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        formatter.write_text(PURPOSE)
        formatter.write_paragraph()
        self.format_usage(ctx, formatter)
        listed: set[str] = set()
        for title, names in GROUPS:
            rows = [(n, self.commands[n].short_help or "") for n in names if n in self.commands]
            listed.update(names)
            if rows:
                with formatter.section(title):
                    formatter.write_dl(rows)
        extra = [(n, c.short_help or "") for n, c in sorted(self.commands.items()) if n not in listed and not c.hidden]
        if extra:
            with formatter.section("Other"):
                formatter.write_dl(extra)
        self.format_options(ctx, formatter)
        with formatter.section("Examples"):
            for example in EXAMPLES:
                formatter.write_text(example)
        formatter.write_paragraph()
        formatter.write_text(
            f"Run `{ctx.command_path} <command> --help` for a command's options, "
            f"`{ctx.command_path} usage` for every command. "
            "Exit codes: 0 ok, 1 failed, 2 usage."
        )

    def format_commands(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        """Commands are listed by task in ``format_help``."""

    def resolve_command(self, ctx, args):
        try:
            return super().resolve_command(ctx, args)
        except click.UsageError as exc:
            name = args[0] if args else ""
            close = difflib.get_close_matches(name, self.commands, n=1, cutoff=0.6)
            if close and "No such command" in exc.message:
                exc.message += f" did you mean {close[0]}?"
            raise


def _leaf_paths(group: click.Group, prefix: tuple[str, ...] = ()):
    for name, command in group.commands.items():
        if isinstance(command, click.Group):
            yield from _leaf_paths(command, (*prefix, name))
        else:
            yield (*prefix, name), command


def _param_text(param: click.Parameter) -> str | None:
    if isinstance(param, click.Argument):
        return param.human_readable_name.upper() if param.required else f"[{param.human_readable_name.upper()}]"
    if not isinstance(param, click.Option) or param.name == "help" or param.hidden:
        return None
    flag = max(param.opts, key=len) if not param.secondary_opts else f"{param.opts[0]}/{param.secondary_opts[0]}"
    if param.is_flag:
        text = flag
    elif isinstance(param.type, click.Choice):
        text = f"{flag} {'|'.join(param.type.choices)}"
    else:
        text = f"{flag} {param.type.name.split()[0].upper()}"
    return text if param.required else f"[{text}]"


def render_usage(root: click.Group, *, version: str, unavailable: set[str] | None = None) -> str:
    """The full command reference, generated from the Click tree."""
    unavailable = unavailable or set()
    leaves = {" ".join(path): command for path, command in _leaf_paths(root)}
    lines = [f"sprintctl v{version}: {PURPOSE}", "Generated from the command tree; `<command> --help` has details."]
    if unavailable:
        lines.append("(n/a) = not available with the active backend (served).")
    placed: set[str] = set()
    for title, names in (*GROUPS, ("Other", tuple(sorted(root.commands)))):
        block = []
        for name in names:
            for path, command in leaves.items():
                if path in placed or (path.split(" ")[0] != name):
                    continue
                placed.add(path)
                params = " ".join(t for t in map(_param_text, command.params) if t)
                mark = " (n/a)" if path in unavailable else ""
                block.extend(textwrap.wrap(
                    f"{path}{mark}  {params}".rstrip(), width=96,
                    initial_indent="  ", subsequent_indent="      ", break_long_words=False, break_on_hyphens=False,
                ))
        if block:
            lines += ["", title.upper(), *block]
    return "\n".join(lines)
