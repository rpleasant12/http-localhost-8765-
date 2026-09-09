"""Four-pane model comparison: one product, four models, side by side.

Renders each model's map for the same product/valid-hour into a single
2x2 matplotlib figure (one file, one image to display - no browser
layout fighting). Each panel carries its own model/F-hour/valid badge;
a shared header names the product and target hour. Per-panel failures
render an honest error box instead of killing the figure. All panels
reuse the disk cache via render_product_map, so repeat views are instant.
"""
import datetime as dt
import os

import numpy as np

from data.model_maps import MAP_DIR, MAP_MODELS, PRODUCTS, render_product_map


def nearest_fh(model, target_hour):
    """Nearest available forecast hour for a model to the target hour."""
    m = MAP_MODELS[model]
    opts = [h for h in range(0, m["max_hour"] + 1, m["hour_step"])
            if h > 0 or model not in ("HREF", "REFS")]  # HREF/REFS start at f01
    return min(opts, key=lambda h: abs(h - target_hour))


def _valid_hour(cycle, fh):
    return (cycle + dt.timedelta(hours=fh)).hour


def render_comparison(product, models, cycle_by_model, fh_by_model, out_dir=MAP_DIR, region="us"):
    """Render a 2x2 comparison figure -> (png_path, meta).

    models           - up to 4 model keys (MAP_MODELS entries)
    cycle_by_model   - {model: cycle datetime}
    fh_by_model      - {model: forecast hour}
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.image as mpimg

    key = "|".join(f"{m}:{fh_by_model[m]:03d}:{cycle_by_model[m]:%Y%m%d%H}" for m in models)
    png = os.path.join(out_dir, f"compare_{product}_{region}_{abs(hash(key + region)) % 10**10}.png")
    if os.path.exists(png) and os.path.getsize(png) > 10_000:
        return png, {"cached": True, "models": list(models)}

    n = len(models)
    rows = 1 if n <= 2 else 2
    cols = n if n <= 2 else 2
    fig, axes = plt.subplots(rows, cols, figsize=(6.6 * cols, 4.3 * rows), dpi=100)
    axes = np.atleast_1d(axes).ravel()

    prod_label = PRODUCTS[product]["label"] if product in PRODUCTS else product
    rendered = []
    for ax, m in zip(axes, models):
        try:
            p, meta = render_product_map(m, cycle_by_model[m], fh_by_model[m], product, out_dir, region=region)
            img = mpimg.imread(p)
            ax.imshow(img)
            ax.set_axis_off()
            ax.set_title(
                f"{m}  \u00b7  F{fh_by_model[m]:03d}  \u00b7  valid {meta['valid'][:16]}Z",
                fontsize=11, pad=4,
            )
            rendered.append(m)
        except Exception as exc:  # noqa: BLE001 - one panel failing must not kill the pane
            ax.set_axis_off()
            ax.text(0.5, 0.5, f"{m}\nunavailable\n{str(exc)[:60]}",
                    ha="center", va="center", fontsize=10, color="#a33",
                    transform=ax.transAxes)
            ax.set_title(m, fontsize=11, pad=4)
    for ax in axes[len(models):]:
        ax.set_axis_off()
        ax.set_visible(False)

    fig.suptitle(f"{prod_label}  \u00b7  target F{next(iter(fh_by_model.values())):03d}"
                 f"  \u00b7  Tennessee Weather Network", fontsize=13, y=0.99)
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    fig.savefig(png, bbox_inches="tight")
    plt.close(fig)
    return png, {"cached": False, "models": rendered}
