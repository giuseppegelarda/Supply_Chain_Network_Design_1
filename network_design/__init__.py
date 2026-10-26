"""Warehouse network design for a 3-stage supply chain: MILP, Monte Carlo on the demand and analyses of the
recommended network."""
import matplotlib
# No GUI backend (Tk): its objects freed from the thread of the pool (_solve_many) would stop the program. Set
# here, so that it comes before any module of the package imports pyplot
matplotlib.use("Agg")
