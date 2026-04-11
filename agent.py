#!/usr/bin/env python3
"""
agenc — a code review agent that connects to any OpenAI-compatible endpoint.

It explores your repo with sandboxed shell commands and gives you feedback on your code.
"""

import json
import subprocess
from dataclasses import dataclass
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


@dataclass
class TokenUsage:
    """Tracks cumulative token usage for a session."""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def add(self, prompt: int, completion: int, total: int):
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        self.total_tokens += total


# ---------------------------------------------------------------------------
# Slash command completion
# ---------------------------------------------------------------------------

# Maps primary command names to their aliases. Only primary names are shown in completions.
SLASH_COMMANDS = {
    "/help": [],
    "/quit": ["/exit", "/q"],
    "/clear": [],
    "/model": [],
    "/models": [],
    "/run": [],
    "/tokens": ["/usage"],
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
    """Provide autocompletion for slash commands and model names."""

    def __init__(self, client=None):
        self.client = client
        self._model_cache = None

    def _get_models(self):
        """Fetch and cache available model names."""
        if self._model_cache is not None:
            return self._model_cache
        if self.client is None:
            return []
        try:
            models = self.client.models.list()
            self._model_cache = [m.id for m in models.data]
            return self._model_cache
        except Exception:
            return []

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        words = text.split()
        
        # Model name completion after '/model ' (check before split() loses the space)
        if text.rstrip().endswith("/model") and text.endswith(" "):
            # User typed "/model " and is looking for model names
            models = self._get_models()
            for model in models:
                yield Completion(model, start_position=0)
            return
        
        # Model name completion when typing "/model <partial>"
        if len(words) >= 2 and words[-2] == "/model" and not words[-1].startswith("/"):
            models = self._get_models()
            current_word = words[-1]
            for model in models:
                if model.startswith(current_word):
                    yield Completion(model, start_position=-len(current_word))
            return

        # If text ends with space, don't complete slash commands
        if text.endswith(" "):
            return

        # Slash command completion
        if words and words[-1].startswith("/"):
            current_word = words[-1]
            for cmd in SLASH_COMMANDS.keys():
                if cmd.startswith(current_word):
                    yield Completion(cmd, start_position=-len(current_word))

# ---------------------------------------------------------------------------
# Fetch available models
# ---------------------------------------------------------------------------

def get_available_models(client: OpenAI) -> list:
    """Fetch available model names from the API."""
    try:
        models = client.models.list()
        return [m.id for m in models.data]
    except Exception as e:
        console.print(f"[warning]Failed to fetch models: {e}[/warning]")
        return []


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
- **Allowed:** `git status`, `git diff`, `git log`, `git show`, `git blame`, `git branch`, `git add`, `git commit`, `git checkout -b`.
- **Blocked:** reset, push, rebase, cherry-pick, merge, `checkout <existing-branch>`, clean, and any other destructive operation.
- Write clear, conventional commit messages.
- **Branch workflow:** When starting new work, create a feature branch from the latest `main`: `git checkout -b feature/<name>`. Never push directly to `main`.

### GitHub CLI (gh)
- **Allowed (read-only):** `gh issue list/view/status`, `gh pr list/view/status/checks/diff`, `gh repo list/view`, `gh help`, `gh version`.
- **Blocked:** create, edit, close, reopen, delete, merge, checkout, api, and any command with `--body`, `--title`, or `-d`/`--delete`.
- Use `gh` to fetch GitHub issues, PRs, and repo info when the user asks about them.

## Technical limits
- All paths must stay within the working directory.
- If you need information, explore with bash — don't assume.
- If a command fails or an edit doesn't apply, read the error carefully, diagnose the issue, and retry with a corrected approach. Don't repeat the same failing command.

## Behavioral guidelines
- Only make the changes you were asked to make. Do not modify behaviour, expand permissions, or refactor anything beyond the scope of the request.
- Don't suggest or perform refactors, linting fixes, or style changes unless the user specifically asks.
- **Do not commit changes to git until the user explicitly asks you to.** Stage changes with `git add` and show a diff preview, but wait for confirmation before running `git commit`.
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


def make_session(client: OpenAI = None) -> PromptSession:
    """Create a PromptSession with multi-line support.

    Enter submits. Alt+Enter inserts a newline. Pasting multi-line text works
    automatically via bracketed paste. Typing / shows slash command completions.
    After '/model ' shows model name completions.
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
        completer=SlashCommandCompleter(client),
    )


def make_client() -> OpenAI:
    if not API_KEY:
        console.print(
            "[warning]Warning: No API key found. Set AGENC_API_KEY or OPENAI_API_KEY.[/warning]"
        )
    return OpenAI(base_url=DEFAULT_BASE_URL, api_key=API_KEY or "unused")


def chat_turn(client: OpenAI, messages: list, model: str) -> tuple[str, TokenUsage]:
    """Run one turn of the agentic loop: call the model, execute any tool calls, repeat.
    
    Returns: (reply_text, token_usage_for_this_turn)
    """
    turn_usage = TokenUsage()
    
    while True:
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
        )

        # Track token usage
        if response.usage:
            turn_usage.add(
                response.usage.prompt_tokens,
                response.usage.completion_tokens,
                response.usage.total_tokens
            )

        msg = response.choices[0].message

        # Append the assistant message to history
        messages.append(msg.model_dump(exclude_none=True))

        # If no tool calls, we're done — return the text
        if not msg.tool_calls:
            return msg.content or "", turn_usage

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
- `/model <name>` — switch model (tab-complete model names)
- `/models` — list available models
- `/run <command>` — run a shell command directly (unsandboxed)
- `/tokens` — show token usage for this session

### Key bindings
- **Enter** — send message
- **Alt+Enter** — insert newline
- Pasting multi-line text works without any special mode
"""
        )
    )


def print_tokens(usage: TokenUsage):
    """Display session token usage."""
    console.print(
        Markdown(
            f"""\
### Token Usage (session)
| Type | Count |
|------|-------|
| Prompt | {usage.prompt_tokens:,} |
| Completion | {usage.completion_tokens:,} |
| **Total** | **{usage.total_tokens:,}** |
"""
        )
    )


def main():
    global DEFAULT_MODEL

    print_welcome()
    client = make_client()

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    usage = TokenUsage()
    session = make_session(client)
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
                usage = TokenUsage()
                console.print("[info]Conversation cleared.[/info]")
                continue
            elif primary_cmd == "/model":
                if len(cmd_parts) > 1:
                    DEFAULT_MODEL = cmd_parts[1]
                    console.print(f"[info]Model set to {DEFAULT_MODEL}[/info]")
                else:
                    console.print(Markdown(f"**Current model:** `{DEFAULT_MODEL}`\n\nUsage: `/model <name>` — switch to a different model. Press Tab after `/model ` to browse available models."))
                continue
            elif primary_cmd == "/models":
                models = get_available_models(client)
                if models:
                    current = DEFAULT_MODEL
                    model_list = "\n".join(
                        f"  * {m}" if m == current else f"  {m}"
                        for m in sorted(models)
                    )
                    console.print(f"[info]Available models ({len(models)}):\n{model_list}[/info]")
                else:
                    console.print("[warning]No models available (check API connection)[/warning]")
                continue
            elif primary_cmd == "/run":
                if len(cmd_parts) > 1:
                    subprocess.run(cmd_parts[1], shell=True, cwd=str(CWD))
                else:
                    console.print("[warning]Usage: /run <command>[/warning]")
                continue
            elif primary_cmd == "/tokens":
                print_tokens(usage)
                continue
            else:
                console.print(f"[warning]Unknown command: {cmd}[/warning]")
                continue

        messages.append({"role": "user", "content": user_input})

        try:
            console.print()  # breathing room
            reply, turn_usage = chat_turn(client, messages, DEFAULT_MODEL)
            usage.add(
                turn_usage.prompt_tokens,
                turn_usage.completion_tokens,
                turn_usage.total_tokens
            )
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
