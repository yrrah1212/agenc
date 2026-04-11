"""
agenc — configuration and security policies.

This module defines environment variables, allow-lists, and block-lists
for the sandbox and git validation.
"""

import os
import re
from pathlib import Path

# ---------------------------------------------------------------------------
# Environment variables
# ---------------------------------------------------------------------------

DEFAULT_MODEL = os.environ.get("AGENC_MODEL", "gpt-4o")
DEFAULT_BASE_URL = os.environ.get("AGENC_BASE_URL", "https://api.openai.com/v1")
API_KEY = os.environ.get("AGENC_API_KEY", os.environ.get("OPENAI_API_KEY", ""))
AUTO_WRITE = os.environ.get("AGENC_AUTO_WRITE", "").lower() in ("1", "true", "yes")
CWD = Path.cwd().resolve()

# ---------------------------------------------------------------------------
# Shell command allow-list
# ---------------------------------------------------------------------------

ALLOWED_COMMANDS = frozenset(
    {
        "ls",
        "cat",
        "head",
        "tail",
        "grep",
        "egrep",
        "find",
        "wc",
        "file",
        "tree",
        "diff",
        "stat",
        "du",
        "sort",
        "uniq",
        "awk",
        "sed",  # sed without -i is read-only
        "cut",
        "tr",
        "basename",
        "dirname",
        "realpath",
        "echo",
        "pwd",
        "bat",       # pretty cat
        "rg",        # ripgrep
        "fd",        # fd-find
        "tokei",     # code stats
        "cloc",      # code stats
        "hexdump",
        "xxd",
        "md5sum",
        "sha256sum",
    }
)

# Patterns that should never appear in a command — even piped.
# Uses word boundaries (\b) to avoid matching inside longer words (e.g. "stash" matching "sh").
BLOCKED_PATTERNS = re.compile(
    r"""
    ( \brm\s | \brmdir\b | \bmv\s | \bcp\s | \bchmod\b | \bchown\b | \bchgrp\b | \bmkfs\b | \bdd\s
    | >\s | >>  | \btee\s
    | \bcurl\b | \bwget\b | \bnc\s | \bncat\b | \bsocat\b
    | \bpython\b | \bpython3\b | \bperl\b | \bruby\b | \bnode\b | \bbash\b | \bsh\b | \bzsh\b
    | \bsudo\b | \bsu\s
    | \bkill\b | \bpkill\b | \breboot\b | \bshutdown\b
    | \bapt\b | \byum\b | \bdnf\b | \bpacman\b | \bbrew\b
    | \bdocker\b | \bkubectl\b
    | \bssh\b | \bscp\b | \brsync\b
    | \beval\b | \bexec\b
    | \bsed\s+-i
    | \bxargs\b
    | \$\(
    | `
    )
    """,
    re.VERBOSE,
)

# ---------------------------------------------------------------------------
# Git subcommand allow-list
# ---------------------------------------------------------------------------

ALLOWED_GIT_SUBCOMMANDS = frozenset(
    {
        # Reading state
        "status",
        "diff",
        "log",
        "show",
        "blame",
        "shortlog",
        "describe",
        "branch",       # listing branches (--delete is blocked below)
        "tag",          # listing tags (--delete is blocked below)
        "stash",        # stash list only (other stash ops blocked below)
        "ls-files",
        "ls-tree",
        "rev-parse",
        "rev-list",
        "cat-file",
        "name-rev",
        "reflog",
        # Making commits
        "add",
        "commit",
        # Creating feature branches
        "checkout",   # only with -b flag (validated in git.py)
    }
)

# Git flags/arguments that should never appear — even on allowed subcommands.
BLOCKED_GIT_PATTERNS = re.compile(
    r"""
    ( --force
    | --hard
    | --delete
    | -[dD]\b          # branch -d / -D
    | --mirror
    | --bare
    | --no-verify      # skip commit hooks — suspicious
    )
    """,
    re.VERBOSE,
)

# ---------------------------------------------------------------------------
# GitHub CLI (gh) subcommand allow-list
# ---------------------------------------------------------------------------

# Safe gh <topic> <action> combinations (read-only).
ALLOWED_GH_ACTIONS = frozenset(
    {
        # Help/version (topic-only or with subcommands)
        ("help",),
        ("version",),
        # Repo: listing/viewing
        ("repo", "view"),
        ("repo", "list"),
        # Issues: read-only operations
        ("issue", "list"),
        ("issue", "view"),
        ("issue", "status"),
        # PRs: read-only operations
        ("pr", "list"),
        ("pr", "view"),
        ("pr", "status"),
        ("pr", "checks"),
        ("pr", "diff"),
    }
)

# gh flags/arguments that should never appear.
BLOCKED_GH_PATTERNS = re.compile(
    r"""
    ( \bcreate\b
    | \bedit\b
    | \bclose\b
    | \breopen\b
    | \bdelete\b
    | \bmerge\b
    | \bcheckout\b
    | \bconvert\b
    | \bsync\b
    | \btransfer\b
    | \barchive\b
    | \bunarchive\b
    | --body
    | --title
    | -d\b          # delete flag
    | --delete
    )
    """,
    re.VERBOSE,
)

# Output compression threshold
MAX_OUTPUT_BYTES = 100_000  # truncate huge command outputs

# Commands whose output IS the payload — never compress on success.
# Covers the first command in a pipeline (e.g. "cat foo | head" → "cat").
CONTENT_COMMANDS = frozenset(
    {
        "cat",
        "head",
        "tail",
        "grep",
        "egrep",
        "rg",
        "awk",
        "sed",
        "diff",
        "hexdump",
        "xxd",
        "bat",
        "gh",
    }
)

# Git subcommands whose output is content, not progress noise.
CONTENT_GIT_SUBCOMMANDS = frozenset(
    {
        "diff",
        "show",
        "log",
        "blame",
        "shortlog",
        "cat-file",
        "ls-files",
        "ls-tree",
    }
)
