# Low-findings backlog

Per [threat-model.md](threat-model.md) **DoD #7**: Low findings live here and do **not** block
sign-off. Each lists why it is Low, an owner, and the trigger that promotes it to real work.
High/Medium findings are never parked here — they are fixed or formally accepted in the threat model.

Owners are roles/teams (named person confirmed at sign-off). Every item below is grounded in a
specific file/config line — the list is **not** padded to hit a count.

| # | Finding | Why Low | Owner | Promote-to-work trigger |
|---|---|---|---|---|
| L1 | **Mobile sidebar usability** — the dashboard sidebar (`atlas/static/`) is awkward on narrow viewports. | UX only; no security, data, or correctness impact. Part of the user's in-progress UI work. | UI WIP (user) | Mobile/tablet becomes a supported surface. |
| L2 | **No static type-checking in the toolchain** — `scripts/lint.sh` runs ruff + bandit only; there is no `mypy` config. Annotations exist but are unverified. | Not a bug; runtime behavior is covered by the hermetic gate. | Platform Engineering | A type-confusion bug ships, or before opening the repo to outside contributors → add `mypy` to `lint.sh`. |
| L3 | **Bandit gates at `--severity-level medium`** — Low-severity findings are not enforced. The reviewed suppressions are **B608** (7 `db._set_clause` UPDATEs — column names from a fixed allowlist, values parameterized) and **B310** (3 `urlopen` calls — guarded by http(s)-only `base_url`), each carrying a per-line `# nosec <code>` with rationale. | Suppressions are injection-/SSRF-safe by construction and reviewed. (NB: there is **no B105** in the tree — an earlier note citing "B105 false positives" was inaccurate.) | SRE/Security | Re-confirm each `# nosec` stays justified whenever its surrounding code changes. |
| L4 | **Worker-token cipher is a hand-rolled HMAC-CTR construction** (`db.py`, stdlib-only constraint: encrypt-then-MAC, 16-byte random nonce, domain-separated keys, `compare_digest`). | Sound construction, not a bug; the stdlib-only rule forbids `cryptography`. Versioned by the `-v1` key-derivation marker. | SRE/Security | A crypto dependency becomes permitted → swap to AES-GCM via `cryptography` (migrate on the `-v1` marker). |
