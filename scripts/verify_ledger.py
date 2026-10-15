"""Eight real HTTP Phase 5 workflows. Appends fictional data; never deletes prior rows."""

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import httpx


def workflow(base):
    with httpx.Client(base_url=base, timeout=20) as client:

        def get(path):
            r = client.get(path)
            assert r.status_code == 200, (path, r.text)
            return r.json()

        def post(path, data, key=None, expected=201):
            r = client.post(
                path, json=data, headers={} if key is None else {"Idempotency-Key": key}
            )
            assert r.status_code == expected, (path, r.status_code, r.text)
            return r.json()

        users = [post("/users", {"name": name})["id"] for name in ["Mohit", "Rahul", "Ankit"]]
        a, b, c = users

        def group(name, members=users):
            g = post("/groups", {"name": name})["id"]
            for u in members:
                post(f"/groups/{g}/members", {"user_id": u})
            assert len(get(f"/groups/{g}/members")) == len(members)
            return g

        def expense(g, payer, amount, splits=None):
            body = {
                "group_id": g,
                "paid_by": payer,
                "amount": amount,
                "description": "Phase 5 HTTP",
            }
            if splits is not None:
                body["splits"] = [{"user_id": u, "amount": v} for u, v in splits]
            return post("/expenses", body)

        def payment(g, sender, receiver, amount, key, expected=201):
            return post(
                f"/groups/{g}/settlements",
                {"from_user_id": sender, "to_user_id": receiver, "amount": amount},
                key,
                expected,
            )

        def state(g):
            value = get(f"/groups/{g}/balances")
            assert get(f"/balances?group_id={g}") == value
            nets = {m["user_id"]: m["net_balance_paise"] for m in value["balances"]}
            assert sum(nets.values()) == 0
            assert value["total_expenses_paise"] == sum(
                m["total_paid_paise"] for m in value["balances"]
            )
            for m in value["balances"]:
                u = m["user_id"]
                assert (
                    nets[u]
                    == m["total_paid_paise"]
                    - m["total_share_paise"]
                    + m["total_settlements_sent_paise"]
                    - m["total_settlements_received_paise"]
                )
                assert nets[u] == sum(
                    d["amount_paise"] for d in value["debts"] if d["to_user_id"] == u
                ) - sum(d["amount_paise"] for d in value["debts"] if d["from_user_id"] == u)
            if value["debt_basis"] == "net_after_repayments" and not any(nets.values()):
                assert value["debts"] == []
            preview = get(f"/groups/{g}/settlement-suggestions")
            assert preview["debt_basis"] == value["debt_basis"]
            assert (
                preview["suggested_transfer_count"]
                == len(preview["transfers"])
                <= len(value["debts"])
            )
            assert (
                preview["transfers_avoided"] == len(value["debts"]) - len(preview["transfers"]) >= 0
            )
            assert preview["suggestions_recorded"] is False
            for u, net in nets.items():
                assert (
                    net
                    + sum(t["amount_paise"] for t in preview["transfers"] if t["from_user_id"] == u)
                    - sum(t["amount_paise"] for t in preview["transfers"] if t["to_user_id"] == u)
                    == 0
                )
            return {
                "balances": value,
                "suggestions": preview,
                "settlements": get(f"/groups/{g}/settlements"),
            }

        evidence = {}
        g = group("A Custom all")
        e = expense(g, a, "1000", [(a, "500"), (b, "300"), (c, "200")])
        v = state(g)
        assert [m["net_balance_paise"] for m in v["balances"]["balances"]] == [
            50000,
            -30000,
            -20000,
        ]
        evidence["A_custom"] = {"expense": e, **v}
        g = group("B Payer excluded")
        e = expense(g, a, "1000", [(b, "600"), (c, "400")])
        v = state(g)
        assert v["balances"]["balances"][0]["total_share_paise"] == 0
        assert [m["net_balance_paise"] for m in v["balances"]["balances"]] == [
            100000,
            -60000,
            -40000,
        ]
        evidence["B_excluded"] = {"expense": e, **v}
        g = group("C Dinner repaid")
        e = expense(g, a, "1200")
        payment(g, b, a, "400", "rahul")
        payment(g, c, a, "400", "ankit")
        v = state(g)
        assert v["balances"]["debts"] == v["suggestions"]["transfers"] == []
        assert get(f"/groups/{g}/expenses") == [e]
        evidence["C_full_equal"] = {"expense": e, **v}
        g = group("D Multilateral repaid")
        e1 = expense(g, a, "900")
        e2 = expense(g, b, "300")
        before = state(g)
        assert len(before["balances"]["debts"]) == 3
        payment(g, b, a, "100", "taxi-offset")
        partial = state(g)
        payment(g, c, a, "400", "hotel-final")
        v = state(g)
        assert all(m["net_balance_paise"] == 0 for m in v["balances"]["balances"])
        assert v["balances"]["debts"] == v["suggestions"]["transfers"] == []
        assert get(f"/groups/{g}/expenses") == [e2, e1]
        evidence["D_multilateral"] = {"before": before, "partial": partial, "final": v}
        g = group("E Partial")
        expense(g, a, "1200")
        payment(g, b, a, "150", "partial")
        partial = state(g)
        assert partial["balances"]["balances"][1]["net_balance_paise"] == -25000
        payment(g, b, a, "250", "final")
        v = state(g)
        assert v["balances"]["balances"][1]["net_balance_paise"] == 0
        evidence["E_partial"] = {"partial": partial, "final": v}
        g = group("F Retry")
        expense(g, a, "1200")
        first = payment(g, b, a, "400", "same-key")
        replay = payment(g, b, a, "4e2", "same-key", 200)
        assert first == replay
        assert len(get(f"/groups/{g}/settlements")) == 1
        payment(g, b, a, "399", "same-key", 409)
        evidence["F_idempotent"] = {"first": first, "replay": replay, **state(g)}
        g = group("G Competing payments")
        expense(g, a, "1200")
        start = Barrier(2, timeout=20)

        def compete(key):
            start.wait()
            r = client.post(
                f"/groups/{g}/settlements",
                json={"from_user_id": b, "to_user_id": a, "amount": "300"},
                headers={"Idempotency-Key": key},
            )
            return {"status_code": r.status_code, "body": r.json()}

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(compete, k) for k in ["race-a", "race-b"]]
            results = [f.result(timeout=25) for f in futures]
        assert sorted(r["status_code"] for r in results) == [201, 422]
        v = state(g)
        assert (
            len(v["settlements"]) == 1
            and v["balances"]["balances"][1]["net_balance_paise"] == -10000
        )
        evidence["G_overpayment"] = {"responses": results, **v}
        g1 = group("H Isolated one", [a, b])
        g2 = group("H Isolated two")
        expense(g1, a, "1000")
        expense(g2, b, "300")
        old = state(g2)
        payment(g1, b, a, "100", "shared")
        assert state(g2) == old
        payment(g2, a, b, "100", "shared")
        evidence["H_isolation"] = {"first": state(g1), "second": state(g2)}
        assert set(users).issubset({u["id"] for u in get("/users")})
        assert get("/health") == get("/health/ready") == {"status": "ok"}
        for path in ["/docs", "/redoc"]:
            assert client.get(path).status_code == 200
        schema = get("/openapi.json")
        assert schema["info"]["version"] == "0.5.0" and len(schema["paths"]) == 11
        return evidence, schema


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--openapi-output", type=Path)
    args = parser.parse_args()
    result, schema = workflow(args.base_url)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    if args.openapi_output:
        args.openapi_output.write_text(json.dumps(schema, indent=2) + "\n")
    print(
        "PASS: eight TCP HTTP workflows, custom/payer-excluded splits, "
        "full and partial repayments, "
        "multilateral zero-debt reconciliation, retry replay, concurrent overpayment and isolation"
    )


if __name__ == "__main__":
    main()
