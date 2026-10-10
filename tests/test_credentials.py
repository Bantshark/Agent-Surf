"""Fix 27: the Anthropic key from the Windows Credential Manager, with a fake
advapi32 (ctypes calls monkeypatched), so it runs on every platform. The key
value must never appear in output, logs or exception messages."""

import ctypes
import logging

import pytest

from agent_surf import cli, config, credentials

KEY = "sk-ant-SYNTHETIC-KEY-0123456789"


class FakeAdvapi:
    """CredReadW/CredWriteW/CredDeleteW/CredFree over a dict of target -> blob."""

    def __init__(self, store=None, fail=None):
        self.store = dict(store or {})
        self.fail = fail
        self.freed = 0
        self.written = []
        self.reads = 0
        self.last_error = 0
        self._keep = []

    def CredReadW(self, target, ctype, flags, ppcred):
        self.reads += 1
        assert ctype == credentials.CRED_TYPE_GENERIC and flags == 0
        if self.fail:
            self.last_error = self.fail
            return 0
        if target not in self.store:
            self.last_error = credentials.ERROR_NOT_FOUND
            return 0
        blob = self.store[target]
        buf = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
        cred = credentials.CREDENTIALW()
        cred.Type = credentials.CRED_TYPE_GENERIC
        cred.CredentialBlobSize = len(blob)
        cred.CredentialBlob = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
        self._keep += [buf, cred]
        ppcred._obj.contents = cred
        return 1

    def CredFree(self, p):
        self.freed += 1

    def CredWriteW(self, pcred, flags):
        cred = pcred._obj
        self.written.append({"type": cred.Type, "persist": cred.Persist, "target": cred.TargetName})
        self.store[cred.TargetName] = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
        return 1

    def CredDeleteW(self, target, ctype, flags):
        if target in self.store:
            del self.store[target]
            return 1
        self.last_error = credentials.ERROR_NOT_FOUND
        return 0


@pytest.fixture
def windows(monkeypatch):
    """Pretend to be Windows with a fake advapi32; credential reads enabled."""
    fake = FakeAdvapi()
    monkeypatch.setattr(credentials, "is_windows", lambda: True)
    monkeypatch.setattr(credentials, "_advapi32", lambda: fake)
    monkeypatch.setattr(credentials, "_last_error", lambda: fake.last_error)
    monkeypatch.delenv("AGENT_SURF_NO_CREDMAN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return fake


def utf16(s):
    return s.encode("utf-16-le")


def test_env_wins_over_credential_manager(windows, monkeypatch):
    windows.store[credentials.ANTHROPIC_TARGET] = utf16("from-credman")
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    assert config.anthropic_key_and_source() == (KEY, "env")
    assert windows.reads == 0


@pytest.mark.parametrize("blob", [utf16(KEY), utf16(KEY) + b"\x00\x00", KEY.encode(), KEY.encode() + b"\x00"])
def test_credential_manager_blob_decoding(windows, blob):
    windows.store[credentials.ANTHROPIC_TARGET] = blob
    assert config.anthropic_key_and_source() == (KEY, "credential manager")
    assert config.anthropic_api_key() == KEY
    assert windows.freed == windows.reads   # CredFree after every successful read


def test_missing_everywhere(windows):
    assert config.anthropic_key_and_source() == (None, None)


def test_non_windows_uses_env_only(monkeypatch):
    monkeypatch.setattr(credentials, "is_windows", lambda: False)
    monkeypatch.delenv("AGENT_SURF_NO_CREDMAN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    def boom():
        raise AssertionError("must not touch advapi32 off Windows")

    monkeypatch.setattr(credentials, "_advapi32", boom)
    assert config.anthropic_key_and_source() == (None, None)


def test_disabled_by_env(windows, monkeypatch):
    windows.store[credentials.ANTHROPIC_TARGET] = utf16(KEY)
    monkeypatch.setenv("AGENT_SURF_NO_CREDMAN", "1")
    assert config.anthropic_api_key() is None and windows.reads == 0


def test_read_error_is_logged_without_value(windows, caplog):
    windows.store[credentials.ANTHROPIC_TARGET] = utf16(KEY)
    windows.fail = 5   # ERROR_ACCESS_DENIED
    caplog.set_level(logging.DEBUG)
    assert config.anthropic_api_key() is None
    assert "Windows error 5" in caplog.text and KEY not in caplog.text
    with pytest.raises(credentials.CredentialError) as e:
        credentials.read_generic(credentials.ANTHROPIC_TARGET)
    assert KEY not in str(e.value)


def test_key_set_writes_generic_local_machine_and_never_prints(windows, monkeypatch, capsys, caplog, tmp_path):
    monkeypatch.setenv("AGENT_SURF_HOME", str(tmp_path / "home"))
    caplog.set_level(logging.DEBUG)
    prompts = []
    monkeypatch.setattr("getpass.getpass", lambda prompt="": prompts.append(prompt) or KEY + "\n")
    assert cli.main(["key", "set", "--json"]) == 0
    captured = capsys.readouterr()
    assert KEY not in captured.out + captured.err + caplog.text
    assert windows.written == [{"type": credentials.CRED_TYPE_GENERIC,
                                "persist": credentials.CRED_PERSIST_LOCAL_MACHINE,
                                "target": credentials.ANTHROPIC_TARGET}]
    assert windows.store[credentials.ANTHROPIC_TARGET] == utf16(KEY)   # stripped, UTF-16-LE
    assert prompts and "hidden" in prompts[0]
    assert config.anthropic_key_and_source() == (KEY, "credential manager")
    assert cli.main(["key", "clear"]) == 0
    assert credentials.ANTHROPIC_TARGET not in windows.store
    assert cli.main(["key", "clear"]) == 0                              # nothing to clear: still fine
    assert "there was no" in capsys.readouterr().err


def test_key_set_empty_input_stores_nothing(windows, monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_SURF_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "  ")
    assert cli.main(["key", "set"]) == cli.EXIT_ERROR
    assert windows.written == []


def test_key_commands_off_windows(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("AGENT_SURF_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(credentials, "is_windows", lambda: False)
    monkeypatch.setattr("getpass.getpass", lambda prompt="": pytest.fail("must not prompt"))
    assert cli.main(["key", "set"]) == cli.EXIT_ERROR
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err


def test_make_client_passes_credman_key_and_logs_nothing(windows, monkeypatch, caplog, capsys):
    import anthropic

    windows.store[credentials.ANTHROPIC_TARGET] = utf16(KEY)
    seen = {}

    class FakeAnthropic:
        def __init__(self, **kw):
            seen.update(kw)

    monkeypatch.setattr(anthropic, "Anthropic", FakeAnthropic)
    caplog.set_level(logging.DEBUG)
    assert cli.make_client(config.load()) is not None
    assert seen == {"api_key": KEY}
    captured = capsys.readouterr()
    assert KEY not in captured.out + captured.err + caplog.text
