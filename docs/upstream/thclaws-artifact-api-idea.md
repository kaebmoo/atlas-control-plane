# Draft: thClaws Ideas discussion post

> Target: https://github.com/thClaws/thClaws/discussions/categories/ideas
> Status: draft, not yet posted
>
> Note before posting: keep the auth wording neutral (as below). If the missing
> auth on `/workspace/sync/*` looks unintended rather than documented behavior,
> report that part privately via the repo Security tab first.

---

**Title:** Idea: authenticated, job-scoped artifact API for external orchestrators

## Use case

We are building a small control plane that dispatches jobs to multiple thClaws
workers over `/agent/run` (serve mode, Bearer `THCLAWS_API_TOKEN`). A common
pattern is a multi-agent pipeline: worker A (coder) produces files, worker B
(reviewer) needs those exact bytes — not a text description of them.

Today the only way to move files is the workspace sync surface
(`/workspace/sync/manifest`, `/export`, `/push`, ...). It works, but it was
clearly designed for workspace mirroring inside a trusted network, and an
external orchestrator hits a few gaps:

1. **Auth story.** `/agent/run` and `/v1/*` are protected by the Bearer token,
   while the sync routes are intended for trusted-network / tunnel / reverse-proxy
   deployments. For an orchestrator that only holds the API token, there is no
   supported way to use sync. We currently treat sync as disabled unless the
   operator asserts a tunnel or ForwardAuth is in place — which is a deployment
   assertion, not something we can verify.

2. **Job scoping.** Sync operates on the whole workspace. The orchestrator has
   to trust that the paths it requests are really the output of the job it just
   ran; there is no `job → files` association on the server side.

3. **Atomicity.** `manifest` then `export` are two requests; files can change
   in between. We re-hash after download and reject mismatches, but artifact
   IDs fixed at job completion would remove the race entirely.

## Proposal

**Tier 1 (small):** harden the existing sync surface, opt-in, so nothing
changes for current deployments:

- require the existing Bearer token on `/workspace/sync/*`
  (e.g. `THCLAWS_SYNC_REQUIRE_AUTH=1`)
- accept an explicit `workspace_dir` and validate it resolves under the
  workspace root
- restrict `push` to a configured prefix allowlist
  (e.g. only under `incoming/`), instead of whole-workspace write
- server-side caps on file count and total bytes per request

This alone would let orchestrators use `export`/`push` safely without a
tunnel.

**Tier 2 (better long-term):** a job-scoped artifact API under the already
Bearer-protected `/v1` surface:

```
GET  /v1/jobs/{job_id}/artifacts                     # manifest: path, size, sha256
GET  /v1/jobs/{job_id}/artifacts/{artifact_id}       # bytes
POST /v1/jobs/{job_id}/inputs                        # stage files before/with dispatch
```

Artifacts would be snapshotted (or at least hashed) when the job completes, so
the manifest is stable. Which files count as artifacts could be declared in the
`/agent/run` request (e.g. `collect_files: ["reports/*.pdf"]`) or by the agent
itself at the end of the run.

Tier 2 solves auth, scoping, and the manifest/export race in one shape, and
gives least-privilege access (per-job, not whole-workspace).

Happy to provide more detail on the orchestrator side, test a branch, or help
with a PR if there is interest in either tier.

---

# ฉบับภาษาไทย (สำหรับโพสต์ใน Facebook group)

**หัวข้อ:** เสนอไอเดีย thClaws: artifact API แบบมี auth และผูกกับ job สำหรับ external orchestrator

## Use case

ผมกำลังสร้าง control plane ตัวเล็ก ๆ ที่ dispatch งานไปยัง thClaws worker
หลายเครื่องผ่าน `/agent/run` (serve mode, Bearer `THCLAWS_API_TOKEN`)
pattern ที่ใช้บ่อยคือ multi-agent pipeline: worker A (coder) สร้างไฟล์
แล้ว worker B (reviewer) ต้องได้ไฟล์จริง ๆ ไม่ใช่แค่ข้อความบรรยายว่ามีไฟล์อะไร

ตอนนี้ทางเดียวที่จะย้ายไฟล์คือ workspace sync surface
(`/workspace/sync/manifest`, `/export`, `/push`, ...) ซึ่งใช้งานได้
แต่ออกแบบมาสำหรับ mirror workspace ใน trusted network
พอเอามาใช้กับ external orchestrator จะเจอช่องว่างสามข้อ:

1. **เรื่อง auth** — `/agent/run` และ `/v1/*` ป้องกันด้วย Bearer token
   แต่ sync routes ตั้งใจให้ใช้หลัง trusted network / tunnel / reverse proxy
   orchestrator ที่ถือแค่ API token จึงไม่มีวิธีใช้ sync แบบที่รองรับอย่างเป็นทางการ
   ตอนนี้ผมต้องปิด sync ไว้เป็นค่าเริ่มต้น จนกว่า operator จะยืนยันว่ามี tunnel
   หรือ ForwardAuth — ซึ่งเป็นการยืนยันระดับ deployment ที่ระบบตรวจสอบเองไม่ได้

2. **ไม่ผูกกับ job** — sync ทำงานระดับทั้ง workspace ฝั่ง orchestrator
   ต้องเชื่อเอาเองว่า path ที่ขอคือ output ของ job ที่เพิ่งรันจริง
   เพราะ server ไม่มีข้อมูลว่า job ไหนสร้างไฟล์อะไร

3. **ไม่ atomic** — `manifest` กับ `export` เป็นสอง request แยกกัน
   ไฟล์อาจถูกแก้ระหว่างนั้น ตอนนี้ผมต้อง re-hash หลังดาวน์โหลดแล้ว reject
   ถ้าไม่ตรง แต่ถ้ามี artifact ID ที่ fix ตอน job เสร็จ race นี้จะหายไปเลย

## ข้อเสนอ

**Tier 1 (เล็ก):** เสริมความแข็งแรงให้ sync surface เดิมแบบ opt-in
ไม่กระทบ deployment ที่มีอยู่:

- บังคับ Bearer token เดิมกับ `/workspace/sync/*`
  (เช่น `THCLAWS_SYNC_REQUIRE_AUTH=1`)
- รับ `workspace_dir` แบบระบุชัด และตรวจว่า resolve แล้วอยู่ใต้ workspace root
- จำกัด `push` ให้เขียนได้เฉพาะ prefix ที่กำหนด
  (เช่น เฉพาะใต้ `incoming/`) แทนการเขียนได้ทั้ง workspace
- เพดานฝั่ง server สำหรับจำนวนไฟล์และขนาดรวมต่อ request

แค่นี้ orchestrator ก็ใช้ `export`/`push` ได้อย่างปลอดภัยโดยไม่ต้องมี tunnel

**Tier 2 (ดีกว่าในระยะยาว):** artifact API ที่ผูกกับ job
อยู่ใต้ `/v1` ซึ่งมี Bearer ป้องกันอยู่แล้ว:

```
GET  /v1/jobs/{job_id}/artifacts                     # manifest: path, size, sha256
GET  /v1/jobs/{job_id}/artifacts/{artifact_id}       # ตัวไฟล์
POST /v1/jobs/{job_id}/inputs                        # ส่งไฟล์เข้าก่อน/พร้อม dispatch
```

Artifact จะถูก snapshot (หรืออย่างน้อย hash) ตอน job เสร็จ ทำให้ manifest นิ่ง
ส่วนไฟล์ไหนนับเป็น artifact อาจประกาศใน request `/agent/run`
(เช่น `collect_files: ["reports/*.pdf"]`) หรือให้ agent ประกาศเองตอนจบ run

Tier 2 แก้ทั้ง auth, job scoping และ race ระหว่าง manifest/export ในคราวเดียว
และให้สิทธิ์แบบ least privilege (ต่อ job ไม่ใช่ทั้ง workspace)

ยินดีให้รายละเอียดฝั่ง orchestrator เพิ่ม ช่วยทดสอบ branch
หรือช่วยทำ PR ถ้าสนใจ tier ไหนก็ตามครับ
