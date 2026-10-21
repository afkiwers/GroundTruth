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
  const shown = () => [...document.querySelectorAll('#issue-table tbody tr')].filter(r => !r.classList.contains('hidden'))
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


LINK_PROBE = """<script>
(() => {
  document.querySelector("button.f[data-sev='error']").click();
  document.querySelector("a[data-show-check='cap_voltage']").click();
  const shown = [...document.querySelectorAll('#issue-table tbody tr')].filter(r => !r.classList.contains('hidden'))
    .map(r => r.dataset.check).sort().join(',');
  const on = [...document.querySelectorAll('button.f.on')].map(b => b.dataset.check || b.dataset.sev).join(',');
  const pre = document.createElement('pre'); pre.id = 'probe'; pre.textContent = shown + '|' + on;
  document.body.appendChild(pre);
})();
</script>"""


def _edge_probe(tmp_path, body: str, probe: str) -> str:
    """The text the probe script writes into the page after running in headless Edge."""
    # the table inside its part's section, as on the report (<section id='issues'>: the section and the table had
    # one id, and the filters' pager refresh found the section: the user's "clicking on current density shows no
    # items unless you toggle Show")
    page = ("<!doctype html><html><head><style>" + html._CSS + "</style></head><body><section id='issues'>" + body
            + f"</section><script>{html._JS}</script>" + probe + "</body></html>")
    f = tmp_path / "page.html"
    f.write_text(page, encoding="utf-8")
    run = subprocess.run([str(EDGE), "--headless=new", "--disable-gpu", f"--user-data-dir={tmp_path / 'edge'}",
                          "--dump-dom", f.as_uri()], capture_output=True, text=True, encoding="utf-8", timeout=120)
    m = re.search(r"<pre id=\"probe\">(.*?)</pre>", run.stdout)
    assert m, run.stdout[-2000:] + run.stderr[-2000:]
    return m.group(1)


PAGER_PROBE = """<script>
(() => {
  const vis = () => [...document.querySelectorAll('#issue-table tbody tr')]
    .filter(r => getComputedStyle(r).display !== 'none').length;
  const info = () => document.querySelector('.pager .pginfo').textContent;
  const res = {};
  res.first = [vis(), info()];
  [...document.querySelectorAll('.pager button.pg')].find(b => b.textContent === '2').click();
  res.page2 = [vis(), info()];
  document.querySelector(".pager button[data-size='50']").click();
  res.fifty = [vis(), info()];
  document.querySelector(".pager button[data-size='0']").click();
  res.all = [vis(), info(), document.querySelectorAll('.pager button.pg').length];
  document.querySelector(".pager button[data-size='25']").click();
  [...document.querySelectorAll('.pager button.pg')].find(b => b.textContent === '3').click();
  document.querySelector("button.f[data-sev='error']").click();
  res.errors = [vis(), info()];
  document.querySelector("button.f[data-sev='all']").click();
  document.querySelector("button.f[data-check='late']").click();
  res.late = [vis(), info()];
  document.querySelector("button.f[data-check-all]").click();
  [...document.querySelectorAll('.pager button.pg')].find(b => b.textContent === '2').click();
  document.querySelector('#issue-table th').click();
  res.sorted = [vis(), info()];
  const pre = document.createElement('pre'); pre.id = 'probe'; pre.textContent = JSON.stringify(res);
  document.body.appendChild(pre);
})();
</script>"""


def test_long_tables_are_paged_and_short_ones_not():
    """More than 25 rows: the table is 'paged'; 25 or fewer: no pager."""
    many = [Issue("cap_voltage", "warning", "C1", f"c {k}", (float(k), 0.0), key=str(k)) for k in range(26)]
    assert 'class="sort paged"' in html._issues(many)
    assert "paged" not in html._issues(many[:25])


@pytest.mark.skipif(not EDGE.exists(), reason="needs Microsoft Edge for a headless page")
def test_the_pager_pages_the_issues_with_their_filters(tmp_path):
    """61 issues (20 errors): 25 rows at first; page 2 the next 25; 50 rows; all (no page buttons); on page 3 the
    Error filter goes back to the first page of its 20; a check whose one row is the last shows it (the page counted
    again: inside the report's section it was not); a sort goes back to page 1."""
    import json
    issues = [Issue("cap_voltage" if k % 2 else "clearance_voltage", "error" if k % 3 == 0 else "warning", "+VDC",
                    f"m {k}", (float(k), 0.0), key=str(k)) for k in range(60)]
    issues.append(Issue("late", "warning", "+VDC", "one check, after the first page", (99.0, 0.0), key="late"))
    res = json.loads(_edge_probe(tmp_path, html._issues(issues), PAGER_PROBE).replace("&quot;", '"'))
    assert res["first"] == [25, "Rows 1–25 of 61"] and res["page2"] == [25, "Rows 26–50 of 61"]
    assert res["fifty"] == [50, "Rows 1–50 of 61"] and res["all"] == [61, "Rows 1–61 of 61", 0]
    assert res["errors"] == [20, "Rows 1–20 of 20"] and res["sorted"] == [25, "Rows 1–25 of 61"]
    assert res["late"] == [1, "Rows 1–1 of 1"]          # a check whose only row was past the first page


@pytest.mark.skipif(not EDGE.exists(), reason="needs Microsoft Edge for a headless page")
def test_a_summary_finding_opens_its_check(tmp_path):
    """The summary's count of a check (Main findings) shows that check's rows only, every severity (the Error chip
    picked before is released)."""
    issues = _issues()
    out = _edge_probe(tmp_path, html._findings_by_check(issues) + html._issues(issues), LINK_PROBE)
    assert out == "cap_voltage,cap_voltage|all,cap_voltage"


@pytest.mark.skipif(not EDGE.exists(), reason="needs Microsoft Edge for a headless page")
def test_clicking_checks_shows_only_them(tmp_path):
    """vds_rating and plane_resonance picked: their two rows; with Error: only vds_rating's, the chips counting
    errors; 'all' checks with Error: the two errors; back to All: all seven."""
    import json
    page = ("<!doctype html><html><head><style>" + html._CSS + "</style></head><body><section id='issues'>"
            + html._issues(_issues()) + f"</section><script>{html._JS}</script>" + PROBE + "</body></html>")
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
