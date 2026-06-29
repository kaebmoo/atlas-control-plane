# BYOK Key Injection (B5)

> TL;DR (ไทย): Atlas core **ไม่เก็บ model key เลย**. ตัวช่วยนี้ (option-b) เขียน key ของ
> ผู้ใช้ลงไฟล์ env/config ของ worker ปลายทางเพื่อให้ thClaws โหลดเอง แล้ว **audit** การ
> กระทำ (ใคร, worker ไหน, provider, เวลา) โดย **ไม่** log/เก็บ/คืนค่า key. ส่วน option-a
> (ส่ง key ไปยัง endpoint ของ thClaws ในอนาคต) นิยาม interface ไว้พร้อมเสียบเมื่อ thClaws
> รองรับ. รันด้วย `python3 -m atlas.byok` โดยอ่าน key จาก env (`ATLAS_BYOK_KEY`) ไม่ใช่ argument.

Customers bring their own model provider keys (BYOK). Atlas **must never store a model
key** in its database, logs, or API responses. This helper provides the boundary.

## Two paths

- **option-b — env injection (built now).** Write the key into the target worker's own
  env/config file so thClaws loads it at startup. Atlas writes the file and audits the
  action; it keeps nothing.
- **option-a — forward to thClaws (interface defined, not built).** When thClaws ships a
  save-key endpoint, Atlas would relay the key to the worker
  (`POST {worker}/agent/keys`, `{provider, key}` → `204`, key held by thClaws only) and
  audit the action — still storing nothing. `atlas/byok.py:forward_key_to_thclaws`
  documents the contract and raises `NotImplementedError` until the endpoint exists, so
  it drops in without guesswork. **Blocked on:** the thClaws team shipping the endpoint.

## Usage (option-b)

```bash
# The key is read from $ATLAS_BYOK_KEY, never passed as an argument (args leak in
# `ps`/shell history).
ATLAS_BYOK_KEY='sk-…' python3 -m atlas.byok \
  --worker reporter-1 --provider openai --config /etc/thclaws/reporter-1.env
```

Writes `OPENAI_API_KEY=…` into the target env file (created/kept `0600`) and records a
`byok.inject` audit entry. Default env vars: `openai→OPENAI_API_KEY`,
`anthropic→ANTHROPIC_API_KEY`, `google→GOOGLE_API_KEY`,
`azure→AZURE_OPENAI_API_KEY`; override with `--env-var`. Also callable as a library /
Fleet provisioning step: `atlas.byok.inject_worker_key(...)`.

## Guarantees (verified by `scripts/check_byok_helper.py`)

- The key is written to the **target worker's** file, never to Atlas state.
- The env file is `0600` (created atomically; never momentarily world-readable).
- The action is audited with actor, worker, provider, env var, and timestamp — **never
  the key value**.
- The key never appears in the return value, the audit API (`GET /api/audit`), or the
  Atlas database file.

## Out of scope

Atlas does not rotate, escrow, or validate provider keys, and never calls a provider
with them — the key belongs to the worker/thClaws layer. Key rotation is a re-injection.
