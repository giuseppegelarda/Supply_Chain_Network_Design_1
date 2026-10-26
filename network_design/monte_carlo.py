"""Monte Carlo over the demand scenarios: report per network configuration and choice of the recommended
network (highest expected profit with the network fixed; EVPI, VSS)."""
import os
import pandas as pd
from network_design.core import (_demand_dict, _open_warehouses, _config_label, _config_display, _print_header,
                                 _capital_recovery_factor, optimization_model, ROW_PROFIT, ROW_TOTAL_COST,
                                 ROW_LOST_SALES, ROW_FILL_RATE, ROW_COST_PER_UNIT, ROW_CO2_EMISSIONS,
                                 CO2_EMISSION_SOURCES, LOST_SALES_TOLERANCE, calculate_cost_breakdown,
                                 _solve_with_breakdown, _solve_many, _get_param_value, _all_networks,
                                 evaluate_fixed_configuration)
from network_design.outputs import (_write_excel, _format_table, report_co2_emissions, FORMAT_EUR,
                                    FORMAT_EUR_PER_UNIT, FORMAT_KG, FORMAT_KG_PER_UNIT, FORMAT_UNITS, FORMAT_INTEGER,
                                    FORMAT_PERCENT)


# Statistics rows of report_configurations.xlsx (only in the AVERAGE columns)
STAT_ITEMS = (("PROFIT", ROW_PROFIT), ("TOTAL COST", ROW_TOTAL_COST), ("COST PER UNIT SOLD", ROW_COST_PER_UNIT),
              ("CO2 EMISSIONS (kg)", ROW_CO2_EMISSIONS))
STAT_ROW_LABELS = ["N. SCENARIOS USED", "N. SCENARIOS WITH LOST SALES",
                   *(f"{stat} - {name}" for name, _ in STAT_ITEMS for stat in ("STD DEV", "P10", "P90"))]


def _stat_rows(df):
    """Statistics rows over the scenarios of df: counts, std dev (ddof=1), P10 and P90."""
    if df.empty:
        return {"N. SCENARIOS USED": 0, **{label: "-" for label in STAT_ROW_LABELS[1:]}}

    def std(series):
        return "-" if len(series) < 2 else float(series.std(ddof=1))

    def percentile(series, q):
        return "-" if len(series) == 0 else float(series.quantile(q))

    stats = {"N. SCENARIOS USED": len(df),
             "N. SCENARIOS WITH LOST SALES": int((df[ROW_LOST_SALES] > LOST_SALES_TOLERANCE).sum())}
    for name, row in STAT_ITEMS:
        series = df[row].dropna()
        stats.update({f"STD DEV - {name}": std(series), f"P10 - {name}": percentile(series, 0.10),
                      f"P90 - {name}": percentile(series, 0.90)})
    return stats


def _report_formats(row_labels, routes):
    """{row label: (Excel format, Python format)} of report_configurations.xlsx, with the unit of each row."""
    by_number = {**{n: FORMAT_EUR for n in range(1, 17)}, **{n: FORMAT_UNITS for n in (17, 18, 19)},
                 20: FORMAT_PERCENT, 21: FORMAT_EUR_PER_UNIT, 22: FORMAT_KG,
                 23: FORMAT_KG_PER_UNIT}
    formats = {label: by_number[int(label.split(".")[0])] for label in row_labels}
    formats.update({label: FORMAT_INTEGER for label in STAT_ROW_LABELS[:2]})
    for name, row in STAT_ITEMS:  # same unit as the row they describe
        formats.update({f"{stat} - {name}": formats[row] for stat in ("STD DEV", "P10", "P90")})
    return {**formats, **{route: FORMAT_UNITS for route in routes}}


def export_excel_reports(df_results, df_solutions, df_flows, plants, warehouses, params, df_scenarios,
                         output_dir="Output", n_workers=1):
    """Writes report_configurations.xlsx (fixed-network averages, one sheet per configuration found); returns the
    fixed-network evaluations {config_name: {"open_warehouses", "df_fixed", "discarded"}}."""
    row_labels = [c for c in df_results.columns if c not in ["scenario", "status", *CO2_EMISSION_SOURCES.values()]]
    row_labels_all = row_labels + STAT_ROW_LABELS
    fixed_evaluations = {}
    df_average = pd.DataFrame({"Item": row_labels_all})
    config_sheets = {}
    df_analysis = df_results.copy()
    df_analysis["config"] = df_solutions.apply(lambda row: _config_label(row[row == 1].index.tolist()), axis=1)
    flow_cols = [f"{i} -> {k}" for i in plants for k in warehouses]

    for config_name, group in df_analysis.groupby("config"):
        profit = group[ROW_PROFIT]
        best_idx, worst_idx = profit.idxmax(), profit.idxmin()
        avg_idx = (profit - profit.mean()).abs().idxmin()
        best_scn, worst_scn, avg_scn = (int(group.loc[idx, "scenario"]) for idx in (best_idx, worst_idx, avg_idx))
        col_best = f"Best Case (Scen. {best_scn})"
        col_avg = f"Average Case (Scen. {avg_scn})"
        col_worst = f"Worst Case (Scen. {worst_scn})"
        avg_optimal = {**group[row_labels].mean().to_dict(), **_stat_rows(group)}
        fixed_config = df_solutions.loc[group.index[0], warehouses].astype(int).to_dict()
        df_fixed, discarded = evaluate_fixed_configuration(fixed_config, params, df_scenarios, df_results,
                                                           n_workers)
        fixed_evaluations[config_name] = {"open_warehouses": _open_warehouses(fixed_config),
                                          "df_fixed": df_fixed, "discarded": discarded}
        n_lost = int((df_fixed[ROW_LOST_SALES] > LOST_SALES_TOLERANCE).sum()) if not df_fixed.empty else 0
        print(f"  [{config_name}] Fixed network evaluated on {len(df_fixed)}/{len(df_results)} scenarios, "
              f"with lost sales in {n_lost}.")
        if discarded:
            print(f"  [{config_name}] WARNING: no optimal solution (excluded) for scenarios {discarded}")
        avg_fixed_values = ({label: "-" for label in row_labels} if df_fixed.empty
                            else df_fixed[row_labels].mean().to_dict())
        avg_fixed = {**avg_fixed_values, **_stat_rows(df_fixed)}
        df_average[_config_display(_open_warehouses(fixed_config))] = [avg_fixed[label] for label in row_labels_all]
        report_df = pd.DataFrame({"Item": row_labels_all,
                                  "AVERAGE OPTIMAL SCENARIOS": [avg_optimal[label] for label in row_labels_all]})

        flow_df = pd.DataFrame({"Route Plant -> Warehouse": flow_cols})
        for col, scn in ((col_best, best_scn), (col_avg, avg_scn), (col_worst, worst_scn)):
            flow_df[col] = df_flows.loc[df_flows["scenario"] == scn, flow_cols].iloc[0].values
        for p in plants:
            in_plant = flow_df["Route Plant -> Warehouse"].isin([f"{p} -> {k}" for k in warehouses])
            totals = {"Route Plant -> Warehouse": f"TOTAL {p}",
                      **{col: flow_df.loc[in_plant, col].sum() for col in (col_best, col_avg, col_worst)}}
            flow_df = pd.concat([flow_df, pd.DataFrame([totals])], ignore_index=True)
        config_sheets[config_name] = [report_df, flow_df]
    formats = _report_formats(row_labels, flow_cols + [f"TOTAL {p}" for p in plants])
    _write_excel(output_dir, "report_configurations.xlsx",
                 {f"Average_{len(df_scenarios)}_Scenarios": df_average, **config_sheets},
                 "Excel report successfully generated", cell_format=lambda label, column: formats.get(label))
    return fixed_evaluations


def report_decision_summary(df_results, df_solutions, fixed_evaluations, base_network, params, df_scenarios,
                            output_dir="Output", n_workers=1):
    """Recommended network: highest expected profit kept fixed on all the scenarios. Writes
    decision_summary.xlsx (with EVPI, VSS) and returns {"network", "df_fixed", "expected_profits"}."""
    warehouses = list(params["cap_wh"].index)
    evaluations = {tuple(ev["open_warehouses"]): ev["df_fixed"] for ev in fixed_evaluations.values()}
    optimal_networks = [tuple(_open_warehouses(row)) for row in df_solutions[warehouses].to_dict("records")]
    candidates = _all_networks(warehouses)[1:]
    for network in optimal_networks + ([tuple(base_network)] if base_network is not None else []):
        if network not in candidates:
            candidates.append(network)
    for network in candidates:
        if network not in evaluations:
            fixed_config = {k: int(k in network) for k in warehouses}
            evaluations[network], discarded = evaluate_fixed_configuration(fixed_config, params, df_scenarios,
                                                                           df_results, n_workers)
            if discarded:
                print(f"  [{_config_label(list(network))}] WARNING: no optimal solution (excluded) for scenarios "
                      f"{discarded}")

    frames = {network: evaluations[network].set_index("scenario") for network in candidates
              if not evaluations[network].empty}
    free_profit = df_results.set_index("scenario")[ROW_PROFIT]
    common = sorted(set(free_profit.index).intersection(*(set(df.index) for df in frames.values())))
    free_profit = free_profit.loc[common]
    rows = []
    for network, df in frames.items():
        profit = df.loc[common, ROW_PROFIT]
        rows.append({"Network": network, "Configuration": _config_display(list(network)),
                     "Optimal In (N. Scenarios)": optimal_networks.count(network),
                     "Expected Profit, Fixed Network (EUR/year)": profit.mean(),
                     "P10 Profit (EUR/year)": profit.quantile(0.10),
                     "N. Scenarios With Lost Sales": int((df.loc[common, ROW_LOST_SALES] > LOST_SALES_TOLERANCE).sum()),
                     "Max Regret (EUR/year)": (free_profit - profit).max()})
    df_summary = pd.DataFrame(rows).sort_values("Expected Profit, Fixed Network (EUR/year)", ascending=False,
                                                kind="stable", ignore_index=True)
    recommended = df_summary["Network"].iloc[0]
    expected = dict(zip(df_summary["Network"], df_summary["Expected Profit, Fixed Network (EUR/year)"]))
    df_summary["Configuration"] = [label + " (recommended)" if network == recommended else label
                                   for network, label in zip(df_summary["Network"], df_summary["Configuration"])]
    df_summary = df_summary.drop(columns="Network")

    evpi = free_profit.mean() - expected[recommended]
    base_key = tuple(base_network) if base_network is not None else None
    vss = expected[recommended] - expected[base_key] if base_key in expected else "-"
    horizon = f"N_YEARS = {_get_param_value(params, 'n_years'):g}"
    df_indicators = pd.DataFrame({
        "Indicator": ["Deterministic solution (network chosen on the base demand)",
                      f"EVPI - Expected Value of Perfect Information (EUR/year, {horizon})",
                      f"VSS - Value of the Stochastic Solution (EUR/year, {horizon})"],
        "Value": [_config_display(base_network) if base_network is not None else "-", evpi, vss],
    })

    _print_header("NETWORK DECISION (HIGHEST EXPECTED PROFIT WITH THE NETWORK FIXED)", "-")
    print(f"EUR/year, network fixed on {len(common)} scenarios; optimal in / lost sales in = number of scenarios")
    console = df_summary.rename(columns={
        "Optimal In (N. Scenarios)": "Optimal in", "Expected Profit, Fixed Network (EUR/year)": "Expected profit",
        "P10 Profit (EUR/year)": "P10 profit", "N. Scenarios With Lost Sales": "Lost sales in",
        "Max Regret (EUR/year)": "Max regret"})
    print(console.to_string(index=False, float_format=lambda v: f"{v:,.0f}"))
    print(f"Deterministic solution (base demand): {df_indicators['Value'].iloc[0]} | {horizon}: EVPI EUR "
          f"{evpi:,.0f}/year, VSS " + (f"EUR {vss:,.0f}/year" if vss != "-" else "-"))

    os.makedirs(output_dir, exist_ok=True)
    excel_path = os.path.join(output_dir, "decision_summary.xlsx")
    with pd.ExcelWriter(excel_path, engine="openpyxl") as writer:
        df_summary.to_excel(writer, sheet_name="Summary", index=False)
        df_indicators.to_excel(writer, sheet_name="Summary", index=False, startrow=len(df_summary) + 2)
        ws = writer.sheets["Summary"]
        # Counts as integers, everything else (profits, regret, EVPI, VSS) in EUR; each column as wide as the wider
        # of the two tables
        _format_table(ws, df_indicators, len(df_summary) + 2, 0, lambda label, column: FORMAT_EUR)
        widths = {c: ws.column_dimensions[c].width for c in "AB"}
        _format_table(ws, df_summary, 0, 0, lambda label, column: FORMAT_INTEGER if "N. Scenarios" in column
                      else FORMAT_EUR)
        for c, width in widths.items():
            ws.column_dimensions[c].width = max(width, ws.column_dimensions[c].width)
    print(f"Decision summary generated: {excel_path}")
    return {"network": list(recommended), "df_fixed": frames[recommended].loc[common].reset_index(),
            "expected_profits": expected}


def run_monte_carlo(params, df_scenarios, cv, output_dir="Output", n_workers=1):
    """Solves every scenario, writes the reports and chooses the recommended network: returns
    (df_results, df_solutions, df_flows, decision)."""
    _print_header("MONTE CARLO SIMULATION - 3-STAGE SUPPLY CHAIN NETWORK DESIGN", newline=False)
    base_demand = _demand_dict(params["demand"])
    warehouses, plants = list(params["cap_wh"].index), list(params["cap_pl"].index)
    n_scenarios = len(df_scenarios)
    n_years, discount_rate = _get_param_value(params, "n_years"), _get_param_value(params, "discount_rate")

    print(f"\nBase demand by market: {base_demand}")
    print(f"Coefficient of Variation (CV): {cv * 100}%")
    print(f"Number of scenarios: {n_scenarios} | Parallel solver processes: {n_workers}")
    print(f"Planning horizon: {n_years:g} years | Discount rate: {discount_rate:.1%} | CAPEX annualization factor: "
          f"{_capital_recovery_factor(discount_rate, n_years):.4f}")

    print("\nRunning initial scenario (Base Demand)...")
    status_init, obj_init, y_init, x_init, w_init = optimization_model(params, base_demand)
    if status_init == "Optimal":
        init = calculate_cost_breakdown(y_init, x_init, w_init, params, base_demand)
        print(f"Initial solution - Status: {status_init}, Profit: EUR {init[ROW_PROFIT]:,.2f}, Total Cost: "
              f"EUR {init[ROW_TOTAL_COST]:,.2f}, Fill Rate: {init[ROW_FILL_RATE]:.1%}")
    else:
        print(f"Initial solution - Status: {status_init}")

    results_list, all_solutions, all_flows, infeasible_scenarios = [], [], [], []
    print("\nRunning simulations over scenarios...")
    results = _solve_many([((), i + 1, None) for i in range(n_scenarios)], params, df_scenarios, n_workers)
    for i, (status, y_sol, x_sol, cost_breakdown) in enumerate(results):
        if cost_breakdown is None:
            infeasible_scenarios.append(i + 1)
            print(f"  [DISCARDED] Scenario {i + 1}: status '{status}' - excluded from results")
            continue
        cost_breakdown["scenario"] = i + 1
        cost_breakdown["status"] = status
        results_list.append(cost_breakdown)
        sol_dict = {k: int(round(y_sol[k])) for k in warehouses}
        all_solutions.append(sol_dict)
        # Whole units: the solver leaves residuals (e.g. -7.8e-10 on a route to a closed warehouse)
        flow_dict = {f"{p} -> {k}": round(x_sol.get((p, k), 0.0)) for p in plants for k in warehouses}
        flow_dict["scenario"] = i + 1
        all_flows.append(flow_dict)

    if infeasible_scenarios:
        print(f"\n[WARNING] {len(infeasible_scenarios)}/{n_scenarios} scenarios "
              f"({len(infeasible_scenarios) / n_scenarios * 100:.1f}%) had no optimal solution and were excluded "
              f"from the statistics and reports: {infeasible_scenarios}.")

    df_results = pd.DataFrame(results_list)
    df_solutions = pd.DataFrame(all_solutions)
    df_flows = pd.DataFrame(all_flows)
    cols = ["scenario", "status"] + [c for c in df_results.columns if c not in ["scenario", "status"]]
    df_results = df_results[cols]
    n_feasible = len(all_solutions)

    _print_header("RESULTS ANALYSIS & CONFIGURATION FREQUENCY", "-")
    print(f"Feasible scenarios: {n_feasible}/{n_scenarios}")
    solution_counts = df_solutions.apply(tuple, axis=1).value_counts()
    print(f"\nUnique network configurations found: {len(solution_counts)}")
    print("\nMost frequent optimal configurations across scenarios:")
    for i, (config_tuple, count) in enumerate(solution_counts.items()):
        open_whs = [warehouses[j] for j, val in enumerate(config_tuple) if val == 1]
        wh_str = ", ".join(open_whs) if open_whs else "None"
        percentage = (count / n_feasible) * 100 if n_feasible else 0.0
        print(f"  Configuration {i + 1}: {count} occurrences ({percentage:.1f}% of feasible scenarios)")
        print(f"    Open Warehouses: {wh_str}")

    _print_header("PROFIT AND COST STATISTICS", "-")
    for name, row in (("profit", ROW_PROFIT), ("cost", ROW_TOTAL_COST)):
        values = df_results[row]
        print(f"Mean {name}: EUR {values.mean():,.2f} | Std dev: EUR {values.std():,.2f} | "
              f"Min: EUR {values.min():,.2f} | Max: EUR {values.max():,.2f}")
    n_lost = int((df_results[ROW_LOST_SALES] > LOST_SALES_TOLERANCE).sum())
    print(f"Average fill rate: {df_results[ROW_FILL_RATE].mean():.2%} | Scenarios with lost sales: {n_lost}")

    fixed_evaluations = export_excel_reports(df_results, df_solutions, df_flows, plants, warehouses, params,
                                             df_scenarios, output_dir=output_dir, n_workers=n_workers)
    print("\nExcel reports successfully exported.")
    report_co2_emissions(fixed_evaluations, n_feasible, output_dir=output_dir)
    base_network = _open_warehouses(y_init) if status_init == "Optimal" else None
    decision = report_decision_summary(df_results, df_solutions, fixed_evaluations, base_network, params,
                                       df_scenarios, output_dir=output_dir, n_workers=n_workers)
    return df_results, df_solutions, df_flows, decision


def select_recommended_solution(df_solutions, warehouses, decision, params, df_scenarios):
    """Flows of the scenario whose fixed-network profit is closest to the expected profit."""
    open_warehouses = decision["network"]
    profits = decision["df_fixed"].set_index("scenario")[ROW_PROFIT]
    representative_scenario = int((profits - profits.mean()).abs().idxmin())
    _, _, x_sol, _ = _solve_with_breakdown(params, df_scenarios.iloc[representative_scenario - 1].to_dict(),
                                           {k: int(k in open_warehouses) for k in warehouses})
    configuration_series = df_solutions[warehouses].apply(tuple, axis=1)
    target = tuple(int(k in open_warehouses) for k in warehouses)
    recommended_info = {
        "config_label": _config_label(open_warehouses),
        "open_warehouses": open_warehouses,
        "scenario": representative_scenario,
        "frequency": int((configuration_series == target).sum()),
        "feasible_scenarios": len(df_solutions),
    }
    print(f"\nRecommended configuration: {recommended_info['config_label']} (highest expected profit with the "
          f"network fixed; optimal in {recommended_info['frequency']}/{recommended_info['feasible_scenarios']} "
          f"feasible scenarios). Representative scenario: {representative_scenario} (network fixed).")
    return x_sol, recommended_info
