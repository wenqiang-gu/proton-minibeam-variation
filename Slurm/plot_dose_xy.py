#!/usr/bin/env python3
"""Plot an X-Y dose distribution from one TOPAS binary dose Z slice."""
from __future__ import annotations

import argparse
import math
import os
import re
import sys
from pathlib import Path
from typing import Sequence

import numpy as np


FLOAT_BYTES = 8


class PlotError(RuntimeError):
    """A concise input or plotting error."""


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="TOPAS binary dose .bin file")
    parser.add_argument(
        "--z-slice", type=int, required=True, metavar="N",
        help="one-based patient-grid Z slice number",
    )
    parser.add_argument(
        "--shape", type=int, nargs=3, metavar=("NX", "NY", "NZ"),
        help="override grid dimensions when the .binheader cannot provide them",
    )
    parser.add_argument(
        "--normalization", choices=("max", "none"), default="max",
        help="normalize the slice maximum to 100%% or retain Gy (default: max)",
    )
    parser.add_argument("--output", type=Path, help="output PNG path")
    parser.add_argument("--title", help="custom figure title")
    parser.add_argument("--colormap", default="viridis", help="Matplotlib colormap")
    parser.add_argument("--dpi", type=int, default=180, help="PNG resolution")
    return parser.parse_args(argv)


def read_header(path: Path) -> list[str]:
    header = path.with_suffix(".binheader")
    if not header.is_file():
        raise PlotError(f"Binary input requires a companion header: {header}")
    if header.stat().st_size == 0:
        raise PlotError(f"Binary header is empty: {header}")
    try:
        return header.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as error:
        raise PlotError(f"Could not read binary header {header}: {error}") from error


def axis_bin_count(lines: Sequence[str], axis: str) -> int | None:
    patterns = (
        rf"\b{axis}\s*=\s*(\d+)",
        rf"\b{axis}\b[^\d\n]{{0,50}}(\d+)\s+(?:bins?|voxels?)\b",
        rf"\b(?:number\s+of\s+)?{axis}[ _-]*(?:bins?|voxels?)\b[^\d\n]{{0,20}}(\d+)",
        rf"\b(\d+)\s+(?:bins?|voxels?)\b[^\n]{{0,50}}\b(?:in|along)\s+{axis}\b",
    )
    found: set[int] = set()
    for line in lines:
        for pattern in patterns:
            match = re.search(pattern, line, flags=re.IGNORECASE)
            if match:
                found.add(int(match.group(1)))
    if len(found) > 1:
        raise PlotError(f"Header contains conflicting {axis}-bin counts: {sorted(found)}")
    return next(iter(found), None)


def grid_shape(lines: Sequence[str], override: Sequence[int] | None) -> tuple[int, int, int]:
    if override is not None:
        shape = tuple(override)
    else:
        parsed = tuple(axis_bin_count(lines, axis) for axis in ("X", "Y", "Z"))
        if any(value is None for value in parsed):
            raise PlotError(
                "Could not determine X/Y/Z grid dimensions from the .binheader; "
                "pass --shape NX NY NZ"
            )
        shape = tuple(int(value) for value in parsed)  # type: ignore[arg-type]
    if len(shape) != 3 or any(value <= 0 for value in shape):
        raise PlotError(f"Grid dimensions must be three positive integers, got {shape}")
    return shape  # type: ignore[return-value]


def select_z_slice(z_slice: int, nz: int) -> int:
    if not 1 <= z_slice <= nz:
        raise PlotError(f"--z-slice must be from 1 to {nz}, got {z_slice}")
    return z_slice - 1


def read_xy_slice(path: Path, shape: tuple[int, int, int], z_index: int) -> np.ndarray:
    if path.suffix.lower() != ".bin":
        raise PlotError(f"Input must have a .bin extension: {path}")
    if not path.is_file():
        raise PlotError(f"Dose input does not exist: {path}")
    if path.stat().st_size == 0:
        raise PlotError(f"Dose input is empty: {path}")
    nx, ny, nz = shape
    expected_bytes = nx * ny * nz * FLOAT_BYTES
    actual_bytes = path.stat().st_size
    if actual_bytes != expected_bytes:
        raise PlotError(
            f"Binary size is {actual_bytes:,} bytes; expected {expected_bytes:,} "
            f"for shape {shape} and float64 values"
        )
    volume = np.memmap(path, dtype="<f8", mode="r", shape=(nz, ny, nx))
    plane = np.array(volume[z_index, :, :])
    del volume
    if not np.all(np.isfinite(plane)):
        raise PlotError(f"Z slice {z_index + 1} contains non-finite dose values")
    return plane


def scale_plane(plane: np.ndarray, normalization: str) -> tuple[np.ndarray, str, float, float]:
    minimum = float(np.min(plane))
    maximum = float(np.max(plane))
    if normalization == "max":
        if maximum <= 0:
            raise PlotError("Cannot maximum-normalize a slice with no positive dose")
        return plane * (100.0 / maximum), "Dose relative to slice maximum (%)", 0.0, 100.0
    if normalization != "none":
        raise PlotError(f"Unknown normalization: {normalization}")
    lower = min(0.0, minimum)
    upper = maximum if maximum > lower else lower + 1.0
    return plane, "DoseToMedium (Gy)", lower, upper


def plot_xy(
    output: Path, plane: np.ndarray, z_slice: int, normalization: str,
    colormap: str, title: str | None, dpi: int,
) -> None:
    try:
        temporary_root = Path(os.environ.get("TMPDIR", "/tmp"))
        cache = temporary_root / "matplotlib-cache"
        cache.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault("MPLCONFIGDIR", str(cache))
        os.environ.setdefault("XDG_CACHE_HOME", str(temporary_root))
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as error:
        raise PlotError(
            "Matplotlib is required; install the packages in requirements.txt"
        ) from error
    if colormap not in matplotlib.colormaps:
        raise PlotError(f"Unknown Matplotlib colormap: {colormap}")
    image, label, vmin, vmax = scale_plane(plane, normalization)
    ny, nx = plane.shape
    figure, axis = plt.subplots(figsize=(8.0, 7.0), constrained_layout=True)
    plotted = axis.imshow(
        image, origin="lower", extent=(0.5, nx + 0.5, 0.5, ny + 0.5),
        interpolation="nearest", cmap=colormap, vmin=vmin, vmax=vmax,
        aspect="equal",
    )
    axis.set_xlabel("Patient-grid X index (1-based)")
    axis.set_ylabel("Patient-grid Y index (1-based)")
    figure.colorbar(plotted, ax=axis, shrink=0.88, pad=0.025).set_label(label)
    figure.suptitle(title or f"Dose distribution at Z slice {z_slice}")
    output.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(output, dpi=dpi, bbox_inches="tight")
    plt.close(figure)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    input_path = args.input.resolve()
    if args.dpi <= 0:
        raise PlotError("--dpi must be positive")
    if not args.colormap.strip():
        raise PlotError("--colormap must not be empty")
    # An explicit shape is the escape hatch for missing or unparsable headers.
    lines = read_header(input_path) if args.shape is None else []
    shape = grid_shape(lines, args.shape)
    z_index = select_z_slice(args.z_slice, shape[2])
    output = (
        args.output.resolve() if args.output is not None else
        input_path.with_suffix("").with_name(f"{input_path.stem}_z_slice_{args.z_slice}.png")
    )
    if output.suffix.lower() != ".png":
        raise PlotError(f"Output must have a .png extension: {output}")
    plane = read_xy_slice(input_path, shape, z_index)
    plot_xy(output, plane, args.z_slice, args.normalization, args.colormap, args.title, args.dpi)
    print(f"Grid: X={shape[0]}, Y={shape[1]}, Z={shape[2]}")
    print(f"Wrote X-Y dose plot for Z slice {args.z_slice}: {output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PlotError as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(2)
