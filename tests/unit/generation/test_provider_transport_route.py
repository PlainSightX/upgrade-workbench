"""服务模型调用的代理路线必须显式生效，并在异常后恢复进程环境。"""

import os

import pytest

from upgrade_workbench.generation.provider import provider_transport_route

ENDPOINT = "https://api.deepseek.com/chat/completions"


@pytest.mark.parametrize("existing", [None, "example.test"])
def test_process_direct_route_is_effective_and_restores_environment(monkeypatch, existing):
    for key in ("NO_PROXY", "no_proxy"):
        if existing is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, existing)
    before = dict(os.environ)

    with pytest.raises(RuntimeError, match="local failure"):
        with provider_transport_route(ENDPOINT, "process_direct") as record:
            assert record["target_proxy_bypassed"] is True
            assert record["certificate_verification"] == "enabled"
            if existing:
                assert existing in os.environ["NO_PROXY"]
            raise RuntimeError("local failure")

    assert dict(os.environ) == before


def test_system_route_does_not_override_environment():
    before = dict(os.environ)
    with provider_transport_route(ENDPOINT, "system") as record:
        assert record["registered_route"] == "system"
        assert dict(os.environ) == before
    assert dict(os.environ) == before


def test_invalid_route_rejected_without_environment_mutation():
    before = dict(os.environ)
    with pytest.raises(ValueError, match="Unrecognized"):
        with provider_transport_route(ENDPOINT, "guess"):
            pytest.fail("invalid route entered")
    assert dict(os.environ) == before
