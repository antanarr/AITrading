import subprocess

import pytest

from copybot import credentials


@pytest.fixture
def helper(monkeypatch, tmp_path):
    binary = tmp_path / "test-helper"
    binary.touch()
    monkeypatch.setattr(credentials, "HELPER", binary)
    monkeypatch.setattr(credentials.sys, "platform", "darwin")
    for name in credentials.NAMES:
        monkeypatch.delenv(name, raising=False)
    return binary


def test_loads_only_requested_keys_without_printing_or_putting_values_in_arguments(
    helper, monkeypatch, capsys
):
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        assert kwargs["capture_output"] is True
        return subprocess.CompletedProcess(args, 0, b"fictional-sensitive-value", b"")

    monkeypatch.setattr(credentials.subprocess, "run", run)
    credentials.load_credentials(["BIRDEYE_API_KEY"])
    assert credentials.os.environ["BIRDEYE_API_KEY"] == "fictional-sensitive-value"
    assert calls == [[str(helper), "--read", "BIRDEYE_API_KEY"]]
    assert capsys.readouterr() == ("", "")


def test_explicit_environment_takes_precedence(helper, monkeypatch):
    monkeypatch.setenv("BIRDEYE_API_KEY", "explicit-value")

    def unexpected(*args):
        pytest.fail("No Keychain access expected for an explicit environment value")

    monkeypatch.setattr(credentials, "_invoke", unexpected)
    credentials.load_credentials(["BIRDEYE_API_KEY"])
    assert credentials.os.environ["BIRDEYE_API_KEY"] == "explicit-value"


def test_missing_item_is_not_a_secret_or_success(helper, monkeypatch):
    monkeypatch.setattr(
        credentials, "_invoke", lambda _: subprocess.CompletedProcess([], 44, b"", b"")
    )
    credentials.load_credentials(["BIRDEYE_API_KEY"])
    assert "BIRDEYE_API_KEY" not in credentials.os.environ


def test_denied_access_does_not_expose_helper_output(helper, monkeypatch):
    monkeypatch.setattr(
        credentials, "_invoke", lambda _: subprocess.CompletedProcess([], 1, b"secret", b"secret")
    )
    with pytest.raises(credentials.CredentialError) as err:
        credentials.load_credentials(["BIRDEYE_API_KEY"])
    assert "secret" not in str(err.value)
    assert "BIRDEYE_API_KEY" not in credentials.os.environ


def test_unknown_key_is_rejected_before_reading(helper):
    with pytest.raises(credentials.CredentialError, match="Unknown credential"):
        credentials.load_credentials(["WALLET_PRIVATE_KEY"])


def test_status_accepts_only_boolean_presence_flags(helper, monkeypatch):
    raw = ("{" + ",".join(f'"{n}":false' for n in credentials.NAMES) + "}").encode()
    monkeypatch.setattr(
        credentials, "_invoke", lambda _: subprocess.CompletedProcess([], 0, raw, b"")
    )
    status = credentials.credential_status()
    assert status["keychain_helper_ready"] is True
    assert not any(status["saved_in_keychain"].values())
    monkeypatch.setattr(
        credentials,
        "_invoke",
        lambda _: subprocess.CompletedProcess([], 0, b'{"BIRDEYE_API_KEY":"secret"}', b""),
    )
    with pytest.raises(credentials.CredentialError) as err:
        credentials.credential_status()
    assert "secret" not in str(err.value)


def test_timeout_is_sanitized(helper, monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("helper", 60, output=b"secret", stderr=b"secret")

    monkeypatch.setattr(credentials.subprocess, "run", timeout)
    with pytest.raises(credentials.CredentialError) as err:
        credentials.load_credentials(["BIRDEYE_API_KEY"])
    assert "secret" not in str(err.value)
