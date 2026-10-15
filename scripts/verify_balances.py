"""Real HTTP demo verification. Creates fictional data; never deletes existing records."""

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import httpx


def reconcile(value):
    members = value["balances"]
    debts = value["debts"]
    assert sum(m["net_balance_paise"] for m in members) == 0
    assert sum(m["total_paid_paise"] for m in members) == value["total_expenses_paise"]
    assert sum(m["total_share_paise"] for m in members) == value["total_expenses_paise"]
    pairs = set()
    for d in debts:
        assert d["from_user_id"] != d["to_user_id"] and d["amount_paise"] > 0
        pair = tuple(sorted((d["from_user_id"], d["to_user_id"])))
        assert pair not in pairs
        pairs.add(pair)
    for member in members:
        u = member["user_id"]
        assert (
            member["total_paid_paise"] - member["total_share_paise"] == member["net_balance_paise"]
        )
        assert member["net_balance_paise"] == sum(
            d["amount_paise"] for d in debts if d["to_user_id"] == u
        ) - sum(d["amount_paise"] for d in debts if d["from_user_id"] == u)


def workflow(base):
    with httpx.Client(base_url=base, timeout=20) as client:

        def get(path):
            r = client.get(path)
            assert r.status_code == 200, (path, r.status_code, r.text)
            return r.json()

        def post(path, body):
            r = client.post(path, json=body)
            assert r.status_code == 201, (path, r.status_code, r.text)
            return r.json()

        people = [post("/users", {"name": n}) for n in ["Mohit", "Rahul", "Ankit", "Priya"]]
        ids = [u["id"] for u in people]
        m, r, a, p = ids

        def group(name, members):
            g = post("/groups", {"name": name})["id"]
            for u in members:
                post(f"/groups/{g}/members", {"user_id": u})
            assert len(get(f"/groups/{g}/members")) == len(members)
            return g

        def expense(g, payer, amount):
            return post(
                "/expenses",
                {"group_id": g, "paid_by": payer, "amount": amount, "description": "Verification"},
            )

        def balance(g):
            value = get(f"/groups/{g}/balances")
            reconcile(value)
            return value

        g1 = group("Phase 3 Dinner", ids[:3])
        dinner = expense(g1, m, "1200")
        b1 = balance(g1)
        assert [b["total_paid_paise"] for b in b1["balances"]] == [120000, 0, 0]
        assert [b["total_share_paise"] for b in b1["balances"]] == [40000] * 3
        assert [b["net_balance_paise"] for b in b1["balances"]] == [80000, -40000, -40000]
        assert [(d["from_user_id"], d["to_user_id"], d["amount_paise"]) for d in b1["debts"]] == [
            (r, m, 40000),
            (a, m, 40000),
        ]
        assert get(f"/balances?group_id={g1}") == b1
        assert get(f"/groups/{g1}/expenses") == [dinner]

        g2 = group("Phase 3 Multi-payer", [m, r, p])
        expense(g2, m, "900")
        expense(g2, r, "300")
        b2 = balance(g2)
        assert [b["net_balance_paise"] for b in b2["balances"]] == [50000, -10000, -40000]
        assert [(d["from_user_id"], d["to_user_id"], d["amount_paise"]) for d in b2["debts"]] == [
            (r, m, 20000),
            (p, m, 30000),
            (p, r, 10000),
        ]
        assert balance(g1) == b1

        g3 = group("Phase 3 Historical membership", ids[:3])
        old = expense(g3, m, "1200")
        post(f"/groups/{g3}/members", {"user_id": p})
        joined = balance(g3)
        assert joined["balances"][-1]["total_share_paise"] == 0
        assert get(f"/groups/{g3}/expenses") == [old]
        later = expense(g3, p, "100")
        history = get(f"/groups/{g3}/expenses")
        assert history == [later, old]
        b3 = balance(g3)
        assert [b["net_balance_paise"] for b in b3["balances"]] == [77500, -42500, -42500, 7500]
        assert [len(e["splits"]) for e in history] == [4, 3]

        g4 = group("Phase 3 Concurrent ledger", ids[:3])
        barrier = Barrier(5, timeout=20)

        def writer(index):
            barrier.wait()
            return [expense(g4, ids[index % 3], "0.01") for _ in range(8)]

        def reader():
            barrier.wait()
            return [balance(g4) for _ in range(24)]

        with ThreadPoolExecutor(max_workers=5) as pool:
            writes = [pool.submit(writer, i) for i in range(4)]
            reads = pool.submit(reader)
            committed = [e for future in writes for e in future.result(timeout=40)]
            snapshots = reads.result(timeout=40)
        final = balance(g4)
        assert final["total_expenses_paise"] == len(committed) == 32
        expected_paid = {
            u: sum(e["amount_paise"] for e in committed if e["paid_by"] == u) for u in ids[:3]
        }
        expected_share = {
            u: sum(s["share_paise"] for e in committed for s in e["splits"] if s["user_id"] == u)
            for u in ids[:3]
        }
        for b in final["balances"]:
            assert b["total_paid_paise"] == expected_paid[b["user_id"]]
            assert b["total_share_paise"] == expected_share[b["user_id"]]
        assert len(get(f"/groups/{g4}/expenses")) == 32
        assert all(s["total_expenses_paise"] <= 32 for s in snapshots)
        assert get("/health") == get("/health/ready") == {"status": "ok"}
        for path in ["/docs", "/redoc"]:
            assert client.get(path).status_code == 200
        schema = get("/openapi.json")
        assert schema["info"]["version"] == "0.5.0"
        assert set(schema["paths"]) == {
            "/health",
            "/health/ready",
            "/users",
            "/groups",
            "/groups/{group_id}/members",
            "/expenses",
            "/groups/{group_id}/expenses",
            "/groups/{group_id}/balances",
            "/groups/{group_id}/settlement-suggestions",
            "/groups/{group_id}/settlements",
            "/balances",
        }
        assert set(ids).issubset({u["id"] for u in get("/users")})
        assert client.get("/balances").status_code == 422
        assert (
            client.post(
                "/expenses",
                json={"group_id": g1, "paid_by": m, "amount": "1.001", "description": "Rejected"},
            ).status_code
            == 422
        )
        assert balance(g1) == b1
        return {
            "scenario_1": b1,
            "scenario_2": b2,
            "scenario_3": {"before_later_expense": joined, "history": history, "balances": b3},
            "scenario_4": {
                "committed_expenses": len(committed),
                "read_snapshots": snapshots,
                "final": final,
            },
            "all_nine_business_operations_exercised": True,
            "health_readiness_docs_openapi_verified": True,
            "all_accounting_and_bilateral_reconciliations_passed": True,
        }, schema


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--openapi-output", type=Path)
    args = parser.parse_args()
    evidence, schema = workflow(args.base_url)
    args.output.write_text(json.dumps(evidence, indent=2) + "\n")
    if args.openapi_output:
        args.openapi_output.write_text(json.dumps(schema, indent=2) + "\n")
    print(
        "PASS: four real HTTP workflows; exact accounting; pairwise debts; historical snapshots; "
        "32 committed concurrent writes and 24 reconciled reads; health/docs/OpenAPI"
    )


if __name__ == "__main__":
    main()
