#!/usr/bin/env python3
"""
agenc — a code review agent that connects to any OpenAI-compatible endpoint.

It explores your repo with sandboxed shell commands and gives you feedback on your code.
"""

import json
import subprocess
from pathlib import Path

from openai import OpenAI
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.theme import Theme

from config import API_KEY, DEFAULT_BASE_URL, DEFAULT_MODEL, CWD
from tools import (
    TOOLS,
    display_tool_call,
    display_tool_result,
    handle_bash,
    handle_create_file,
    handle_edit_file,
)

# ---------------------------------------------------------------------------
# Prompt styling
# ---------------------------------------------------------------------------

PROMPT_STYLE = Style.from_dict({
    "blue": "#0087ff",  # Bright blue
    "dim": "#555555",
})


# ---------------------------------------------------------------------------
# Slash command completion
# ---------------------------------------------------------------------------

# Maps primary command names to their aliases. Only primary names are shown in completions.
SLASH_COMMANDS = {
    "/help": [],
    "/quit": ["/exit", "/q"],
    "/clear": [],
    "/model": [],
    "/run": [],
}


def get_slash_command_aliases():
    """Return a flat dict mapping all command names (including aliases) to their primary name."""
    aliases = {}
    for primary, alias_list in SLASH_COMMANDS.items():
        aliases[primary] = primary
        for alias in alias_list:
            aliases[alias] = primary
    return aliases


class SlashCommandCompleter(Completer):
    """Provide autocompletion for slash commands when '/' is typed."""

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        # If text ends with space, don't complete (user moved past the command)
        if text.endswith(" "):
            return
        # Get the current word being typed (after last space)
        words = text.split()
        if not words:
            return
        current_word = words[-1]
        # Only complete if current word starts with "/"
        if current_word.startswith("/"):
            for cmd in SLASH_COMMANDS.keys():
                if cmd.startswith(current_word):
                    yield Completion(cmd, start_position=-len(current_word))

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = f"""\
You are **agenc**, a concise, direct coding assistant running inside a developer's terminal.

## Role
- Review code, explain logic, find bugs, suggest improvements, and answer questions about the repo.
- Be direct and technical. Lead with the answer or action, then add detail only if needed. Skip pleasantries.

## Working directory
{CWD}

## Principles
1. **Gather context before acting.** Use `bash` to read files before editing, check `git status` before committing, and verify state after making changes. Don't guess at contents.
2. **Be specific.** Reference file names, line numbers, and function names.
3. **Think before you conclude.** When analyzing complex problems, trace the actual code flow step-by-step. Follow the data, verify assumptions, and distinguish between display logic vs. functional logic. Don't report issues until you've traced the full execution path.
4. **Act, don't ask.** When a request is slightly ambiguous, pick the most reasonable interpretation, proceed, and note your assumption. Only ask for clarification when the request is genuinely unanswerable without it.
5. **Give actionable feedback.** Concrete suggestions the developer can apply — not vague observations.
6. **When reviewing, look for:** bugs, edge cases, naming, structure, performance, security, readability.

## Tools

### bash
- Read-only utilities: ls, cat, grep, find, head, tail, tree, rg, sed (for reading), etc.
- Command output is automatically compressed — on success you may see only the tail of long outputs. If you need specific lines from the middle, use head/tail/sed to extract them.

### File editing
- Use `edit_file` for surgical changes — always read the file first to get exact text for old_str.
- Use `create_file` for new files or full rewrites.
- The user must approve every write — if they reject, adjust your approach.
- Make focused, minimal edits. Don't rewrite entire files when a small edit will do.
- For multi-file changes: make all edits, then verify the result with `git diff` or by reading the changed files.

### Git
- **Allowed:** `git status`, `git diff`, `git log`, `git show`, `git blame`, `git branch`, `git add`, `git commit`.
- **Blocked:** reset, push, rebase, cherry-pick, merge, checkout, clean, and any other destructive operation.
- Write clear, conventional commit messages.

## Technical limits
- All paths must stay within the working directory.
- If you need information, explore with bash — don't assume.
- If a command fails or an edit doesn't apply, read the error carefully, diagnose the issue, and retry with a corrected approach. Don't repeat the same failing command.

## Behavioral guidelines
- Only make the changes you were asked to make. Do not modify behaviour, expand permissions, or refactor anything beyond the scope of the request.
- Don't suggest or perform refactors, linting fixes, or style changes unless the user specifically asks.
- Prefer simple, direct solutions. Avoid unnecessary abstractions, modes, or indirection. The right amount of code is the minimum that correctly solves the problem.
- Never echo or log secrets, tokens, API keys, or credentials. Be cautious with any command that deletes or overwrites data.
- Use markdown for formatting. Keep responses concise — a terminal is not the place for essays.
"""

# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------

console = Console(
    theme=Theme(
        {
            "info": "dim cyan",
            "warning": "yellow",
            "tool": "dim green",
        }
    )
)


def make_session() -> PromptSession:
    """Create a PromptSession with multi-line support.

    Enter submits. Alt+Enter inserts a newline. Pasting multi-line text works
    automatically via bracketed paste. Typing / shows slash command completions.
    
    When '/' is typed, shows slash command completions.
    """
    bindings = KeyBindings()

    @bindings.add("enter")
    def _(event):
        event.current_buffer.validate_and_handle()

    @bindings.add("escape", "enter")  # Alt+Enter
    def _(event):
        event.current_buffer.insert_text("\n")

    return PromptSession(
        key_bindings=bindings,
        multiline=True,
        completer=SlashCommandCompleter(),
    )


def make_client() -> OpenAI:
    if not API_KEY:
        console.print(
            "[warning]Warning: No API key found. Set AGENC_API_KEY or OPENAI_API_KEY.[/warning]"
        )
    return OpenAI(base_url=DEFAULT_BASE_URL, api_key=API_KEY or "unused")


def chat_turn(client: OpenAI, messages: list, model: str) -> str:
    """Run one turn of the agentic loop: call the model, execute any tool calls, repeat."""
    while True:
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
        )

        msg = response.choices[0].message

        # Append the assistant message to history
        messages.append(msg.model_dump(exclude_none=True))

        # If no tool calls, we're done — return the text
        if not msg.tool_calls:
            return msg.content or ""

        # Process each tool call
        for tc in msg.tool_calls:
            fn = tc.function
            try:
                args = json.loads(fn.arguments)
            except json.JSONDecodeError:
                args = {"command": fn.arguments}

            display_tool_call(fn.name, args)

            if fn.name == "bash":
                _, result = handle_bash(args)
                display_tool_result(result)
                # Build tool output string
                tool_output = ""
                if result["stdout"]:
                    tool_output += result["stdout"]
                if result["stderr"]:
                    tool_output += f"\nSTDERR: {result['stderr']}"
                if not tool_output.strip():
                    tool_output = "(empty output)"
            elif fn.name == "create_file":
                tool_output = handle_create_file(args)
                console.print(f"  [info]{tool_output}[/info]")
            elif fn.name == "edit_file":
                tool_output = handle_edit_file(args)
                console.print(f"  [info]{tool_output}[/info]")
            else:
                tool_output = f"Unknown tool: {fn.name}"

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": tool_output,
                }
            )


def print_welcome():
    console.print(
        Panel(
            f"[bold]agenc[/bold] — code review agent\n"
            f"model: [info]{DEFAULT_MODEL}[/info]  endpoint: [info]{DEFAULT_BASE_URL}[/info]\n"
            f"cwd: [info]{CWD}[/info]\n\n"
            f"Type your question or [bold]/help[/bold] for commands. [bold]/quit[/bold] to exit.",
            border_style="blue",
        )
    )


def print_help():
    console.print(
        Markdown(
            """\
### Commands
- `/help`  — show this message
- `/quit`  — exit
- `/clear` — clear conversation history
- `/model <name>` — switch model
- `/run <command>` — run a shell command directly (unsandboxed)

### Key bindings
- **Enter** — send message
- **Alt+Enter** — insert newline
- Pasting multi-line text works without any special mode
"""
        )
    )


def main():
    global DEFAULT_MODEL

    print_welcome()
    client = make_client()

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    session = make_session()
    prompt = HTML("<b><blue>you</blue></b> > ")

    while True:
        try:
            user_input = session.prompt(prompt, style=PROMPT_STYLE).strip()
        except KeyboardInterrupt:
            console.print("\n[info]Input cancelled.[/info]")
            continue
        except EOFError:
            console.print("\n[info]Goodbye![/info]")
            break

        if not user_input:
            continue

        # Slash commands
        if user_input.startswith("/"):
            cmd_parts = user_input.split(maxsplit=1)
            cmd = cmd_parts[0].lower()
            aliases = get_slash_command_aliases()

            # Resolve alias to primary command
            primary_cmd = aliases.get(cmd)

            if primary_cmd == "/quit":
                console.print("[info]Goodbye![/info]")
                break
            elif primary_cmd == "/help":
                print_help()
                continue
            elif primary_cmd == "/clear":
                messages = [{"role": "system", "content": SYSTEM_PROMPT}]
                console.print("[info]Conversation cleared.[/info]")
                continue
            elif primary_cmd == "/model":
                if len(cmd_parts) > 1:
                    DEFAULT_MODEL = cmd_parts[1]
                    console.print(f"[info]Model set to {DEFAULT_MODEL}[/info]")
                else:
                    console.print(f"[info]Current model: {DEFAULT_MODEL}[/info]")
                continue
            elif primary_cmd == "/run":
                if len(cmd_parts) > 1:
                    subprocess.run(cmd_parts[1], shell=True, cwd=str(CWD))
                else:
                    console.print("[warning]Usage: /run <command>[/warning]")
                continue
            else:
                console.print(f"[warning]Unknown command: {cmd}[/warning]")
                continue

        messages.append({"role": "user", "content": user_input})

        try:
            console.print()  # breathing room
            reply = chat_turn(client, messages, DEFAULT_MODEL)
            console.print()
            console.print(Markdown(reply))
            console.print()
        except KeyboardInterrupt:
            console.print("\n[warning]Interrupted.[/warning]")
            # Remove the dangling message if the model didn't complete its turn
            if messages[-1]["role"] in ("user", "assistant"):
                messages.pop()
        except Exception as exc:
            console.print(f"\n[bold red]Error:[/bold red] {exc}")
            # Remove the dangling message if the model didn't complete its turn
            if messages[-1]["role"] in ("user", "assistant"):
                messages.pop()


if __name__ == "__main__":
    main()
