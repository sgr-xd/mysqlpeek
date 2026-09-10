"""Configuration loading: single-instance env, profiles file, and limit layering."""

from __future__ import annotations

import json
import os

import pytest

from mysqlpeek.config import AppConfig, ConfigError, Limits
from mysqlpeek.registry import InstanceRegistry, UnknownInstance

posix_only = pytest.mark.skipif(
    os.name == "nt", reason="POSIX mode bits are synthesized on Windows"
)

MY_ENV = (
    "MYSQL_HOST",
    "MYSQL_PORT",
    "MYSQL_USER",
    "MYSQL_PASSWORD",
    "MYSQL_PASSWORD_FILE",
    "MYSQL_DATABASE",
    "MYSQL_SSL",
    "MYSQLPEEK_PROFILES",
    "MYSQLPEEK_AUDIT_LOG",
    "MYSQLPEEK_MAX_LIMIT",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in MY_ENV:
        monkeypatch.delenv(name, raising=False)


class TestSingleInstanceFromEnv:
    def test_builds_one_instance_named_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MYSQL_HOST", "db.example")
        monkeypatch.setenv("MYSQL_DATABASE", "shop")
        config = AppConfig.from_env()
        assert list(config.instances) == ["default"]
        assert config.default_instance == "default"
        assert config.instances["default"].host == "db.example"
        assert config.instances["default"].database == "shop"
        assert config.instances["default"].port == 3306

    def test_database_is_optional(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Many read-only accounts have no default schema; that is not an error.
        monkeypatch.setenv("MYSQL_HOST", "db.example")
        assert AppConfig.from_env().instances["default"].database == ""

    def test_missing_host_is_an_actionable_error(self) -> None:
        with pytest.raises(ConfigError, match="MYSQLPEEK_PROFILES"):
            AppConfig.from_env()

    def test_password_file_wins_over_env(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        secret = tmp_path / "pw"
        secret.write_text("from-file\n")
        monkeypatch.setenv("MYSQL_HOST", "db.example")
        monkeypatch.setenv("MYSQL_PASSWORD", "from-env")
        monkeypatch.setenv("MYSQL_PASSWORD_FILE", str(secret))
        assert AppConfig.from_env().instances["default"].password == "from-file"

    def test_password_never_appears_in_repr(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MYSQL_HOST", "db.example")
        monkeypatch.setenv("MYSQL_PASSWORD", "hunter2")
        instance = AppConfig.from_env().instances["default"]
        assert "hunter2" not in repr(instance)
        assert "hunter2" not in json.dumps(instance.describe())


def write_profiles(tmp_path, payload: dict) -> str:
    path = tmp_path / "instances.json"
    path.write_text(json.dumps(payload))
    path.chmod(0o600)
    return str(path)


class TestProfilesFile:
    def test_loads_multiple_instances(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        path = write_profiles(
            tmp_path,
            {
                "default": "prod",
                "instances": {
                    "prod": {"host": "db-ro.example.com", "database": "shop"},
                    "dev": {"host": "db-staging.example.com", "database": "shop"},
                },
            },
        )
        monkeypatch.setenv("MYSQLPEEK_PROFILES", path)
        config = AppConfig.from_env()
        assert sorted(config.instances) == ["dev", "prod"]
        assert config.default_instance == "prod"

    def test_host_field_accepts_host_port_and_urls(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        path = write_profiles(
            tmp_path,
            {
                "instances": {
                    "a": {"host": "db.example:3307"},
                    "b": {"host": "mysqls://db.example.com"},
                    "c": {"host": "db.example", "port": 3308, "ssl": True},
                }
            },
        )
        monkeypatch.setenv("MYSQLPEEK_PROFILES", path)
        config = AppConfig.from_env()
        assert (config.instances["a"].host, config.instances["a"].port) == ("db.example", 3307)
        assert config.instances["b"].ssl is True
        assert config.instances["b"].port == 3306
        assert (config.instances["c"].port, config.instances["c"].ssl) == (3308, True)

    def test_default_falls_back_to_first_instance(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        path = write_profiles(tmp_path, {"instances": {"only": {"host": "h"}}})
        monkeypatch.setenv("MYSQLPEEK_PROFILES", path)
        assert AppConfig.from_env().default_instance == "only"

    def test_unknown_default_is_rejected(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        path = write_profiles(tmp_path, {"default": "nope", "instances": {"a": {"host": "h"}}})
        monkeypatch.setenv("MYSQLPEEK_PROFILES", path)
        with pytest.raises(ConfigError, match="not defined"):
            AppConfig.from_env()

    def test_instance_without_host_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        path = write_profiles(tmp_path, {"instances": {"a": {"database": "x"}}})
        monkeypatch.setenv("MYSQLPEEK_PROFILES", path)
        with pytest.raises(ConfigError, match="no host"):
            AppConfig.from_env()

    def test_malformed_json_is_reported_clearly(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        path = tmp_path / "bad.json"
        path.write_text("{not json")
        monkeypatch.setenv("MYSQLPEEK_PROFILES", str(path))
        with pytest.raises(ConfigError, match="not valid JSON"):
            AppConfig.from_env()

    def test_literal_password_is_rejected(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        path = write_profiles(tmp_path, {"instances": {"a": {"host": "h", "password": "hunter2"}}})
        monkeypatch.setenv("MYSQLPEEK_PROFILES", path)
        with pytest.raises(ConfigError, match="must be a reference"):
            AppConfig.from_env()

    def test_absent_password_is_allowed(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        path = write_profiles(tmp_path, {"instances": {"a": {"host": "h"}}})
        monkeypatch.setenv("MYSQLPEEK_PROFILES", path)
        assert AppConfig.from_env().instances["a"].password == ""

    @posix_only
    def test_group_writable_file_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        path = tmp_path / "instances.json"
        path.write_text(json.dumps({"instances": {"a": {"host": "h"}}}))
        path.chmod(0o664)
        monkeypatch.setenv("MYSQLPEEK_PROFILES", str(path))
        with pytest.raises(ConfigError, match="writable by other users"):
            AppConfig.from_env()


class TestLimitLayering:
    def test_file_wide_limits_apply_to_every_instance(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        path = write_profiles(
            tmp_path,
            {"limits": {"max_limit": 500}, "instances": {"a": {"host": "h"}, "b": {"host": "h2"}}},
        )
        monkeypatch.setenv("MYSQLPEEK_PROFILES", path)
        config = AppConfig.from_env()
        assert config.instances["a"].limits.max_limit == 500
        assert config.instances["b"].limits.max_limit == 500

    def test_instance_limits_override_file_wide(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        path = write_profiles(
            tmp_path,
            {
                "limits": {"max_limit": 500, "max_execution_time": 60},
                "instances": {
                    "prod": {"host": "h", "limits": {"max_limit": 50, "max_join_size": 1000}},
                    "dev": {"host": "h2"},
                },
            },
        )
        monkeypatch.setenv("MYSQLPEEK_PROFILES", path)
        config = AppConfig.from_env()
        assert config.instances["prod"].limits.max_limit == 50
        assert config.instances["prod"].limits.max_join_size == 1000
        assert config.instances["prod"].limits.max_execution_time == 60
        assert config.instances["dev"].limits.max_limit == 500

    def test_unknown_limit_name_is_rejected(self) -> None:
        with pytest.raises(ConfigError, match="unknown limit"):
            Limits().merged({"max_rows_to_read": 5}, "test")

    def test_non_integer_limit_is_rejected(self) -> None:
        with pytest.raises(ConfigError, match="must be an integer"):
            Limits().merged({"max_limit": "lots"}, "test")


class TestRegistry:
    @pytest.fixture
    def registry(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> InstanceRegistry:
        path = write_profiles(
            tmp_path,
            {"default": "prod", "instances": {"prod": {"host": "h1"}, "dev": {"host": "h2"}}},
        )
        monkeypatch.setenv("MYSQLPEEK_PROFILES", path)
        return InstanceRegistry(AppConfig.from_env())

    def test_names_and_default(self, registry: InstanceRegistry) -> None:
        assert registry.names == ["dev", "prod"]
        assert registry.default_name == "prod"

    def test_none_resolves_to_default(self, registry: InstanceRegistry) -> None:
        assert registry.get(None).name == "prod"

    def test_named_instance_resolves(self, registry: InstanceRegistry) -> None:
        assert registry.get("dev").name == "dev"

    def test_handle_is_cached(self, registry: InstanceRegistry) -> None:
        assert registry.get("dev") is registry.get("dev")

    def test_each_instance_gets_its_own_policy(self, registry: InstanceRegistry) -> None:
        assert registry.get("dev").policy is not registry.get("prod").policy

    def test_unknown_instance_names_the_valid_ones(self, registry: InstanceRegistry) -> None:
        with pytest.raises(UnknownInstance) as exc:
            registry.get("staging")
        assert "staging" in str(exc.value)
        assert "prod" in str(exc.value)

    def test_describe_all_hides_passwords_and_marks_default(
        self, registry: InstanceRegistry
    ) -> None:
        described = registry.describe_all()
        assert {d["instance"] for d in described} == {"dev", "prod"}
        assert [d["is_default"] for d in described if d["instance"] == "prod"] == [True]
        assert "password" not in json.dumps(described)

    def test_connections_are_lazy(self, registry: InstanceRegistry) -> None:
        # Nothing is connected until an instance is actually used, so an unreachable
        # box cannot stop the server from starting.
        assert all(not d["connected"] for d in registry.describe_all())
        registry.get("dev")
        connected = {d["instance"] for d in registry.describe_all() if d["connected"]}
        assert connected == {"dev"}


class TestEmptyEnvironmentValues:
    """A plugin host passes every declared option through, blank ones included."""

    def test_blank_user_and_port_fall_back_to_defaults(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MYSQL_HOST", "db.example")
        for name in ("MYSQL_USER", "MYSQL_DATABASE", "MYSQL_PORT"):
            monkeypatch.setenv(name, "")
        instance = AppConfig.from_env().instances["default"]
        assert instance.username == "root"
        assert instance.database == ""
        assert instance.port == 3306

    def test_whitespace_only_is_also_treated_as_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MYSQL_HOST", "db.example")
        monkeypatch.setenv("MYSQL_USER", "   ")
        assert AppConfig.from_env().instances["default"].username == "root"

    def test_blank_host_still_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MYSQL_HOST", "")
        with pytest.raises(ConfigError):
            AppConfig.from_env()


class TestServerAddressParsing:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("db.internal", ("db.internal", None, None)),
            ("db.internal:3307", ("db.internal", 3307, None)),
            ("10.0.0.1:3306", ("10.0.0.1", 3306, None)),
            ("mysql://db.example.com:3306", ("db.example.com", 3306, None)),
            ("mysqls://db.example.com", ("db.example.com", None, True)),
            ("mysql+ssl://db.example.com:3307", ("db.example.com", 3307, True)),
            ("  spaced.example  ", ("spaced.example", None, None)),
            ("", ("", None, None)),
        ],
    )
    def test_forms(self, value: str, expected: tuple) -> None:
        from mysqlpeek.config import parse_server

        assert parse_server(value) == expected

    def test_non_numeric_port_is_rejected(self) -> None:
        from mysqlpeek.config import parse_server

        with pytest.raises(ConfigError, match="non-numeric port"):
            parse_server("host:notaport")

    def test_unknown_scheme_is_rejected(self) -> None:
        from mysqlpeek.config import parse_server

        with pytest.raises(ConfigError, match="unsupported scheme"):
            parse_server("postgres://host")

    def test_https_style_tls_url_turns_on_ssl(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MYSQL_HOST", "mysqls://db.example.com")
        instance = AppConfig.from_env().instances["default"]
        assert instance.ssl is True
        assert instance.port == 3306

    def test_explicit_port_still_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MYSQL_HOST", "db.example:3307")
        monkeypatch.setenv("MYSQL_PORT", "3306")
        assert AppConfig.from_env().instances["default"].port == 3306


class TestCommandLine:
    def test_version_needs_no_configuration(self, capsys) -> None:
        from mysqlpeek.__main__ import main

        assert main(["--version"]) == 0
        assert capsys.readouterr().out.startswith("mysqlpeek ")

    def test_help_needs_no_configuration(self, capsys) -> None:
        from mysqlpeek.__main__ import main

        assert main(["--help"]) == 0
        assert "read-only MySQL / MariaDB MCP server" in capsys.readouterr().out

    def test_unrecognised_argument_fails_loudly(self, capsys) -> None:
        from mysqlpeek.__main__ import main

        assert main(["--nope"]) == 2
        assert "unrecognised argument" in capsys.readouterr().err

    def test_missing_configuration_still_reports_clearly(self, capsys) -> None:
        from mysqlpeek.__main__ import main

        assert main([]) == 2
        assert "configuration error" in capsys.readouterr().err

    def test_version_matches_the_packaged_metadata(self) -> None:
        from importlib.metadata import version as distribution_version

        from mysqlpeek.server import version

        assert version() == distribution_version("mysqlpeek")
