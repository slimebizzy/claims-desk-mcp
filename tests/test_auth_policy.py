"""Authorization, policy and approval records."""
import time

from app import config, store
from app.auth import mint
from conftest import token

APPROVAL = "io.github.slimebizzy/approvalId"
GET = {"claim_id": "CL-1042"}


def setr(amount, key, claim="CL-1042", reason="Inspection found the roof needs full replacement"):
    return {"claim_id": claim, "amount": amount, "reason": reason, "idempotency_key": key}


def approve(client, request_id, who="priya.nair", approve=True):
    tok = client.post("/console/api/dev-login", json={"user": who}).json()["token"]
    return client.post(f"/console/api/requests/{request_id}/decision", json={"approve": approve, "note": "ok"},
                       headers={"Authorization": "Bearer " + tok})


# --- token checks -------------------------------------------------------------

def test_no_token_is_401_with_resource_metadata(rpc):
    r = rpc("claims", "tools/list")
    assert r.status_code == 401
    assert "resource_metadata=" in r.headers["www-authenticate"]


def test_wrong_issuer_is_rejected(rpc):
    bad = mint("dana.ruiz", config.resource_url("claims"), issuer="https://auth.someone-else.example")
    assert rpc("claims", "tools/list", tok=bad).status_code == 401


def test_forged_signature_is_rejected(rpc):
    bad = mint("dana.ruiz", config.resource_url("claims"), secret="not-the-real-key-but-long-enough-000000")
    assert rpc("claims", "tools/list", tok=bad).status_code == 401


def test_expired_token_is_rejected(rpc):
    r = rpc("claims", "tools/list", tok=token("dana.ruiz", ttl_s=-10))
    assert r.status_code == 401 and "expired" in r.json()["error_description"].lower()


def test_read_token_cannot_reach_the_write_surface(rpc):
    """A token minted for the read-only server fails audience validation on the
    reserves server before any handler runs."""
    read_tok = token("dana.ruiz", "read")
    r = rpc("claims", "tools/call", {"name": "claim.set_reserve", "arguments": setr(20000, "k-aud-0001")}, tok=read_tok)
    assert r.status_code == 401 and "audience" in r.json()["error_description"]
    assert store.get_claim(store.connect(), "CL-1042")["reserve"] == 18000


def test_missing_scope_is_403_insufficient_scope(rpc):
    tok = token("copilot-svc", "claims")
    r = rpc("claims", "tools/call", {"name": "claim.set_reserve", "arguments": setr(20000, "k-scope-01")}, tok=tok)
    assert r.status_code == 403 and r.json()["error"] == "insufficient_scope"
    assert 'scope="claims:write"' in r.headers["www-authenticate"]


# --- policy -------------------------------------------------------------------

def test_within_authority_applies_directly(rpc):
    res = rpc.call("claims", "claim.set_reserve", setr(22000, "k-within-1"), token("dana.ruiz"))
    assert res["structuredContent"]["status"] == "updated"


def test_catastrophe_raise_applies_to_hail_but_not_under_fraud_review(rpc):
    # CL-1042 is hail in a declared catastrophe: Level 1 authority is $50,000 (§2.3).
    assert rpc.call("claims", "claim.set_reserve", setr(45000, "k-cat-0001"),
                    token("dana.ruiz"))["structuredContent"]["status"] == "updated"
    # CL-1103 is under SIU review: the raise does not apply, so the §2.1 limit holds.
    res = rpc.call("claims", "claim.set_reserve", setr(30000, "k-siu-0001", claim="CL-1103",
                   reason="Recovered vehicle total loss valuation came back higher"), token("dana.ruiz"))
    assert res["structuredContent"]["status"] == "approval_required"


def test_not_assigned_is_denied(rpc):
    res = rpc.call("claims", "claim.set_reserve", setr(1000, "k-assign-1", claim="CL-1077"), token("dana.ruiz"))
    assert res["isError"] and res["structuredContent"]["status"] == "denied"


def test_big_change_needs_a_file_note(rpc):
    res = rpc.call("claims", "claim.set_reserve", setr(9500 * 2, "k-note-001", claim="CL-1103", reason="higher"),
                   token("dana.ruiz"))
    assert res["structuredContent"]["status"] == "invalid" and "§2.2" in res["structuredContent"]["error"]


def test_masked_contact_details_for_unassigned_readers(rpc):
    mine = rpc.call("claims", "claim.get", GET, token("dana.ruiz"))["structuredContent"]["claimant"]
    theirs = rpc.call("claims", "claim.get", GET, token("sam.okafor"))["structuredContent"]["claimant"]
    assert mine["phone"] == "512-555-0142" and theirs["phone"] != mine["phone"]


# --- approvals ----------------------------------------------------------------

def _request(rpc, amount=62000, key="k-apr-0001"):
    res = rpc.call("claims", "claim.set_reserve", setr(amount, key), token("dana.ruiz"))["structuredContent"]
    assert res["status"] == "approval_required"
    return res["request_id"]


def _approved(rpc, client, amount=62000):
    rid = _request(rpc, amount)
    assert approve(client, rid).status_code == 200
    st = rpc.call("claims", "approval.status", {"request_id": rid}, token("dana.ruiz"))["structuredContent"]
    return rid, st["approval_id"]


def test_full_approval_flow(rpc, client):
    _, apr = _approved(rpc, client)
    res = rpc.call("claims", "claim.set_reserve", setr(62000, "k-apr-0002"), token("dana.ruiz"), meta={APPROVAL: apr})
    assert res["structuredContent"]["status"] == "updated"
    assert res["structuredContent"]["approval_id"] == apr


def test_changed_argument_cannot_reuse_approval(rpc, client):
    _, apr = _approved(rpc, client)
    res = rpc.call("claims", "claim.set_reserve", setr(62001, "k-apr-0003"), token("dana.ruiz"), meta={APPROVAL: apr})
    assert res["structuredContent"] == {"status": "denied", "error": "Arguments differ from what was approved"}
    assert store.get_claim(store.connect(), "CL-1042")["reserve"] == 18000


def test_approval_is_single_use(rpc, client):
    _, apr = _approved(rpc, client)
    rpc.call("claims", "claim.set_reserve", setr(62000, "k-apr-0004"), token("dana.ruiz"), meta={APPROVAL: apr})
    res = rpc.call("claims", "claim.set_reserve", setr(62000, "k-apr-0005"), token("dana.ruiz"), meta={APPROVAL: apr})
    assert res["structuredContent"]["error"] == "Approval was already used"


def test_expired_approval_is_refused(rpc, client, monkeypatch):
    _, apr = _approved(rpc, client)
    # Move the clock, not the record: editing the stored expiry would break its signature.
    later = time.time() + config.APPROVAL_TTL_S + 1
    monkeypatch.setattr(store, "now", lambda: later)
    res = rpc.call("claims", "claim.set_reserve", setr(62000, "k-apr-0006"), token("dana.ruiz"), meta={APPROVAL: apr})
    assert res["structuredContent"]["error"] == "Approval expired"


def test_tampered_approval_record_fails_signature(rpc, client):
    _, apr = _approved(rpc, client)
    store.connect().execute("UPDATE approvals SET digest='sha256:00' WHERE id=?", (apr,))
    res = rpc.call("claims", "claim.set_reserve", setr(62000, "k-apr-0007"), token("dana.ruiz"), meta={APPROVAL: apr})
    assert res["structuredContent"]["error"] == "Approval signature is invalid"


def test_someone_elses_approval_is_refused(rpc, client):
    _, apr = _approved(rpc, client)
    # Sam has no claim to use it on, but even on his own claim the actor binding fails first.
    res = rpc.call("claims", "claim.set_reserve", setr(150000, "k-apr-0008", claim="CL-1077"), token("sam.okafor"),
                   meta={APPROVAL: apr})
    assert res["structuredContent"]["error"] == "Approval was granted to a different user"


def test_supervisor_cannot_approve_above_their_authority(rpc, client):
    res = rpc.call("claims", "claim.set_reserve", setr(285000, "k-big-0001", claim="CL-1131",
                   reason="Contractor estimate came in higher after attic inspection"), token("sam.okafor"))
    rid = res["structuredContent"]["request_id"]
    r = approve(client, rid, who="priya.nair")
    assert r.status_code == 409 and "Regional Claims Manager" in r.json()["error"]
    assert approve(client, rid, who="lee.chen").status_code == 200


def test_denied_request_produces_no_approval(rpc, client):
    rid = _request(rpc)
    approve(client, rid, approve=False)
    st = rpc.call("claims", "approval.status", {"request_id": rid}, token("dana.ruiz"))["structuredContent"]
    assert st["status"] == "denied" and "approval_id" not in st


def test_approval_status_hides_other_peoples_requests(rpc):
    rid = _request(rpc)
    res = rpc.call("claims", "approval.status", {"request_id": rid}, token("sam.okafor"))
    assert res["structuredContent"]["status"] == "not_found"


def test_asking_twice_reuses_the_pending_request(rpc):
    assert _request(rpc, key="k-twice-01") == _request(rpc, key="k-twice-02")


# --- idempotency --------------------------------------------------------------

def test_idempotent_retry_returns_the_first_result(rpc):
    a = rpc.call("claims", "claim.set_reserve", setr(21000, "k-idem-001"), token("dana.ruiz"))["structuredContent"]
    b = rpc.call("claims", "claim.set_reserve", setr(21000, "k-idem-001"), token("dana.ruiz"))["structuredContent"]
    assert b["replayed"] and {k: v for k, v in b.items() if k != "replayed"} == a
    hist = store.connect().execute("SELECT COUNT(*) FROM reserve_history").fetchone()[0]
    assert hist == 1


def test_idempotency_key_reuse_with_new_arguments_is_rejected(rpc):
    rpc.call("claims", "claim.set_reserve", setr(21000, "k-idem-002"), token("dana.ruiz"))
    res = rpc.call("claims", "claim.set_reserve", setr(22000, "k-idem-002"), token("dana.ruiz"))
    assert res["structuredContent"]["status"] == "rejected"


# --- audit ----------------------------------------------------------------------

def test_audit_redacts_notes_and_contact_details(rpc):
    rpc.call("claims", "claim.get", GET, token("dana.ruiz"))
    rpc.call("claims", "claim.set_reserve", setr(21000, "k-audit-01", reason="Call Marisol at 512-555-0142 about roof"),
             token("dana.ruiz"))
    dump = " ".join(str(dict(r)) for r in store.connect().execute("SELECT * FROM audit"))
    assert "512-555-0142" not in dump and "Marisol" not in dump
    row = store.connect().execute("SELECT * FROM audit WHERE tool='claim.set_reserve'").fetchone()
    assert row["actor"] == "dana.ruiz" and row["issuer"] == config.TRUSTED_ISSUER
    assert row["audience"] == config.resource_url("claims") and row["decision"] == "allow"


def test_trace_context_is_recorded(rpc):
    tp = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    rpc("claims", "tools/list", tok=token("dana.ruiz"), headers={"traceparent": tp})
    row = store.connect().execute("SELECT trace_id FROM audit WHERE method='tools/list'").fetchone()
    assert row["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
