from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ATLAS = ROOT / "atlas"


def main() -> None:
    # Silo invariant (ADR docs/adr/0001-multi-tenancy-silo-vs-pooled.md): Atlas core is
    # instance-per-tenant, so no `tenant_id` column/scoping exists in atlas/ core. Pooled
    # tenancy is deferred; adding tenant_id here would silently reverse the decision.
    offenders = []
    for path in sorted(ATLAS.glob("*.py")):
        if "tenant_id" in path.read_text(encoding="utf-8"):
            offenders.append(path.name)
    assert not offenders, (
        f"tenant_id found in atlas core ({offenders}); this reverses the silo invariant. "
        "See docs/adr/0001-multi-tenancy-silo-vs-pooled.md before proceeding."
    )
    print("silo invariant check ok")


if __name__ == "__main__":
    main()
