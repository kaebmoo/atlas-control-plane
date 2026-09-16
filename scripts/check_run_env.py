"""Hermetic check for the local launcher’s optional repo-local .env loading."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]


def _make_fake_python(bin_dir: Path) -> None:
    fake = bin_dir / "python3"
    fake.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"${ATLAS_TEST_FROM_DOTENV-}\"\n"
        "printf '%s\\n' \"$*\"\n",
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)


def _run(root: Path, bin_dir: Path) -> list[str]:
    env = os.environ.copy()
    env.pop("ATLAS_TEST_FROM_DOTENV", None)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    result = subprocess.run(
        [str(root / "scripts" / "run.sh")],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.splitlines()


def main() -> None:
    with TemporaryDirectory() as tmp:
        tmp_root = Path(tmp) / "repo"
        (tmp_root / "scripts").mkdir(parents=True)
        shutil.copy2(ROOT / "scripts" / "run.sh", tmp_root / "scripts" / "run.sh")
        bin_dir = Path(tmp) / "bin"
        bin_dir.mkdir()
        _make_fake_python(bin_dir)

        (tmp_root / ".env").write_text("ATLAS_TEST_FROM_DOTENV=loaded\n", encoding="utf-8")
        loaded = _run(tmp_root, bin_dir)
        assert loaded[0] == "loaded", loaded
        assert loaded[1].startswith("-m atlas --host "), loaded

        (tmp_root / ".env").unlink()
        no_file = _run(tmp_root, bin_dir)
        assert no_file[0] == "", no_file

    print("run.sh .env loading check ok")


if __name__ == "__main__":
    main()
