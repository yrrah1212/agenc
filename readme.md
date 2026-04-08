# agenc

A code review agent that runs in your terminal and connects to any OpenAI-compatible endpoint. It explores your repo with sandboxed shell commands, reviews your code, and can stage and commit changes.

## Quick start

```bash
# Install dependencies
pip install openai rich

# Configure your endpoint (pick one)
export AGENC_API_KEY="sk-..."
export AGENC_BASE_URL="https://api.openai.com/v1"   # default
export AGENC_MODEL="gpt-4o"                          # default

# Or use a local model
export AGENC_BASE_URL="http://localhost:11434/v1"    # Ollama
export AGENC_MODEL="qwen2.5-coder:32b"
export AGENC_API_KEY="unused"

# Or use OpenRouter
export AGENC_BASE_URL="https://openrouter.ai/api/v1"
export AGENC_API_KEY="sk-or-..."
export AGENC_MODEL="anthropic/claude-sonnet-4"

# Run from your repo root
cd /path/to/your/repo
python /path/to/agenc/agent.py
```

## Features

- **Interactive REPL** — conversational code review in your terminal
- **Read-only sandbox** — `ls`, `cat`, `grep`, `find`, `rg`, etc. for exploring code
- **Git integration** — read repo state (`status`, `diff`, `log`, `blame`, ...) and make commits (`add`, `commit`)
- **Smart output compression** — successful commands are summarized to save context; failures preserve full detail
- **Path jailing** — all file access is restricted to the current working directory
- **Any OpenAI-compatible endpoint** — works with OpenAI, Ollama, OpenRouter, llama.cpp, vLLM, etc.
- **Rich terminal output** — markdown rendering, syntax highlighting

## REPL commands

| Command        | Description                    |
|----------------|--------------------------------|
| `/help`        | Show available commands        |
| `/quit`        | Exit the agent                 |
| `/clear`       | Clear conversation history     |
| `/model <n>`   | Switch model mid-session       |
| `/cwd`         | Print working directory        |

## Git support

The agent can interact with git at the subcommand level:

**Allowed (read):** `status`, `diff`, `log`, `show`, `blame`, `shortlog`, `describe`, `branch`, `tag`, `stash list`, `stash show`, `ls-files`, `ls-tree`, `rev-parse`, `rev-list`, `cat-file`, `name-rev`, `reflog`

**Allowed (write):** `add`, `commit`

**Blocked:** `push`, `pull`, `fetch`, `reset`, `rebase`, `cherry-pick`, `merge`, `checkout`, `switch`, `clean`, `rm`, `restore`, and any other subcommand not in the allow-list. The flags `--force`, `--hard`, `--delete`, `-d`/`-D`, `--mirror`, `--bare`, and `--no-verify` are also blocked globally.

## Output compression

Command output is automatically compressed to keep the model's context window clean:

| Scenario                  | Behavior                                           |
|---------------------------|----------------------------------------------------|
| Success, ≤60 lines        | Passed through unchanged                           |
| Success, 61–200 lines     | First 10 + last 20 lines, with omission note       |
| Success, >200 lines       | Last 30 lines + total count                        |
| Failure, ≤120 lines       | Passed through unchanged (full context for debug)  |
| Failure, >120 lines       | Last 80 lines + total count                        |

Stderr is always preserved in full on failure. A byte-level safety net truncates at 100KB regardless.

## Security model

The agent runs shell commands in a two-layer sandbox:

**Layer 1 — Command validation:**
- Read-only utilities are allow-listed: `ls`, `cat`, `grep`, `head`, `tail`, `find`, `tree`, `wc`, `rg`, `fd`, `diff`, `awk`, `sed`, `sort`, `uniq`, `cut`, `tr`, etc.
- `git` is validated at the subcommand level (see above).
- `sed -i` (in-place edit) is explicitly blocked.
- Dangerous patterns are rejected with word-boundary matching: `rm`, `mv`, `cp`, `curl`, `python`, `sudo`, `bash`, `$(...)`, backticks, output redirection (`>`, `>>`), etc.

**Layer 2 — Path jailing:**
- All path arguments are resolved and must stay within `$CWD`.
- Commands time out after 30 seconds.
- Output is capped at 100KB.

## Architecture

```
agent.py (single file)
├── Configuration         — env vars, allow/block lists
├── Git validation        — subcommand allow-list + blocked flags
├── Sandbox               — validate_command(), run_shell()
├── Output compression    — compress_output() based on exit code
├── Tool definitions      — OpenAI function-calling schema
├── System prompt         — review-focused instructions
└── REPL loop             — input → model → tool calls → display
```

The agent uses a simple agentic loop: it sends the conversation to the model, and if the model responds with tool calls, it executes them and feeds results back until the model produces a final text response.
