import httpx

from worker import health


def test_no_url_means_no_request(monkeypatch):
    calls = []
    monkeypatch.setattr(health.httpx, "get", lambda *args, **kwargs: calls.append(args))

    health.ping(None)

    assert calls == []


def test_a_failed_ping_is_printed_without_the_url(monkeypatch, capsys):
    def fail(url, **kwargs):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(health.httpx, "get", fail)

    health.ping("https://hc-ping.example/secret-token")

    output = capsys.readouterr().out
    assert output == "healthcheck ping failed: ConnectError\n"
    assert "secret-token" not in output
