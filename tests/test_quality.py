import re
from pathlib import Path

from backend.fixtures import get_fixture
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
