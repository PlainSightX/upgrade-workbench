"""请求容量按版本冻结；旧回执不随新任务的配置改变解释。"""

from __future__ import annotations

LEGACY_OUTPUT_TOKENS = 65_536
LEGACY_TIMEOUT_SECONDS = 300
LEGACY_RESPONSE_BYTES = 384_000
MAX_EXTENDED_OUTPUT_TOKENS = 131_072
MAX_EXTENDED_TIMEOUT_SECONDS = 1_200
MAX_EXTENDED_RESPONSE_BYTES = 4 * 1024 * 1024


def limits(value=None):
    if value is None:
        return LEGACY_OUTPUT_TOKENS, LEGACY_TIMEOUT_SECONDS, LEGACY_RESPONSE_BYTES
    if (not isinstance(value, dict) or set(value) != {"version", "max_response_bytes"}
            or value["version"] != "generation-capacity-v1"):
        raise ValueError("Invalid versioned runtime capacity")
    size = value["max_response_bytes"]
    if type(size) is not int or not 1 <= size <= MAX_EXTENDED_RESPONSE_BYTES:
        raise ValueError("Invalid runtime response byte limit")
    return MAX_EXTENDED_OUTPUT_TOKENS, MAX_EXTENDED_TIMEOUT_SECONDS, size


def validate(output_tokens, timeout_seconds, value=None):
    maximum_output, maximum_timeout, response_bytes = limits(value)
    for key, actual, maximum in (("max_output_tokens", output_tokens, maximum_output),
                                 ("timeout_seconds", timeout_seconds, maximum_timeout)):
        if type(actual) is not int or not 1 <= actual <= maximum:
            raise ValueError(f"{key} must be an integer between 1 and {maximum}")
    return {"max_output_tokens": output_tokens, "timeout_seconds": timeout_seconds,
            "max_response_bytes": response_bytes}


def from_record(record):
    result = validate(record["max_output_tokens"], record["timeout_seconds"],
                      record.get("runtime_capacity"))
    if record.get("max_response_bytes") != result["max_response_bytes"]:
        raise ValueError("Frozen response capacity differs from its versioned policy")
    return result
