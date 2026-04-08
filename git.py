"""
agenc — git subcommand validation.

This module validates git commands against an allow-list and blocks
dangerous flags.
"""

from typing import Optional

from config import ALLOWED_GIT_SUBCOMMANDS, BLOCKED_GIT_PATTERNS


def validate_git_command(parts: list[str]) -> Optional[str]:
    """Validate a git command.  `parts` is the shlex-split command starting with 'git'.
    
    Returns an error message if blocked, else None.
    """
    # Strip global git flags to find the subcommand (e.g. git -C /foo status)
    args = parts[1:]
    subcmd = None
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("-"):
            # Skip flags that take a value: -C, -c, --git-dir, --work-tree
            if a in ("-C", "-c", "--git-dir", "--work-tree", "--namespace"):
                i += 2  # skip flag + its value
            else:
                i += 1
        else:
            subcmd = a
            break

    if subcmd is None:
        return "Blocked: git command with no subcommand."

    if subcmd not in ALLOWED_GIT_SUBCOMMANDS:
        return f"Blocked: git subcommand '{subcmd}' is not in the allow-list. Allowed: {', '.join(sorted(ALLOWED_GIT_SUBCOMMANDS))}"

    # Check for dangerous flags on the full argument string
    full_args = " ".join(parts[1:])
    if BLOCKED_GIT_PATTERNS.search(full_args):
        return "Blocked: git command contains a forbidden flag."

    # Special case: 'stash' is only allowed for listing/showing
    if subcmd == "stash":
        stash_action = args[i + 1] if i + 1 < len(args) else "list"
        if stash_action not in ("list", "show"):
            return f"Blocked: 'git stash {stash_action}' is not allowed. Only 'git stash list' and 'git stash show'."

    return None
