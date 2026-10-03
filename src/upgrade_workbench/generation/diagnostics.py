"""传输异常只输出固定分类和整数；异常消息可能包含密钥或代理地址。"""

from __future__ import annotations

import errno
import http.client
import socket
import ssl
from urllib.error import HTTPError, URLError


def transport_diagnostic(error: BaseException) -> dict:
    """分类已观察到的异常，不由消息猜测代理、请求是否送达或供应商是否扣费。"""
    root = error
    for _ in range(4):
        if not isinstance(root, URLError) or not isinstance(root.reason, BaseException):
            break
        root = root.reason
    category = "unclassified"
    if isinstance(error, HTTPError):
        category = "http_status"
        root = error
    elif isinstance(root, socket.gaierror):
        category = "dns"
    elif isinstance(root, ssl.SSLCertVerificationError):
        category = "tls_certificate"
    elif isinstance(root, ssl.SSLError):
        category = "tls"
    elif isinstance(root, TimeoutError):
        category = "timeout"
    elif isinstance(root, ConnectionRefusedError):
        category = "connection_refused"
    elif isinstance(root, ConnectionResetError):
        category = "connection_reset"
    elif isinstance(root, ConnectionAbortedError):
        category = "connection_aborted"
    elif isinstance(root, BrokenPipeError):
        category = "broken_pipe"
    elif isinstance(root, http.client.HTTPException):
        category = "http_protocol"
    elif isinstance(root, OSError) and not isinstance(root, URLError):
        category = {errno.ENETUNREACH: "network_unreachable",
                    errno.EHOSTUNREACH: "host_unreachable"}.get(root.errno, "os_error")
    result = {"schema_version": 1, "category": category,
              "proxy_involvement": "not_determined", "raw_message_stored": False}
    for attr, key in (("errno", "errno"), ("winerror", "winerror"),
                      ("verify_code", "tls_verify_code"), ("code", "http_status")):
        value = getattr(root, attr, None)
        if type(value) is int and -(2**31) <= value < 2**31:
            result[key] = value
    return result
