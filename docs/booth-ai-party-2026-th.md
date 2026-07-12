# Atlas @ Chiang Mai AI Party 2026 — สคริปต์ booth + พูด 5 นาที

งาน: **Chiang Mai AI Party 2026** · ธีม *"AI for Money"* · 18 ก.ค. 2026 · Maker Asia เชียงใหม่
รูปแบบ: show & tell — คนมา *ลองของจริง* ไม่ได้มานั่งฟังเลกเชอร์

แทร็กที่ Atlas เข้า: **AI Agent** (orchestration หลาย agent) + **Local AI** (รันบนเครื่องตัวเอง)
กลุ่มคนฟัง: maker / dev / ครีเอเตอร์ / คนเล่น local model — อยากเอา AI ไปหาเงิน
**ไม่ใช่** ผู้บริหารรัฐ → เก็บ NT business plan ไว้ อย่าเอาขึ้นเวทีนี้

---

## ประโยคเปิด (hook — 15 วินาทีแรก)

> "ทุกคนเล่น AI agent ตัวเดียวกันหมดใช่ไหมครับ ผมจะให้ดูว่าถ้าเอา agent **หลายตัว**
> มาทำงานต่อกันเป็นสายการผลิต — บนเครื่องเราเอง ข้อมูลไม่ออกไปไหน คุมงบไม่ให้บานปลาย —
> มันทำอะไรได้"

ป้ายหน้า booth (บรรทัดเดียว):
> **สั่ง AI หลายตัวทำงานต่อกัน — บนเครื่องคุณเอง คุมงบ ตรวจย้อนหลังได้**

---

## สคริปต์พูด 5 นาที

**0:00–0:30 — Hook + Atlas คืออะไร**
พูดประโยคเปิดด้านบน แล้วต่อ:
Atlas คือ **control plane** ตัวสั่งงาน ไม่ใช่ตัวรัน AI เอง มันคุย worker (thClaws) หลายตัว
หลายเครื่อง จาก dashboard เดียว ผ่าน HTTP API ธรรมดา — routing, สตรีมผลสด, เก็บประวัติ,
ต่อ agent เป็น workflow

**0:30–3:00 — Demo สด (พระเอกของงาน)** — News Desk ที่ **http://127.0.0.1:8090** (ตัว PoC จริงของ
booth: รัน `poc/booth_demo/setup.py` ครั้งเดียว แล้วรัน `poc/booth_demo/app.py` — ขั้นตอนเต็มแบบ
5 terminal ดูที่ [`poc/booth_demo/README.md`](../poc/booth_demo/README.md))
1. โชว์หน้า index ว่า worker 2 ตัว online (reporter + anchor)
2. กรอกหัวข้อในหน้า `/news` → **ให้คนดู job ของ reporter สตรีมสด** คู่กับ Atlas dashboard ข้างๆ
3. reporter เสร็จ → ไฟล์ขึ้นให้ดาวน์โหลด พร้อมแบนเนอร์บอกว่า Atlas **ส่งไฟล์เข้าเวิร์กสเปซของ
   anchor แล้ว** → anchor อ่านไฟล์พวกนั้นแล้วเขียนบทข่าว → ชี้ให้เห็น job แม่/ลูกผูกกัน
4. รันหยุดรอที่ approval gate → กด Approve ก่อนเผยแพร่ → นี่คือจุดต่างจาก chatbot

*(Fallback ถ้า PoC ของ booth มีปัญหา: flow แบบ dashboard ทั่วไปใน `demo-script.md` โชว์แนวคิด
routing/handoff/approval gate เดียวกันได้ ทีละสเต็ปในแดชบอร์ด)*

**3:00–4:00 — ทำไมเรื่องนี้ = เงิน** (ผูกกับธีมงาน)
- **ประหยัด:** รันบนเครื่องตัวเอง / local model → ข้อมูลไม่รั่ว ไม่มีบิล cloud ต่อ token
- **คุมไม่ให้เจ๊ง:** budget cap + human approval + audit log ทุกขั้น → ไม่ตื่นมาเจอบิลบานปลาย
  (Gartner บอก 40% ของโปรเจกต์ agentic จะถูกยกเลิกเพราะ *คุมไม่ได้* ไม่ใช่โมเดลไม่เก่ง)
- **ทำเงิน:** เพราะมี governance ในตัว เอา workflow ไป **ขายเป็นบริการ** ให้องค์กรได้จริง
  (รับเรื่องร้องเรียน, ร่างเอกสาร, สรุป→ผลิตคอนเทนต์) — ไม่ใช่ของเล่นที่ตายกลางทาง

**4:00–4:30 — ปิด + CTA**
- open source, รันด้วย **Python stdlib + SQLite** ไม่ต้องมี database แยก, air-gapped ได้
- model-agnostic — เสียบ local / open-source / commercial model ก็ได้
- "มาลองสั่ง workflow เองที่ booth ได้เลย / สแกน QR ไปดู repo"

**ประโยคปิด:**
> "chatbot ตอบได้ แต่ Atlas ทำให้ AI **ทำงานจริงเป็นทีม** ได้ — บนเครื่องคุณ คุมงบ ตรวจได้"

---

## Checklist ตั้ง booth

- [ ] จอใหญ่โชว์ dashboard + ปล่อย live stream วิ่งค้างไว้ = แม่เหล็กดูดคน
- [ ] รัน 2 worker + Atlas + booth PoC (`poc/booth_demo/setup.py` แล้วค่อย `app.py`) ไว้ล่วงหน้า —
      **แต่ละ worker ต้องแยกคนละ working directory** (ดู `poc/booth_demo/README.md`)
- [ ] **Fallback เน็ตงานห่วย:** ต่อ worker กับ **local model** ไว้ ไม่งั้น cloud call ค้างกลาง demo
      — และอัด GIF demo สำรองไว้เปิดถ้าสดพัง
- [ ] ซ้อมให้จบใน 3 นาที (คนที่ booth ไม่รอนาน)
- [ ] QR code → GitHub repo ติดไว้ที่ป้าย
- [ ] เตรียมประโยคเดียวตอบ "มันคืออะไร" (= ป้ายด้านบน) พูดซ้ำได้ทั้งวันไม่เพลีย

## สิ่งที่ควร *ไม่* ทำ

- อย่าเปิดด้วยสถาปัตยกรรม / นิยาม node / policy — เปิดด้วย demo สด
- อย่ายัด NT sovereign gov deck (฿1.2bn, GDCC, procurement) — ผิดกลุ่มคนฟัง
  พูดได้แค่ 1 ประโยคเป็นของแถม: *"ตัวนี้ต่อยอดไปขายองค์กร/ภาครัฐที่ต้องการ audit ได้ด้วย"*
- อย่าเคลม sovereignty เกินจริง — ถ้ามีคนถาม พูดว่า "ลด jurisdiction exposure + คุมได้เอง"
  ไม่ใช่ "ไม่มี dependency ใด ๆ เลย"
