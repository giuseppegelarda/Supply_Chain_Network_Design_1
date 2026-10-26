"""Model core: input data, demand scenarios, annualized costs, the MILP and its cost breakdown, parallel
solving and networks kept fixed."""
import os
import copy
import itertools
import multiprocessing
from concurrent.futures import ProcessPoolExecutor
import pandas as pd
import numpy as np
from pulp import LpProblem, LpMaximize, LpVariable, lpSum, LpStatus, value, HiGHS


def load_data(data_dir="DATA", n_years=10, discount_rate=0.08):
    """Loads the input Excel files of data_dir, plus n_years and discount_rate as 1x1 DataFrames."""
    def read(name, index_col=0):
        return pd.read_excel(os.path.join(data_dir, f"{name}.xlsx"), index_col=index_col)

    params = {
        "cap_wh": read("CAP_WH"),
        "cap_pl": read("CAP_PL"),
        "demand": read("DEMAND"),
        "distances_pl_wh": read("DISTANCES_PL_WH"),
        "freight_cost_pl_wh": read("FREIGHT_COST"),
        "freight_cost_wh_mkt": read("FREIGHT_COST_WH_MKT"),
        # FIX_COST: one-off CAPEX to open each warehouse; OPEX: its annual operating cost
        "fix_cost": read("FIX_COST"),
        "opex": read("OPEX"),
        "selling_price": read("SELLING_PRICE"),
        "variable_pl_cost": read("VARIABLE_PL_COST"),
        "size_container": read("SIZE_CONTAINERS"),
        "unit_weight": read("UNIT_WEIGHT"),
        "offsetting_cost": read("OFFSETTING_COST"),
        # NOTE: EMI_FACTOR_PL_WH has a single row ("Shipping") with the kgCO2/(ton*km) factor, NOT a matrix.
        "emi_factor_pl_wh": read("EMI_FACTOR_PL_WH"),
        "emi_factor_production": read("EMI_FACTOR_PRODUCTION"),
        "emi_factor_storage": read("EMI_FACTOR_STORAGE"),
        # Single-column files (header in A1, value in A2): index_col would leave an empty DataFrame
        "emi_factor_indian_transport": read("EMI_FACTOR_INDIAN_TRANSPORT", index_col=None),
        # Reference tariffs: the CO2 of each route stays fixed when the sensitivity changes a tariff
        "co2_ref_freight_wh_mkt": read("FREIGHT_COST_WH_MKT"),
        "holding_percentual": read("HOLDING_PERCENTUAL", index_col=None),
        "n_years": pd.DataFrame({"n_years": [n_years]}),
        "discount_rate": pd.DataFrame({"discount_rate": [discount_rate]}),
    }

    for df in params.values():
        if isinstance(df, pd.DataFrame):
            df.index = df.index.astype(str).str.strip()
            df.columns = df.columns.astype(str).str.strip()
    return params


def _to_float(value):
    """Float of a value, also from a text cell with a decimal comma (e.g. "0,18")."""
    if isinstance(value, str):
        value = value.strip().replace(",", ".")
    return float(value)


def _demand_dict(demand):
    """Returns demand data (DataFrame, Series or dict) as a {market: demand} dict."""
    if isinstance(demand, pd.DataFrame):
        return demand.iloc[:, 0].to_dict()
    return demand.to_dict() if isinstance(demand, pd.Series) else dict(demand)


def _open_warehouses(y_sol):
    """Warehouses open in a solution ({warehouse: 0/1}, solver values rounded), in their original order."""
    return [k for k, is_open in y_sol.items() if int(round(is_open)) == 1]


def _config_label(open_warehouses):
    """Name of a network configuration, e.g. "Mumbai_Kolkata" ("No_Warehouse" if none is open)."""
    return "_".join(open_warehouses) if open_warehouses else "No_Warehouse"


def _config_display(open_warehouses):
    """Readable name of a network configuration for charts, e.g. "Mumbai + Kolkata"."""
    return " + ".join(open_warehouses) if open_warehouses else "No warehouse"


def _print_header(title, char="=", newline=True):
    """Prints a console section header framed by two rows of `char`."""
    print(("\n" if newline else "") + char * 60)
    print(title)
    print(char * 60)


def _capital_recovery_factor(rate, years):
    """Capital recovery factor r(1+r)^T / ((1+r)^T - 1), 1/T if r = 0: CAPEX * CRF = annual cost."""
    if rate == 0:
        return 1.0 / years
    growth = (1.0 + rate) ** years
    return rate * growth / (growth - 1.0)


def _unit_costs(params, plants, warehouses, markets):
    """Annual cost (and kg of CO2, "kg_co2_*") per unit of each variable (y_k, x_ik, w_kj) by item, plus
    the revenue: shared by optimization_model and calculate_cost_breakdown."""
    pw = [(i, k) for i in plants for k in warehouses]  # Plant -> Warehouse routes (x_ik)
    wm = [(k, j) for k in warehouses for j in markets]  # Warehouse -> Market routes (w_kj)
    # Offsetting price: EUR/ton -> EUR/kg
    offset = float(params["offsetting_cost"].iloc[0, 0]) / 1000.0
    # UNIT_WEIGHT: grams -> tons (EMI_FACTOR_PL_WH is in kgCO2/(ton*km))
    unit_ton = float(params["unit_weight"].iloc[0, 0]) / 1_000_000.0
    ship_factor = float(params["emi_factor_pl_wh"].iloc[0, 0])
    indian_factor = float(params["emi_factor_indian_transport"].iloc[0, 0])
    distance = params["distances_pl_wh"]
    crf = _capital_recovery_factor(float(params["discount_rate"].iloc[0, 0]), float(params["n_years"].iloc[0, 0]))
    capex = {k: params["fix_cost"].loc[k].iloc[0] * crf for k in warehouses}
    opex = {k: params["opex"].loc[k].iloc[0] for k in warehouses}
    price = float(params["selling_price"].iloc[0, 0])
    # EMI_FACTOR_PRODUCTION and EMI_FACTOR_STORAGE: grams per unit -> kg
    kg_co2 = {
        "co2_prod": {(i, k): params["emi_factor_production"].loc[i].iloc[0] / 1000.0 for i, k in pw},
        "co2_pl_wh": {(i, k): distance.loc[i, k] * unit_ton * ship_factor for i, k in pw},
        "co2_storage": {(i, k): params["emi_factor_storage"].loc[k].iloc[0] / 1000.0 for i, k in pw},
        "co2_wh_mkt": {(k, j): params["co2_ref_freight_wh_mkt"].loc[k, j] * indian_factor for k, j in wm},
    }
    return {
        "revenue": {(k, j): price for k, j in wm},
        "fix": {k: capex[k] + opex[k] for k in warehouses},
        "capex": capex,
        "opex": opex,
        "prod": {(i, k): params["variable_pl_cost"].loc[i].iloc[0] for i, k in pw},
        "freight_pl_wh": {(i, k): params["freight_cost_pl_wh"].loc[i, k] for i, k in pw},
        "freight_wh_mkt": {(k, j): params["freight_cost_wh_mkt"].loc[k, j] for k, j in wm},
        **{item: {key: kg * offset for key, kg in factors.items()} for item, factors in kg_co2.items()},
        **{f"kg_{item}": factors for item, factors in kg_co2.items()},
    }


def optimization_model(params, demand_values, fixed_config=None):
    """Solves the 3-stage MILP maximizing the annual profit (sales <= demand, inventory cost not in the
    objective); fixed_config: {warehouse: 0/1} to fix the network. Returns (status, objective, y, x, w)."""
    plants, warehouses = list(params["cap_pl"].index), list(params["cap_wh"].index)
    markets = list(demand_values.keys())
    model = LpProblem("Capacitated_3Stage_Supply_Chain", LpMaximize)
    x = LpVariable.dicts("prod_ship", [(i, k) for i in plants for k in warehouses], lowBound=0, cat="Continuous")
    w = LpVariable.dicts("dist_ship", [(k, j) for k in warehouses for j in markets], lowBound=0, cat="Continuous")
    y = LpVariable.dicts("warehouse", warehouses, cat="Binary")

    unit = _unit_costs(params, plants, warehouses, markets)
    model += lpSum(unit["revenue"][kj] * w[kj] for kj in w) - (
        lpSum(unit["fix"][k] * y[k] for k in warehouses)
        + lpSum(unit["prod"][ik] * x[ik] for ik in x)
        + lpSum(unit["freight_pl_wh"][ik] * x[ik] for ik in x)
        + lpSum(unit["freight_wh_mkt"][kj] * w[kj] for kj in w)
        + lpSum(unit["co2_prod"][ik] * x[ik] for ik in x)
        + lpSum(unit["co2_pl_wh"][ik] * x[ik] for ik in x)
        + lpSum(unit["co2_storage"][ik] * x[ik] for ik in x)
        + lpSum(unit["co2_wh_mkt"][kj] * w[kj] for kj in w)
    )

    for j in markets:
        model += lpSum([w[(k, j)] for k in warehouses]) <= demand_values[j]
    for k in warehouses:
        model += lpSum([x[(i, k)] for i in plants]) == lpSum([w[(k, j)] for j in markets])
    for i in plants:
        model += lpSum([x[(i, k)] for k in warehouses]) <= params["cap_pl"].loc[i].iloc[0]
    for k in warehouses:
        model += lpSum([x[(i, k)] for i in plants]) <= params["cap_wh"].loc[k].iloc[0] * y[k]
    if fixed_config is not None:
        for k in warehouses:
            model += y[k] == fixed_config[k]

    model.solve(HiGHS(msg=False))  # msg=False: no solver log
    y_sol = {k: y[k].varValue for k in warehouses}
    x_sol = {ik: var.varValue for ik, var in x.items()}
    w_sol = {kj: var.varValue for kj, var in w.items()}
    # The objective excludes the inventory cost: profits shown to the user come from calculate_cost_breakdown
    return LpStatus[model.status], value(model.objective), y_sol, x_sol, w_sol


def generate_demand_scenarios(base_demand, n_scenarios=50, cv=0.2):
    """Normal demand scenarios of every market (seed 42, negative draws set to 0), in whole units."""
    np.random.seed(42)
    scenarios = {}
    for market, base_value in _demand_dict(base_demand).items():
        sigma = cv * base_value
        scenarios[market] = np.round(np.clip(np.random.normal(base_value, sigma, n_scenarios), 0, None))
    return pd.DataFrame(scenarios)


# Rows of calculate_cost_breakdown used elsewhere (also the row labels of report_configurations.xlsx)
ROW_PROFIT = "1. PROFIT (REVENUE - TOTAL COST)"
ROW_REVENUE = "2. REVENUE"
ROW_TOTAL_COST = "3. TOTAL COST"
ROW_INVENTORY_COST = "11. INVENTORY COST ONLY"
ROW_TOTAL_CO2_COST = "12. TOTAL CO2 COST"
ROW_DEMAND = "17. DEMAND (UNITS)"
ROW_UNITS_SOLD = "18. UNITS SOLD"
ROW_LOST_SALES = "19. LOST SALES (UNITS)"
ROW_FILL_RATE = "20. FILL RATE (UNITS SOLD / DEMAND)"
ROW_COST_PER_UNIT = "21. COST PER UNIT SOLD (3. TOTAL COST / UNITS SOLD)"
ROW_CO2_EMISSIONS = "22. TOTAL CO2 EMISSIONS (kg)"
ROW_CO2_PER_UNIT = "23. CO2 EMISSIONS PER UNIT SOLD (kg/unit)"
# kg of CO2 by source: not in report_configurations.xlsx, used by report_co2_emissions
CO2_EMISSION_SOURCES = {
    "Production": "CO2 EMISSIONS - PRODUCTION (kg)",
    "Transport to warehouses": "CO2 EMISSIONS - TRANSPORT TO WAREHOUSES (kg)",
    "Transport from warehouses": "CO2 EMISSIONS - TRANSPORT FROM WAREHOUSES (kg)",
    "Storage": "CO2 EMISSIONS - STORAGE (kg)",
}
# Lost sales below this many units are solver rounding, not real lost sales
LOST_SALES_TOLERANCE = 1.0


def calculate_cost_breakdown(y_sol: dict, x_sol: dict, w_sol: dict, params: dict, demand_values: dict) -> pd.Series:
    """Annual profit, revenue, cost items (inventory cost included), service level and kg of CO2
    of a solution, as a Series."""
    plants, warehouses = list(params["cap_pl"].index), list(params["cap_wh"].index)
    markets = list(demand_values.keys())
    unit = _unit_costs(params, plants, warehouses, markets)

    def item_cost(item, solution):
        return sum(solution.get(key, 0) * unit_cost for key, unit_cost in unit[item].items())

    cost_fix_wh = item_cost("fix", y_sol)
    cost_capex_wh = item_cost("capex", y_sol)
    cost_opex_wh = item_cost("opex", y_sol)
    cost_var_prod = item_cost("prod", x_sol)
    cost_trans_pl_wh = item_cost("freight_pl_wh", x_sol)
    cost_trans_wh_mkt = item_cost("freight_wh_mkt", w_sol)

    # Inventory cost: average cycle stock, min(container size, flow) / 2 per route, at its production cost
    holding_rate = _to_float(params["holding_percentual"].iloc[0, 0])
    size = params["size_container"]
    total_demand = sum(demand_values.values())
    cost_inventory = 0.0
    for (i, k), unit_price in unit["prod"].items():
        per_warehouse = isinstance(size, pd.DataFrame) and k in size.columns
        c_size = size.loc[i, k] if per_warehouse else size.loc[i].iloc[0]
        cost_inventory += min(c_size, x_sol.get((i, k), 0)) / 2.0 * holding_rate * unit_price

    cost_co2_prod = item_cost("co2_prod", x_sol)
    cost_co2_pl_wh = item_cost("co2_pl_wh", x_sol)
    cost_co2_wh_mkt = item_cost("co2_wh_mkt", w_sol)
    cost_co2_storage = item_cost("co2_storage", x_sol)
    cost_co2_total = cost_co2_prod + cost_co2_pl_wh + cost_co2_wh_mkt + cost_co2_storage
    cost_total = (cost_fix_wh + cost_var_prod + cost_trans_pl_wh + cost_trans_wh_mkt
                  + cost_co2_total + cost_inventory)
    cost_logistic = cost_trans_pl_wh + cost_trans_wh_mkt + cost_fix_wh + cost_inventory
    revenue = item_cost("revenue", w_sol)
    units_sold = sum(w_sol.get(kj, 0) or 0 for kj in unit["revenue"])
    lost_sales = total_demand - units_sold
    lost_sales = lost_sales if lost_sales > LOST_SALES_TOLERANCE else 0.0
    emissions = {
        CO2_EMISSION_SOURCES["Production"]: item_cost("kg_co2_prod", x_sol),
        CO2_EMISSION_SOURCES["Transport to warehouses"]: item_cost("kg_co2_pl_wh", x_sol),
        CO2_EMISSION_SOURCES["Transport from warehouses"]: item_cost("kg_co2_wh_mkt", w_sol),
        CO2_EMISSION_SOURCES["Storage"]: item_cost("kg_co2_storage", x_sol),
    }

    return pd.Series(
        {
            ROW_PROFIT: revenue - cost_total,
            ROW_REVENUE: revenue,
            ROW_TOTAL_COST: cost_total,
            "4. LOGISTICS COST": cost_logistic,
            "5. PRODUCTION COST": cost_var_prod,
            "6. TRANSPORT COST TO WAREHOUSES ONLY": cost_trans_pl_wh,
            "7. TRANSPORT COST FROM WAREHOUSES ONLY": cost_trans_wh_mkt,
            "8. WAREHOUSE COST (ANNUALIZED CAPEX + OPEX)": cost_fix_wh,
            "9. WAREHOUSE ANNUALIZED CAPEX ONLY": cost_capex_wh,
            "10. WAREHOUSE OPEX ONLY": cost_opex_wh,
            ROW_INVENTORY_COST: cost_inventory,
            ROW_TOTAL_CO2_COST: cost_co2_total,
            "13. CO2 COST - PRODUCTION ONLY": cost_co2_prod,
            "14. CO2 COST - TRANSPORT TO WAREHOUSES ONLY": cost_co2_pl_wh,
            "15. CO2 COST - TRANSPORT FROM WAREHOUSES ONLY": cost_co2_wh_mkt,
            "16. CO2 COST - STORAGE ONLY": cost_co2_storage,
            ROW_DEMAND: total_demand,
            ROW_UNITS_SOLD: units_sold,
            ROW_LOST_SALES: lost_sales,
            ROW_FILL_RATE: units_sold / total_demand if total_demand > 0 else np.nan,
            ROW_COST_PER_UNIT: cost_total / units_sold if units_sold > 0 else np.nan,
            ROW_CO2_EMISSIONS: sum(emissions.values()),
            ROW_CO2_PER_UNIT: sum(emissions.values()) / units_sold if units_sold > 0 else np.nan,
            **emissions,
        }
    )


def _solve_with_breakdown(params, demand_values, fixed_config=None, with_w=False):
    """Solves the model: (status, y_sol, x_sol, breakdown or None if not optimal[, w_sol if with_w])."""
    status, _, y_sol, x_sol, w_sol = optimization_model(params, demand_values, fixed_config)
    breakdown = calculate_cost_breakdown(y_sol, x_sol, w_sol, params, demand_values) if status == "Optimal" else None
    return (status, y_sol, x_sol, breakdown) + ((w_sol,) if with_w else ())


# State of a solver process: base parameters, scenarios and parameters of the last task (see _solve_many)
_SOLVER_STATE = {}
# Pool shared by the _solve_many calls with the same inputs (kept alive, so that their ids identify them)
_SOLVER_POOL = {}


def _init_solver_state(params, df_scenarios):
    """Stores the base parameters and the demand scenarios for the tasks of this process (see _solve_many)."""
    _SOLVER_STATE.clear()
    _SOLVER_STATE.update(params=params, scenarios=df_scenarios, changes=(), task_params=params)


def _solve_task(task):
    """Solves one task of _solve_many, reusing the parameters of the previous task if the changes are the same."""
    changes, demand, fixed_config, *with_w = task
    if changes != _SOLVER_STATE["changes"]:
        params = _SOLVER_STATE["params"]
        for key, value, row, col in changes:
            params = _set_param_value(params, key, value, row=row, col=col)
        _SOLVER_STATE.update(changes=changes, task_params=params)
    if not isinstance(demand, dict):  # scenario number, 1..n
        demand = _SOLVER_STATE["scenarios"].iloc[demand - 1].to_dict()
    return _solve_with_breakdown(_SOLVER_STATE["task_params"], demand, fixed_config, *with_w)


def _solve_many(tasks, params, df_scenarios=None, n_workers=1):
    """Solves the tasks (changes, demand, fixed_config[, with_w]) and returns their _solve_with_breakdown
    results in order: in a pool of n_workers processes if n_workers > 1, kept for the same inputs."""
    tasks = list(tasks)
    if n_workers <= 1 or len(tasks) < 2:
        _init_solver_state(params, df_scenarios)
        return [_solve_task(task) for task in tasks]
    key = (n_workers, id(params), id(df_scenarios))
    if _SOLVER_POOL.get("key") != key:
        _shutdown_solver_pool()
        # "spawn" on every OS (already the default on Windows and macOS): a process forked on Linux after a solve
        # inherits HiGHS's thread pool without its threads and can wait for them forever
        _SOLVER_POOL.update(key=key, inputs=(params, df_scenarios),
                            pool=ProcessPoolExecutor(n_workers, mp_context=multiprocessing.get_context("spawn"),
                                                     initializer=_init_solver_state,
                                                     initargs=(params, df_scenarios)))
    # Chunks of consecutive tasks, which usually share the same changes
    return list(_SOLVER_POOL["pool"].map(_solve_task, tasks, chunksize=max(1, len(tasks) // (4 * n_workers))))


def _shutdown_solver_pool():
    """Closes the process pool of _solve_many, if any."""
    if "pool" in _SOLVER_POOL:
        _SOLVER_POOL["pool"].shutdown()
    _SOLVER_POOL.clear()


def _get_param_value(params, key, row=None, col=None):
    """Reads a scalar value from params[key] (scalar, row-indexed or row/column matrix layout)."""
    if row is None:
        return float(params[key].iloc[0, 0])
    return float(params[key].loc[row].iloc[0] if col is None else params[key].loc[row, col])


def _set_param_value(params, key, value, row=None, col=None):
    """Returns a COPY of params with a single value changed, so the base case stays intact between runs."""
    new_params = copy.deepcopy(params)
    df = new_params[key]
    target_row = df.index[0] if row is None else row
    target_col = df.columns[0] if col is None else col
    # Integer columns (e.g. FIX_COST) cast to float first: recent pandas versions no longer upcast them
    if df[target_col].dtype != float:
        df[target_col] = df[target_col].astype(float)
    # Single-step assignment: .loc[...].iloc[...] = value does not reliably update a copy
    df.loc[target_row, target_col] = float(value)
    return new_params


# Parameters that only change the warehouses' annual cost: with the network fixed, solved by formula
FIXED_COST_KEYS = ("fix_cost", "opex", "discount_rate")
NO_WAREHOUSE = _config_display([])


def _all_networks(warehouses):
    """Every possible network (tuple of open warehouses, in their original order), from no warehouse to all."""
    return [c for size in range(len(warehouses) + 1) for c in itertools.combinations(warehouses, size)]


def _network_costs(params, networks):
    """Annual cost of the warehouses (annualized CAPEX + OPEX) of each network, by its _config_display label."""
    fix = _unit_costs(params, list(params["cap_pl"].index), list(params["cap_wh"].index),
                      list(_demand_dict(params["demand"])))["fix"]
    return pd.Series({_config_display(list(network)): sum(fix[k] for k in network) for network in networks})


def _expected_with_warehouse_cost(expected, base_params, params, networks):
    """Expected profits of the fixed networks (by label) when only the warehouses' annual cost changes from
    base_params to params (CAPEX, OPEX, discount rate, N_YEARS): the flows stay the same, no solver run."""
    return expected - (_network_costs(params, networks) - _network_costs(base_params, networks))


def _fixed_network_results(base_params, df_scenarios, networks, changes_list=((),), n_workers=1, flows=False):
    """Per changes set, {"profit", "objective" (inventory cost added back), "co2", "lost"[, flows]}: one row per
    scenario and one column per network kept fixed (no warehouse: all 0, lost sales = the demand)."""
    warehouses = list(base_params["cap_wh"].index)
    scenarios = pd.Index(range(1, len(df_scenarios) + 1), name="scenario")
    solved = [network for network in networks if network]
    results = iter(_solve_many([(tuple(changes), scenario, {k: int(k in network) for k in warehouses})
                                + ((True,) if flows else ())
                                for changes in changes_list for network in solved for scenario in scenarios],
                               base_params, df_scenarios, n_workers))
    routes = ([(i, k) for i in base_params["cap_pl"].index for k in warehouses]
              + [(k, j) for k in warehouses for j in df_scenarios.columns]) if flows else []
    output = []
    for _ in changes_list:
        tables = {"profit": {}, "objective": {}, "co2": {}, "lost": {}, **{f"flow {a} -> {b}": {} for a, b in routes}}
        for network in networks:
            label = _config_display(list(network))
            if not network:
                for table in tables.values():
                    table[label] = pd.Series(0.0, index=scenarios)
                tables["lost"][label] = pd.Series(df_scenarios.sum(axis=1).to_numpy(), index=scenarios)
                continue
            solutions = [next(results) for _ in scenarios]
            breakdowns = [solution[3] for solution in solutions]
            values = {row: pd.Series([b[row] if b is not None else np.nan for b in breakdowns], index=scenarios)
                      for row in (ROW_PROFIT, ROW_INVENTORY_COST, ROW_CO2_EMISSIONS, ROW_LOST_SALES)}
            tables["profit"][label] = values[ROW_PROFIT]
            tables["objective"][label] = values[ROW_PROFIT] + values[ROW_INVENTORY_COST]
            tables["co2"][label] = values[ROW_CO2_EMISSIONS]
            tables["lost"][label] = values[ROW_LOST_SALES]
            for a, b in routes:  # x_sol (Plant -> Warehouse) or w_sol (Warehouse -> Market)
                item = 2 if a in base_params["cap_pl"].index else 4
                flow = [solution[item].get((a, b), 0.0) for solution in solutions]
                tables[f"flow {a} -> {b}"][label] = pd.Series(flow, index=scenarios)
        frames = {name: pd.DataFrame(table) for name, table in tables.items()}
        solved_everywhere = frames["profit"].dropna().index
        output.append({name: frame.loc[solved_everywhere] for name, frame in frames.items()})
    return output


def evaluate_fixed_configuration(fixed_config, params, df_scenarios, df_results, n_workers=1):
    """Solves the scenarios of df_results with the network fixed: (df_fixed, discarded)."""
    scenarios = [int(scenario) for scenario in df_results["scenario"]]
    results = _solve_many([((), scenario, fixed_config) for scenario in scenarios], params, df_scenarios, n_workers)
    rows, discarded_scenarios = [], []
    for scenario, (_, _, _, breakdown) in zip(scenarios, results):
        if breakdown is None:
            discarded_scenarios.append(scenario)
            continue
        row = breakdown.copy()
        row["scenario"] = scenario
        rows.append(row)
    return pd.DataFrame(rows), discarded_scenarios
