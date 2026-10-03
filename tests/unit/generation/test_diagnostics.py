"""故障注入只证明分类、脱敏和不重发，不推断历史线上故障根因。"""

import errno
import http.client
import json
import socket
import ssl
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

from upgrade_workbench.generation import ProposalInputError, complete_request
from upgrade_workbench.generation.diagnostics import transport_diagnostic

SECRET = "secret-key-at-https://proxy-user:password@internal.example/private"


@pytest.mark.parametrize("error,category", [
    (URLError(socket.gaierror(-2, SECRET)), "dns"),
    (URLError(ssl.SSLCertVerificationError(1, SECRET)), "tls_certificate"),
    (ssl.SSLError(1, SECRET), "tls"),
    (URLError(ConnectionRefusedError(errno.ECONNREFUSED, SECRET)), "connection_refused"),
    (ConnectionResetError(errno.ECONNRESET, SECRET), "connection_reset"),
    (ConnectionAbortedError(errno.ECONNABORTED, SECRET), "connection_aborted"),
    (BrokenPipeError(errno.EPIPE, SECRET), "broken_pipe"),
    (URLError(TimeoutError(SECRET)), "timeout"),
    (TimeoutError(SECRET), "timeout"),
    (OSError(errno.ENETUNREACH, SECRET), "network_unreachable"),
    (OSError(errno.EHOSTUNREACH, SECRET), "host_unreachable"),
    (http.client.RemoteDisconnected(SECRET), "connection_reset"),
    (http.client.BadStatusLine(SECRET), "http_protocol"),
    (URLError(SECRET), "unclassified"),
    (RuntimeError(SECRET), "unclassified"),
    (HTTPError(SECRET, 407, SECRET, {"Proxy-Authenticate": SECRET}, None), "http_status"),
])
def test_safe_diagnostic_receipt_no_replay(prepared, error, category):
    calls = []

    def fail(*args, **kwargs):
        calls.append(1)
        raise error

    result = complete_request(prepared, api_key="unused-test-key", transport=fail)
    assert result["transport_diagnostic"]["category"] == category
    assert result["transport_diagnostic"]["proxy_involvement"] == "not_determined"
    assert result["automatic_retries"] == 0 and result["calls"] == 1
    assert result["model_usage"]["availability"] == "unavailable"
    for path in Path(result["report_path"]).parent.rglob("*"):
        if path.is_file():
            assert SECRET.encode() not in path.read_bytes()
            assert b"internal.example" not in path.read_bytes()
    with pytest.raises(ProposalInputError):
        complete_request(prepared, api_key="unused-test-key", transport=fail)
    assert len(calls) == 1


def test_numeric_codes_only_and_no_exception_repr():
    error = ssl.SSLCertVerificationError(1, SECRET)
    error.verify_code = 20
    error.verify_message = SECRET
    value = transport_diagnostic(URLError(URLError(error)))
    assert value["tls_verify_code"] == 20 and value["errno"] == 1
    assert SECRET not in json.dumps(value)
    error.verify_code = SECRET
    assert "tls_verify_code" not in transport_diagnostic(error)


def test_cyclic_url_reason_is_bounded():
    error = URLError(SECRET)
    error.reason = error
    assert transport_diagnostic(error)["category"] == "unclassified"


def test_storage_failure_is_not_classified_as_transport(prepared, monkeypatch):
    from .conftest import provider_response

    original = Path.write_bytes

    def fail_response_write(path, data):
        if path.name == "response.sanitized.json":
            raise PermissionError(errno.EACCES, SECRET)
        return original(path, data)

    monkeypatch.setattr(Path, "write_bytes", fail_response_write)
    result = complete_request(prepared, api_key="unused-test-key", transport=lambda *_a, **_kw: provider_response())
    assert result["status"] == "outcome_unknown"
    assert "transport_diagnostic" not in result
    assert SECRET not in Path(result["report_path"]).read_text(encoding="utf-8")
