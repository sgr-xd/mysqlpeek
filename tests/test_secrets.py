"""Secret reference resolution.

These tests are the contract that makes a profiles file safe to commit: a reference
resolves, a literal is never treated as one, and a failure says which field broke.
"""

from __future__ import annotations

import os

import pytest

from mysqlpeek.secrets import SecretError, has_reference, register_scheme, resolve

posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX shell semantics")


class TestDetection:
    @pytest.mark.parametrize(
        "value",
        ["${env:VAR}", "prefix-${env:VAR}", "${file:/tmp/x}", "${cmd:echo hi}"],
    )
    def test_recognises_references(self, value: str) -> None:
        assert has_reference(value)

    @pytest.mark.parametrize(
        "value",
        [
            "plain-value",
            "hunter2",
            # Near-misses must not count. The risk runs the other way too: if one of
            # these were treated as a reference, a literal password could masquerade
            # as one and slip past the no-literals rule.
            "$env:VAR",
            "{env:VAR}",
            "${ENV:VAR}",
            "${notascheme}",
            "env:VAR",
            "",
        ],
    )
    def test_rejects_non_references(self, value: str) -> None:
        assert not has_reference(value)

    def test_non_strings_are_not_references(self) -> None:
        assert not has_reference(None)
        assert not has_reference(1234)


class TestEnvScheme:
    def test_resolves(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CS_TEST_VAR", "value")
        assert resolve("${env:CS_TEST_VAR}", "t") == "value"

    def test_missing_variable_names_the_field(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CS_ABSENT", raising=False)
        with pytest.raises(SecretError) as exc:
            resolve("${env:CS_ABSENT}", "cluster prod.password")
        assert "cluster prod.password" in str(exc.value)
        assert "CS_ABSENT" in str(exc.value)

    def test_default_is_used_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CS_ABSENT", raising=False)
        assert resolve("${env:CS_ABSENT:-fallback}", "t") == "fallback"

    def test_default_is_ignored_when_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CS_TEST_VAR", "real")
        assert resolve("${env:CS_TEST_VAR:-fallback}", "t") == "real"

    def test_empty_value_is_preserved(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # An explicitly empty password is different from an unset one.
        monkeypatch.setenv("CS_EMPTY", "")
        assert resolve("${env:CS_EMPTY}", "t") == ""


class TestFileScheme:
    def test_reads_and_strips(self, tmp_path) -> None:
        secret = tmp_path / "s"
        secret.write_text("  s3cret\n")
        assert resolve(f"${{file:{secret}}}", "t") == "s3cret"

    def test_missing_file_is_reported(self, tmp_path) -> None:
        with pytest.raises(SecretError, match="cannot read"):
            resolve(f"${{file:{tmp_path / 'nope'}}}", "t")


class TestCmdScheme:
    def test_takes_stdout(self) -> None:
        assert resolve("${cmd:printf s3cret}", "t") == "s3cret"

    @posix_only
    def test_runs_without_a_shell(self) -> None:
        """Shell metacharacters are arguments, not syntax.

        If this ran through a shell, the `;` would start a second command. Instead the
        whole thing is argv to `printf`, so nothing is chained.
        """
        result = resolve("${cmd:printf a;whoami}", "t")
        assert "a;whoami" in result or result == "a;whoami"

    def test_missing_binary_is_reported(self) -> None:
        with pytest.raises(SecretError, match="command not found"):
            resolve("${cmd:mysqlpeek-no-such-binary}", "t")

    @posix_only
    def test_failing_command_surfaces_stderr(self) -> None:
        with pytest.raises(SecretError, match="command failed"):
            resolve("${cmd:sh -c 'echo boom >&2; exit 1'}", "t")

    @posix_only
    def test_empty_output_is_an_error(self) -> None:
        # Silently accepting an empty secret would produce a confusing auth failure
        # much later.
        with pytest.raises(SecretError, match="no output"):
            resolve("${cmd:true}", "t")

    def test_empty_command_is_an_error(self) -> None:
        with pytest.raises(SecretError, match="empty command"):
            resolve("${cmd:}", "t")


class TestComposition:
    def test_interpolates_within_a_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CS_TIER", "prod")
        assert resolve("ch-${env:CS_TIER}.internal", "t") == "ch-prod.internal"

    def test_multiple_references_in_one_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CS_A", "one")
        monkeypatch.setenv("CS_B", "two")
        assert resolve("${env:CS_A}-${env:CS_B}", "t") == "one-two"

    def test_literal_passes_through_untouched(self) -> None:
        assert resolve("just a string", "t") == "just a string"

    def test_unknown_scheme_lists_the_valid_ones(self) -> None:
        with pytest.raises(SecretError) as exc:
            resolve("${vault:secret/x}", "t")
        assert "unknown reference scheme" in str(exc.value)
        assert "env" in str(exc.value)

    def test_a_resolved_value_is_not_re_expanded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A secret that happens to contain ${...} is data, not a reference.

        Re-expanding would let whoever controls a secret's contents read other secrets.
        """
        monkeypatch.setenv("CS_TRICKY", "${env:CS_OTHER}")
        monkeypatch.setenv("CS_OTHER", "should-not-appear")
        assert resolve("${env:CS_TRICKY}", "t") == "${env:CS_OTHER}"


class TestExtensibility:
    def test_a_scheme_can_be_registered(self) -> None:
        register_scheme("cstest", lambda arg: f"resolved:{arg}")
        assert resolve("${cstest:thing}", "t") == "resolved:thing"


class TestCommandSplitting:
    """argv splitting must survive Windows paths.

    shlex's POSIX mode treats backslash as an escape, so a Windows path would arrive
    at the OS with its separators stripped. These run on every platform because the
    splitter's behaviour is selected by os.name, not by the host.
    """

    def test_windows_path_keeps_its_backslashes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from mysqlpeek import secrets

        monkeypatch.setattr(secrets.os, "name", "nt")
        argv = secrets._split_command(r"C:\Users\me\get.ps1 --secret ch-prod")
        assert argv[0] == r"C:\Users\me\get.ps1"
        assert argv[1:] == ["--secret", "ch-prod"]

    def test_windows_quoted_argument_loses_its_quotes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from mysqlpeek import secrets

        monkeypatch.setattr(secrets.os, "name", "nt")
        argv = secrets._split_command(r'powershell -Command "Get-Secret ch prod"')
        assert argv == ["powershell", "-Command", "Get-Secret ch prod"]

    def test_posix_splitting_is_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from mysqlpeek import secrets

        monkeypatch.setattr(secrets.os, "name", "posix")
        assert secrets._split_command("vault kv get -field=password secret/x") == [
            "vault",
            "kv",
            "get",
            "-field=password",
            "secret/x",
        ]
