"""Benchmark: C++ engine vs Python position replay on the same data."""
import time

from fo.config import get_settings
from fo.db.database import connect
from fo.engine.positions import replay_from_db


def python_replay(conn):
    """Pure-Python equivalent of the C++ hot loop, for timing comparison."""
    import json
    from fo.models.enums import AssetClass
    from fo.models.portfolio import Position

    events = conn.execute(
        "SELECT event_type, payload FROM trade_events ORDER BY event_id"
    ).fetchall()
    book, trades = {}, {}
    for ev in events:
        p = json.loads(ev["payload"])
        tid = p["trade_id"]
        if ev["event_type"] == "NEW":
            signed = p["quantity"] if p["side"] == "BUY" else -p["quantity"]
            rec = {"client": p["client_id"], "inst": p["instrument_id"],
                   "signed": signed, "price": p["price"]}
            trades[tid] = rec
            key = (rec["client"], rec["inst"])
            book.setdefault(key, Position(rec["inst"], AssetClass.EQUITY)).apply_fill(
                rec["signed"], rec["price"])
        elif ev["event_type"] == "AMEND":
            rec = trades[tid]
            key = (rec["client"], rec["inst"])
            book[key].apply_fill(-rec["signed"], rec["price"])
            ch = p.get("changes", {})
            if "quantity" in ch:
                rec["signed"] = (1.0 if rec["signed"] >= 0 else -1.0) * ch["quantity"]
            if "price" in ch:
                rec["price"] = ch["price"]
            book[key].apply_fill(rec["signed"], rec["price"])
        else:
            rec = trades[tid]
            book[(rec["client"], rec["inst"])].apply_fill(-rec["signed"], rec["price"])
    return book


def main():
    import json
    conn = connect(get_settings().db_path)
    events = conn.execute(
        "SELECT event_type, payload FROM trade_events ORDER BY event_id"
    ).fetchall()

    # Parse ONCE into flat arrays (this is I/O + parsing, not the hot path).
    trades, cids, iids, sqtys, pxs = {}, [], [], [], []
    def emit(c, i, s, p):
        cids.append(c); iids.append(i); sqtys.append(s); pxs.append(p)
    for ev in events:
        p = json.loads(ev["payload"]); tid = p["trade_id"]
        if ev["event_type"] == "NEW":
            s = p["quantity"] if p["side"] == "BUY" else -p["quantity"]
            rec = {"c": p["client_id"], "i": p["instrument_id"], "s": s, "p": p["price"]}
            trades[tid] = rec; emit(rec["c"], rec["i"], rec["s"], rec["p"])
        elif ev["event_type"] == "AMEND":
            rec = trades[tid]; emit(rec["c"], rec["i"], -rec["s"], rec["p"])
            ch = p.get("changes", {})
            if "quantity" in ch: rec["s"] = (1.0 if rec["s"] >= 0 else -1.0) * ch["quantity"]
            if "price" in ch: rec["p"] = ch["price"]
            emit(rec["c"], rec["i"], rec["s"], rec["p"])
        else:
            rec = trades[tid]; emit(rec["c"], rec["i"], -rec["s"], rec["p"])

    n, runs = len(cids), 500

    # Pure-Python replay of the pre-parsed arrays.
    def py_loop():
        book = {}
        for k in range(n):
            key = cids[k] + "|" + iids[k]
            st = book.setdefault(key, [0.0, 0.0, 0.0])  # net, avg, realized
            sq, px = sqtys[k], pxs[k]
            if sq == 0: continue
            same = st[0] == 0 or (st[0] > 0) == (sq > 0)
            if same:
                tot = st[0] + sq
                st[1] = (abs(st[0])*st[1] + abs(sq)*px) / abs(tot); st[0] = tot
            else:
                closing = min(abs(sq), abs(st[0])); d = 1.0 if st[0] > 0 else -1.0
                st[2] += closing * (px - st[1]) * d; st[0] += sq
                if abs(st[0]) < 1e-9: st[0] = 0.0; st[1] = 0.0
                elif (st[0] > 0) != (d > 0): st[1] = px
        return book

    import fo_engine
    t = time.perf_counter()
    for _ in range(runs): py_loop()
    py_time = (time.perf_counter() - t) / runs

    t = time.perf_counter()
    for _ in range(runs):
        fo_engine.replay_positions(cids, iids, sqtys, pxs)
    cpp_time = (time.perf_counter() - t) / runs

    print(f"fills per replay  : {n}")
    print(f"python loop       : {py_time*1000:.3f} ms/run")
    print(f"c++ (fo_engine)   : {cpp_time*1000:.3f} ms/run")
    print(f"speedup           : {py_time/cpp_time:.1f}x")

if __name__ == "__main__":
    main()
