"""The HTML report's Issues table filters (report/html.py): one severity, and any number of checks (the user: "I only
want to see clearance_voltage or cap_voltage or vds_rating ... or I want to see vds_rating and plane_resonance. So be
able to filter out one or more"). The markup in Python; the clicks in a headless browser when Edge is installed (as
CLAUDE.md's page screenshots: the page's own script, driven by a test script appended to it, writes what it shows)."""
import re
import subprocess
from pathlib import Path

import pytest

from groundtruth.core.issues import Issue
from groundtruth.report import html

EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")


def _issues() -> list[Issue]:
    """Seven issues of four checks: clearance_voltage 3 (2 warnings, 1 error), cap_voltage 2 (warnings),
    vds_rating 1 (error), plane_resonance 1 (info)."""
    def one(check, sev, k):
        """An issue of `check` at `sev`, its own location."""
        return Issue(check, sev, "+VDC", f"{check} {k}", (float(k), 0.0), key=str(k))
    return [one("clearance_voltage", "warning", 1), one("clearance_voltage", "warning", 2),
            one("clearance_voltage", "error", 3), one("cap_voltage", "warning", 4), one("cap_voltage", "warning", 5),
            one("vds_rating", "error", 6), one("plane_resonance", "info", 7)]


def test_the_table_has_a_chip_per_check_and_the_rows_their_check():
    """Every row carries its check; a chip per check with its count, sorted, and an 'all' chip picked at first; a
    table of one check has no check chips."""
    out = html._issues(_issues())
    assert len(re.findall(r"<tr data-sev='\w+' data-check='\w+'", out)) == 7
    chips = re.findall(r"data-check='(\w+)'[^>]*>\w+ <span class='cn'>(\d+)</span>", out)
    assert chips == [("cap_voltage", "2"), ("clearance_voltage", "3"), ("plane_resonance", "1"), ("vds_rating", "1")]
    assert "data-check-all='1'" in out and "id='issues-shown'" in out
    assert "data-check-all" not in html._issues(_issues()[:3])


PROBE = """<script>
(() => {
  const click = q => document.querySelector(q).click();
  const shown = () => [...document.querySelectorAll('#issues tbody tr')].filter(r => !r.classList.contains('hidden'))
    .map(r => r.dataset.check + ':' + r.dataset.sev).sort().join(',');
  const res = {};
  click("button.f[data-check='vds_rating']"); click("button.f[data-check='plane_resonance']");
  res.two = shown();
  click("button.f[data-sev='error']"); res.two_err = shown();
  res.counts = [...document.querySelectorAll('button.f[data-check] .cn')].map(s => s.textContent).join(',');
  click("button.f[data-check-all]"); res.all_err = shown();
  res.line = document.getElementById('issues-shown').textContent;
  click("button.f[data-sev='all']"); res.all = shown().split(',').length;
  const pre = document.createElement('pre'); pre.id = 'probe'; pre.textContent = JSON.stringify(res);
  document.body.appendChild(pre);
})();
</script>"""


@pytest.mark.skipif(not EDGE.exists(), reason="needs Microsoft Edge for a headless page")
def test_clicking_checks_shows_only_them(tmp_path):
    """vds_rating and plane_resonance picked: their two rows; with Error: only vds_rating's, the chips counting
    errors; 'all' checks with Error: the two errors; back to All: all seven."""
    import json
    page = ("<!doctype html><html><head><style>tr.hidden{display:none}</style></head><body>"
            + html._issues(_issues()) + f"<script>{html._JS}</script>" + PROBE + "</body></html>")
    f = tmp_path / "issues.html"
    f.write_text(page, encoding="utf-8")
    run = subprocess.run([str(EDGE), "--headless=new", "--disable-gpu", f"--user-data-dir={tmp_path / 'edge'}",
                          "--dump-dom", f.as_uri()], capture_output=True, text=True, encoding="utf-8", timeout=120)
    m = re.search(r"<pre id=\"probe\">(.*?)</pre>", run.stdout)
    assert m, run.stdout[-2000:] + run.stderr[-2000:]
    res = json.loads(m.group(1).replace("&quot;", '"'))
    assert res["two"] == "plane_resonance:info,vds_rating:error"
    assert res["two_err"] == "vds_rating:error"
    assert res["counts"] == "0,1,0,1"                 # errors per check: cap, clearance, plane, vds
    assert res["all_err"] == "clearance_voltage:error,vds_rating:error" and res["line"] == "2 of 7 shown"
    assert res["all"] == 7
