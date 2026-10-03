"""布局不覆盖服务身份；临时目录无论正常或异常退出都收口。"""

import json

import pytest

from upgrade_workbench.runtime import RuntimeLayout, load_layout, short_test_root


@pytest.mark.parametrize("fail", [False, True])
def test_owned_test_directory_cleaned(tmp_path, fail):
    layout = RuntimeLayout(tmp_path)
    try:
        with short_test_root(layout) as owned:
            (owned / "w").mkdir()
            if fail:
                raise RuntimeError("owned_failure")
    except RuntimeError:
        assert fail
    assert not list(layout.tests.iterdir())


def test_layout_has_no_service_identity(tmp_path):
    path = tmp_path / "layout.json"
    path.write_text(json.dumps({"schema_version": 1, "runtime_root": str(tmp_path / "runtime")}))
    assert load_layout(path).tests == tmp_path / "runtime/t"
    path.write_text(json.dumps({"schema_version": 1, "runtime_root": str(tmp_path), "database": "other"}))
    with pytest.raises(ValueError, match="invalid_runtime_layout"):
        load_layout(path)
