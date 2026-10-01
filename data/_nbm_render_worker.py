"""Render one NBM percentile map in an isolated worker process.

Parent: data/nbm_percentiles.py _render() pickles the job to a temp file and
runs this script via subprocess.run() with a hard timeout. The matplotlib/
cartopy stack has hung repeatedly inside the long-lived updater process
(2026-09-30/10-01: contour reprojection wedging for >45 min, forcing the
watchdog to kill the whole updater and skip publishes). Doing the render in
a killable child turns any wedge into a bounded, per-map failure instead of
a site-wide freeze.
"""
import os
import pickle
import sys

sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    job_path, out_path = sys.argv[1], sys.argv[2]
    with open(job_path, "rb") as f:
        vals, lat, lon, spec, title, cmap, levels = pickle.load(f)

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import cartopy.crs as ccrs
    import cartopy.feature as cfeature

    from data.model_maps import MAP_REGIONS

    dec = 2
    v = vals[::dec, ::dec]
    la, lo = lat[::dec, ::dec], lon[::dec, ::dec]
    fig = plt.figure(figsize=(9, 5.5), dpi=90)
    proj = ccrs.LambertConformal(central_longitude=-96, central_latitude=39)
    ax = fig.add_subplot(1, 1, 1, projection=proj)
    ax.set_extent(MAP_REGIONS["us"]["extent"], crs=ccrs.PlateCarree())
    ax.coastlines("50m", linewidth=0.5)
    ax.add_feature(cfeature.STATES, linewidth=0.4, edgecolor="gray")
    cf = ax.contourf(lo, la, v, levels=levels, cmap=cmap,
                     transform=ccrs.PlateCarree(), alpha=0.85, extend="both")
    plt.colorbar(cf, ax=ax, shrink=0.8, label=f"{spec['label']} ({spec['unit']})")
    ax.set_title(title, fontsize=11)
    tmp = out_path.replace(".png", ".tmp.png")
    fig.savefig(tmp, bbox_inches="tight")
    plt.close(fig)
    os.replace(tmp, out_path)


if __name__ == "__main__":
    main()
