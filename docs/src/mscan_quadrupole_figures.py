"""Figures for docs/mscan_quadrupole.pdf (stability diagram and rod waveforms).

Run from the repository root:  python3 docs/src/mscan_quadrupole_figures.py
Nothing runs on import: Explorer imports every .py file of a folder on its plugin path.
"""


def square_wave_trace(a, q):
    """Half-trace of the one-period transfer matrix, 50 % square wave, x direction.

    x'' + (a + 2q s(xi)) x = 0 over one period xi in [0, pi], s = +1 then -1 (Steel and
    Henchman sign convention). The motion is stable when |trace| < 2.
    """
    import numpy as np

    w1 = np.sqrt((a + 2 * q).astype(complex)) + 1e-16
    w2 = np.sqrt((a - 2 * q).astype(complex)) + 1e-16
    h = np.pi / 2
    return (2 * np.cos(w1 * h) * np.cos(w2 * h)
            - (w1 / w2 + w2 / w1) * np.sin(w1 * h) * np.sin(w2 * h)).real


def stable(a, q):
    """x and y both stable (y sees -a, -q)."""
    return (abs(square_wave_trace(a, q)) < 2) & (abs(square_wave_trace(-a, -q)) < 2)


def main():
    from pathlib import Path

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    out = Path(__file__).resolve().parent
    ink, ink2, grid = "#0b0b0b", "#52514e", "#e1e0d9"
    fill, edge = "#cde2fb", "#2a78d6"
    lines = [(0.15, "#1baf7a"), (0.20, "#eb6834"), (0.21, "#6250d6")]
    plt.rcParams.update({
        "font.family": "serif", "font.serif": ["DejaVu Serif"], "font.size": 9,
        "axes.edgecolor": "#c3c2b7", "axes.labelcolor": ink, "xtick.color": ink2, "ytick.color": ink2,
        "axes.grid": True, "grid.color": grid, "grid.linewidth": 0.6, "axes.axisbelow": True,
        "pdf.fonttype": 42,
    })
    meta = {"CreationDate": None, "Creator": None, "Producer": None}

    # Stability diagram, full view and apex.
    fig, axes = plt.subplots(1, 2, figsize=(6.6, 2.75), constrained_layout=True)
    for ax, (q0, q1, a0, a1, n) in zip(axes, ((0, 0.8, 0, 0.3, 900), (0.44, 0.62, 0.17, 0.25, 900))):
        q, a = np.meshgrid(np.linspace(q0, q1, n), np.linspace(a0, a1, n))
        ok = stable(a, q)
        ax.contourf(q, a, ok.astype(float), levels=[0.5, 1.5], colors=[fill])
        ax.contour(q, a, ok.astype(float), levels=[0.5], colors=[edge], linewidths=1.0)
        qq = np.linspace(q0, q1, 400)
        for k, color in lines:
            ax.plot(qq, 2 * k * qq, color=color, lw=1.4)
        ax.plot([0.5529], [0.2381], "o", ms=4, color=ink)
        ax.set_xlim(q0, q1)
        ax.set_ylim(a0, a1)
        ax.set_xlabel("q = 4eA / (m r$_0^2$ Ω$^2$)")
    axes[0].set_ylabel("a = 8eU / (m r$_0^2$ Ω$^2$)")
    axes[0].set_title("Stability region, 50 % square wave", fontsize=9, color=ink)
    axes[1].set_title("Near the apex (a = 0.2381, q = 0.5529)", fontsize=9, color=ink)
    for k, color in lines:
        axes[0].plot([], [], color=color, lw=1.4, label=f"scan line, k = {k:.2f}")
    axes[0].legend(loc="upper left", frameon=False, fontsize=7.5, handlelength=1.6)
    axes[0].annotate("RF-only limit, q = 0.712", xy=(0.712, 0.0), xytext=(0.36, 0.035), fontsize=7.5, color=ink2,
                     arrowprops=dict(arrowstyle="-", color=ink2, lw=0.6))
    fig.savefig(out / "mscan_quadrupole_stability.pdf", metadata=meta)
    plt.close(fig)

    # Rod potentials over two periods for an example point.
    x0, u, amp = 15.0, 10.0, 50.0
    t = np.linspace(0, 2, 2001)
    s = np.where((t % 1) < 0.5, 1.0, -1.0)
    fig, ax = plt.subplots(figsize=(6.6, 2.7), constrained_layout=True)
    ax.plot(t, x0 + u + amp * s, color="#2a78d6", lw=1.4, label="pair 1: x + U + A s(t)")
    ax.plot(t, x0 - u - amp * s, color="#eb6834", lw=1.4, label="pair 2: x − U − A s(t)")
    for level, text, color in ((x0 + u, "x + U = 25 V (mean of pair 1)", "#2a78d6"), (x0, "x = 15 V (axis)", ink2),
                               (x0 - u, "x − U = 5 V (mean of pair 2)", "#eb6834")):
        ax.axhline(level, color=color, lw=0.8, ls=(0, (1, 2)))
        ax.text(2.03, level, text, va="center", ha="left", fontsize=7.5, color=ink2)
    ax.set_xlim(0, 2)
    ax.set_ylim(-42, 72)
    ax.set_xlabel("time (RF periods)")
    ax.set_ylabel("rod potential (V)")
    ax.set_title("Example: x = 15 V, U = 10 V, A = 50 V (k = 0.2)", fontsize=9, color=ink)
    ax.legend(loc="upper center", ncol=2, frameon=False, fontsize=8, bbox_to_anchor=(0.5, -0.32))
    fig.savefig(out / "mscan_quadrupole_waveforms.pdf", metadata=meta)
    plt.close(fig)


if __name__ == "__main__":
    main()
