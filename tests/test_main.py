"""
Automatic checks of the model, run from the project folder with: pytest

They solve the model on the input data in DATA/ and check that the accounting is consistent (profit, costs,
units, CO2) and that the main modeling choices behave as intended (constraints, lost sales, annualized CAPEX).
"""
import os

import pandas as pd
import pytest

from network_design import core, sensitivity

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "DATA")


@pytest.fixture(scope="module")
def params():
    return core.load_data(DATA_DIR, n_years=10, discount_rate=0.08)


@pytest.fixture(scope="module")
def base_demand(params):
    return core._demand_dict(params["demand"])


@pytest.fixture(scope="module")
def base_solution(params, base_demand):
    status, objective, y_sol, x_sol, w_sol = core.optimization_model(params, base_demand)
    assert status == "Optimal"
    breakdown = core.calculate_cost_breakdown(y_sol, x_sol, w_sol, params, base_demand)
    return objective, y_sol, x_sol, w_sol, breakdown


def _scaled(demand, factor):
    return {market: value * factor for market, value in demand.items()}


def test_capital_recovery_factor():
    assert core._capital_recovery_factor(0, 10) == pytest.approx(0.1)
    assert core._capital_recovery_factor(0.08, 10) == pytest.approx(0.149029, abs=1e-6)
    # The annuity repays the investment exactly: the present value of CRF paid for T years is 1
    rate, years = 0.08, 10
    crf = core._capital_recovery_factor(rate, years)
    assert sum(crf / (1 + rate) ** year for year in range(1, years + 1)) == pytest.approx(1)


def test_one_year_without_discounting_counts_capex_and_opex_in_full(params, base_demand):
    params_1y = core._set_param_value(core._set_param_value(params, "n_years", 1), "discount_rate", 0)
    warehouses = list(params["cap_wh"].index)
    unit = core._unit_costs(params_1y, list(params["cap_pl"].index), warehouses, list(base_demand))
    for k in warehouses:
        assert unit["fix"][k] == pytest.approx(params["fix_cost"].loc[k].iloc[0] + params["opex"].loc[k].iloc[0])


def test_breakdown_accounting_identities(params, base_solution):
    b = base_solution[-1]
    assert b[core.ROW_PROFIT] == pytest.approx(b[core.ROW_REVENUE] - b[core.ROW_TOTAL_COST])
    assert b[core.ROW_TOTAL_COST] == pytest.approx(
        b["5. PRODUCTION COST"] + b["6. TRANSPORT COST TO WAREHOUSES ONLY"]
        + b["7. TRANSPORT COST FROM WAREHOUSES ONLY"] + b["8. WAREHOUSE COST (ANNUALIZED CAPEX + OPEX)"]
        + b["11. INVENTORY COST ONLY"] + b[core.ROW_TOTAL_CO2_COST])
    assert b["4. LOGISTICS COST"] == pytest.approx(
        b["6. TRANSPORT COST TO WAREHOUSES ONLY"] + b["7. TRANSPORT COST FROM WAREHOUSES ONLY"]
        + b["8. WAREHOUSE COST (ANNUALIZED CAPEX + OPEX)"] + b["11. INVENTORY COST ONLY"])
    assert b["8. WAREHOUSE COST (ANNUALIZED CAPEX + OPEX)"] == pytest.approx(
        b["9. WAREHOUSE ANNUALIZED CAPEX ONLY"] + b["10. WAREHOUSE OPEX ONLY"])
    assert b[core.ROW_TOTAL_CO2_COST] == pytest.approx(
        b["13. CO2 COST - PRODUCTION ONLY"] + b["14. CO2 COST - TRANSPORT TO WAREHOUSES ONLY"]
        + b["15. CO2 COST - TRANSPORT FROM WAREHOUSES ONLY"] + b["16. CO2 COST - STORAGE ONLY"])
    # Units: revenue = price * units sold, demand = units sold + lost sales
    price = float(params["selling_price"].iloc[0, 0])
    assert b[core.ROW_REVENUE] == pytest.approx(price * b[core.ROW_UNITS_SOLD])
    assert b[core.ROW_DEMAND] == pytest.approx(b[core.ROW_UNITS_SOLD] + b[core.ROW_LOST_SALES])
    assert b[core.ROW_FILL_RATE] == pytest.approx(b[core.ROW_UNITS_SOLD] / b[core.ROW_DEMAND])
    assert b[core.ROW_COST_PER_UNIT] == pytest.approx(b[core.ROW_TOTAL_COST] / b[core.ROW_UNITS_SOLD])
    # CO2: offsetting cost = kg emitted * price per kg, total kg = sum of the sources
    offset_per_kg = float(params["offsetting_cost"].iloc[0, 0]) / 1000.0
    assert b[core.ROW_TOTAL_CO2_COST] == pytest.approx(b[core.ROW_CO2_EMISSIONS] * offset_per_kg)
    assert b[core.ROW_CO2_EMISSIONS] == pytest.approx(sum(b[row] for row in core.CO2_EMISSION_SOURCES.values()))
    assert b[core.ROW_CO2_PER_UNIT] == pytest.approx(b[core.ROW_CO2_EMISSIONS] / b[core.ROW_UNITS_SOLD])


def test_objective_excludes_only_the_inventory_cost(base_solution):
    objective, *_, b = base_solution
    assert objective == pytest.approx(b[core.ROW_PROFIT] + b["11. INVENTORY COST ONLY"])


def test_base_solution_respects_the_constraints(params, base_demand, base_solution):
    _, y_sol, x_sol, w_sol, _ = base_solution
    plants, warehouses = list(params["cap_pl"].index), list(params["cap_wh"].index)
    for j, demand in base_demand.items():
        assert sum(w_sol[(k, j)] for k in warehouses) <= demand + 1e-6
    for k in warehouses:
        inflow = sum(x_sol[(i, k)] for i in plants)
        assert inflow == pytest.approx(sum(w_sol[(k, j)] for j in base_demand), abs=1e-3)
        assert inflow <= params["cap_wh"].loc[k].iloc[0] * y_sol[k] + 1e-6
    for i in plants:
        assert sum(x_sol[(i, k)] for k in warehouses) <= params["cap_pl"].loc[i].iloc[0] + 1e-6


def test_base_demand_is_fully_served(base_solution):
    assert base_solution[-1][core.ROW_FILL_RATE] == pytest.approx(1)


def test_fixed_network_is_respected(params, base_demand):
    fixed = {"Mumbai": 0, "Kolkata": 1}
    status, _, y_sol, x_sol, _ = core.optimization_model(params, base_demand, fixed)
    assert status == "Optimal"
    assert {k: round(v) for k, v in y_sol.items()} == fixed
    assert sum(x_sol[(i, "Mumbai")] for i in params["cap_pl"].index) == pytest.approx(0)


def test_capacity_shortage_becomes_lost_sales(params, base_demand):
    # A single warehouse facing 20% more demand than the base case cannot serve it all: the model stays
    # feasible, sells up to the warehouse capacity and reports the rest as lost sales.
    demand = _scaled(base_demand, 1.2)
    status, y_sol, _, b = core._solve_with_breakdown(params, demand, {"Mumbai": 1, "Kolkata": 0})
    assert status == "Optimal"
    capacity = params["cap_wh"].loc["Mumbai"].iloc[0]
    assert sum(demand.values()) > capacity
    assert b[core.ROW_UNITS_SOLD] == pytest.approx(capacity)
    assert b[core.ROW_LOST_SALES] == pytest.approx(sum(demand.values()) - capacity)
    assert b[core.ROW_FILL_RATE] < 1


def test_nothing_is_sold_below_cost(params, base_demand):
    # With a selling price below the production cost every unit loses money: no warehouse opens, nothing is sold
    params_cheap = core._set_param_value(params, "selling_price", 1.0)
    status, y_sol, _, b = core._solve_with_breakdown(params_cheap, base_demand)
    assert status == "Optimal"
    assert all(round(v) == 0 for v in y_sol.values())
    assert b[core.ROW_UNITS_SOLD] == pytest.approx(0)
    assert b[core.ROW_PROFIT] == pytest.approx(0)


def test_parallel_solves_match_sequential(params, base_demand):
    # The same tasks (scenarios, a fixed network, a changed parameter, the base demand) solved one after the other
    # and by a pool of 2 processes give the same results, in the same order
    scenarios = core.generate_demand_scenarios(base_demand, n_scenarios=3)
    tasks = [((), 1, None), ((), 2, {"Mumbai": 1, "Kolkata": 0}), ((("selling_price", 3.0, None, None),), 3, None),
             ((), base_demand, None)]
    sequential = core._solve_many(tasks, params, scenarios, n_workers=1)
    try:
        parallel = core._solve_many(tasks, params, scenarios, n_workers=2)
    finally:
        core._shutdown_solver_pool()
    assert len(parallel) == len(tasks)
    for (status_s, y_s, x_s, b_s), (status_p, y_p, x_p, b_p) in zip(sequential, parallel):
        assert (status_s, y_s, x_s) == (status_p, y_p, x_p)
        pd.testing.assert_series_equal(b_s, b_p)


def test_set_param_value_leaves_the_original_unchanged(params):
    original = params["selling_price"].iloc[0, 0]
    changed = core._set_param_value(params, "selling_price", original * 2)
    assert params["selling_price"].iloc[0, 0] == original
    assert changed["selling_price"].iloc[0, 0] == pytest.approx(original * 2)


def test_co2_frontier_switch_prices_and_mix_thresholds(params, base_demand, tmp_path):
    # Cost-CO2 frontier on the base demand: the solver switches to each neighbor mix exactly at the slope between
    # the two mixes, and a local mix threshold (production cost, distribution to one market) changes the mix just
    # beyond it, not just before
    recommended = {"Mumbai": 1, "Kolkata": 1}
    scenarios = pd.DataFrame([base_demand])
    parameters = {"Production Cost Gothenburg": ("variable_pl_cost", "Gothenburg", None),
                  "Production Cost Shanghai": ("variable_pl_cost", "Shanghai", None),
                  "Distribution Mumbai to Delhi": ("freight_cost_wh_mkt", "Mumbai", "Delhi")}
    cases = [case for case in sensitivity.build_default_sensitivity_cases(params) if case["label"] in parameters]
    result = sensitivity.run_co2_frontier(params, scenarios, [0, 100, 150, 200, 500], ("Mumbai", "Kolkata"), cases,
                                          output_dir=str(tmp_path))
    margin = result["mix_margin"]
    assert len(margin) >= 1

    def kg(changes):
        p = params
        for key, value, row, col in changes:
            p = core._set_param_value(p, key, value, row=row, col=col)
        return core._solve_with_breakdown(p, base_demand, recommended)[3][core.ROW_CO2_EMISSIONS]

    current = kg([])
    for _, r in margin.iterrows():
        switch, neighbor = r["Switch at CO2 Price (EUR/ton CO2)"], r["Expected CO2 of the Neighbor (kg/year)"]
        beyond = switch * (0.999 if r["Direction"] == "More CO2" else 1.001)
        before = switch * (1.001 if r["Direction"] == "More CO2" else 0.999)
        assert kg([("offsetting_cost", beyond, None, None)]) == pytest.approx(neighbor, rel=1e-6)
        assert kg([("offsetting_cost", before, None, None)]) == pytest.approx(current, rel=1e-6)
    neighbors = margin.set_index("Direction")["Expected CO2 of the Neighbor (kg/year)"]
    thresholds = result["mix_thresholds"].set_index("Parameter")
    checked = set()
    for label, (key, row, col) in parameters.items():
        base_value = thresholds.loc[label, "Base Value"]
        for direction in margin["Direction"]:
            threshold = thresholds.loc[label, f"Threshold Toward the {direction} Mix"]
            if isinstance(threshold, str):
                continue
            step = threshold - base_value
            beyond = kg([(key, base_value + step * 1.01, row, col)])
            assert kg([(key, base_value + step * 0.99, row, col)]) == pytest.approx(current, rel=1e-6)
            assert (beyond - current) * (1 if direction == "More CO2" else -1) > current * 1e-3
            if key == "variable_pl_cost":  # every moved unit changes plant: the neighbor mix itself
                assert beyond == pytest.approx(neighbors[direction], rel=1e-6)
            assert thresholds.loc[label, f"Verified, {direction}"] == "Yes"
            checked.add(label)
    assert checked == set(parameters)
