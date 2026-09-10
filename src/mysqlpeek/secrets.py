"""Secret references.

Any string in a profiles file may contain `${scheme:argument}`, resolved once at
startup. The point is that the file holds *references*, never values, so it is safe to
commit and safe to read over someone's shoulder.

Three schemes ship in core, all dependency-free:

    ${env:VAR}                  an environment variable
    ${env:VAR:-fallback}        ... with a default if unset
    ${file:/path/to/secret}     the contents of a file, trailing newline stripped
    ${cmd:some command}         stdout of a command

`cmd:` is deliberately the only integration point for a secret manager. Depending on a
cloud SDK would serve one vendor's users and strand the rest; shelling out to the tool
the operator already runs serves all of them:

    ${cmd:gcloud secrets versions access latest --secret=mysql-prod}
    ${cmd:aws secretsmanager get-secret-value --secret-id mysql-prod --query SecretString --output text}
    ${cmd:vault kv get -field=password secret/mysql-prod}
    ${cmd:op read op://vault/mysql-prod/password}

`register_scheme` exists so a native backend can be added as an optional extra without
the base package growing a dependency.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Callable

# ${scheme:argument} — scheme is a short lowercase word, argument runs to the closing
# brace. Nested braces are not supported and not needed.
_REFERENCE = re.compile(r"\$\{([a-z][a-z0-9_]*):([^}]*)\}")

_COMMAND_TIMEOUT_SECONDS = 15


class SecretError(RuntimeError):
    """A reference could not be resolved."""


Resolver = Callable[[str], str]
_SCHEMES: dict[str, Resolver] = {}


def register_scheme(name: str, resolver: Resolver) -> None:
    """Add a resolver, e.g. a native cloud secret-manager client from an extra."""
    _SCHEMES[name] = resolver


def has_reference(value: object) -> bool:
    return isinstance(value, str) and bool(_REFERENCE.search(value))


def resolve(value: str, where: str) -> str:
    """Expand every reference in `value`.

    `where` names the field being resolved so a failure says which cluster and key is
    at fault rather than just what went wrong.
    """

    def substitute(match: re.Match[str]) -> str:
        scheme, argument = match.group(1), match.group(2).strip()
        resolver = _SCHEMES.get(scheme)
        if resolver is None:
            raise SecretError(
                f"{where}: unknown reference scheme {scheme!r}. "
                f"Available: {', '.join(sorted(_SCHEMES))}"
            )
        try:
            return resolver(argument)
        except SecretError as exc:
            raise SecretError(f"{where}: {exc}") from None

    return _REFERENCE.sub(substitute, value)


# -- built-in schemes ------------------------------------------------------------


def _from_env(argument: str) -> str:
    # `VAR:-default` mirrors shell parameter expansion.
    name, _, default = argument.partition(":-")
    name = name.strip()
    value = os.environ.get(name)
    if value is None:
        if default:
            return default
        raise SecretError(f"environment variable {name} is not set")
    return value


def _from_file(argument: str) -> str:
    path = Path(argument).expanduser()
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SecretError(f"cannot read {path}: {exc}") from None


def _split_command(argument: str) -> list[str]:
    """Split a command string into argv, correctly on both POSIX and Windows.

    shlex's POSIX mode treats a backslash as an escape, which destroys every Windows
    path: `C:\\Users\\me\\get.ps1` comes back as `C:Usersmeget.ps1`. Non-POSIX mode
    keeps backslashes but leaves quotes attached to the tokens, so they are stripped
    here.
    """
    if os.name != "nt":
        return shlex.split(argument)
    tokens = shlex.split(argument, posix=False)
    return [_unquote(token) for token in tokens]


def _unquote(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
        return token[1:-1]
    return token


def _from_command(argument: str) -> str:
    """Run a command and take its stdout.

    Executed without a shell: the string is split with shlex and handed to the OS
    directly, so a value interpolated into the command cannot start a subshell or chain
    another command with `;`.
    """
    if not argument.strip():
        raise SecretError("empty command")
    try:
        argv = _split_command(argument)
    except ValueError as exc:
        raise SecretError(f"cannot parse command {argument!r}: {exc}") from None
    if not argv:
        raise SecretError("empty command")

    try:
        completed = subprocess.run(  # noqa: S603 - argv form, no shell
            argv,
            capture_output=True,
            text=True,
            timeout=_COMMAND_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError:
        raise SecretError(f"command not found: {argv[0]}") from None
    except subprocess.TimeoutExpired:
        raise SecretError(
            f"command timed out after {_COMMAND_TIMEOUT_SECONDS}s: {argv[0]}"
        ) from None

    if completed.returncode != 0:
        detail = (completed.stderr or "").strip().splitlines()
        tail = detail[-1] if detail else f"exit status {completed.returncode}"
        raise SecretError(f"command failed ({argv[0]}): {tail}")

    secret = completed.stdout.strip()
    if not secret:
        raise SecretError(f"command produced no output: {argv[0]}")
    return secret


register_scheme("env", _from_env)
register_scheme("file", _from_file)
register_scheme("cmd", _from_command)
