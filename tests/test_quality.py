import json
import re
from pathlib import Path

from backend._version import __version__
from backend.fixtures import get_fixture
from backend.main import app
from backend.report import build_report
from backend.scheduler import baseline_schedule


def test_business_text_has_an_eleven_pixel_floor():
    css = Path("frontend/src/styles.css").read_text()
    sizes = [float(value) for value in re.findall(r"font(?:-size)?:\s*([0-9.]+)px", css)]
    assert sizes
    assert min(sizes) >= 11


def test_static_report_escapes_user_controlled_technician_ids():
    scenario = get_fixture("main")
    malicious = '<img src=x onerror="alert(1)">'
    scenario.technicians[0].id = malicious
    report = build_report(scenario, baseline_schedule(scenario, 0))
    assert malicious not in report
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in report


def test_user_facing_copy_stays_direct_and_version_numbers_stay_consistent():
    paths = [
        Path("README.md"),
        Path("backend/fixtures.py"),
        Path("backend/main.py"),
        Path("backend/report.py"),
        Path("backend/scheduler.py"),
        Path("backend/storage.py"),
        Path("frontend/src/App.tsx"),
        Path("frontend/src/Management.tsx"),
        Path("frontend/src/StrategyLab.tsx"),
    ]
    copy = "\n".join(path.read_text() for path in paths)
    banned = [
        "API 文档位于",
        "本地数据保存在仓库根目录的",
        "均已排除在 Git 提交之外",
        "接口调试页面：",
        "显著提高未分配代价",
        "强约束路线",
        "复杂路由未声称",
        "更高的综合业务代价",
        "完整演示日",
        "用于验证资格诊断",
        "统一评估推荐",
        "计划版本 v",
        "Explainable field service scheduling",
    ]
    assert not [phrase for phrase in banned if phrase in copy]
    readme = Path("README.md").read_text()
    assert "<http://127.0.0.1:8000/docs>" in readme
    assert "`fieldflow.db`" in readme
    assert "不会进入 Git" in readme
    assert "V{result.version:03d}" in Path("backend/report.py").read_text()


def test_application_version_has_one_checked_release_value():
    package = json.loads(Path("frontend/package.json").read_text())
    assert app.version == package["version"] == __version__
