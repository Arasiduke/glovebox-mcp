"""Region setting tests: enter through the real door (server subprocess) and check an unknown region fails at startup."""
from __future__ import annotations

import os
import subprocess
import sys


def test_unknown_region_makes_server_fail_at_startup_and_list_valid_values():
    env = {**os.environ, "GLOVEBOX_REGION": "fr", "PYTHONUTF8": "1"}
    p = subprocess.run([sys.executable, "-m", "glovebox.server"], env=env, stdin=subprocess.DEVNULL,
                       capture_output=True, timeout=60)
    err = p.stderr.decode("utf-8", "replace")
    assert p.returncode != 0, err
    assert "GLOVEBOX_REGION='fr' is not supported" in err and "zh-TW, en-US" in err, err
