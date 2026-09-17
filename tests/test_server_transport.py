import os

from skydeamon import server


def _no_env(monkeypatch):
    # Never touch the real repo .env or network in transport tests.
    monkeypatch.setattr(server, "_load_env", lambda *a, **k: None)
    monkeypatch.delenv("SKYDEMON_LOGIN", raising=False)
    monkeypatch.delenv("SKYDEMON_PASSWORD", raising=False)


def test_main_stdio_default(monkeypatch):
    calls = {}
    monkeypatch.setattr(server.mcp, "run", lambda *a, **k: calls.update(args=a, kwargs=k))
    _no_env(monkeypatch)
    server.main([])
    assert calls == {"args": (), "kwargs": {}}


def test_main_streamable_http(monkeypatch):
    calls = {}
    monkeypatch.setattr(server.uvicorn, "run", lambda *a, **k: calls.update(args=a, kwargs=k))
    _no_env(monkeypatch)
    server.main(["--transport", "streamable-http", "--port", "8080"])
    assert calls["kwargs"]["port"] == 8080
    assert calls["kwargs"]["host"] == "127.0.0.1"


def test_load_env_sets_missing_only(tmp_path, monkeypatch, capsys):
    env_file = tmp_path / ".env"
    env_file.write_text('SKYDEMON_LOGIN=a@b.c\nSKYDEMON_PASSWORD="s3cr3t"\n# comment\nEMPTY=\n',
                        encoding="utf-8")
    monkeypatch.delenv("SKYDEMON_LOGIN", raising=False)
    monkeypatch.delenv("SKYDEMON_PASSWORD", raising=False)
    monkeypatch.setenv("SKYDEMON_PASSWORD", "keep-me")  # real env wins
    server._load_env(env_file)
    assert os.environ["SKYDEMON_LOGIN"] == "a@b.c"
    assert os.environ["SKYDEMON_PASSWORD"] == "keep-me"
    out, err = capsys.readouterr()
    assert "s3cr3t" not in out and "s3cr3t" not in err


def test_load_env_missing_file(tmp_path):
    server._load_env(tmp_path / ".env")  # must not raise
