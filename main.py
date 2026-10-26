"""Runs the whole study with the settings below: Monte Carlo and recommended network (Output/), analyses of
the recommended network (sensitivity_analysis/) and charts. Run from the project folder: python main.py"""
import os
from network_design.core import load_data, _demand_dict, generate_demand_scenarios, _shutdown_solver_pool
from network_design.monte_carlo import run_monte_carlo, select_recommended_solution
from network_design.sensitivity import (build_default_sensitivity_cases, run_default_sensitivity_suite,
                                        run_n_years_analysis, run_capacity_analysis, run_cv_analysis, run_co2_frontier)
from network_design.outputs import generate_all_plots


if __name__ == "__main__":
    N_SCENARIOS = 50
    CV = 0.2
    # Planning horizon and discount rate used to annualize the warehouses' CAPEX (equivalent annual cost)
    N_YEARS = 10
    DISCOUNT_RATE = 0.08
    N_YEARS_VALUES = range(1, 16)  # horizons tested by the N_YEARS analysis
    # All the logical cores but one; 1 = everything solved in this process (same results)
    N_WORKERS = max(1, (os.cpu_count() or 2) - 1)
    DATA_DIR = "DATA"
    PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
    OUTPUT_DIR = os.path.join(PROJECT_DIR, "Output")
    SENSITIVITY_OUTPUT_DIR = os.path.join(PROJECT_DIR, "sensitivity_analysis")
    # Analyses of the recommended network, most of the running time: False = skipped (outputs not regenerated)
    RUN_SENSITIVITY = True  # each parameter from SENSITIVITY_MIN_MULTIPLIER to SENSITIVITY_MAX_MULTIPLIER x base
    SENSITIVITY_MIN_MULTIPLIER = 0.5
    SENSITIVITY_MAX_MULTIPLIER = 1.5
    SENSITIVITY_POINTS = 11  # values per parameter (with 0.5-1.5 and 11 points they include -10% and +10%)
    # Detail (every value, tipping points, own sheet) only from this impact (%) or if the network changes
    SENSITIVITY_MIN_IMPACT_PCT = 2.0
    RUN_CAPACITY_ANALYSIS = True  # same capacity for every warehouse, each value in CAPACITY_VALUES (units/year)
    CAPACITY_VALUES = [18_000_000, 19_000_000, 20_000_000, 21_000_000, 22_000_000, 24_000_000]
    RUN_CO2_FRONTIER = True  # cost-CO2 frontier: every network fixed, the CO2 price as the weight of the CO2
    CO2_FRONTIER_PRICES = [0, 50, 100, 125, 140, 150, 160, 175, 180, 200, 300, 500]  # EUR/ton CO2
    RUN_CV_ANALYSIS = True  # demand scenarios generated again with each value in CV_VALUES (same seed)
    CV_VALUES = [0.05, 0.10, 0.15, 0.20, 0.30, 0.40]  # up to 0.4: few negative draws set to 0

    params = load_data(data_dir=DATA_DIR, n_years=N_YEARS, discount_rate=DISCOUNT_RATE)
    base_demand = _demand_dict(params["demand"])
    markets = list(base_demand.keys())
    plants, warehouses = list(params["cap_pl"].index), list(params["cap_wh"].index)
    df_scenarios = generate_demand_scenarios(base_demand=base_demand, n_scenarios=N_SCENARIOS, cv=CV)

    df_results, df_solutions, df_flows, decision = run_monte_carlo(params, df_scenarios, CV, output_dir=OUTPUT_DIR,
                                                                   n_workers=N_WORKERS)
    x_recommended, recommended_info = select_recommended_solution(df_solutions, warehouses, decision, params,
                                                                  df_scenarios)

    if RUN_SENSITIVITY:
        run_default_sensitivity_suite(params, df_scenarios, output_dir=SENSITIVITY_OUTPUT_DIR,
                                      min_multiplier=SENSITIVITY_MIN_MULTIPLIER,
                                      max_multiplier=SENSITIVITY_MAX_MULTIPLIER, n_points=SENSITIVITY_POINTS,
                                      n_workers=N_WORKERS, min_impact_pct=SENSITIVITY_MIN_IMPACT_PCT)

    run_n_years_analysis(params, base_demand, df_scenarios, N_YEARS_VALUES, current_n_years=N_YEARS,
                         output_dir=SENSITIVITY_OUTPUT_DIR, n_workers=N_WORKERS,
                         expected_profits=decision["expected_profits"])

    if RUN_CAPACITY_ANALYSIS:
        run_capacity_analysis(params, df_scenarios, CAPACITY_VALUES, output_dir=SENSITIVITY_OUTPUT_DIR,
                              n_workers=N_WORKERS)

    if RUN_CO2_FRONTIER:
        run_co2_frontier(params, df_scenarios, CO2_FRONTIER_PRICES, decision["network"],
                         build_default_sensitivity_cases(params), output_dir=SENSITIVITY_OUTPUT_DIR,
                         n_workers=N_WORKERS)

    if RUN_CV_ANALYSIS:
        run_cv_analysis(params, CV_VALUES, n_scenarios=N_SCENARIOS, current_cv=CV, output_dir=SENSITIVITY_OUTPUT_DIR,
                        n_workers=N_WORKERS)
    _shutdown_solver_pool()

    generate_all_plots(df_scenarios, markets, x_recommended, recommended_info, plants, warehouses, params,
                       output_dir=OUTPUT_DIR)
