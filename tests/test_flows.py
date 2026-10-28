import uuid

from tests.conftest import add_student, login, new_client, rand_phone

V1 = "/api/v1"


async def pay(c, student_id, amount, key=None, method="cash"):
    return await c.post(
        f"{V1}/fees/payments",
        json={"student_id": student_id, "amount": amount, "method": method},
        headers={"Idempotency-Key": key or uuid.uuid4().hex},
    )


async def test_login_wrong_code_then_right():
    async with new_client() as c:
        phone = rand_phone()
        r = await c.post(f"{V1}/auth/otp/request", json={"phone": phone})
        code = r.json()["dev_code"]
        bad = "000000" if code != "000000" else "111111"
        r = await c.post(f"{V1}/auth/otp/verify", json={"phone": phone, "code": bad})
        assert r.status_code == 400 and r.json()["code"] == "otp_invalid"
        r = await c.post(f"{V1}/auth/otp/verify", json={"phone": phone, "code": code})
        assert r.status_code == 200
        me = (await c.get(f"{V1}/auth/me")).json()
        assert me["institution"] is None


async def test_student_gets_current_month_due(owner):
    st = await add_student(owner)
    assert st["outstanding"] == 80000 and st["due_months"] == 1
    assert st["batch_names"] == ["SSC A"]


async def test_payment_is_idempotent_and_allocates_advance(owner):
    st = await add_student(owner)
    r1 = await pay(owner, st["id"], 240000, key="same-key-" + st["id"])
    assert r1.status_code == 201, r1.text
    body = r1.json()
    assert [a["amount"] for a in body["allocations"]] == [80000, 80000, 80000]  # this month + 2 advance
    assert body["outstanding_after"] == 0
    assert body["sms_status"] in ("queued", "held")
    r2 = await pay(owner, st["id"], 240000, key="same-key-" + st["id"])
    assert r2.json()["replayed"] is True and r2.json()["payment_id"] == body["payment_id"]
    ledger = (await owner.get(f"{V1}/fees/students/{st['id']}/ledger")).json()
    assert len(ledger["payments"]) == 1 and ledger["advance"] == 160000


async def test_partial_then_reverse(owner):
    st = await add_student(owner)
    r = await pay(owner, st["id"], 30000)
    assert r.json()["allocations"][0]["due_status"] == "partial"
    assert r.json()["outstanding_after"] == 50000
    rev = await owner.post(f"{V1}/fees/payments/{r.json()['payment_id']}/reverse", json={"reason": "wrong student"})
    assert rev.status_code == 200, rev.text
    again = await owner.post(f"{V1}/fees/payments/{r.json()['payment_id']}/reverse", json={"reason": "again"})
    assert again.status_code == 409
    ledger = (await owner.get(f"{V1}/fees/students/{st['id']}/ledger")).json()
    assert ledger["outstanding"] == 80000


async def test_discount_owner_only_and_waives(owner):
    st = await add_student(owner)
    due_id = (await owner.get(f"{V1}/fees/students/{st['id']}/ledger")).json()["dues"][0]["id"]
    r = await owner.post(f"{V1}/fees/dues/{due_id}/discount", json={"discount": 80000, "reason": "free student"})
    assert r.status_code == 200 and r.json()["status"] == "waived"


async def test_attendance_sync_idempotent_and_absent_sms(owner):
    a = await add_student(owner, "A")
    b = await add_student(owner, "B")
    from app.core.timeutil import today_local

    payload = {
        "id": str(uuid.uuid4()),
        "batch_id": owner.batch_id,
        "date": today_local().isoformat(),
        "records": [{"student_id": a["id"], "status": "present"}, {"student_id": b["id"], "status": "absent"}],
    }
    r1 = (await owner.post(f"{V1}/attendance/sync", json=payload)).json()
    r2 = (await owner.post(f"{V1}/attendance/sync", json=payload)).json()
    assert r1["session_id"] == r2["session_id"]
    assert (r1["present"], r1["absent"]) == (1, 1)
    assert r1["sms_queued"] == 1 and r2["sms_queued"] == 0
    # Correcting the absence cancels the pending SMS.
    payload["records"][1]["status"] = "present"
    await owner.post(f"{V1}/attendance/sync", json=payload)
    msgs = (await owner.get(f"{V1}/messages", params={"student_id": b["id"]})).json()
    assert [m["status"] for m in msgs if m["template_key"] == "absent"] == ["cancelled"]


async def test_tenant_isolation(owner):
    st = await add_student(owner)
    async with new_client() as other:
        await login(other, rand_phone())
        await other.post(f"{V1}/institutions", json={"name": "Other Coaching"})
        assert (await other.get(f"{V1}/students/{st['id']}")).status_code == 404
        assert (await other.get(f"{V1}/students")).json()["total"] == 0
        r = await pay(other, st["id"], 80000)
        assert r.status_code == 404


async def test_assistant_permissions(owner):
    phone = rand_phone()
    r = await owner.post(f"{V1}/members", json={"phone": phone, "name": "Asst", "role": "assistant"})
    assert r.status_code == 201
    st = await add_student(owner)
    async with new_client() as asst:
        me = await login(asst, phone)
        assert me["role"] == "assistant"
        assert (await asst.get(f"{V1}/dashboard/teacher")).status_code == 403
        assert (await asst.post(f"{V1}/batches", json={"name": "X", "monthly_fee": 1})).status_code == 403
        r = await pay(asst, st["id"], 80000)
        assert r.status_code == 201
        # Assistant may reverse their own entry within 10 minutes.
        rev = await asst.post(f"{V1}/fees/payments/{r.json()['payment_id']}/reverse", json={"reason": "typo"})
        assert rev.status_code == 200


async def test_dashboard_numbers(owner):
    st = await add_student(owner)
    await add_student(owner, "Due Student")
    await pay(owner, st["id"], 80000)
    d = (await owner.get(f"{V1}/dashboard/teacher")).json()
    assert d["today_collected"] == 80000 and d["today_count"] == 1
    assert d["total_due"] == 80000 and d["due_students"] == 1
    assert d["month_rate"] == 50.0


async def test_dashboard_today_attendance(owner):
    a = await add_student(owner, "Here")
    b = await add_student(owner, "Away")
    from app.core.timeutil import today_local

    await owner.post(
        f"{V1}/attendance/sync",
        json={
            "batch_id": owner.batch_id,
            "date": today_local().isoformat(),
            "records": [{"student_id": a["id"], "status": "present"}, {"student_id": b["id"], "status": "absent"}],
        },
    )
    d = (await owner.get(f"{V1}/dashboard/teacher")).json()
    assert d["attendance_rate_today"] == 50.0
    # Absences are listed first in the live feed.
    assert [(m["name"], m["status"]) for m in d["live_marks"]] == [("Away", "absent"), ("Here", "present")]
    taken = [c for c in d["today_classes"] if c["batch_id"] == owner.batch_id]
    if taken:  # only when the test batch meets today
        assert taken[0]["absent_names"] == ["Away"]


async def test_portals_route_each_kind_of_login(owner):
    me = (await owner.get(f"{V1}/auth/me")).json()
    assert (me["portal"], me["home"]) == ("teacher", "/teacher")

    staff_phone, guardian_phone, student_phone = rand_phone(), rand_phone(), rand_phone()
    await owner.post(f"{V1}/members", json={"phone": staff_phone, "role": "assistant"})
    r = await owner.post(
        f"{V1}/students",
        json={"name": "Child", "phone": student_phone, "guardian_phone": guardian_phone, "enrollments": [{"batch_id": owner.batch_id}]},
    )
    child = r.json()

    async with new_client() as staff:
        me = await login(staff, staff_phone)
        assert (me["portal"], me["home"]) == ("staff", "/staff")
        d = (await staff.get(f"{V1}/dashboard/staff")).json()
        assert "cash" in d and "call_list" in d
        assert (await staff.get(f"{V1}/dashboard/teacher")).status_code == 403

    async with new_client() as guardian:
        me = await login(guardian, guardian_phone)
        assert (me["portal"], me["home"]) == ("guardian", "/guardian")
        d = (await guardian.get(f"{V1}/dashboard/guardian")).json()
        assert [c["student_id"] for c in d["children"]] == [child["id"]]
        assert d["children"][0]["outstanding"] == 80000
        # Guardians never reach the institution's own data.
        assert (await guardian.get(f"{V1}/students")).status_code == 403
        assert (await guardian.get(f"{V1}/dashboard/student")).status_code == 403

    async with new_client() as student:
        me = await login(student, student_phone)
        assert me["portal"] == "student"
        d = (await student.get(f"{V1}/dashboard/student")).json()
        assert d["children"][0]["name"] == "Child"

    async with new_client() as stranger:
        me = await login(stranger, rand_phone())
        assert (me["portal"], me["home"]) == (None, "/setup")


async def test_batch_members_and_attendance_rate(owner):
    a = await add_student(owner, "A")
    b = await add_student(owner, "B")
    await pay(owner, a["id"], 80000)
    await pay(owner, b["id"], 30000)
    from app.core.timeutil import today_local

    await owner.post(
        f"{V1}/attendance/sync",
        json={
            "id": str(uuid.uuid4()),
            "batch_id": owner.batch_id,
            "date": today_local().isoformat(),
            "records": [{"student_id": a["id"], "status": "present"}, {"student_id": b["id"], "status": "absent"}],
        },
    )
    batch = (await owner.get(f"{V1}/batches/{owner.batch_id}")).json()
    assert batch["month_attendance"] == 50 and batch["last_month_attendance"] is None
    assert batch["due_students"] == 1 and batch["month_due"] == 50000

    r = await owner.get(f"{V1}/batches/{owner.batch_id}/members")
    assert r.status_code == 200, r.text
    rows = {m["name"]: m for m in r.json()}
    assert (rows["A"]["month_status"], rows["A"]["attendance"]) == ("paid", 100)
    assert (rows["B"]["month_status"], rows["B"]["month_paid"], rows["B"]["attendance"]) == ("partial", 30000, 0)
    assert rows["B"]["month_amount"] == 80000 and rows["B"]["outstanding"] == 50000


async def test_student_list_extras_and_summary(owner):
    a = await add_student(owner, "A")
    b = await add_student(owner, "B")
    await pay(owner, b["id"], 30000)
    from app.core.timeutil import today_local

    await owner.post(
        f"{V1}/attendance/sync",
        json={
            "id": str(uuid.uuid4()),
            "batch_id": owner.batch_id,
            "date": today_local().isoformat(),
            "records": [{"student_id": a["id"], "status": "present"}, {"student_id": b["id"], "status": "absent"}],
        },
    )
    page = (await owner.get(f"{V1}/students")).json()
    rows = {s["name"]: s for s in page["items"]}
    assert (rows["A"]["partial"], rows["A"]["month_attendance"]) == (False, 100)
    assert (rows["B"]["partial"], rows["B"]["month_attendance"], rows["B"]["outstanding"]) == (True, 0, 50000)

    await owner.post(f"{V1}/students/{a['id']}/leave", json={})
    summary = (await owner.get(f"{V1}/students/summary")).json()
    assert summary == {"active": 1, "joined_this_month": 1, "with_due": 1, "left": 1}


async def test_fee_overview_and_receipt_switch(owner):
    a = await add_student(owner, "A")
    r = await owner.post(
        f"{V1}/fees/payments",
        json={"student_id": a["id"], "amount": 80000, "method": "bkash", "send_receipt": False},
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )
    assert r.status_code == 201 and r.json()["sms_status"] is None
    ov = (await owner.get(f"{V1}/fees/overview")).json()
    assert (ov["collected"], ov["billed"], ov["by_method"]) == (80000, 80000, {"bkash": 80000})
    assert (ov["today_amount"], ov["today_count"], ov["today_receipts"]) == (80000, 1, 0)
    pays = (await owner.get(f"{V1}/fees/payments")).json()
    assert pays[0]["batch_names"] == ["SSC A"]


async def test_notice_templates_audience_and_scheduled_notice(owner):
    a = await add_student(owner, "Tania")
    await owner.post(f"{V1}/students", json={"name": "NoPhone", "enrollments": [{"batch_id": owner.batch_id}]})
    tpls = (await owner.get(f"{V1}/messages/templates")).json()
    assert {"n_holiday", "absent"} <= {t["key"] for t in tpls}
    assert next(t for t in tpls if t["key"] == "absent")["kind"] == "auto"

    made = await owner.post(f"{V1}/messages/templates", json={"name": "সভা", "body_bn": "{student}-এর সভা {date}-এ"})
    assert made.status_code == 201 and made.json()["kind"] == "notice"
    assert (await owner.delete(f"{V1}/messages/templates/{made.json()['key']}")).status_code == 204
    assert (await owner.delete(f"{V1}/messages/templates/absent")).status_code == 409

    aud = (await owner.post(f"{V1}/messages/notice/audience", json={"batch_ids": [owner.batch_id]})).json()
    assert aud == {"students": 2, "guardians": 1, "missing_phone": 1}

    from datetime import UTC, datetime, timedelta

    later = (datetime.now(UTC) + timedelta(days=2)).isoformat()
    r = await owner.post(
        f"{V1}/messages/notice",
        json={"message": "{student} আসবে, {batch}", "batch_ids": [owner.batch_id], "title": "সভা", "send_at": later},
    )
    assert r.json() == {"queued": 1, "recipients": 1}
    msgs = (await owner.get(f"{V1}/messages", params={"kind": "notice"})).json()
    assert msgs[0]["body"].startswith("Tania আসবে, SSC A") and msgs[0]["status"] == "held"
    notices = (await owner.get(f"{V1}/messages/notices")).json()
    assert notices[0]["title"] == "সভা" and notices[0]["targets"] == ["SSC A"] and notices[0]["pending"] is True
    ov = (await owner.get(f"{V1}/messages/overview")).json()
    assert ov["scheduled_notices"] == 1 and ov["counts"]["notice"] == 1
    assert ov["counts"]["welcome"] == 1 and a["name"] == "Tania"
