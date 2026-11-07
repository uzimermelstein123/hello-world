import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from fuel_cap_engine.service import QuoteRequest, run_quote, run_sweep
from fuel_cap_engine.webapp import SNAPSHOT_SLOT, Handler, export_snapshot, render_page


@pytest.fixture(scope="module")
def quote():
    return run_quote(QuoteRequest())


def test_quote_payload_is_consistent(quote):
    assert quote["status"] == "ok"
    k, c = quote["kpis"], quote["curve"]
    assert k["fee_dollars"] == pytest.approx(k["hedge_cost"] + k["min_profit"] + k["fee_rounding"])
    assert k["fee_pct"] == pytest.approx(k["fee_dollars"] / quote["profile"]["monthly_spend"] * 100)
    # per-position payouts add up to the total payout curve
    for i in range(0, len(c["prices"]), 25):
        assert sum(v[i] for v in c["by_position"].values()) == pytest.approx(c["payout"][i], abs=0.02)
    assert all(n >= k["min_profit"] - 1e-6 for n in c["net"])
    assert quote["audit"]["all_passed"]
    assert {o["market_id"] for o in quote["orders"]} == set(c["by_position"])


def test_infeasible_payload_suggests_a_cap():
    q = run_quote(QuoteRequest(include_cme=False, monthly_spend=20_000_000, promised_cap=0.125))
    assert q["status"] == "infeasible"
    assert q["report"]["shortfalls"] and q["suggested_cap"]["cap"] > 0.125


def test_sweep_defaults_around_current_cap():
    sw = run_sweep(QuoteRequest())
    caps = [x["cap"] for x in sw["quotes"]]
    assert sw["current_cap"] in caps and caps == sorted(caps)


def test_snapshot_embedding_escapes_script_close(tmp_path):
    html = render_page({"quote": {"name": "</script><b>x"}}, standalone=False)
    assert SNAPSHOT_SLOT not in html and "<\\/script><b>x" in html
    out = tmp_path / "snap.html"
    export_snapshot(out)
    text = out.read_text()
    assert text.startswith("<!doctype html>") and '"status":"ok"' in text


def test_http_routes():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        assert b"Fuel Cap Desk" in urllib.request.urlopen(base + "/").read()
        d = json.loads(urllib.request.urlopen(base + "/api/defaults").read())
        assert d["promised_cap"] == pytest.approx(0.13)
        req = urllib.request.Request(base + "/api/quote", data=json.dumps({"monthly_spend": -5}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(req)
        assert ei.value.code == 400 and "monthly_spend" in json.loads(ei.value.read())["error"]
    finally:
        srv.shutdown()
