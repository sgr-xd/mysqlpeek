"""Configuration, resolved once at startup.

Two shapes are supported and they produce the same object graph:

* **Single instance from the environment** — `MYSQL_HOST` and friends. The instance
  is named `default`.
* **Several instances from a profiles file** — `MYSQLPEEK_PROFILES=/path/to.json`.

Credentials never arrive as command-line arguments: an argument is visible in shell
history and in `ps` output to every other user on the box.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from .secrets import SecretError, has_reference
from .secrets import resolve as resolve_reference

DEFAULT_PORT = 3306


class ConfigError(RuntimeError):
    """Raised when the environment does not describe a usable connection."""


def parse_server(value: str) -> tuple[str, int | None, bool | None]:
    """Split a server address into host, port and TLS flag.

    One field instead of three. People copy connection details around as
    `host:port` or as a URL, so accepting those forms removes two questions from
    the setup dialog and two chances to get it wrong:

        db.internal                     -> host only
        db.internal:3307                -> host + port
        mysql://db.internal:3306        -> host + port, TLS unspecified
        mysqls://db.example.com         -> host, TLS on
        mysql+ssl://db.example.com      -> the same

    Returns None for port or TLS where the input did not say, so an explicit
    setting can still take precedence.
    """
    text = (value or "").strip()
    if not text:
        return "", None, None

    if "://" in text:
        parts = urlsplit(text)
        if not parts.hostname:
            raise ConfigError(f"cannot read a hostname from {value!r}")
        scheme = parts.scheme.lower()
        if scheme in ("mysqls", "mysql+ssl", "mariadbs", "mariadb+ssl"):
            secure: bool | None = True
        elif scheme in ("mysql", "mariadb"):
            secure = None
        else:
            raise ConfigError(
                f"{value!r}: unsupported scheme {parts.scheme!r}; use mysql:// or mysqls://"
            )
        try:
            port = parts.port
        except ValueError:
            raise ConfigError(f"{value!r} has an invalid port") from None
        return parts.hostname, port, secure

    # Bare host, optionally with a port. Bracketed IPv6 goes through urlsplit
    # above; a bare IPv6 literal has too many colons to disambiguate here.
    if text.count(":") == 1:
        host, _, port_text = text.partition(":")
        try:
            return host.strip(), int(port_text), None
        except ValueError:
            raise ConfigError(f"{value!r} has a non-numeric port") from None

    return text, None, None


def _env_str(name: str, default: str) -> str:
    """An environment variable, treating empty as absent.

    A plugin host passes every declared setting through, so an option the user left
    blank arrives as "" rather than being missing. Taking that literally would connect
    as user "" and fail authentication with an error that points nowhere useful.
    """
    value = os.environ.get(name, "").strip()
    return value or default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Limits:
    """Server-side caps applied to every query mysqlpeek executes.

    Each one is set as a session variable on the connection, so it binds inside the
    server. Even a query that slips past our own parser is still bounded by these.

        max_execution_time -> max_execution_time (MySQL) / max_statement_time (MariaDB)
        max_result_rows    -> sql_select_limit
        max_join_size      -> max_join_size with sql_big_selects=0: the optimiser refuses
                              a statement it expects to examine more rows than this,
                              before reading any of them
    """

    max_execution_time: int = 30
    max_result_rows: int = 10_000
    max_join_size: int = 100_000_000

    # LIMIT handling. default_limit is appended when a query has none; max_limit clamps
    # anything larger. Kept separate from max_result_rows so the SQL stays readable.
    default_limit: int = 100
    max_limit: int = 10_000

    @classmethod
    def from_env(cls) -> "Limits":
        return cls(
            max_execution_time=_env_int("MYSQLPEEK_MAX_EXECUTION_TIME", 30),
            max_result_rows=_env_int("MYSQLPEEK_MAX_RESULT_ROWS", 10_000),
            max_join_size=_env_int("MYSQLPEEK_MAX_JOIN_SIZE", 100_000_000),
            default_limit=_env_int("MYSQLPEEK_DEFAULT_LIMIT", 100),
            max_limit=_env_int("MYSQLPEEK_MAX_LIMIT", 10_000),
        )

    def merged(self, overrides: Mapping[str, Any] | None, where: str) -> "Limits":
        """Return a copy with `overrides` applied.

        Layering is env defaults → file-wide `limits` → per-instance `limits`, so a
        production profile can be stricter than the box-wide default without repeating
        every field.
        """
        if not overrides:
            return self
        known = {f.name for f in fields(self)}
        unknown = sorted(set(overrides) - known)
        if unknown:
            raise ConfigError(
                f"{where}: unknown limit(s) {', '.join(unknown)}. "
                f"Valid limits are: {', '.join(sorted(known))}"
            )
        coerced: dict[str, int] = {}
        for key, value in overrides.items():
            try:
                coerced[key] = int(value)
            except (TypeError, ValueError) as exc:
                raise ConfigError(f"{where}: limit {key} must be an integer") from exc
        return replace(self, **coerced)

    def describe(self) -> dict[str, int]:
        return {
            "max_execution_time_s": self.max_execution_time,
            "max_result_rows": self.max_result_rows,
            "max_join_size": self.max_join_size,
            "default_limit": self.default_limit,
            "max_limit": self.max_limit,
        }


@dataclass(frozen=True)
class InstanceConfig:
    """Everything needed to reach one MySQL or MariaDB server."""

    name: str
    host: str
    port: int
    username: str
    database: str
    ssl: bool
    ssl_verify: bool
    ssl_ca: str
    connect_timeout: int
    limits: Limits
    description: str = ""
    # repr=False so the password cannot leak into a traceback or a log line that
    # happens to format the config object.
    password: str = field(default="", repr=False)

    def describe(self) -> dict[str, object]:
        """Connection facts safe to show a caller. Never includes the password."""
        out: dict[str, object] = {
            "instance": self.name,
            "host": self.host,
            "port": self.port,
            "username": self.username,
            "database": self.database or None,
            "ssl": self.ssl,
            "read_only": True,
            "limits": self.limits.describe(),
        }
        if self.description:
            out["description"] = self.description
        return out


@dataclass(frozen=True)
class AppConfig:
    instances: dict[str, InstanceConfig]
    default_instance: str
    audit_path: Path | None

    @classmethod
    def from_env(cls) -> "AppConfig":
        profiles_raw = os.environ.get("MYSQLPEEK_PROFILES", "").strip()
        audit_raw = os.environ.get("MYSQLPEEK_AUDIT_LOG", "").strip()
        audit_path = Path(audit_raw).expanduser() if audit_raw else None

        if profiles_raw:
            return cls._from_profiles(Path(profiles_raw).expanduser(), audit_path)
        return cls._from_single_env(audit_path)

    # -- single instance from the environment ------------------------------------

    @classmethod
    def _from_single_env(cls, audit_path: Path | None) -> "AppConfig":
        host = os.environ.get("MYSQL_HOST", "").strip()
        if not host:
            raise ConfigError(
                "No instance configured. Either set MYSQL_HOST (plus MYSQL_USER and "
                "MYSQL_PASSWORD_FILE or MYSQL_PASSWORD), or point MYSQLPEEK_PROFILES at "
                "a JSON profiles file."
            )

        host, parsed_port, parsed_ssl = parse_server(host)
        if not host:
            raise ConfigError("MYSQL_HOST does not contain a hostname")

        # Explicit settings win; otherwise fall back to whatever the address implied,
        # and only then to the protocol defaults.
        ssl = _env_bool("MYSQL_SSL", parsed_ssl if parsed_ssl is not None else False)
        default_port = parsed_port if parsed_port is not None else DEFAULT_PORT
        instance = InstanceConfig(
            name="default",
            host=host,
            port=_env_int("MYSQL_PORT", default_port),
            username=_env_str("MYSQL_USER", "root"),
            password=_password_from_env(),
            database=_env_str("MYSQL_DATABASE", ""),
            ssl=ssl,
            ssl_verify=_env_bool("MYSQL_SSL_VERIFY", True),
            ssl_ca=_env_str("MYSQL_SSL_CA", ""),
            connect_timeout=_env_int("MYSQL_CONNECT_TIMEOUT", 10),
            limits=Limits.from_env(),
        )
        return cls({"default": instance}, "default", audit_path)

    # -- several instances from a profiles file -----------------------------------

    @classmethod
    def _from_profiles(cls, path: Path, audit_path: Path | None) -> "AppConfig":
        _refuse_if_writable_by_others(path)

        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ConfigError(f"cannot read MYSQLPEEK_PROFILES at {path}: {exc}") from None
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{path} is not valid JSON: {exc}") from None

        if not isinstance(raw, dict):
            raise ConfigError(f"{path}: top level must be an object")

        entries = raw.get("instances")
        if not isinstance(entries, dict) or not entries:
            raise ConfigError(f"{path}: expected a non-empty 'instances' object")

        base_limits = Limits.from_env().merged(raw.get("limits"), f"{path}: limits")

        instances: dict[str, InstanceConfig] = {}
        for name, entry in entries.items():
            if not isinstance(entry, dict):
                raise ConfigError(f"{path}: instance {name!r} must be an object")
            instances[name] = _instance_from_profile(name, entry, base_limits, path)

        default = raw.get("default") or next(iter(instances))
        if default not in instances:
            raise ConfigError(
                f"{path}: default instance {default!r} is not defined. "
                f"Known instances: {', '.join(sorted(instances))}"
            )
        return cls(instances, default, audit_path)


def _instance_from_profile(
    name: str,
    entry: dict[str, Any],
    base_limits: Limits,
    path: Path,
) -> InstanceConfig:
    where = f"{path}: instance {name}"

    def text(key: str, default: str = "") -> str:
        """A string field, with any ${scheme:arg} references expanded."""
        raw = entry.get(key, default)
        if raw is None:
            return default
        try:
            return resolve_reference(str(raw), f"{where}.{key}").strip()
        except SecretError as exc:
            raise ConfigError(str(exc)) from None

    def number(key: str, default: int) -> int:
        raw = entry.get(key, default)
        try:
            return int(resolve_reference(str(raw), f"{where}.{key}"))
        except SecretError as exc:
            raise ConfigError(str(exc)) from None
        except ValueError:
            raise ConfigError(f"{where}.{key} must be an integer, got {raw!r}") from None

    host_raw = text("host")
    if not host_raw:
        raise ConfigError(f"{where} has no host")
    host, parsed_port, parsed_ssl = parse_server(host_raw)

    ssl = bool(entry.get("ssl", parsed_ssl if parsed_ssl is not None else False))
    return InstanceConfig(
        name=name,
        host=host,
        port=number("port", parsed_port if parsed_port is not None else DEFAULT_PORT),
        username=text("user") or text("username") or "root",
        password=_password_from_profile(name, entry, where),
        database=text("database"),
        ssl=ssl,
        ssl_verify=bool(entry.get("ssl_verify", True)),
        ssl_ca=text("ssl_ca"),
        connect_timeout=number("connect_timeout", 10),
        limits=base_limits.merged(entry.get("limits"), f"{where} limits"),
        description=str(entry.get("description", "")),
    )


def _password_from_env() -> str:
    """File first, then env var.

    A file is preferable: it can be mode 0600, it does not appear in `docker inspect`
    or a process environment dump, and it is what Kubernetes and Docker secrets mount.
    """
    path_raw = os.environ.get("MYSQL_PASSWORD_FILE", "").strip()
    if path_raw:
        return _read_password_file(Path(path_raw).expanduser())
    # An empty password is legitimate on a dev server, so absence of the variable and
    # an explicitly empty value are treated the same.
    return os.environ.get("MYSQL_PASSWORD", "")


def _password_from_profile(name: str, entry: dict[str, Any], where: str) -> str:
    """Resolve an instance password, which must be a reference and never a literal.

    Rejecting inline values is what makes a profiles file safe to commit: the file can
    then only ever say *where* the secret lives. A warning would not achieve that —
    people commit past warnings.
    """
    raw = entry.get("password")
    if raw is None:
        # No password at all is legitimate: a local dev server, or a user whose
        # credentials come from elsewhere entirely.
        return ""

    if not has_reference(raw):
        raise ConfigError(
            f"{where}.password must be a reference, not a literal value. "
            "Use ${env:VAR}, ${file:/path/to/secret} or ${cmd:...} so the profiles "
            "file holds no secrets. For example:\n"
            '  "password": "${env:PROD_MYSQL_PASSWORD}"\n'
            '  "password": "${file:~/.config/mysqlpeek/prod.pw}"\n'
            '  "password": "${cmd:vault kv get -field=password secret/mysql-prod}"'
        )

    try:
        return resolve_reference(str(raw), f"{where}.password")
    except SecretError as exc:
        raise ConfigError(str(exc)) from None


def _read_password_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ConfigError(f"cannot read password file {path}: {exc}") from None


def _refuse_if_writable_by_others(path: Path) -> None:
    """Refuse a profiles file that anyone but the owner can write.

    A ${cmd:...} reference means the file decides what gets executed, so its integrity
    matters more than its confidentiality. SSH refuses to use a private key on the same
    grounds, and for the same reason.
    """
    if os.name == "nt":
        # Windows synthesizes st_mode as 0o666 for any writable file, so the POSIX
        # group/other bits carry no access-control meaning there — testing them would
        # refuse every file. Doing this properly needs ACL inspection and a pywin32
        # dependency, which is not worth it for a guard that is defence in depth.
        return
    try:
        mode = path.stat().st_mode
    except OSError as exc:
        raise ConfigError(f"cannot read MYSQLPEEK_PROFILES at {path}: {exc}") from None
    if mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ConfigError(
            f"{path} is writable by other users, so it cannot be trusted to name "
            f"commands or secret locations. Run: chmod go-w {path}"
        )
