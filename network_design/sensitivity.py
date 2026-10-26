"""Analyses of the recommended network: one-parameter sensitivity, planning horizon (N_YEARS), warehouse
capacity, demand uncertainty (CV) and cost-CO2 frontier."""
import os
import textwrap
import pandas as pd
import numpy as np
from network_design.core import (_demand_dict, _open_warehouses, _config_display, _print_header,
                                 _capital_recovery_factor, _unit_costs, generate_demand_scenarios, ROW_PROFIT,
                                 ROW_INVENTORY_COST, calculate_cost_breakdown, _solve_with_breakdown, _solve_many,
                                 _get_param_value, _set_param_value, FIXED_COST_KEYS, NO_WAREHOUSE, _all_networks,
                                 _expected_with_warehouse_cost, _fixed_network_results)
from network_design.outputs import (_figure_png, LABEL_BOX, _network_colors, _write_excel, _change_text,
                                    plot_sensitivity_tornado, SENSITIVITY_UNIT_FORMATS, export_sensitivity_results,
                                    _analysis_format, FORMAT_EUR, FORMAT_KG, FORMAT_UNITS, FORMAT_PERCENT,
                                    FORMAT_PERCENT_1)
import matplotlib.pyplot as plt


def _drop_empty_network(df, prefixes):
    """Drops the empty network's columns unless it is recommended or optimal somewhere (otherwise always 0)."""
    optimal = f"N. Scenarios Optimal - {NO_WAREHOUSE}"
    if (df["Recommended Network"] == NO_WAREHOUSE).any() or (optimal in df and df[optimal].any()):
        return df
    return df.drop(columns=[f"{prefix} - {NO_WAREHOUSE}" for prefix in prefixes])


def _network_changes(best_at, low, high, iterations, known=None):
    """Exact values between low and high at which best_at changes: [(value, below, above), ...]."""
    memo = dict(known or {})

    def best(value):
        if value not in memo:
            memo[value] = best_at(value)
        return memo[value]

    tolerance = (high - low) / 2 ** iterations
    changes, start = [], low
    for _ in range(10):  # safety bound: a handful of changes at most between two values
        end = high
        while end - start > tolerance:
            mid = (start + end) / 2
            if not start < mid < end:  # floating point resolution reached
                break
            if best(mid) == best(start):
                start = mid
            else:
                end = mid
        point = (start + end) / 2
        # At a cost tie the solver can return an in-between solution: two changes this close are the same change
        if changes and point - changes[-1][0] <= 2 * tolerance:
            changes[-1] = (changes[-1][0], changes[-1][1], best(end))
        else:
            changes.append((point, best(start), best(end)))
        start = end
        if best(start) == best(high):
            break
    return changes


def _ranked(profits):
    """Networks from the highest expected profit (ties: network order)."""
    return profits.sort_values(ascending=False, kind="stable")


def _ranking_columns(order):
    """Recommended network, second best and the first one's advantage, from the _ranked expected profits."""
    return {"Recommended Network": order.index[0], "Second Best Network": order.index[1],
            "Advantage Over Second Best (EUR/year)": order.iloc[0] - order.iloc[1]}


def run_expected_profit_sensitivity(base_params, df_scenarios, cases, n_workers=1, iterations=12, min_impact_pct=2.0):
    """Expected profit of every network kept fixed for each case: screening at the ends, every
    value and the tipping points if relevant. Returns (base_expected, results)."""
    networks = _all_networks(list(base_params["cap_wh"].index))
    labels = [_config_display(list(network)) for network in networks]

    def solve(changes_list):  # (expected profit, expected kg of CO2) of every network, for each changes set
        return [(tables["profit"].mean(), tables["co2"].mean())
                for tables in _fixed_network_results(base_params, df_scenarios, networks, changes_list, n_workers)]

    (base_profits, base_co2), = solve([()])
    base_expected = _ranked(base_profits)
    recommended, base_profit = base_expected.index[0], base_expected.iloc[0]
    base_kg = base_co2[recommended]
    print(f"Base: {recommended}, expected profit EUR {base_profit:,.0f}/year "
          f"(+{base_profit - base_expected.iloc[1]:,.0f} over {base_expected.index[1]}), expected CO2 "
          f"{base_kg:,.0f} kg/year")

    results = []
    for i, spec in enumerate(cases, start=1):
        key, row, col = spec["key"], spec.get("row"), spec.get("col")
        base_value = _get_param_value(base_params, key, row=row, col=col)
        values = spec["values"]
        fixed_cost = key in FIXED_COST_KEYS

        def at(value, key=key, row=row, col=col, fixed_cost=fixed_cost):  # (expected profits, expected kg of CO2)
            if fixed_cost:  # closed form, no solver run
                params_v = _set_param_value(base_params, key, value, row=row, col=col)
                return _expected_with_warehouse_cost(base_profits, base_params, params_v, networks), base_co2
            return solve([((key, value, row, col),)])[0]

        def best_at(value, at=at):
            return _ranked(at(value)[0]).index[0]

        def evaluate(tested, at=at, key=key, row=row, col=col, fixed_cost=fixed_cost):  # re-solved in one batch
            if fixed_cost:
                return {value: at(value) for value in tested}
            return dict(zip(tested, solve([((key, value, row, col),) for value in tested])))

        # 1. Screening: the two ends of the range
        ends = [values[0], values[-1]]
        curve = evaluate(ends)
        ends_best = [_ranked(curve[value][0]).index[0] for value in ends]
        impact = max(abs(_ranked(curve[value][0]).iloc[0] - base_profit) / abs(base_profit) * 100 for value in ends)
        detail = impact >= min_impact_pct or any(net != recommended for net in ends_best) or fixed_cost
        # 2. Detail: every other value of the range
        if detail:
            curve.update(evaluate(values[1:-1]))
        tested = values if detail else ends
        rows = []
        for value in tested:
            profits, co2 = curve[value]
            order = _ranked(profits)
            rows.append({
                "Parameter Value": value,
                # Rounded: the linspace values miss the base by ~1e-16 (e.g. -1.7e-14% at the base value)
                "Change vs Base (%)": round((value - base_value) / abs(base_value) * 100, 9),
                **{f"Expected Profit - {label}": profits[label] for label in labels},
                **_ranking_columns(order),
                "Expected Profit Change vs Base (%)": (order.iloc[0] - base_profit) / abs(base_profit) * 100,
                "Expected CO2 (kg/year)": co2[order.index[0]],
                "Expected CO2 Change vs Base (%)": (co2[order.index[0]] - base_kg) / base_kg * 100,
            })
        df_curve = pd.DataFrame(rows)
        ends_text = ", ".join(f"{r['Change vs Base (%)']:+.0f}% {r['Expected Profit Change vs Base (%)']:+.2f}% "
                              f"({r['Recommended Network']})" for r in (df_curve.iloc[0], df_curve.iloc[-1]))

        if not detail:
            near = [value for value in values if any(np.isclose(value / base_value, m) for m in (0.9, 1.1))]
            tables = _fixed_network_results(base_params, df_scenarios, [networks[labels.index(recommended)]],
                                            [((key, value, row, col),) for value in near], n_workers)
            co2_near = {value / base_value: (t["co2"][recommended].mean() - base_kg) / base_kg * 100
                        for value, t in zip(near, tables)}
            results.append({"spec": spec, "base_value": base_value, "curve": df_curve, "changes": None,
                            "detail": False, "method": "re-solved", "co2_near": co2_near})
            print(f"  [{i}/{len(cases)}] {spec['label']}: {ends_text} -> screening only (impact below "
                  f"{min_impact_pct:g}%)")
            continue
        steps = 40 if fixed_cost else iterations
        best = df_curve["Recommended Network"].tolist()
        changes = []
        for (low, net_low), (high, net_high) in zip(zip(values, best), zip(values[1:], best[1:])):
            if net_low != net_high:
                changes += _network_changes(best_at, low, high, steps, known={low: net_low, high: net_high})
        if fixed_cost:  # also beyond the tested range (no solver run)
            if best_at(0.0) != best[0]:
                changes += _network_changes(best_at, 0.0, values[0], steps, known={values[0]: best[0]})
            far = next((values[-1] * 2 ** j for j in range(1, 11) if best_at(values[-1] * 2 ** j) != best[-1]), None)
            if far is not None:
                changes += _network_changes(best_at, far / 2, far, steps, known={far / 2: best[-1]})
        changes.sort(key=lambda change: abs(change[0] - base_value))
        results.append({"spec": spec, "base_value": base_value, "curve": df_curve, "changes": changes,
                        "detail": True, "method": "formula (no solver run)" if fixed_cost else "re-solved"})
        print(f"  [{i}/{len(cases)}] {spec['label']}: {ends_text}"
              + (f" | recommended network changes: {', '.join(_change_text(c, base_value) for c in changes)}"
                 if changes else "") + " -> detail")
    return base_expected, results


def _sort_by_impact(df_summary, impact_column):
    """Summary sorted by impact_column (rounded to 9 decimals: no solver residuals) and parameter name."""
    return df_summary.sort_values([impact_column, "Parameter"], ascending=[False, True],
                                  key=lambda col: col.round(9) if col.name == impact_column else col
                                  ).reset_index(drop=True)


def _sensitivity_summary(results, recommended, min_impact_pct=2.0):
    """One row per parameter, largest impact first: returns (df_summary, low_label, high_label)."""
    first = results[0]["curve"]["Change vs Base (%)"]
    low_label, high_label = f"{first.iloc[0]:+.0f}%", f"{first.iloc[-1]:+.0f}%"
    rows, n_sheets = [], 0
    for result in results:
        curve, base_value, spec, detail = result["curve"], result["base_value"], result["spec"], result["detail"]
        ratio = curve["Parameter Value"] / base_value
        n_sheets += detail

        def at(multiplier, column, curve=curve, ratio=ratio, detail=detail):  # at multiplier x base, if tested
            if not detail:
                return "-"
            match = curve.loc[np.isclose(ratio, multiplier), column]
            return match.iloc[0] if len(match) else np.nan

        def co2_at(multiplier, result=result):  # without the detail: recommended network kept fixed (co2_near)
            if result["detail"]:
                return at(multiplier, "Expected CO2 Change vs Base (%)")
            match = [change for ratio_near, change in result["co2_near"].items() if np.isclose(ratio_near, multiplier)]
            return match[0] if match else np.nan

        def advantage(r):  # expected profit of `recommended` minus the best other network's (no warehouse included)
            profits = r[[column for column in curve.columns if column.startswith("Expected Profit - ")]]
            profits.index = profits.index.str.removeprefix("Expected Profit - ")
            return profits[recommended] - profits.drop(recommended).max()

        low, high = curve.iloc[0], curve.iloc[-1]
        column = "Expected Profit Change vs Base (%)"
        profit = {"low": low[column], "-10": at(0.9, column), "+10": at(1.1, column), "high": high[column]}
        changes = result["changes"] or []
        within = [c for c in changes if spec["values"][0] <= c[0] <= spec["values"][-1]]
        rows.append({
            "Parameter": spec["label"],
            "Method": result["method"],
            "Base Value": base_value,
            "Minimum Value Tested": spec["values"][0],
            "Maximum Value Tested": spec["values"][-1],
            f"Expected Profit Change at {low_label} (%)": profit["low"],
            "Expected Profit Change at -10% (%)": profit["-10"],
            "Expected Profit Change at +10% (%)": profit["+10"],
            f"Expected Profit Change at {high_label} (%)": profit["high"],
            "Expected Profit Impact ±10% (%)": (np.nanmax([abs(profit["-10"]), abs(profit["+10"])]) if detail
                                                else "-"),
            f"Expected Profit Impact ±{high_label[1:]} (%)": max(abs(profit["low"]), abs(profit["high"])),
            f"Recommended Network at {low_label}": low["Recommended Network"],
            f"Recommended Network at {high_label}": high["Recommended Network"],
            f"Advantage of the Recommended Network at {low_label} (EUR/year)": advantage(low),
            f"Advantage of the Recommended Network at {high_label} (EUR/year)": advantage(high),
            "Recommended Network Changes in the Range": "Yes" if within else ("No" if detail else "No (ends only)"),
            "Exact Recommended Network Changes": ", ".join(_change_text(c, base_value) for c in changes) or "-",
            f"Expected CO2 Change at {low_label} (%)": low["Expected CO2 Change vs Base (%)"],
            "Expected CO2 Change at -10% (%)": co2_at(0.9),
            "Expected CO2 Change at +10% (%)": co2_at(1.1),
            f"Expected CO2 Change at {high_label} (%)": high["Expected CO2 Change vs Base (%)"],
            "Networks Recommended": ", ".join(curve["Recommended Network"].unique()),
            "Detail Sheet": f"S{n_sheets:02d}" if detail else f"- (impact below {min_impact_pct:g}%)",
        })
    df_summary = _sort_by_impact(pd.DataFrame(rows), f"Expected Profit Impact ±{high_label[1:]} (%)")
    return df_summary, low_label, high_label


def build_default_sensitivity_cases(params, min_multiplier=0.5, max_multiplier=1.5, n_points=11):
    """Sensitivity cases, each with n_points values from min to max multiplier x base."""
    cases = []

    def add_case(label, key, unit, **cell):  # cell: optional row=/col= selecting the cell
        base_value = _get_param_value(params, key, **cell)
        values = np.linspace(base_value * min_multiplier, base_value * max_multiplier, n_points).tolist()
        cases.append({"label": label, "key": key, **cell, "unit": unit, "values": values})

    add_case("Selling Price", "selling_price", "EUR/unit")
    add_case("CO2 Offsetting Price", "offsetting_cost", "EUR/ton CO2")
    for warehouse in params["fix_cost"].index:
        add_case(f"CAPEX {warehouse}", "fix_cost", "EUR", row=warehouse)
    for warehouse in params["opex"].index:
        add_case(f"Annual OPEX {warehouse}", "opex", "EUR/year", row=warehouse)
    add_case("Discount Rate", "discount_rate", "per year")
    for plant in params["variable_pl_cost"].index:
        add_case(f"Production Cost {plant}", "variable_pl_cost", "EUR/unit", row=plant)
    for plant in params["freight_cost_pl_wh"].index:
        for warehouse in params["freight_cost_pl_wh"].columns:
            add_case(f"Transport {plant} to {warehouse}", "freight_cost_pl_wh", "EUR/unit", row=plant, col=warehouse)
    for warehouse in params["freight_cost_wh_mkt"].index:
        for market in params["freight_cost_wh_mkt"].columns:
            if warehouse != market:
                add_case(f"Distribution {warehouse} to {market}", "freight_cost_wh_mkt", "EUR/unit", row=warehouse,
                         col=market)
    return cases


def run_default_sensitivity_suite(base_params, df_scenarios, output_dir="sensitivity_analysis", min_multiplier=0.5,
                                  max_multiplier=1.5, n_points=11, n_workers=1, min_impact_pct=2.0):
    """Sensitivity of the recommended network: sensitivity_tornado.png and the Excel report."""
    output_dir = os.path.abspath(output_dir)
    os.makedirs(output_dir, exist_ok=True)
    print(f"Sensitivity analysis output folder: {output_dir}")
    cases = build_default_sensitivity_cases(base_params, min_multiplier, max_multiplier, n_points)
    n_solved = sum(case["key"] not in FIXED_COST_KEYS for case in cases)
    _print_header("SENSITIVITY ANALYSIS - RECOMMENDED NETWORK (EXPECTED PROFIT, NETWORK FIXED)")
    print(f"Parameters analyzed: {len(cases)} ({n_solved} re-solved, {len(cases) - n_solved} by formula) | Range: "
          f"{min_multiplier:.0%} - {max_multiplier:.0%} of the base value | Points per parameter: {n_points} | "
          f"{len(df_scenarios)} demand scenarios")
    base_expected, results = run_expected_profit_sensitivity(base_params, df_scenarios, cases, n_workers=n_workers,
                                                             min_impact_pct=min_impact_pct)
    df_summary, low_label, high_label = _sensitivity_summary(results, base_expected.index[0], min_impact_pct)
    plot_sensitivity_tornado(df_summary, low_label, high_label, base_expected, len(df_scenarios),
                             filename=os.path.join(output_dir, "sensitivity_tornado.png"))
    excel_path = export_sensitivity_results(results, df_summary, output_dir, len(df_scenarios))
    print(f"Sensitivity analysis completed: {len(cases)} parameters, "
          f"{sum(result['detail'] for result in results)} with the detail (own sheet and chart).")
    return results, df_summary, excel_path


def run_n_years_analysis(base_params, base_demand, df_scenarios, years, current_n_years=None,
                         output_dir="sensitivity_analysis", n_workers=1, expected_profits=None):
    """N_YEARS analysis: base-demand optimum, Monte Carlo frequencies and (with expected_profits)
    recommended network for each horizon, with the tipping points; Excel report with the charts."""
    _print_header("PLANNING HORIZON (N_YEARS) ANALYSIS")
    plants, warehouses = list(base_params["cap_pl"].index), list(base_params["cap_wh"].index)
    rate = _get_param_value(base_params, "discount_rate")
    years = list(years)
    candidates = [list(c) for c in _all_networks(warehouses)[1:]]
    labels = [_config_display(c) for c in candidates]
    colors = _network_colors(labels)

    def params_for(n_years):
        return _set_param_value(base_params, "n_years", n_years)

    def free_optimum(params, demand_values):
        _, y_sol, _, breakdown = _solve_with_breakdown(params, demand_values)
        if breakdown is None:
            return None
        return _config_display(_open_warehouses(y_sol))

    # 1. Base demand: profit of every configuration and free-model optimum for each horizon
    base_rows = []
    for n_years in years:
        params_n = params_for(n_years)
        warehouse_cost = _unit_costs(params_n, plants, warehouses, list(base_demand))["fix"]
        row = {"N_YEARS": n_years, "CAPEX Annualization Factor": _capital_recovery_factor(rate, n_years),
               **{f"Warehouse Annual Cost - {k}": warehouse_cost[k] for k in warehouses}}
        for config, label in zip(candidates, labels):
            fixed_config = {k: int(k in config) for k in warehouses}
            _, _, _, breakdown = _solve_with_breakdown(params_n, base_demand, fixed_config)
            row[f"Profit - {label}"] = breakdown[ROW_PROFIT] if breakdown is not None else np.nan
        row["Optimal Configuration"] = free_optimum(params_n, base_demand)
        base_rows.append(row)
    df_base = pd.DataFrame(base_rows)
    profit_cols = [f"Profit - {label}" for label in labels]
    df_base["Gap Best vs Second Best"] = df_base[profit_cols].apply(lambda r: np.diff(np.sort(r.dropna()))[-1]
                                                                    if r.notna().sum() > 1 else np.nan, axis=1)

    tipping_rows = []

    def add_tipping(analysis, prev, curr, from_config, to_config, exact="-"):
        tipping_rows.append({"Analysis": analysis, "From Configuration": from_config, "To Configuration": to_config,
                             "Between N_YEARS": f"{prev['N_YEARS']} - {curr['N_YEARS']}",
                             "Exact Break-even N_YEARS": exact})

    for prev, curr in zip(base_rows, base_rows[1:]):
        if prev["Optimal Configuration"] != curr["Optimal Configuration"]:
            low, high = prev["N_YEARS"], curr["N_YEARS"]
            for _ in range(20):  # bisection: break-even located within (high - low) / 2^20 years
                mid = (low + high) / 2
                if free_optimum(params_for(mid), base_demand) == prev["Optimal Configuration"]:
                    low = mid
                else:
                    high = mid
            add_tipping("Base demand (optimal configuration)", prev, curr, prev["Optimal Configuration"],
                        curr["Optimal Configuration"], (low + high) / 2)

    # 3a. Recommended network: expected profits moved by the change of the annual warehouse cost (no solver run)
    expected_rows = []
    if expected_profits is not None:
        networks = _all_networks(warehouses)
        network_labels = [_config_display(list(network)) for network in networks]
        reference = pd.Series([expected_profits[network] if network else 0.0 for network in networks],
                              index=network_labels)

        def expected_at(n_years):  # networks from the highest expected profit (ties: network order)
            return _ranked(_expected_with_warehouse_cost(reference, base_params, params_for(n_years), networks))

        for n_years in years:
            order = expected_at(n_years)
            expected_rows.append({"N_YEARS": n_years,
                                  **{f"Expected Profit - {label}": order[label] for label in network_labels},
                                  **_ranking_columns(order)})
        for prev, curr in zip(expected_rows, expected_rows[1:]):
            if prev["Recommended Network"] != curr["Recommended Network"]:
                known = {prev["N_YEARS"]: prev["Recommended Network"], curr["N_YEARS"]: curr["Recommended Network"]}
                for value, below, above in _network_changes(lambda n_years: expected_at(n_years).index[0],
                                                            prev["N_YEARS"], curr["N_YEARS"], 40, known=known):
                    add_tipping("Expected profit, network fixed (recommended network)", prev, curr, below, above,
                                value)

    # 2. Monte Carlo. only_full_service[i]: the only configuration that can hold all the demand of scenario i
    warehouse_cap = base_params["cap_wh"].iloc[:, 0]
    plant_capacity = float(base_params["cap_pl"].iloc[:, 0].sum())
    only_full_service = []
    for total_demand in df_scenarios.sum(axis=1):
        with_capacity = [label for config, label in zip(candidates, labels)
                         if total_demand <= plant_capacity and warehouse_cap[config].sum() >= total_demand]
        only_full_service.append(with_capacity[0] if len(with_capacity) == 1 else None)

    n_scenarios = len(df_scenarios)
    results = _solve_many([((("n_years", n_years, None, None),), scenario, None) for n_years in years
                           for scenario in range(1, n_scenarios + 1)], base_params, df_scenarios, n_workers)
    free_optima = [_config_display(_open_warehouses(y_sol)) if breakdown is not None else None
                   for _, y_sol, _, breakdown in results]
    free_profits = [breakdown[ROW_PROFIT] if breakdown is not None else np.nan for _, _, _, breakdown in results]
    mc_rows = []
    for j, n_years in enumerate(years):
        optimal = free_optima[j * n_scenarios:(j + 1) * n_scenarios]
        counts = {label: optimal.count(label) for label in labels}
        only_full = {label: sum(opt == only == label for opt, only in zip(optimal, only_full_service))
                     for label in labels}
        feasible = sum(counts.values())
        most_frequent = max(labels, key=lambda label: counts[label])
        majority = next((label for label in labels if counts[label] > feasible / 2), "No majority")
        mc_rows.append({"N_YEARS": n_years, "Feasible Scenarios": feasible,
                        **{f"N. Scenarios Optimal - {label}": counts[label] for label in labels},
                        **{f"Share Optimal - {label}": counts[label] / feasible if feasible else np.nan
                           for label in labels},
                        **{f"N. Scenarios Optimal, Only One Serving All Demand - {label}": only_full[label]
                           for label in labels},
                        "Most Frequently Optimal Configuration": most_frequent,
                        "Most Frequently Optimal Frequency": counts[most_frequent],
                        "Majority Configuration (>50%)": majority})
        print(f"  N_YEARS = {n_years:>2}: " + ", ".join(f"{label} {counts[label]}" for label in labels)
              + f" (most frequently optimal: {most_frequent})")
    df_mc = pd.DataFrame(mc_rows)

    # 3b. Recommended network: WS (free model on every scenario, part 2), RP, EEV, EVPI and VSS for each horizon
    for j, (row, base_row) in enumerate(zip(expected_rows, base_rows)):
        ws = np.nanmean(free_profits[j * n_scenarios:(j + 1) * n_scenarios])
        rp = row[f"Expected Profit - {row['Recommended Network']}"]
        eev_network = base_row["Optimal Configuration"]
        eev = row[f"Expected Profit - {eev_network}"] if eev_network is not None else np.nan
        row.update({"Base-Demand Optimal Configuration": eev_network if eev_network is not None else "-",
                    "WS (EUR/year)": ws, "RP (EUR/year)": rp, "EEV (EUR/year)": eev,
                    "EVPI = WS - RP (EUR/year)": ws - rp, "VSS = RP - EEV (EUR/year)": rp - eev})
        print(f"  N_YEARS = {row['N_YEARS']:>2}: recommended {row['Recommended Network']} "
              f"(+{row['Advantage Over Second Best (EUR/year)']:,.0f} over {row['Second Best Network']}) | "
              f"EVPI EUR {ws - rp:,.0f}/year, VSS EUR {rp - eev:,.0f}/year")
    df_expected = _drop_empty_network(pd.DataFrame(expected_rows), ("Expected Profit",)) if expected_rows else None
    for prev, curr in zip(mc_rows, mc_rows[1:]):
        for column, analysis in (("Most Frequently Optimal Configuration",
                                  "Monte Carlo (most frequently optimal configuration)"),
                                 ("Majority Configuration (>50%)", "Monte Carlo (majority of scenarios, >50%)")):
            if prev[column] != curr[column]:
                add_tipping(analysis, prev, curr, prev[column], curr[column])
        for label in labels:
            was, now = prev[f"N. Scenarios Optimal - {label}"] > 0, curr[f"N. Scenarios Optimal - {label}"] > 0
            if was != now:
                add_tipping("Monte Carlo (" + ("no longer optimal in any scenario)" if was
                                               else "optimal again in some scenario)"), prev, curr, label, "-")
    df_tipping = pd.DataFrame(tipping_rows, columns=["Analysis", "From Configuration", "To Configuration",
                                                     "Between N_YEARS", "Exact Break-even N_YEARS"])
    for row in tipping_rows:
        exact = row["Exact Break-even N_YEARS"]
        print(f"  Tipping point [{row['Analysis']}]: {row['From Configuration']} -> {row['To Configuration']} "
              f"between N_YEARS {row['Between N_YEARS']}" + (f" (break-even {exact:.2f} years)"
                                                             if exact != "-" else ""))

    settings = pd.DataFrame({"Setting": ["Discount Rate", "Current N_YEARS", "Monte Carlo Scenarios"],
                             "Value": [rate, current_n_years if current_n_years is not None else "-",
                                       len(df_scenarios)]})
    images = {}  # chart of each sheet, right of its table

    def mark_current(ax):
        if current_n_years is not None:
            ax.axvline(current_n_years, color="black", linestyle=":", linewidth=1.5)

    def shade(ax_top, ax_bottom, analysis, first, colors, title, dx):  # background by network, break-even lines
        tipping = [r for r in tipping_rows if r["Analysis"] == analysis]
        edges = [years[0] - 0.5] + [r["Exact Break-even N_YEARS"] for r in tipping] + [years[-1] + 0.5]
        segments = [first] + [r["To Configuration"] for r in tipping]
        for (start, end), label in zip(zip(edges, edges[1:]), segments):
            for ax in (ax_top, ax_bottom):
                ax.axvspan(start, end, color=colors.get(label, "lightgray"), alpha=0.08)
            ax_top.text((start + end) / 2, 1.01, f"{title}: {label}", transform=ax_top.get_xaxis_transform(),
                        ha="center", va="bottom", fontsize=10, fontweight="bold", color=colors.get(label, "black"))
        for edge in edges[1:-1]:
            for ax in (ax_top, ax_bottom):
                ax.axvline(edge, color="red", linestyle="--", alpha=0.7)
            ax_bottom.annotate(f"Break-even\n{edge:.2f} years", xy=(edge, 0), xytext=(dx, 40),
                               ha="left" if dx > 0 else "right", textcoords="offset points", fontsize=9,
                               color="red", fontweight="bold")

    # Chart 1: equivalent annual profit of each configuration (top) and its gap to the most profitable (bottom)
    fig, (ax_cost, ax_gap) = plt.subplots(2, 1, figsize=(12, 9), sharex=True, gridspec_kw={"height_ratios": [3, 2]})
    best = df_base[profit_cols].max(axis=1)
    for label, col in zip(labels, profit_cols):
        ax_cost.plot(df_base["N_YEARS"], df_base[col] / 1e6, marker="o", markersize=4, linewidth=2,
                     color=colors[label], label=label)
        ax_gap.plot(df_base["N_YEARS"], (best - df_base[col]) / 1e3, marker="o", markersize=4, linewidth=2,
                    color=colors[label], label=label)
    shade(ax_cost, ax_gap, "Base demand (optimal configuration)", df_base["Optimal Configuration"].iloc[0], colors,
          "Optimal", 8)
    mark_current(ax_cost)
    mark_current(ax_gap)
    ax_cost.set_ylabel("Equivalent annual profit (EUR M)", fontsize=12)
    ax_cost.set_title("Planning Horizon Analysis - Base Demand: Equivalent Annual Profit vs N_YEARS\n"
                      f"(network fixed to each configuration, discount rate {rate:.1%}, dotted line = current "
                      f"N_YEARS)", fontsize=13, pad=22)
    ax_cost.legend(title="Configuration", loc="lower right")
    ax_cost.grid(alpha=0.3)
    ax_gap.set_ylabel("Profit gap to the best configuration (EUR k)", fontsize=12)
    ax_gap.set_xlabel("N_YEARS (planning horizon, years)", fontsize=12)
    ax_gap.set_xticks(years)
    ax_gap.grid(alpha=0.3)
    fig.tight_layout()
    images["Base_Demand"] = _figure_png(fig)

    # Chart 2: share of the scenarios where each configuration is optimal (hatched: the only one serving all)
    fig, ax = plt.subplots(figsize=(13, 6.5))
    bottom = np.zeros(len(df_mc))
    for label in reversed(labels):
        shares = df_mc[f"Share Optimal - {label}"].fillna(0).to_numpy() * 100
        ax.bar(df_mc["N_YEARS"], shares, bottom=bottom, color=colors[label], edgecolor="white", label=label)
        only_full = df_mc[f"N. Scenarios Optimal, Only One Serving All Demand - {label}"].to_numpy()
        if only_full.any():
            ax.bar(df_mc["N_YEARS"], only_full / df_mc["Feasible Scenarios"].to_numpy() * 100, bottom=bottom,
                   color=colors[label], edgecolor="white", hatch="//", label=f"{label}: only one serving all demand")
        for x, share, base, count in zip(df_mc["N_YEARS"], shares, bottom, df_mc[f"N. Scenarios Optimal - {label}"]):
            if share >= 6:
                ax.text(x, base + share / 2, str(count), ha="center", va="center", fontsize=8, color="white",
                        fontweight="bold", bbox={"boxstyle": "round,pad=0.15", "facecolor": colors[label],
                                                 "edgecolor": "none"})
        bottom += shares
    n_marked = 0  # consecutive tipping labels alternate between two heights, so close ones do not overlap
    for prev, curr in zip(mc_rows, mc_rows[1:]):
        x = (prev["N_YEARS"] + curr["N_YEARS"]) / 2
        texts = []
        column = "Most Frequently Optimal Configuration"
        if prev[column] != curr[column]:
            texts.append(f"Most frequently optimal:\n{prev[column]} -> {curr[column]}")
        if prev["Majority Configuration (>50%)"] != curr["Majority Configuration (>50%)"]:
            texts.append(f"Majority (>50%):\n{prev['Majority Configuration (>50%)']} -> "
                         f"{curr['Majority Configuration (>50%)']}")
        if texts:
            ax.axvline(x, color="red", linestyle="--", linewidth=2)
            ax.text(x, 103 + 15 * (n_marked % 2), "\n".join(texts), ha="center", va="bottom", fontsize=9,
                    color="red", fontweight="bold",
                    bbox={"facecolor": "white", "edgecolor": "red", "boxstyle": "round,pad=0.3"})
            n_marked += 1
    ax.axhline(50, color="gray", linestyle="--", linewidth=1)
    mark_current(ax)
    ax.set_ylim(0, 140)
    ax.set_yticks(range(0, 101, 20))
    ax.set_xticks(years)
    ax.set_xlabel("N_YEARS (planning horizon, years)", fontsize=12)
    ax.set_ylabel("Share of scenarios where optimal (%)", fontsize=12)
    ax.set_title("Planning Horizon Analysis - Monte Carlo: How Often Each Configuration Is Optimal\n"
                 f"({len(df_scenarios)} demand scenarios, discount rate {rate:.1%}, numbers = scenarios, "
                 f"dotted line = current N_YEARS)", fontsize=13, pad=12)
    ax.legend(title="Configuration", loc="upper left", bbox_to_anchor=(1.01, 1))
    fig.tight_layout()
    images["Monte_Carlo"] = _figure_png(fig)

    # Chart 3: expected profit of every network kept fixed (top) and EVPI / VSS (bottom)
    if df_expected is not None:
        fig, (ax_profit, ax_value) = plt.subplots(2, 1, figsize=(12, 9), sharex=True,
                                                  gridspec_kw={"height_ratios": [3, 2]})
        network_colors = _network_colors(network_labels)
        x = df_expected["N_YEARS"]
        for label in network_labels:
            recommended = df_expected["Recommended Network"] == label
            if label == NO_WAREHOUSE and not recommended.any():  # profit 0: it would only flatten the curves
                continue
            profit = df_expected[f"Expected Profit - {label}"] / 1e6
            ax_profit.plot(x, profit, marker="o", markersize=4, linewidth=2, color=network_colors[label], label=label)
            ax_profit.scatter(x[recommended], profit[recommended], s=90, color=network_colors[label],
                              edgecolor="black", zorder=3)
        shade(ax_profit, ax_value, "Expected profit, network fixed (recommended network)",
              df_expected["Recommended Network"].iloc[0], network_colors, "Recommended", -8)
        ax_value.plot(x, df_expected["EVPI = WS - RP (EUR/year)"] / 1e3, marker="o", markersize=4, linewidth=2,
                      color="tab:purple", label="EVPI = WS - RP")
        ax_value.plot(x, df_expected["VSS = RP - EEV (EUR/year)"] / 1e3, marker="s", markersize=4, linewidth=2,
                      color="tab:olive", label="VSS = RP - EEV")
        mark_current(ax_profit)
        mark_current(ax_value)
        ax_profit.set_ylabel("Expected profit, network fixed (EUR M)", fontsize=12)
        ax_profit.set_title("Planning Horizon Analysis - Recommended Network: Expected Profit vs N_YEARS\n"
                            f"(each network kept fixed on {len(df_scenarios)} demand scenarios, large markers = "
                            f"recommended, discount rate {rate:.1%}, dotted line = current N_YEARS)", fontsize=13,
                            pad=22)
        ax_profit.legend(title="Configuration", loc="lower right")
        ax_profit.grid(alpha=0.3)
        ax_value.set_ylabel("EUR k / year", fontsize=12)
        ax_value.set_xlabel("N_YEARS (planning horizon, years)", fontsize=12)
        ax_value.set_xticks(years)
        ax_value.legend(loc="upper right")
        ax_value.grid(alpha=0.3)
        fig.tight_layout()
        images["Expected_Profit"] = _figure_png(fig)
    _write_excel(output_dir, "n_years_analysis.xlsx",
                 {"Tipping_Points": df_tipping, "Base_Demand": df_base,
                  "Monte_Carlo": df_mc.loc[:, ~df_mc.columns.str.contains("Only One Serving")],  # chart hatching only
                  **({"Expected_Profit": df_expected} if df_expected is not None else {}), "Settings": settings},
                 "N_YEARS analysis report generated", cell_format=_analysis_format, images=images)
    return df_base, df_mc, df_tipping


def run_capacity_analysis(base_params, df_scenarios, capacities, output_dir="sensitivity_analysis", n_workers=1):
    """Expected profit of every fixed network and scenarios where it is optimal, per warehouse capacity."""
    _print_header("WAREHOUSE CAPACITY ANALYSIS - RECOMMENDED NETWORK (EXPECTED PROFIT, NETWORK FIXED)")
    warehouses = list(base_params["cap_wh"].index)
    networks = _all_networks(warehouses)
    labels = [_config_display(list(network)) for network in networks]
    print(f"Same capacity for every warehouse: {len(capacities)} values | {len(networks) - 1} networks x "
          f"{len(df_scenarios)} scenarios each")
    rows = []
    for i, capacity in enumerate(capacities, start=1):
        changes = tuple(("cap_wh", capacity, k, None) for k in warehouses)
        tables = _fixed_network_results(base_params, df_scenarios, networks, [changes], n_workers)[0]
        profits, objectives = tables["profit"], tables["objective"]
        expected = _ranked(profits.mean())
        optimal = objectives.idxmax(axis=1)
        counts = {label: int((optimal == label).sum()) for label in labels}
        top = max(counts.values())
        rows.append({"Capacity per Warehouse (units/year)": capacity,
                     **{f"Expected Profit - {label}": expected[label] for label in labels},
                     **_ranking_columns(expected),
                     **{f"N. Scenarios Optimal - {label}": counts[label] for label in labels},
                     "Most Frequently Optimal Network": " / ".join(label for label in labels if counts[label] == top)})
        print(f"  [{i}/{len(capacities)}] Capacity {capacity:,.0f} units/year per warehouse: recommended "
              f"{expected.index[0]} (+{expected.iloc[0] - expected.iloc[1]:,.0f} EUR/year over {expected.index[1]}) "
              f"| optimal in: " + ", ".join(f"{label} {counts[label]}" for label in labels if counts[label]))
    df_capacity = _drop_empty_network(pd.DataFrame(rows), ("Expected Profit", "N. Scenarios Optimal"))
    labels = [label for label in labels if f"Expected Profit - {label}" in df_capacity.columns]

    # Chart, inside the Excel file: expected profit of each network (top) and scenarios where it is optimal (bottom)
    colors = {**_network_colors(labels), NO_WAREHOUSE: "lightgray"}
    x = df_capacity["Capacity per Warehouse (units/year)"].to_numpy() / 1e6
    fig, (ax_profit, ax_count) = plt.subplots(2, 1, figsize=(12, 9), sharex=True,
                                              gridspec_kw={"height_ratios": [3, 2]})
    for i, label in enumerate(labels):  # alternate line styles, so networks with almost equal profits stay visible
        ax_profit.plot(x, df_capacity[f"Expected Profit - {label}"] / 1e6, marker="o", linewidth=2,
                       linestyle="--" if i % 2 else "-", color=colors[label], label=label)
    low, high = ax_profit.get_ylim()
    ax_profit.set_ylim(low, high + (high - low) * 0.1)  # room for the labels above the best network
    for xi, (_, r) in zip(x, df_capacity.iterrows()):
        best = r["Recommended Network"]
        ax_profit.annotate(f"+{r['Advantage Over Second Best (EUR/year)']:,.0f}",
                           (xi, r[f"Expected Profit - {best}"] / 1e6), xytext=(0, 8), textcoords="offset points",
                           ha="center", fontsize=9, fontweight="bold", color=colors[best], zorder=3, bbox=LABEL_BOX)
    bottom = np.zeros(len(x))
    for label in reversed(labels):  # networks with more warehouses at the bottom, as in the N_YEARS Monte Carlo chart
        counts = df_capacity[f"N. Scenarios Optimal - {label}"].to_numpy()
        ax_count.bar(x, counts, bottom=bottom, width=0.6, color=colors[label], edgecolor="white", label=label)
        for xi, count, base in zip(x, counts, bottom):
            if count >= 3:  # background in the bar's color, so the current capacity line does not cross the number
                ax_count.text(xi, base + count / 2, str(count), ha="center", va="center", fontsize=8, color="white",
                              fontweight="bold", zorder=3, bbox={"boxstyle": "round,pad=0.15",
                                                                 "facecolor": colors[label], "edgecolor": "none"})
        bottom += counts
    for capacity in sorted(set(base_params["cap_wh"].iloc[:, 0])):  # current capacity
        for ax in (ax_profit, ax_count):
            ax.axvline(capacity / 1e6, color="black", linestyle=":", linewidth=1.5)
    ax_profit.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:,.2f}"))
    ax_profit.set_ylabel("Expected profit, network fixed (EUR M/year)", fontsize=12)
    ax_profit.set_title("Warehouse Capacity Analysis - Recommended Network\n(same capacity for every warehouse; every "
                        f"network kept fixed on {len(df_scenarios)} demand scenarios)\nnumbers = best network's "
                        "advantage over the second best (EUR/year); dotted line = current capacity", fontsize=13)
    ax_profit.legend(title="Network (fixed)", loc="lower right")
    ax_profit.grid(alpha=0.3)
    ax_count.set_ylim(0, len(df_scenarios))
    ax_count.set_ylabel("Scenarios where optimal", fontsize=12)
    ax_count.set_xlabel("Capacity per warehouse (M units/year)", fontsize=12)
    ax_count.set_xticks(x)
    ax_count.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    _write_excel(output_dir, "capacity_analysis.xlsx", {"Capacity": df_capacity}, "Capacity analysis report generated",
                 cell_format=_analysis_format, images={"Capacity": _figure_png(fig)})
    return df_capacity


def run_cv_analysis(base_params, cvs, n_scenarios=50, current_cv=None, output_dir="sensitivity_analysis",
                    n_workers=1):
    """Expected profit of every fixed network and break-even N_YEARS of the recommended one, for each CV
    (scenarios generated again with the same seed); Excel report with the chart."""
    _print_header("DEMAND UNCERTAINTY (CV) ANALYSIS - RECOMMENDED NETWORK (EXPECTED PROFIT, NETWORK FIXED)")
    base_demand = _demand_dict(base_params["demand"])
    networks = _all_networks(list(base_params["cap_wh"].index))
    labels = [_config_display(list(network)) for network in networks]
    solved = [network for network in networks if network]
    n_years = _get_param_value(base_params, "n_years")
    warehouse_capacity = float(base_params["cap_wh"].iloc[:, 0].max())
    plant_capacity = float(base_params["cap_pl"].iloc[:, 0].sum())
    print(f"{len(cvs)} CV values | {len(solved)} networks x {n_scenarios} scenarios each | N_YEARS = {n_years:g}")

    all_scenarios = [generate_demand_scenarios(base_demand, n_scenarios, cv) for cv in cvs]
    # The scenarios of every CV in one table (and one batch): those of the j-th CV are rows j * n + 1 .. (j + 1) * n
    tables = _fixed_network_results(base_params, pd.concat(all_scenarios, ignore_index=True), networks,
                                    n_workers=n_workers)[0]
    rows = []
    for j, (cv, df) in enumerate(zip(cvs, all_scenarios)):
        frames = {name: tables[name].loc[j * n_scenarios + 1:(j + 1) * n_scenarios]
                  for name in ("profit", "objective")}
        expected = frames["profit"].mean()
        order = _ranked(expected)
        recommended = order.index[0]
        optimal = frames["objective"].idxmax(axis=1)
        counts = {label: int((optimal == label).sum()) for label in labels}

        def best_at(years, expected=expected):  # network fixed: N_YEARS only changes the annual warehouse cost
            return _ranked(_expected_with_warehouse_cost(expected, base_params,
                                                         _set_param_value(base_params, "n_years", years),
                                                         networks)).index[0]

        break_even, below = "-", "-"
        if best_at(1) != recommended:
            break_even, below, _ = _network_changes(best_at, 1, n_years, 40, known={n_years: recommended})[-1]
        total = df.sum(axis=1)
        rows.append({"CV": cv, "Mean Total Demand (units/year)": total.mean(),
                     f"N. Scenarios Total Demand > One Warehouse ({warehouse_capacity:,.0f})":
                         int((total > warehouse_capacity).sum()),
                     f"N. Scenarios Total Demand > Plants ({plant_capacity:,.0f})": int((total > plant_capacity).sum()),
                     "N. Market Demands Set to 0": int((df == 0).sum().sum()),
                     **{f"Expected Profit - {label}": expected[label] for label in labels},
                     **_ranking_columns(order),
                     **{f"N. Scenarios Optimal - {label}": counts[label] for label in labels},
                     "Break-even N_YEARS of the Recommended Network": break_even,
                     "Recommended Network Below the Break-even": below})
        print(f"  CV {cv:.2f}: recommended {recommended} (+{order.iloc[0] - order.iloc[1]:,.0f} EUR/year over "
              f"{order.index[1]}) | break-even N_YEARS "
              + (f"{break_even:.2f} (below: {below})" if break_even != "-" else "- (recommended from 1 year)")
              + f" | total demand > plants' capacity in {int((total > plant_capacity).sum())} scenarios | optimal in: "
              + ", ".join(f"{label} {counts[label]}" for label in labels if counts[label]))
    df_cv = _drop_empty_network(pd.DataFrame(rows), ("Expected Profit", "N. Scenarios Optimal"))

    # Chart, inside the Excel file: advantage of the recommended network over the second best (top) and its
    # break-even N_YEARS (bottom)
    colors = _network_colors(labels)
    x = df_cv["CV"].to_numpy()
    fig, (ax_advantage, ax_years) = plt.subplots(2, 1, figsize=(12, 9), sharex=True,
                                                 gridspec_kw={"height_ratios": [3, 2]})
    advantage = df_cv["Advantage Over Second Best (EUR/year)"].to_numpy() / 1e3
    ax_advantage.plot(x, advantage, color="gray", linewidth=2, zorder=1)
    for label in df_cv["Recommended Network"].unique():
        is_label = (df_cv["Recommended Network"] == label).to_numpy()
        ax_advantage.scatter(x[is_label], advantage[is_label], s=90, color=colors[label], edgecolor="black",
                             zorder=2, label=label)
    for xi, value, r in zip(x, advantage, df_cv.to_dict("records")):  # white background: no line crosses the label
        ax_advantage.annotate(f"+{r['Advantage Over Second Best (EUR/year)']:,.0f}\nover {r['Second Best Network']}",
                              (xi, value), xytext=(0, 10), textcoords="offset points", ha="center", fontsize=9,
                              fontweight="bold", color=colors[r["Recommended Network"]], zorder=3, bbox=LABEL_BOX)
    low, high = ax_advantage.get_ylim()
    ax_advantage.set_ylim(min(0, low), high + (high - min(0, low)) * 0.15)  # room for the labels
    break_even = pd.to_numeric(df_cv["Break-even N_YEARS of the Recommended Network"], errors="coerce").to_numpy()
    ax_years.plot(x, break_even, marker="o", markersize=6, linewidth=2, color="tab:red")
    for xi, value in zip(x, break_even):
        if not np.isnan(value):
            ax_years.annotate(f"{value:.2f}", (xi, value), xytext=(8, 4), textcoords="offset points", ha="left",
                              fontsize=9, fontweight="bold", color="tab:red", zorder=3, bbox=LABEL_BOX)
    ax_years.axhline(n_years, color="black", linestyle=":", linewidth=1.5)
    ax_years.text(x[-1], n_years, f"current N_YEARS = {n_years:g}", ha="right", va="bottom", fontsize=9)
    if current_cv is not None:
        for ax in (ax_advantage, ax_years):
            ax.axvline(current_cv, color="black", linestyle=":", linewidth=1.5)
    ax_advantage.set_ylabel("Advantage over the second best (EUR k/year)", fontsize=12)
    ax_advantage.set_title("Demand Uncertainty (CV) Analysis - Recommended Network\n(every network kept fixed on "
                           f"{n_scenarios} demand scenarios, same seed, N_YEARS = {n_years:g}; dotted line = current "
                           "CV)", fontsize=13)
    ax_advantage.legend(title="Recommended network", loc="upper left")
    ax_advantage.grid(alpha=0.3)
    ax_years.set_ylim(0, max(n_years, np.nanmax(break_even, initial=0)) * 1.15)
    ax_years.set_ylabel("Break-even N_YEARS\n(recommended from)", fontsize=12)
    ax_years.set_xlabel("Coefficient of variation (CV) of the demand of each market", fontsize=12)
    ax_years.set_xticks(x)
    ax_years.set_xticklabels([f"{cv:.2f}" for cv in x])
    ax_years.grid(alpha=0.3)
    fig.tight_layout()
    _write_excel(output_dir, "cv_analysis.xlsx", {"CV": df_cv}, "CV analysis report generated",
                 cell_format=_analysis_format, images={"CV": _figure_png(fig)})
    return df_cv


def run_co2_frontier(base_params, df_scenarios, prices, recommended, cases, output_dir="sensitivity_analysis",
                     n_workers=1, min_co2_change_pct=0.1):
    """Cost-CO2 frontier of every network fixed (CO2 price as the weight), mix margins and local mix
    thresholds of the recommended network; returns {"frontier", "mix_margin", "mix_thresholds"}."""
    _print_header("COST-CO2 FRONTIER (EXPECTED PROFIT BEFORE THE CO2 OFFSETTING VS EXPECTED CO2, NETWORK FIXED)")
    plants, warehouses = list(base_params["cap_pl"].index), list(base_params["cap_wh"].index)
    base_demand = _demand_dict(base_params["demand"])
    pw = [(i, k) for i in plants for k in warehouses]
    wm = [(k, j) for k in warehouses for j in base_demand]
    networks = [network for network in _all_networks(warehouses) if network]
    labels = [_config_display(list(network)) for network in networks]
    recommended_label = _config_display(list(recommended))
    current_price = _get_param_value(base_params, "offsetting_cost")
    prices = sorted(set(prices) | {current_price})
    print(f"{len(networks)} networks x {len(df_scenarios)} scenarios | CO2 prices (EUR/ton CO2): "
          + ", ".join(f"{price:g}" for price in prices) + f" | current: {current_price:g}")

    def evaluate(nets, price_list):  # {(network label, price): expected point of the network fixed at that price}
        all_tables = _fixed_network_results(base_params, df_scenarios, nets,
                                            [(("offsetting_cost", price, None, None),) for price in price_list],
                                            n_workers, flows=True)
        points = {}
        for price, tables in zip(price_list, all_tables):
            for network in nets:
                label = _config_display(list(network))
                kg = tables["co2"][label].mean()
                points[(label, price)] = {
                    "network": network, "label": label, "kg": kg,
                    # Before the offsetting: the solver's objective (no inventory cost) and the reported profit
                    "objective": tables["objective"][label].mean() + kg * price / 1000,
                    "profit": tables["profit"][label].mean() + kg * price / 1000,
                    "x": {(a, b): tables[f"flow {a} -> {b}"][label].mean() for a, b in pw},
                    "w": {(a, b): tables[f"flow {a} -> {b}"][label].mean() for a, b in wm}}
        return points

    def same_mix(a, b):  # the same expected flows (within one unit)
        return all(abs(a[f][route] - b[f][route]) < 1 for f in ("x", "w") for route in a[f])

    def slope(a, b):  # EUR/ton CO2 at which the solver is indifferent between mixes a (more CO2) and b
        return (a["objective"] - b["objective"]) / (a["kg"] - b["kg"]) * 1000

    grid = evaluate(networks, prices)
    tolerance = 1e-6 * max(abs(point["objective"]) for point in grid.values())  # solver accuracy (EUR/year)

    def vertices(points):  # the mixes the solver chooses for some price >= 0, from the most CO2
        hull = []
        for point in sorted(points, key=lambda point: -point["kg"]):
            if hull and same_mix(hull[-1], point):
                continue
            # A point not above the chord of its neighbors (e.g. an in-between solution exactly at a tie) is no mix
            while len(hull) >= 2 and point["kg"] < hull[-1]["kg"] and (
                    hull[-1]["objective"] <= hull[-2]["objective"] + (point["objective"] - hull[-2]["objective"])
                    * (hull[-1]["kg"] - hull[-2]["kg"]) / (point["kg"] - hull[-2]["kg"]) + tolerance):
                hull.pop()
            hull.append(point)
        return hull

    mixes = {}
    for network, label in zip(networks, labels):
        found = vertices([grid[(label, price)] for price in prices])
        checked = set()
        for _ in range(20):  # safety bound: each round adds at least one mix
            pending = [(a, b) for a, b in zip(found, found[1:]) if (id(a), id(b)) not in checked]
            if not pending:
                break
            at_slopes = evaluate([network], [slope(a, b) for a, b in pending])
            new = []
            for a, b in pending:
                checked.add((id(a), id(b)))
                price = slope(a, b)
                point = at_slopes[(label, price)]
                tie = a["objective"] - a["kg"] * price / 1000  # a and b at that price
                if point["objective"] - point["kg"] * price / 1000 > tie + tolerance:
                    new.append(point)
            if not new:
                break
            found = vertices(found + new)
        mixes[label] = found

    def mix_text(point):  # production per plant, e.g. "Gothenburg 13.83 M, Shanghai 4.99 M"
        return ", ".join(f"{i} {sum(point['x'][(i, k)] for k in warehouses) / 1e6:.2f} M" for i in plants)

    all_points = [point for label in labels for point in mixes[label]]

    def efficient(point):  # no point with less (or equal) CO2 and more (or equal) profit before the offsetting
        return not any(other is not point and other["kg"] <= point["kg"] + 1 and other["profit"] >= point["profit"] - 1
                       and (other["kg"] < point["kg"] - 1 or other["profit"] > point["profit"] + 1)
                       for other in all_points)

    frontier_rows, current = [], {}
    for label in labels:
        points = mixes[label]
        for n, point in enumerate(points):
            low = slope(points[n - 1], point) if n > 0 else 0.0
            high = slope(point, points[n + 1]) if n + 1 < len(points) else None
            point.update(low=low, high=high, efficient=efficient(point))
            if low <= current_price and (high is None or current_price < high):
                current[label] = n
            frontier_rows.append({
                "Network": label, "Mix (Production per Plant, units/year)": mix_text(point),
                "Chosen From CO2 Price (EUR/ton CO2)": low,
                "Chosen Up To CO2 Price (EUR/ton CO2)":
                    high if high is not None else f"- (tested up to {prices[-1]:g})",
                "Chosen at the Current Price": "Yes" if current.get(label) == n else "No",
                "Expected CO2 (kg/year)": point["kg"],
                "Expected Profit Before Offsetting (EUR/year)": point["profit"],
                f"Expected Profit at {current_price:g} EUR/ton CO2 (EUR/year)": point["profit"]
                - point["kg"] * current_price / 1000,
                "Expected Inventory Cost (EUR/year)": point["objective"] - point["profit"],
                "Units Sold (units/year)": sum(point["w"].values()),
                "Efficient": "Yes" if point["efficient"] else "No",
                "Cost per Ton Avoided vs Previous Mix, Reported Profit (EUR/ton CO2)":
                    (points[n - 1]["profit"] - point["profit"]) / (points[n - 1]["kg"] - point["kg"]) * 1000
                    if n > 0 else "-"})
        print(f"  {label}: " + " | ".join(
            f"{p['kg']:,.0f} kg, EUR {p['profit']:,.0f} ({mix_text(p)}; from {p['low']:.1f} EUR/t)" for p in points))
    df_frontier = pd.DataFrame(frontier_rows)

    # Neighbor mixes of the recommended network's current one, with the margin on the solver's objective
    points = mixes[recommended_label]
    c = current[recommended_label]
    y_rec = {k: int(k in recommended) for k in warehouses}

    def objective_at(point, params):  # expected solver objective of a mix, its expected flows kept, with params
        breakdown = calculate_cost_breakdown(y_rec, point["x"], point["w"], params, base_demand)
        return breakdown[ROW_PROFIT] + breakdown[ROW_INVENTORY_COST]

    neighbors = [(direction, points[n]) for direction, n in (("More CO2", c - 1), ("Less CO2", c + 1))
                 if 0 <= n < len(points)]
    margin_rows, margin_texts = [], []
    for direction, point in neighbors:
        margin = objective_at(points[c], base_params) - objective_at(point, base_params)
        moved = sum(max(0.0, sum(point["x"][(i, k)] - points[c]["x"][(i, k)] for k in warehouses)) for i in plants)
        margin_texts.append(f"{margin:,.0f} EUR/year over {moved / 1e6:.2f} M units toward the {direction} Mix")
        margin_rows.append({
            "Direction": direction, "Neighbor Mix (Production per Plant, units/year)": mix_text(point),
            "Expected CO2 of the Neighbor (kg/year)": point["kg"],
            "CO2 Change (kg/year)": point["kg"] - points[c]["kg"],
            "CO2 Change (%)": (point["kg"] - points[c]["kg"]) / points[c]["kg"],
            f"Margin of the Current Mix at {current_price:g} EUR/ton CO2 (EUR/year)": margin,
            "Switch at CO2 Price (EUR/ton CO2)": point["high"] if direction == "More CO2" else point["low"],
            **{f"Change of Production {i} (units/year)":
               sum(point["x"][(i, k)] - points[c]["x"][(i, k)] for k in warehouses) for i in plants},
            **{f"Change of Flow {i} -> {k} (units/year)": point["x"][(i, k)] - points[c]["x"][(i, k)] for i, k in pw},
            "Change of Inventory Cost (EUR/year, not seen by the solver)":
                (point["objective"] - point["profit"]) - (points[c]["objective"] - points[c]["profit"])})
        print(f"  {recommended_label}, {direction.lower()} mix: margin EUR {margin:,.0f}/year, CO2 "
              f"{(point['kg'] - points[c]['kg']) / points[c]['kg']:+.1%} ({mix_text(point)})")
    df_margin = pd.DataFrame(margin_rows)

    # Local mix thresholds: base value -/+ margin per moved unit / weight of the parameter on one moved unit
    threshold_rows, checks = [], []
    for spec in [spec for spec in cases if spec["key"] not in ("selling_price", *FIXED_COST_KEYS)]:
        key, row, col = spec["key"], spec.get("row"), spec.get("col")
        base_value = _get_param_value(base_params, key, row=row, col=col)
        step = base_value if base_value else 1.0
        params_step = _set_param_value(base_params, key, base_value + step, row=row, col=col)
        entry = {"Parameter": spec["label"], "Unit": spec["unit"], "Base Value": base_value}
        for direction, point in neighbors:
            g_base = objective_at(points[c], base_params) - objective_at(point, base_params)
            g_step = objective_at(points[c], params_step) - objective_at(point, params_step)
            if abs(g_step - g_base) < 1.0:  # the parameter weighs the same on both mixes (EUR/year over a full step)
                entry.update({f"Threshold Toward the {direction} Mix": "no mix change",
                              f"Threshold Toward the {direction} Mix, Change vs Base (%)": "-",
                              f"Expected CO2 After the Switch, {direction} (kg/year)": "-",
                              f"Verified, {direction}": "-"})
                continue
            moved = sum(max(0.0, sum(point["x"][(i, k)] - points[c]["x"][(i, k)] for k in warehouses)) for i in plants)
            per_unit = abs(point["kg"] - points[c]["kg"]) / moved / 1000 if key == "offsetting_cost" else 1.0
            threshold = base_value - np.sign(g_step - g_base) * g_base / moved / per_unit
            entry.update({f"Threshold Toward the {direction} Mix": threshold,
                          f"Threshold Toward the {direction} Mix, Change vs Base (%)":
                              (threshold - base_value) / abs(base_value) if base_value else np.nan,
                          f"Expected CO2 After the Switch, {direction} (kg/year)": None,  # filled in by the check
                          f"Verified, {direction}": None})
            checks.append((entry, direction, [(key, base_value + (threshold - base_value) * share, row, col)
                                              for share in (0.99, 1.01)]))
        threshold_rows.append(entry)
    # Each threshold checked with the solver at 99% and 101% of the change, the recommended network fixed
    solved = _fixed_network_results(base_params, df_scenarios, [tuple(recommended)],
                                    [(change,) for *_, changes in checks for change in changes], n_workers)
    current_kg = points[c]["kg"]
    for n, (entry, direction, _) in enumerate(checks):
        before, beyond = (solved[2 * n + m]["co2"][recommended_label].mean() for m in (0, 1))
        sign = 1 if direction == "More CO2" else -1
        verified = (abs(before - current_kg) <= current_kg * min_co2_change_pct / 100
                    and sign * (beyond - current_kg) > current_kg * min_co2_change_pct / 100)
        entry.update({f"Expected CO2 After the Switch, {direction} (kg/year)": beyond,
                      f"Verified, {direction}": "Yes" if verified else
                      f"No (expected CO2 {before:,.0f} kg at 99% of the change, {beyond:,.0f} kg at 101%)"})
    n_verified = sum(entry[f"Verified, {direction}"] == "Yes" for entry, direction, _ in checks)
    print(f"  Mix thresholds checked with the solver (99% and 101% of the change): {n_verified}/{len(checks)} "
          "verified")
    df_thresholds = pd.DataFrame(threshold_rows)

    notes = {
        "Frontier": "Each network kept fixed on the scenarios; CO2 offsetting price as the weight of the CO2 "
                    f"(tested from {prices[0]:g} to {prices[-1]:g} EUR/ton CO2, plus the exact switch prices). "
                    "Profit before offsetting = expected profit + kg x price. Chosen From = also the cost per ton of "
                    "CO2 avoided vs the previous mix for the solver (without the inventory cost, which the "
                    "optimization does not see).",
        "Mix_Thresholds": f"LOCAL thresholds ({recommended_label}): the value at which the units that change plant "
                          "toward the neighbor mix start moving, margin per moved unit (how much more the neighbor "
                          "costs the solver, inventory cost not included, / units that change plant: "
                          + ", ".join(margin_texts) + ") / weight of the parameter on one moved unit "
                          "(1 for a cost per unit, the extra kg of CO2 per moved unit / 1000 for the CO2 price); "
                          "exact as long as the competing "
                          "mixes stay the same; for large changes see sensitivity_results.xlsx. A parameter that "
                          "weighs on only some of the moved units (e.g. the distribution to one market) moves those "
                          "alone, toward an in-between mix. Verified: solved with the network fixed on the scenarios "
                          f"at 99% and 101% of the change, the expected CO2 stays within {min_co2_change_pct:g}% of "
                          "the current mix's before the threshold and moves by more than that toward the neighbor "
                          "beyond it (expected CO2 after the switch = found at 101%); if No, the threshold is only "
                          "indicative (see the sensitivity analysis). A threshold below 0 (change below -100%) cannot "
                          "be reached. The selling price, CAPEX, OPEX and the discount rate are not listed: they weigh "
                          "the same on every mix of the fixed network (CAPEX, OPEX and the discount rate are the "
                          "warehouses' fixed cost; the selling price multiplies the units sold, the same in the "
                          "mixes).",
    }
    # Chart: expected profit before the offsetting vs expected CO2, one line per network (mixes from the most CO2)
    colors = _network_colors(labels)
    fig, ax = plt.subplots(figsize=(12, 8))
    others = [label for label in labels if label != recommended_label]
    for label in labels:
        points, is_recommended = mixes[label], label == recommended_label
        # Label position on each segment: the middle for the recommended network, 1/4 or 3/4 for the others
        position = 0.5 if is_recommended else (0.25, 0.75)[others.index(label) % 2]
        x = [point["kg"] / 1000 for point in points]
        y = [point["profit"] / 1e6 for point in points]
        ax.plot(x, y, color=colors[label], linewidth=3 if is_recommended else 1.5, alpha=1 if is_recommended else 0.6,
                zorder=2 if is_recommended else 1, label=label + (" (recommended)" if is_recommended else ""))
        for point, xi, yi in zip(points, x, y):  # dominated points lighter
            ax.scatter(xi, yi, s=110 if is_recommended else 60, color=colors[label],
                       alpha=1 if point["efficient"] else 0.35, edgecolor="black" if point["efficient"] else "none",
                       zorder=3)
        for a, b in zip(points, points[1:]):  # cost per ton avoided (solver) above each segment
            ax.annotate(f"{b['low']:.0f} EUR/t", ((a["kg"] + (b["kg"] - a["kg"]) * position) / 1000,
                                                  (a["profit"] + (b["profit"] - a["profit"]) * position) / 1e6),
                        xytext=(0, 8), textcoords="offset points", ha="center", fontsize=9, fontweight="bold",
                        color=colors[label], zorder=4, bbox=LABEL_BOX)
        ax.scatter(x[current[label]], y[current[label]], s=380, facecolors="none", edgecolors="black", linewidths=2,
                   zorder=4)
        if is_recommended:  # production per plant of each mix
            for point, xi, yi in zip(points, x, y):
                ax.annotate(mix_text(point).replace(", ", "\n"), (xi, yi), xytext=(10, -10), textcoords="offset points",
                            ha="left", va="top", fontsize=8, color=colors[label], zorder=4, bbox=LABEL_BOX)
    ax.scatter([], [], s=380, facecolors="none", edgecolors="black", linewidths=2,
               label=f"Mix chosen at the current price ({current_price:g} EUR/t)")
    ax.scatter([], [], s=60, color="gray", alpha=0.35, label="Dominated (another point has less CO2 and more profit)")
    low, high = ax.get_ylim()
    ax.set_ylim(low - (high - low) * 0.12, high + (high - low) * 0.05)  # room for the production labels
    ax.set_xlabel("Expected CO2 emissions (t/year)", fontsize=12)
    ax.set_ylabel("Expected profit before the CO2 offsetting (EUR M/year)", fontsize=12)
    ax.set_title("Cost-CO2 Frontier - Expected Profit Before the CO2 Offsetting vs Expected CO2\n(each network kept "
                 f"fixed on {len(df_scenarios)} demand scenarios, CO2 price as the weight from {prices[0]:g} to "
                 f"{prices[-1]:g} EUR/t;\nlabels on the lines = cost per ton of CO2 avoided for the solver)",
                 fontsize=13)
    ax.legend(loc="lower right", fontsize=9)
    ax.grid(alpha=0.3)
    sold = {label: sum(mixes[label][current[label]]["w"].values()) for label in labels}
    per_unit = {label: min(point["kg"] / sum(point["w"].values()) for point in mixes[label]) for label in labels}
    note = [f"Units sold a year on average: " + ", ".join(f"{label} {sold[label] / 1e6:.2f} M" for label in labels)
            + " (a network that sells less also emits less). CO2 per unit sold at the cleanest mix (expected kg / "
            "expected units sold): " + ", ".join(f"{label} {per_unit[label]:.3f}" for label in labels) + " kg/unit.",
            "No warehouse (profit 0, CO2 0) is left out."]
    for label in labels:
        for a, b in zip(mixes[label], mixes[label][1:]):
            reported = (a["profit"] - b["profit"]) / (a["kg"] - b["kg"]) * 1000
            if abs(reported - b["low"]) > 1:
                note.append(f"{label}, from {mix_text(a)} to {mix_text(b)}: the inventory cost changes (the stock of "
                            f"a third Plant -> Warehouse route, not seen by the solver), so a ton avoided costs "
                            f"{b['low']:.0f} EUR for the solver and about {reported:.0f} EUR in the reported profit.")
    fig.text(0.01, -0.01, "\n".join(textwrap.fill(line, 160) for line in note), ha="left", va="top", fontsize=9)
    fig.tight_layout()
    units = {spec["label"]: spec["unit"] for spec in cases}
    unit_formats = {**SENSITIVITY_UNIT_FORMATS, "EUR/ton CO2": ('0.00 "EUR/t"', "{:.2f} EUR/t")}

    def cell_format(label, column):  # by column; the parameter values of Mix_Thresholds by their unit
        if "(EUR/ton CO2)" in column:
            return unit_formats["EUR/ton CO2"]
        if "(kg/year)" in column:
            return FORMAT_KG
        if column.endswith("(%)"):
            return FORMAT_PERCENT_1 if column == "CO2 Change (%)" else FORMAT_PERCENT
        if "(units/year)" in column:
            return FORMAT_UNITS
        if "(EUR/year" in column:
            return FORMAT_EUR
        return unit_formats[units[label]]  # Base Value and thresholds

    _write_excel(output_dir, "co2_frontier.xlsx",
                 {"Frontier": df_frontier, "Mix_Thresholds": df_thresholds},
                 "CO2 frontier report generated", notes, cell_format=cell_format,
                 images={"Frontier": _figure_png(fig)})
    return {"frontier": df_frontier, "mix_margin": df_margin, "mix_thresholds": df_thresholds}
