#!/usr/bin/env python3
"""Plot one CT Z slice with contours from a DICOM RT Structure Set."""

import argparse
import colorsys
import os
from pathlib import Path
import sys

import numpy as np
import pydicom


class PlotError(Exception):
    pass


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="directory containing CT DICOM and RTSTRUCT files")
    parser.add_argument("--z-slice", type=int, required=True, metavar="N", help="one-based CT slice number")
    parser.add_argument("--structures", nargs="+", metavar="NAME", help="only plot named structures")
    parser.add_argument("--output", type=Path, help="output PNG path")
    parser.add_argument("--title", help="custom figure title")
    parser.add_argument("--dpi", type=int, default=180, help="PNG resolution (default: 180)")
    parser.add_argument(
        "--window", type=float, nargs=2, metavar=("CENTER", "WIDTH"),
        help="override the DICOM display window",
    )
    return parser.parse_args(argv)


def _required(dataset, names, description):
    missing = [name for name in names if not hasattr(dataset, name)]
    if missing:
        raise PlotError("{} is missing required tags: {}".format(description, ", ".join(missing)))


def read_dicom_directory(directory):
    directory = Path(directory).resolve()
    if not directory.is_dir():
        raise PlotError("DICOM input directory does not exist: {}".format(directory))
    ct_datasets = []
    rt_datasets = []
    for path in sorted(directory.iterdir()):
        if not path.is_file():
            continue
        try:
            dataset = pydicom.dcmread(str(path))
        except Exception:
            continue
        modality = str(getattr(dataset, "Modality", ""))
        if modality == "CT":
            ct_datasets.append((path, dataset))
        elif modality == "RTSTRUCT":
            rt_datasets.append((path, dataset))
    if not ct_datasets:
        raise PlotError("no CT DICOM instances were found in {}".format(directory))
    if len(rt_datasets) != 1:
        raise PlotError(
            "expected exactly one RTSTRUCT in {}, found {}".format(directory, len(rt_datasets))
        )

    first_path, first = ct_datasets[0]
    required = (
        "Rows", "Columns", "PixelSpacing", "ImageOrientationPatient",
        "ImagePositionPatient", "SOPInstanceUID", "SeriesInstanceUID", "FrameOfReferenceUID",
    )
    _required(first, required, "CT {}".format(first_path))
    rows, columns = int(first.Rows), int(first.Columns)
    pixel_spacing = np.asarray(first.PixelSpacing, dtype=float)
    orientation = np.asarray(first.ImageOrientationPatient, dtype=float)
    if pixel_spacing.shape != (2,) or np.any(pixel_spacing <= 0):
        raise PlotError("CT PixelSpacing must contain two positive values")
    if orientation.shape != (6,):
        raise PlotError("CT ImageOrientationPatient must contain six values")
    column_direction = orientation[:3]
    row_direction = orientation[3:]
    if not np.allclose(
        [np.linalg.norm(column_direction), np.linalg.norm(row_direction),
         np.dot(column_direction, row_direction)], [1, 1, 0], atol=1e-6
    ):
        raise PlotError("CT ImageOrientationPatient is not orthonormal")
    normal = np.cross(column_direction, row_direction)
    series_uid = str(first.SeriesInstanceUID)
    frame_uid = str(first.FrameOfReferenceUID)
    sop_uids = set()
    ordered = []
    for path, dataset in ct_datasets:
        _required(dataset, required, "CT {}".format(path))
        if (int(dataset.Rows), int(dataset.Columns)) != (rows, columns):
            raise PlotError("CT series has inconsistent Rows or Columns: {}".format(path))
        if not np.allclose(dataset.PixelSpacing, pixel_spacing, atol=1e-6):
            raise PlotError("CT series has inconsistent PixelSpacing: {}".format(path))
        if not np.allclose(dataset.ImageOrientationPatient, orientation, atol=1e-6):
            raise PlotError("CT series has inconsistent image orientation: {}".format(path))
        if str(dataset.SeriesInstanceUID) != series_uid:
            raise PlotError("multiple CT series were found in {}".format(directory))
        if str(dataset.FrameOfReferenceUID) != frame_uid:
            raise PlotError("CT series has inconsistent FrameOfReferenceUID: {}".format(path))
        sop_uid = str(dataset.SOPInstanceUID)
        if sop_uid in sop_uids:
            raise PlotError("CT series contains duplicate SOPInstanceUID {}".format(sop_uid))
        sop_uids.add(sop_uid)
        origin = np.asarray(dataset.ImagePositionPatient, dtype=float)
        if origin.shape != (3,) or not np.isfinite(origin).all():
            raise PlotError("CT ImagePositionPatient is invalid: {}".format(path))
        ordered.append((float(np.dot(origin, normal)), origin, path, dataset))
    ordered.sort(key=lambda item: item[0])
    projections = np.asarray([item[0] for item in ordered])
    if len(ordered) > 1:
        differences = np.diff(projections)
        spacing = float(np.median(differences))
        if np.any(differences <= 1e-6) or not np.allclose(differences, spacing, atol=1e-3):
            raise PlotError("CT slice positions are duplicated or nonuniform")
    else:
        spacing = float(getattr(first, "SliceThickness", 0))
        if spacing <= 0:
            raise PlotError("a single-slice CT requires a positive SliceThickness")

    rt_path, rtstruct = rt_datasets[0]
    rt_frames = {
        str(item.FrameOfReferenceUID)
        for item in getattr(rtstruct, "ReferencedFrameOfReferenceSequence", [])
        if hasattr(item, "FrameOfReferenceUID")
    }
    if rt_frames and frame_uid not in rt_frames:
        raise PlotError("RTSTRUCT and CT FrameOfReferenceUID values do not match")
    return {
        "directory": directory,
        "ct": [item[3] for item in ordered],
        "origins": [item[1] for item in ordered],
        "projections": projections,
        "rows": rows,
        "columns": columns,
        "row_spacing": float(pixel_spacing[0]),
        "column_spacing": float(pixel_spacing[1]),
        "column_direction": column_direction,
        "row_direction": row_direction,
        "normal": normal,
        "slice_spacing": spacing,
        "sop_uids": sop_uids,
        "rtstruct": rtstruct,
        "rt_path": rt_path,
    }


def select_z_slice(z_slice, slice_count):
    if not 1 <= z_slice <= slice_count:
        raise PlotError("--z-slice must be from 1 to {}, got {}".format(slice_count, z_slice))
    return z_slice - 1


def _first_number(value):
    if isinstance(value, (str, bytes)) or np.isscalar(value):
        return float(value)
    return float(value[0])


def ct_plane(dataset):
    try:
        pixels = dataset.pixel_array.astype(np.float64)
    except Exception as exc:
        raise PlotError("could not decode CT PixelData: {}".format(exc))
    slope = float(getattr(dataset, "RescaleSlope", 1))
    intercept = float(getattr(dataset, "RescaleIntercept", 0))
    plane = pixels * slope + intercept
    if not np.isfinite(plane).all():
        raise PlotError("selected CT slice contains non-finite values")
    return plane


def display_window(dataset, plane, override=None):
    if override is not None:
        center, width = map(float, override)
    elif hasattr(dataset, "WindowCenter") and hasattr(dataset, "WindowWidth"):
        center = _first_number(dataset.WindowCenter)
        width = _first_number(dataset.WindowWidth)
    else:
        minimum, maximum = float(np.min(plane)), float(np.max(plane))
        if maximum <= minimum:
            maximum = minimum + 1.0
        return minimum, maximum
    if not np.isfinite([center, width]).all() or width <= 0:
        raise PlotError("CT window width must be positive and window values must be finite")
    return center - width / 2.0, center + width / 2.0


def _fallback_color(number):
    rgb = colorsys.hsv_to_rgb((number * 0.61803398875) % 1.0, 0.8, 1.0)
    return tuple(rgb)


def _display_color(group, number):
    values = getattr(group, "ROIDisplayColor", None)
    if values is None:
        return _fallback_color(number)
    try:
        color = np.asarray(values, dtype=float)
    except (TypeError, ValueError):
        return _fallback_color(number)
    if color.shape != (3,) or not np.isfinite(color).all() or np.any(color < 0) or np.any(color > 255):
        return _fallback_color(number)
    return tuple((color / 255.0).tolist())


def contours_for_slice(series, z_index, selected_names=None):
    rtstruct = series["rtstruct"]
    roi_by_number = {}
    roi_names = set()
    for roi in getattr(rtstruct, "StructureSetROISequence", []):
        if not hasattr(roi, "ROINumber") or not hasattr(roi, "ROIName"):
            raise PlotError("RTSTRUCT contains a malformed StructureSetROISequence item")
        number = int(roi.ROINumber)
        name = str(roi.ROIName)
        if number in roi_by_number or name in roi_names:
            raise PlotError("RTSTRUCT contains duplicate ROI numbers or names")
        roi_by_number[number] = name
        roi_names.add(name)
    requested = None if selected_names is None else set(selected_names)
    if requested is not None:
        unknown = sorted(requested.difference(roi_names))
        if unknown:
            raise PlotError("unknown structure name(s): {}".format(", ".join(unknown)))

    groups = {}
    for group in getattr(rtstruct, "ROIContourSequence", []):
        if not hasattr(group, "ReferencedROINumber"):
            raise PlotError("RTSTRUCT contains an ROI contour without ReferencedROINumber")
        number = int(group.ReferencedROINumber)
        if number not in roi_by_number:
            raise PlotError("ROIContourSequence references unknown ROI number {}".format(number))
        if number in groups:
            raise PlotError("RTSTRUCT contains multiple contour groups for ROI {}".format(number))
        groups[number] = group

    selected_uid = str(series["ct"][z_index].SOPInstanceUID)
    selected_projection = float(series["projections"][z_index])
    origin = series["origins"][z_index]
    result = []
    for number, name in roi_by_number.items():
        if requested is not None and name not in requested:
            continue
        group = groups.get(number)
        if group is None:
            continue
        paths = []
        for contour_index, contour in enumerate(getattr(group, "ContourSequence", []), 1):
            geometric_type = str(getattr(contour, "ContourGeometricType", ""))
            if geometric_type not in ("CLOSED_PLANAR", "CLOSEDPLANAR_XOR"):
                raise PlotError("ROI {!r} contour {} has unsupported type {!r}".format(
                    name, contour_index, geometric_type
                ))
            values = np.asarray(getattr(contour, "ContourData", []), dtype=float)
            if values.size < 9 or values.size % 3 or not np.isfinite(values).all():
                raise PlotError("ROI {!r} contour {} has malformed ContourData".format(name, contour_index))
            points = values.reshape(-1, 3)
            contour_projections = np.dot(points, series["normal"])
            if np.ptp(contour_projections) > 1e-3:
                raise PlotError("ROI {!r} contour {} is not planar".format(name, contour_index))
            references = {
                str(image.ReferencedSOPInstanceUID)
                for image in getattr(contour, "ContourImageSequence", [])
                if hasattr(image, "ReferencedSOPInstanceUID")
            }
            unknown_refs = references.difference(series["sop_uids"])
            if unknown_refs:
                raise PlotError("ROI {!r} contour references CT images outside this series".format(name))
            if references:
                on_slice = selected_uid in references
            else:
                on_slice = abs(float(np.mean(contour_projections)) - selected_projection) <= max(
                    1e-3, series["slice_spacing"] / 2.0
                )
            if not on_slice:
                continue
            if abs(float(np.mean(contour_projections)) - selected_projection) > max(
                1e-3, series["slice_spacing"] / 2.0
            ):
                raise PlotError("ROI {!r} contour reference and physical slice disagree".format(name))
            delta = points - origin
            columns = np.dot(delta, series["column_direction"]) / series["column_spacing"] + 1.0
            rows = np.dot(delta, series["row_direction"]) / series["row_spacing"] + 1.0
            paths.append((columns, rows))
        if paths:
            result.append((name, _display_color(group, number), paths))
    return result


def output_path(directory, z_slice, requested=None):
    if requested is None:
        output = directory.parent / "{}_z_slice_{}.png".format(directory.name, z_slice)
    else:
        output = Path(requested).resolve()
    if output.suffix.lower() != ".png":
        raise PlotError("output must have a .png extension: {}".format(output))
    return output


def plot_slice(series, z_index, z_slice, contours, output, title=None, dpi=180, window=None):
    if dpi <= 0:
        raise PlotError("--dpi must be a positive integer")
    temporary_root = Path(os.environ.get("TMPDIR", "/tmp"))
    cache = temporary_root / "matplotlib-cache"
    cache.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(cache))
    os.environ.setdefault("XDG_CACHE_HOME", str(temporary_root))
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.lines import Line2D
    except ImportError as exc:
        raise PlotError("Matplotlib is required; install the packages in requirements.txt") from exc

    dataset = series["ct"][z_index]
    plane = ct_plane(dataset)
    vmin, vmax = display_window(dataset, plane, window)
    figure, axis = plt.subplots(figsize=(9, 8), constrained_layout=True)
    axis.imshow(
        plane, cmap="gray", origin="lower", interpolation="nearest",
        extent=(0.5, series["columns"] + 0.5, 0.5, series["rows"] + 0.5),
        vmin=vmin, vmax=vmax, aspect="equal",
    )
    handles = []
    for name, color, paths in contours:
        for columns, rows in paths:
            axis.plot(columns, rows, color=color, linewidth=1.3)
        handles.append(Line2D([0], [0], color=color, linewidth=2, label=name))
    if handles:
        axis.legend(handles=handles, loc="center left", bbox_to_anchor=(1.01, 0.5), fontsize="small")
    axis.set_xlabel("CT X index (1-based)")
    axis.set_ylabel("CT Y index (1-based)")
    patient_z = float(series["origins"][z_index][2])
    axis.set_title(title or "CT and RT structures at Z slice {} (patient Z = {:.3f} mm)".format(
        z_slice, patient_z
    ))
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        figure.savefig(str(output), dpi=dpi, bbox_inches="tight")
    finally:
        plt.close(figure)


def main(argv=None):
    args = parse_args(argv)
    try:
        series = read_dicom_directory(args.input)
        z_index = select_z_slice(args.z_slice, len(series["ct"]))
        contours = contours_for_slice(series, z_index, args.structures)
        output = output_path(series["directory"], args.z_slice, args.output)
        plot_slice(series, z_index, args.z_slice, contours, output, args.title, args.dpi, args.window)
    except (PlotError, OSError, ValueError, TypeError) as exc:
        print("Error: {}".format(exc), file=sys.stderr)
        return 1
    print("CT dimensions: X={}, Y={}, Z={}".format(
        series["columns"], series["rows"], len(series["ct"])
    ))
    print("Selected Z slice {} at patient Z = {:.3f} mm".format(
        args.z_slice, float(series["origins"][z_index][2])
    ))
    if contours:
        print("Plotted structures: {}".format(", ".join(item[0] for item in contours)))
    else:
        print("No selected structures intersect Z slice {}".format(args.z_slice))
    print("Wrote CT/RTSTRUCT plot: {}".format(output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
