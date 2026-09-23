"""`scripts/screenshots.py`, the README screenshots' generator, keeps working.

Runs the script as a subprocess -- it's a standalone dev script, not part of
the package -- into `tmp_path` and checks the three SVGs land and look sane.
Never touches the real `/sys`, `/proc` or `~/.config`; the script itself
builds its own fixture tree and points `XDG_CONFIG_HOME` at a temp dir.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO_ROOT / "scripts" / "screenshots.py"


def test_screenshots_script_writes_three_plausible_svgs(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), str(tmp_path)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr

    main_svg = (tmp_path / "main.svg").read_text(encoding="utf-8")
    processes_svg = (tmp_path / "processes.svg").read_text(encoding="utf-8")
    theme_panel_svg = (tmp_path / "theme-panel.svg").read_text(encoding="utf-8")

    assert "RAM" in main_svg
    assert "Swap" in main_svg
    assert "ghostty" in main_svg
    assert "ghostty" in processes_svg  # the process view's own title
    assert "claude" in processes_svg
    assert "Theme" in theme_panel_svg
    assert "dracula" in theme_panel_svg
