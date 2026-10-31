"""Admissions: office admission with fee choices, QR links, family join requests, seats and fee plans."""

import uuid

from tests.conftest import login, new_client, rand_phone


async def _batch(c, **extra) -> dict:
    body = {"name": f"HSC ICT {uuid.uuid4().hex[:4]}", "days": ["sun"], "monthly_fee": 80000, **extra}
    r = await c.post("/api/v1/batches", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _key() -> dict:
    return {"Idempotency-Key": uuid.uuid4().hex}


async def _ledger(c, student_id: str) -> dict:
    r = await c.get(f"/api/v1/fees/students/{student_id}/ledger")
    assert r.status_code == 200, r.text
    return r.json()


async def test_office_admission_charges_admission_and_takes_cash(owner):
    b = await _batch(owner, admission_fee=100000)
    key = _key()
    body = {
        "batch_id": b["id"], "name": "Rafi", "guardian_phone": rand_phone(),
        "payment": {"method": "cash", "amount": 180000},
    }
    r = await owner.post("/api/v1/admissions", json=body, headers=key)
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["charged"] == 180000 and out["outstanding"] == 0
    kinds = {a["kind"] for a in out["receipt"]["allocations"]}
    assert kinds == {"admission", "monthly"}

    # A retried tap returns the same student, not a second one.
    again = await owner.post("/api/v1/admissions", json=body, headers=key)
    assert again.status_code == 201 and again.json()["student_id"] == out["student_id"]

    dues = (await _ledger(owner, out["student_id"]))["dues"]
    adm = next(d for d in dues if d["kind"] == "admission")
    assert adm["label"] == "ভর্তি ফি" and adm["status"] == "paid"


async def test_office_admission_waive_and_pay_later(owner):
    b = await _batch(owner, admission_fee=150000)
    r = await owner.post(
        "/api/v1/admissions",
        json={"batch_id": b["id"], "name": "Mim", "guardian_phone": rand_phone(), "charge_admission": False,
              "fee_discount": 20000, "discount_note": "বোন আছে"},
        headers=_key(),
    )
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["receipt"] is None
    assert out["outstanding"] == 60000  # no admission fee, ৳800 - ৳200 for the first month
    dues = (await _ledger(owner, out["student_id"]))["dues"]
    assert [d["kind"] for d in dues] == ["monthly"]


async def test_course_fee_in_instalments(owner):
    b = await _batch(owner, fee_plan="course", course_fee=600000, installments=3, monthly_fee=0)
    r = await owner.post(
        "/api/v1/admissions", json={"batch_id": b["id"], "name": "Tanha", "guardian_phone": rand_phone()}, headers=_key()
    )
    assert r.status_code == 201, r.text
    dues = (await _ledger(owner, r.json()["student_id"]))["dues"]
    inst = sorted((d for d in dues if d["kind"] == "installment"), key=lambda d: d["month"])
    assert [d["amount"] for d in inst] == [200000, 200000, 200000]
    assert inst[0]["label"] == "কিস্তি ১/৩"
    assert r.json()["outstanding"] == 200000  # only this month's instalment is due now

    # Course batches get no monthly dues from the generator.
    gen = await owner.post("/api/v1/fees/generate", json={})
    assert gen.status_code == 200, gen.text
    dues = (await _ledger(owner, r.json()["student_id"]))["dues"]
    assert not [d for d in dues if d["kind"] == "monthly"]


async def test_qr_join_request_then_admit_with_offer(owner):
    b = await _batch(owner, admission_fee=100000, capacity=2, location="Station Road")
    r = await owner.post(
        "/api/v1/admissions/links",
        json={"title": "HSC ICT 2027", "batch_ids": [b["id"]], "offer_kind": "percent", "offer_value": 20},
    )
    assert r.status_code == 201, r.text
    code = r.json()["code"]

    async with new_client() as fam:
        page = await fam.get(f"/api/v1/public/join/{code}", params={"count": True})
        assert page.status_code == 200, page.text
        p = page.json()
        assert p["batches"][0]["seats_left"] == 2 and p["offer"]["value"] == 20

        # Applying needs the phone OTP; a brand-new number then lands in the family portal.
        assert (await fam.post(f"/api/v1/public/join/{code}/apply", json={"batch_id": b["id"], "student_name": "X"})).status_code == 401
        guardian = rand_phone()
        me = await login(fam, guardian)
        assert me["home"] == "/setup"
        body = {"batch_id": b["id"], "student_name": "Sakib", "school": "Zilla School"}
        r = await fam.post(f"/api/v1/public/join/{code}/apply", json=body)
        assert r.status_code == 201, r.text
        req = r.json()
        assert req["status"] == "pending" and req["admission_fee"] == 80000  # 20% off ৳1000
        assert (await fam.post(f"/api/v1/public/join/{code}/apply", json=body)).json()["id"] == req["id"]
        assert (await fam.get("/api/v1/auth/me")).json()["home"] == "/guardian"

        fam_view = (await fam.get("/api/v1/dashboard/family")).json()
        assert fam_view["places"][0]["requests"][0]["status"] == "pending"

    # The pending request holds a seat.
    seats = {x["id"]: x for x in (await owner.get("/api/v1/admissions/batches")).json()}
    assert seats[b["id"]]["held"] == 1 and seats[b["id"]]["seats_left"] == 1

    pending = (await owner.get("/api/v1/admissions/requests")).json()
    assert [x["id"] for x in pending] == [req["id"]]
    r = await owner.post(
        f"/api/v1/admissions/requests/{req['id']}/admit",
        json={"payment": {"method": "cash", "amount": 160000}},
        headers=_key(),
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["charged"] == 160000 and out["outstanding"] == 0  # ৳800 offer price + ৳800 month
    links = (await owner.get("/api/v1/admissions/links")).json()
    assert links[0]["admitted"] == 1 and links[0]["pending"] == 0 and links[0]["scan_count"] == 1

    async with new_client() as fam:
        await login(fam, guardian)
        view = (await fam.get("/api/v1/dashboard/family")).json()
        kids = view["places"][0]["children"]
        assert kids[0]["name"] == "Sakib" and kids[0]["batches"][0]["location"] == "Station Road"


async def test_last_seat_and_closed_link(owner):
    b = await _batch(owner, capacity=1)
    code = (await owner.post("/api/v1/admissions/links", json={"title": "Class 10", "batch_ids": [b["id"]]})).json()["code"]
    async with new_client() as a, new_client() as z:
        await login(a, rand_phone())
        await login(z, rand_phone())
        assert (await a.post(f"/api/v1/public/join/{code}/apply", json={"batch_id": b["id"], "student_name": "A"})).status_code == 201
        full = await z.post(f"/api/v1/public/join/{code}/apply", json={"batch_id": b["id"], "student_name": "Z"})
        assert full.status_code == 409 and full.json()["code"] == "batch_full"

    # bKash needs a TrxID; a paused link refuses new requests.
    link_id = (await owner.get("/api/v1/admissions/links")).json()[0]["id"]
    await owner.patch(f"/api/v1/admissions/links/{link_id}", json={"is_active": False})
    async with new_client() as c:
        await login(c, rand_phone())
        r = await c.post(f"/api/v1/public/join/{code}/apply", json={"batch_id": b["id"], "student_name": "B"})
        assert r.status_code == 409 and r.json()["code"] == "link_closed"


async def test_assistant_cannot_waive(owner):
    b = await _batch(owner, admission_fee=100000)
    assistant = rand_phone()
    r = await owner.post("/api/v1/members", json={"phone": assistant, "name": "Helper", "role": "assistant"})
    assert r.status_code == 201, r.text
    async with new_client() as c:
        await login(c, assistant)
        body = {"batch_id": b["id"], "name": "Nila", "guardian_phone": rand_phone()}
        r = await c.post("/api/v1/admissions", json={**body, "charge_admission": False}, headers=_key())
        assert r.status_code == 403
        r = await c.post("/api/v1/admissions", json=body, headers=_key())
        assert r.status_code == 201 and r.json()["charged"] == 180000


async def test_guardian_lookup_finds_siblings(owner):
    g = rand_phone()
    r = await owner.post(
        "/api/v1/students",
        json={"name": "Big Sis", "guardian_phone": g, "guardian_name": "Karim", "enrollments": [{"batch_id": owner.batch_id}]},
    )
    assert r.status_code == 201, r.text
    found = (await owner.get("/api/v1/admissions/guardian", params={"phone": g})).json()
    assert found["guardian_name"] == "Karim" and found["children"][0]["name"] == "Big Sis"


async def test_batch_starting_later_charges_admission_now_and_first_month_ahead(owner):
    from app.core.timeutil import add_months, current_month

    later = add_months(current_month(), 2)
    b = await _batch(owner, admission_fee=100000, starts_on=later.isoformat())
    r = await owner.post(
        "/api/v1/admissions",
        json={"batch_id": b["id"], "name": "Early", "guardian_phone": rand_phone(), "payment": {"method": "cash", "amount": 180000}},
        headers=_key(),
    )
    assert r.status_code == 201, r.text
    dues = {d["kind"]: d for d in (await _ledger(owner, r.json()["student_id"]))["dues"]}
    assert dues["admission"]["month"] == current_month().isoformat()
    assert dues["monthly"]["month"] == later.isoformat() and dues["monthly"]["status"] == "paid"


async def test_overview_and_request_tabs(owner):
    b = await _batch(owner, admission_fee=100000, capacity=10)
    r = await owner.post(
        "/api/v1/admissions",
        json={"batch_id": b["id"], "name": "Rafiq", "guardian_phone": rand_phone(), "guardian_relation": "father",
              "payment": {"method": "cash", "amount": 180000}},
        headers=_key(),
    )
    assert r.status_code == 201, r.text
    st = (await owner.get(f"/api/v1/students/{r.json()['student_id']}")).json()
    assert st["guardian_relation"] == "father"
    o = (await owner.get("/api/v1/admissions/overview")).json()
    assert o["admitted_month"] >= 1 and o["admission_fees_month"] >= 100000
    rows = (await owner.get("/api/v1/admissions/admitted")).json()
    assert rows[0]["name"] == "Rafiq" and rows[0]["paid"] == 180000 and rows[0]["batch_name"] == b["name"]
    assert rows[0]["student_code"].isdigit() and len(rows[0]["student_code"]) == 6 and rows[0]["from_qr"] is False
    assert o["free_seats"] >= 9
    assert (await owner.get("/api/v1/admissions/requests", params={"status": "closed"})).status_code == 200
