from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    if not shutil.which("sqlite3"):
        # backup.sh shells out to the sqlite3 CLI; without it this check cannot run.
        print("backup check skipped (sqlite3 CLI unavailable)")
        return
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "data" / "atlas.sqlite"
        db_path.parent.mkdir(parents=True)
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE t(x)")
        conn.commit()
        conn.close()
        uploads = db_path.parent / "uploads"
        uploads.mkdir()
        (uploads / "evidence.bin").write_bytes(b"artifact-bytes")

        dest = Path(tmp) / "backups"
        env = {**os.environ, "ATLAS_DB": str(db_path)}
        subprocess.run(["bash", str(ROOT / "scripts" / "backup.sh"), str(dest)], check=True, env=env, capture_output=True)

        assert list(dest.glob("atlas-*.sqlite")), "backup must produce a sqlite snapshot"
        upload_backups = list(dest.glob("atlas-uploads-*.tar.gz"))
        assert upload_backups, "backup must also archive the upload store (file_ref bytes)"
        with tarfile.open(upload_backups[0]) as tar:
            names = tar.getnames()
        assert any(name.endswith("uploads/evidence.bin") for name in names), names

    print("backup check ok")


if __name__ == "__main__":
    main()
