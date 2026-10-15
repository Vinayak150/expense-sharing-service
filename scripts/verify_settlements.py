"""Exercise six settlement workflows over TCP HTTP; creates fictional data without deleting rows."""

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import httpx


def check_plan(value, nets, pairwise_count):
    transfers = value["transfers"]
    assert value["currency"] == "INR" and value["payments_recorded"] is False
    assert value["strategy"] in ("netted_greedy", "pairwise", "component_netting")
    assert value["pairwise_transfer_count"] == pairwise_count
    assert value["suggested_transfer_count"] == len(transfers) <= pairwise_count
    assert value["transfers_avoided"] == pairwise_count - len(transfers) >= 0
    assert len(transfers) <= max(0, sum(n != 0 for n in nets.values()) - 1)
    assert value["total_suggested_transfer_paise"] == sum(t["amount_paise"] for t in transfers)
    assert [(t["from_user_id"], t["to_user_id"], t["amount_paise"]) for t in transfers] == sorted(
        (t["from_user_id"], t["to_user_id"], t["amount_paise"]) for t in transfers
    )
    assert len({(t["from_user_id"], t["to_user_id"]) for t in transfers}) == len(transfers)
    for t in transfers:
        assert t["from_user_id"] in nets and t["to_user_id"] in nets
        assert t["from_user_id"] != t["to_user_id"] and type(t["amount_paise"]) is int
        assert t["amount_paise"] > 0
        assert t["amount"] == f"{t['amount_paise'] // 100}.{t['amount_paise'] % 100:02d}"
    total = value["total_suggested_transfer_paise"]
    assert value["total_suggested_transfer_amount"] == f"{total // 100}.{total % 100:02d}"
    for u, net in nets.items():
        assert (
            net
            + sum(t["amount_paise"] for t in transfers if t["from_user_id"] == u)
            - sum(t["amount_paise"] for t in transfers if t["to_user_id"] == u)
            == 0
        )


def reference_state(users, history):
    nets = {
        u: sum(sum(s.values()) for p, s in history if p == u) - sum(s.get(u, 0) for _, s in history)
        for u in users
    }
    obligations = {
        (a, b): sum(s.get(a, 0) for p, s in history if p == b)
        for a in users
        for b in users
        if a != b
    }
    count = sum(amount > obligations[b, a] for (a, b), amount in obligations.items())
    return nets, count


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
        ids = [p["id"] for p in people]
        a, b, c, d = ids

        def group(name, members):
            g = post("/groups", {"name": name})["id"]
            for u in members:
                post(f"/groups/{g}/members", {"user_id": u})
            assert len(get(f"/groups/{g}/members")) == len(members)
            return g

        def expense(g, payer, amount):
            return post(
                "/expenses",
                {
                    "group_id": g,
                    "paid_by": payer,
                    "amount": amount,
                    "description": "Phase 4 live verification",
                },
            )

        def suggestion(g):
            return get(f"/groups/{g}/settlement-suggestions")

        def verified_preview(g):
            before = get(f"/groups/{g}/balances")
            history = get(f"/groups/{g}/expenses")
            result = suggestion(g)
            nets = {m["user_id"]: m["net_balance_paise"] for m in before["balances"]}
            check_plan(result, nets, len(before["debts"]))
            assert get(f"/groups/{g}/balances") == before
            assert get(f"/balances?group_id={g}") == before
            assert get(f"/groups/{g}/expenses") == history
            assert suggestion(g) == result
            return {"balances": before, "suggestions": result, "history": history}

        g1 = group("Phase 4 Dinner", ids[:3])
        expense(g1, a, "1200")
        one = verified_preview(g1)
        assert [
            (t["from_user_id"], t["to_user_id"], t["amount_paise"])
            for t in one["suggestions"]["transfers"]
        ] == [(b, a, 40000), (c, a, 40000)]

        g2 = group("Phase 4 Hotel and Taxi", ids[:3])
        expense(g2, a, "900")
        expense(g2, b, "300")
        two = verified_preview(g2)
        assert two["suggestions"]["strategy"] == "netted_greedy"
        assert two["suggestions"]["pairwise_transfer_count"] == 3
        assert two["suggestions"]["suggested_transfer_count"] == 2
        assert two["suggestions"]["transfers_avoided"] == 1
        assert two["suggestions"]["total_suggested_transfer_paise"] == 50000
        assert [t["amount_paise"] for t in two["suggestions"]["transfers"]] == [10000, 40000]
        assert verified_preview(g1) == one

        g3 = group("Phase 4 Circular offset", ids[:3])
        for payer in ids[:3]:
            expense(g3, payer, "300")
        three = verified_preview(g3)
        assert three["suggestions"]["transfers"] == []
        assert all(m["net_balance_paise"] == 0 for m in three["balances"]["balances"])

        g4 = group("Phase 4 Membership snapshot", ids[:3])
        old = expense(g4, a, "1200")
        post(f"/groups/{g4}/members", {"user_id": d})
        joined = verified_preview(g4)
        assert joined["balances"]["balances"][-1]["total_share_paise"] == 0
        later = expense(g4, d, "100")
        four = verified_preview(g4)
        assert four["history"] == [later, old]
        assert len(old["splits"]) == 3 and len(later["splits"]) == 4

        g5 = group("Phase 4 Pairwise fallback", ids)
        for payer, amount in [(a, "0.07"), (c, "0.03"), (c, "0.03"), (b, "0.02"), (b, "0.02")]:
            expense(g5, payer, amount)
        five = verified_preview(g5)
        assert [m["net_balance_paise"] for m in five["balances"]["balances"]] == [1, -2, 2, -1]
        assert five["suggestions"]["strategy"] == "pairwise"
        assert five["suggestions"]["pairwise_transfer_count"] == 2
        assert five["suggestions"]["suggested_transfer_count"] == 2
        assert [
            (t["from_user_id"], t["to_user_id"], t["amount_paise"])
            for t in five["suggestions"]["transfers"]
        ] == [(b, c, 2), (d, a, 1)]

        g6 = group("Phase 4 Concurrent preview", ids[:3])
        planned = [(ids[i % 3], i + 1) for i in range(20)]
        history = []
        possible = [reference_state(ids[:3], history)]
        for payer, amount in planned:
            shares = {
                u: sum(index % 3 == pos for index in range(amount)) for pos, u in enumerate(ids[:3])
            }
            history.append((payer, shares))
            possible.append(reference_state(ids[:3], history))
        start = Barrier(3, timeout=20)

        def writer():
            start.wait()
            return [
                expense(g6, payer, f"{amount // 100}.{amount % 100:02d}")
                for payer, amount in planned
            ]

        def reader():
            start.wait()
            return [suggestion(g6) for _ in range(20)]

        with ThreadPoolExecutor(max_workers=3) as pool:
            writes = pool.submit(writer)
            readers = [pool.submit(reader) for _ in range(2)]
            committed = writes.result(timeout=40)
            previews = [p for reader in readers for p in reader.result(timeout=40)]
        matches = []
        for preview in previews:
            valid = []
            for index, (nets, count) in enumerate(possible):
                try:
                    check_plan(preview, nets, count)
                except AssertionError:
                    continue
                valid.append(index)
            assert valid, "Preview did not match any complete committed ledger prefix"
            matches.append(valid)
        six = verified_preview(g6)
        assert six["balances"]["total_expenses_paise"] == sum(range(1, 21)) == 210
        check_plan(six["suggestions"], *possible[-1])
        assert len(six["history"]) == len(committed) == 20

        assert get("/health") == get("/health/ready") == {"status": "ok"}
        for path in ["/docs", "/redoc"]:
            assert client.get(path).status_code == 200
        schema = get("/openapi.json")
        assert schema["info"]["version"] == "0.5.0"
        assert len(schema["paths"]) == 11
        assert set(ids).issubset({u["id"] for u in get("/users")})
        return {
            "workflow_1_dinner": one,
            "workflow_2_hotel_taxi": two,
            "workflow_3_zero_nets": three,
            "workflow_4_membership": {"joined": joined, "later": four},
            "workflow_5_pairwise_fallback": five,
            "workflow_6_concurrent": {
                "final": six,
                "committed_expenses": 20,
                "previews": previews,
                "matching_committed_prefixes": matches,
            },
            "all_ten_business_operations_verified": True,
            "all_plans_independently_reconciled": True,
            "static_history_and_balances_unchanged_by_previews": True,
            "health_readiness_docs_openapi_verified": True,
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
        "PASS: six HTTP workflows; real pairwise fallback; 20 concurrent commits and 40 "
        "snapshot-consistent previews; exact reconciliation; history unchanged"
    )


if __name__ == "__main__":
    main()
