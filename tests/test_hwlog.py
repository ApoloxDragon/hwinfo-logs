import io
import contextlib
import os
import tempfile
import unittest

from hwlog.cli import main
from hwlog.parser import load

HEADER = 'Date,Time,"CPU (Tctl/Tdie) [\xb0C]","Fan [RPM]","Fan [RPM]","Throttle [Yes/No]","Intrusion [Yes/No]",\r\n'
ROWS = [
    "9.10.2026,23:59:58.000,40.0,1000,2000,No,Yes,\r\n",
    "10.10.2026,0:0:0.000,95.5,1100,2100,Yes,Yes,\r\n",
    "10.10.2026,0:0:2.000,96.0,1100,2100,Yes,Yes,\r\n",
    "10.10.2026,0:0:4.000,50.0,,2100,No,Yes,\r\n",
]
FOOTER = HEADER + ",,System: Board,System: Board,System: Board,Drive: X [C:, D:],Drive: X [C:, D:],\r\n"


def make(text: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".CSV")
    with os.fdopen(fd, "wb") as fh:
        fh.write(text.encode("cp1252"))
    return path


class HwlogTests(unittest.TestCase):
    def setUp(self):
        self.path = make(HEADER + "".join(ROWS) + FOOTER)
        self.addCleanup(os.remove, self.path)

    def test_parse(self):
        log = load(self.path)
        self.assertEqual(log.rows, 4)  # footer not counted as data
        self.assertEqual([c.name for c in log.columns[3:5]], ["Fan [RPM]", "Fan [RPM]#2"])
        self.assertEqual(log.columns[2].unit, "\xb0C")
        self.assertEqual(log.data[5], [0, 1, 1, 0])  # Yes/No -> 1/0
        self.assertTrue(log.data[3][3] != log.data[3][3])  # empty -> NaN
        self.assertEqual(log.times[1] - log.times[0], 2.0)  # crosses midnight
        self.assertEqual(log.columns[5].group, "Drive: X [C:, D:]")  # comma-split group re-merged

    def run_cli(self, *argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(main(list(argv)), 0)
        return buf.getvalue()

    def test_events(self):
        out = self.run_cli("events", self.path)
        self.assertIn("Throttle [Yes/No]", out)
        self.assertNotIn("Intrusion", out)  # constant Yes is not an event
        self.assertIn("temp>=90", out)
        self.assertEqual(out.count("Throttle"), 1)  # aggregated per sensor

    def test_stats_and_select(self):
        out = self.run_cli("stats", self.path, "-c", "Tctl")
        self.assertIn("96.0", out)
        self.assertIn("Fan [RPM]#2", self.run_cli("columns", self.path, "-c", "#2"))

    def test_compare_windows(self):
        out = self.run_cli("compare", self.path, "-c", "Tctl", "--window-a", "+0..+2", "--window-b", "+4..+6")
        self.assertIn("+5.25", out)  # avg 67.75 -> 73.0
        out = self.run_cli("compare", self.path, "-c", "Tctl", "--window-a", "+0..+6", "--window-b", "+0..+6", "--when", "Tctl>=90")
        self.assertIn("2 samples", out)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["compare", self.path, "-c", "Tctl"]), 2)  # one log, no windows

    def test_clock_window_edges(self):
        # first sample is 23:59:58; a start just before it must not roll over to the next day
        out = self.run_cli("timeline", self.path, "-c", "Tctl", "--start", "23:59:00", "--end", "23:59:59")
        self.assertIn("40", out)
        # a clock time after midnight means the next day
        out = self.run_cli("timeline", self.path, "-c", "Tctl", "--start", "00:00:02", "--end", "00:00:05", "-b", "10")
        self.assertIn("73", out)  # samples at 0:0:2 (96) and 0:0:4 (50)

    def test_presentmon_warnings(self):
        hdr = 'Date,Time,"Framerate [FPS]","GPU Utilization [%]",\r\n'
        rows = "".join(f"1.1.2026,0:{i // 30}:{(i % 30) * 2},0,95,\r\n" for i in range(60))  # 2 min of FPS=0 under load
        footer = hdr + ",,PresentMon [dwm.exe],dGPU [#0]: X,\r\n"
        path = make(hdr + rows + footer)
        self.addCleanup(os.remove, path)
        out = self.run_cli("info", path)
        self.assertIn("PresentMon target: dwm.exe", out)
        self.assertIn("not a game", out)
        self.assertIn("read 0 while GPU load", out)
        self.assertNotIn("WARNING", self.run_cli("info", self.path))  # normal log stays quiet

    def test_runs_hist_corr(self):
        out = self.run_cli("runs", f"a={self.path}@+0..+2", f"b={self.path}@+4..+6", "-c", "Tctl")
        self.assertIn("a (2)", out)
        self.assertIn("67.8/95.5", out)
        self.assertIn("73.0/96.0", out)
        out = self.run_cli("hist", self.path, "-c", "Tctl", "--edges", "60,90")
        self.assertIn(">=90", out)
        self.assertIn("50.0%", out)  # 2 of 4 samples are >= 90
        out = self.run_cli("corr", self.path, "Tctl", "--all")
        self.assertIn("Fan [RPM]", out)

    def test_summary_hides_noise(self):
        hdr = 'Date,Time,"CPU (Tctl/Tdie) [\xb0C]","GPU Thermal Limit [\xb0C]","GPU Memory Usage [MB]",\r\n'
        rows = "".join(f"1.1.2026,0:0:{i * 2},{40 + i},87,{100000 + i},\r\n" for i in range(10))
        path = make(hdr + rows)
        self.addCleanup(os.remove, path)
        out = self.run_cli("summary", path)
        self.assertIn("Tctl", out)
        self.assertNotIn("Thermal Limit", out)
        self.assertNotIn("Memory Usage", out)

    def test_not_hwinfo(self):
        bad = make("ip,count\n1.2.3.4,5\n")
        self.addCleanup(os.remove, bad)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["info", bad]), 2)


if __name__ == "__main__":
    unittest.main()
