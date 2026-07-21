// Front-office C++ analytics engine (hot path).
// Weighted-average-cost position keeping with realized-P&L tracking,
// mirroring fo/models/portfolio.py::Position so results reconcile
// exactly against the Python implementation.
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>
#include <string>
#include <unordered_map>
#include <vector>
#include <cmath>

namespace py = pybind11;

// One position's running state.
struct PositionState {
    double net_qty = 0.0;
    double avg_cost = 0.0;
    double realized_pnl = 0.0;
};

// Apply one signed fill at `price` using weighted-average cost.
// Same rules as the Python Position.apply_fill: same-direction moves
// the average; opposite-direction realizes P&L and can flip through 0.
static void apply_fill(PositionState& p, double signed_qty, double price) {
    if (signed_qty == 0.0) return;

    bool same_dir = (p.net_qty == 0.0) ||
                    ((p.net_qty > 0.0) == (signed_qty > 0.0));
    if (same_dir) {
        double total = p.net_qty + signed_qty;
        p.avg_cost = (std::fabs(p.net_qty) * p.avg_cost +
                      std::fabs(signed_qty) * price) / std::fabs(total);
        p.net_qty = total;
        return;
    }

    double closing = std::min(std::fabs(signed_qty), std::fabs(p.net_qty));
    double direction = (p.net_qty > 0.0) ? 1.0 : -1.0;
    p.realized_pnl += closing * (price - p.avg_cost) * direction;
    p.net_qty += signed_qty;

    if (std::fabs(p.net_qty) < 1e-9) {
        p.net_qty = 0.0;
        p.avg_cost = 0.0;
    } else if ((p.net_qty > 0.0) != (direction > 0.0)) {
        p.avg_cost = price;  // flipped through zero: remainder opens here
    }
}

// Replay a batch of fills, keyed by (client_id, instrument_id).
// Input: parallel vectors (columnar — cheap to pass from Python).
// Output: dict "client|instrument" -> {net_qty, avg_cost, realized_pnl}.
py::dict replay_positions(
    const std::vector<std::string>& client_ids,
    const std::vector<std::string>& instrument_ids,
    const std::vector<double>& signed_qtys,
    const std::vector<double>& prices)
{
    const size_t n = client_ids.size();
    std::unordered_map<std::string, PositionState> book;
    book.reserve(n);

    for (size_t i = 0; i < n; ++i) {
        std::string key = client_ids[i] + "|" + instrument_ids[i];
        apply_fill(book[key], signed_qtys[i], prices[i]);
    }

    py::dict out;
    for (const auto& kv : book) {
        py::dict row;
        row["net_qty"] = kv.second.net_qty;
        row["avg_cost"] = kv.second.avg_cost;
        row["realized_pnl"] = kv.second.realized_pnl;
        out[py::str(kv.first)] = row;
    }
    return out;
}

PYBIND11_MODULE(fo_engine, m) {
    m.doc() = "Front-office C++ analytics engine (position/P&L hot path)";
    m.def("add", [](int a, int b) { return a + b; });  // keep smoke test
    m.def("replay_positions", &replay_positions,
          "Replay signed fills into WAC positions with realized P&L",
          py::arg("client_ids"), py::arg("instrument_ids"),
          py::arg("signed_qtys"), py::arg("prices"));
}