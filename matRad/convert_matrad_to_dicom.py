#!/usr/bin/env python3
"""Convert a matRad v7.3 CT/cst file to a DICOM CT series and RTSTRUCT."""

import argparse
import colorsys
import datetime as dt
import os
from pathlib import Path
import shutil
import sys
import tempfile

import h5py
import numpy as np
import pydicom
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.uid import (
    CTImageStorage,
    ExplicitVRLittleEndian,
    RTStructureSetStorage,
    generate_uid,
)
from skimage.measure import find_contours


class ConversionError(Exception):
    pass


def _matlab_string(dataset):
    values = np.asarray(dataset).reshape(-1, order="F")
    return "".join(chr(int(value)) for value in values).rstrip("\x00")


def _dereference_first(handle, obj):
    if isinstance(obj, h5py.Dataset) and obj.dtype.kind == "O":
        values = np.asarray(obj).reshape(-1)
        if not values.size or not values[0]:
            raise ConversionError("encountered an empty MATLAB object reference")
        return handle[values[0]]
    return obj


def _decode_value(handle, obj):
    obj = _dereference_first(handle, obj)
    if not isinstance(obj, h5py.Dataset):
        return None
    if obj.attrs.get("MATLAB_class") == b"char":
        return _matlab_string(obj)
    values = np.asarray(obj).reshape(-1, order="F")
    if values.size == 1:
        return values[0].item()
    return values.tolist()


def _read_meta(handle, group):
    return {name: _decode_value(handle, value) for name, value in group.items()}


def _numeric_vector(obj, name):
    values = np.asarray(obj, dtype=float).reshape(-1, order="F")
    if not values.size or not np.isfinite(values).all():
        raise ConversionError("{} must contain finite numeric values".format(name))
    return values


def _regular_spacing(values, name):
    if values.size < 2:
        raise ConversionError("{} must contain at least two coordinates".format(name))
    differences = np.diff(values)
    if np.any(differences <= 0) or not np.allclose(
        differences, differences[0], rtol=1e-6, atol=1e-6
    ):
        raise ConversionError("{} coordinates must be regularly increasing".format(name))
    return float(differences[0])


def read_matrad(path):
    try:
        handle = h5py.File(str(path), "r")
    except (OSError, ValueError) as exc:
        raise ConversionError("cannot read MATLAB v7.3 file {}: {}".format(path, exc))

    with handle:
        if "ct" not in handle or "cst" not in handle:
            raise ConversionError("MAT file must contain both 'ct' and 'cst'")
        ct = handle["ct"]
        required = {"cubeHU", "cubeDim", "x", "y", "z", "dicomInfo", "dicomMeta"}
        missing = sorted(required.difference(ct.keys()))
        if missing:
            raise ConversionError("ct is missing required fields: {}".format(", ".join(missing)))

        dimensions = tuple(int(value) for value in _numeric_vector(ct["cubeDim"], "cubeDim"))
        if len(dimensions) != 3 or any(value <= 0 for value in dimensions):
            raise ConversionError("cubeDim must contain positive NX, NY, NZ values")
        nx, ny, nz = dimensions
        x = _numeric_vector(ct["x"], "ct.x")
        y = _numeric_vector(ct["y"], "ct.y")
        z = _numeric_vector(ct["z"], "ct.z")
        if (x.size, y.size, z.size) != dimensions:
            raise ConversionError("coordinate lengths do not match cubeDim")
        spacing = (_regular_spacing(x, "X"), _regular_spacing(y, "Y"), _regular_spacing(z, "Z"))

        cube_obj = _dereference_first(handle, ct["cubeHU"])
        stored_cube = np.asarray(cube_obj)
        # MATLAB stores the logical cube as (Y, X, Z). MATLAB v7.3/HDF5
        # exposes those dimensions in reverse order as (Z, X, Y).
        if stored_cube.shape != (nz, nx, ny):
            raise ConversionError(
                "cubeHU has HDF5 shape {}, expected (Z, X, Y) {}".format(
                    stored_cube.shape, (nz, nx, ny)
                )
            )
        cube = stored_cube.transpose(0, 2, 1)
        if not np.issubdtype(cube.dtype, np.number) or not np.isfinite(cube).all():
            raise ConversionError("cubeHU must contain only finite numeric HU values")
        rounded = np.rint(cube)
        if rounded.min() < -32768 or rounded.max() > 32767:
            raise ConversionError("cubeHU values do not fit signed 16-bit CT pixels")

        info = _read_meta(handle, ct["dicomInfo"])
        meta = _read_meta(handle, ct["dicomMeta"])
        orientation = np.asarray(
            info.get("ImageOrientationPatient", meta.get("ImageOrientationPatient", [])), dtype=float
        ).reshape(-1)
        if orientation.size != 6 or not np.allclose(
            orientation, [1, 0, 0, 0, 1, 0], rtol=0, atol=1e-6
        ):
            raise ConversionError("only axial identity ImageOrientationPatient is supported")

        cst = handle["cst"]
        if not isinstance(cst, h5py.Dataset) or cst.dtype.kind != "O" or cst.shape[0] < 4:
            raise ConversionError("cst must be a matRad cell array with at least four rows")
        structures = []
        seen = set()
        voxel_count = nx * ny * nz
        for column in range(cst.shape[1]):
            name_obj = handle[cst[1, column]]
            kind_obj = handle[cst[2, column]]
            name = _matlab_string(name_obj)
            kind = _matlab_string(kind_obj)
            if not name:
                raise ConversionError("cst structure {} has an empty name".format(column + 1))
            if len(name) > 64:
                raise ConversionError(
                    "cst structure name exceeds the DICOM 64-character limit: {!r}".format(name)
                )
            if name in seen:
                raise ConversionError("duplicate cst structure name: {}".format(name))
            seen.add(name)
            scenario_cell = handle[cst[3, column]]
            index_obj = _dereference_first(handle, scenario_cell)
            raw = np.asarray(index_obj, dtype=float).reshape(-1, order="F")
            if not np.isfinite(raw).all() or not np.equal(raw, np.rint(raw)).all():
                raise ConversionError("structure {!r} contains invalid voxel indices".format(name))
            indices = raw.astype(np.int64)
            if indices.size and (indices.min() < 1 or indices.max() > voxel_count):
                raise ConversionError("structure {!r} contains out-of-range voxel indices".format(name))
            if np.unique(indices).size != indices.size:
                indices = np.unique(indices)
            structures.append((name, kind, indices))

        return {
            "cube": rounded.astype(np.int16),
            "dimensions": dimensions,
            "coordinates": (x, y, z),
            "spacing": spacing,
            "orientation": orientation.tolist(),
            "info": info,
            "meta": meta,
            "structures": structures,
        }


def validate_crop(dimensions, crop):
    names = ("X", "Y", "Z")
    slices = []
    for name, size, margins in zip(names, dimensions, crop):
        low, high = margins
        if low < 0 or high < 0:
            raise ConversionError("{} crop margins must be nonnegative".format(name))
        if low + high >= size:
            raise ConversionError("{} crop removes the entire axis of {} voxels".format(name, size))
        slices.append(slice(low, size - high))
    return tuple(slices)


def crop_data(data, crop, selected_names=None):
    nx, ny, nz = data["dimensions"]
    sx, sy, sz = validate_crop((nx, ny, nz), crop)
    cube = data["cube"][sz, sy, sx]
    x, y, z = data["coordinates"]
    structures = []
    omitted = []
    requested = None if selected_names is None else set(selected_names)
    available = {item[0] for item in data["structures"]}
    if requested is not None:
        missing = sorted(requested.difference(available))
        if missing:
            raise ConversionError("unknown structure name(s): {}".format(", ".join(missing)))
    for name, kind, indices in data["structures"]:
        if requested is not None and name not in requested:
            continue
        mask = np.zeros((nz, ny, nx), dtype=bool)
        if indices.size:
            iy, ix, iz = np.unravel_index(indices - 1, (ny, nx, nz), order="F")
            mask[iz, iy, ix] = True
        cropped = mask[sz, sy, sx]
        if not cropped.any():
            omitted.append(name)
        else:
            structures.append((name, kind, cropped))
    return {
        **data,
        "cube": cube,
        "original_dimensions": (nx, ny, nz),
        "dimensions": (cube.shape[2], cube.shape[1], cube.shape[0]),
        "coordinates": (x[sx], y[sy], z[sz]),
        "structures": structures,
        "omitted": omitted,
    }


def _new_file_dataset(path, sop_class, sop_instance):
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = sop_class
    file_meta.MediaStorageSOPInstanceUID = sop_instance
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    file_meta.ImplementationClassUID = generate_uid()
    return FileDataset(str(path), {}, file_meta=file_meta, preamble=b"\0" * 128)


def _text(meta, key, default=""):
    value = meta.get(key, default)
    return default if value is None else str(value)


def _apply_patient_study(ds, meta, anonymize, study_uid):
    if anonymize:
        ds.PatientName = ""
        ds.PatientID = ""
        ds.PatientBirthDate = ""
        ds.PatientSex = ""
        ds.AccessionNumber = ""
    else:
        for key in (
            "PatientName", "PatientID", "PatientBirthDate", "PatientSex", "AccessionNumber",
            "StudyID", "StudyDate", "StudyTime", "ReferringPhysicianName",
        ):
            value = _text(meta, key)
            if value:
                setattr(ds, key, value)
    ds.StudyInstanceUID = study_uid


def write_ct_series(data, directory, anonymize=False):
    nx, ny, nz = data["dimensions"]
    x, y, z = data["coordinates"]
    dx, dy, dz = data["spacing"]
    meta = data["meta"]
    study_uid = _text(meta, "StudyInstanceUID") or generate_uid()
    frame_uid = _text(meta, "FrameOfReferenceUID") or generate_uid()
    series_uid = generate_uid()
    now = dt.datetime.now()
    records = []
    for index in range(nz):
        sop_uid = generate_uid()
        path = directory / "CT_{:04d}.dcm".format(index + 1)
        ds = _new_file_dataset(path, CTImageStorage, sop_uid)
        ds.SOPClassUID = CTImageStorage
        ds.SOPInstanceUID = sop_uid
        ds.Modality = "CT"
        _apply_patient_study(ds, meta, anonymize, study_uid)
        ds.SeriesInstanceUID = series_uid
        ds.FrameOfReferenceUID = frame_uid
        ds.SeriesNumber = 9001
        ds.InstanceNumber = index + 1
        ds.ImageType = ["DERIVED", "SECONDARY", "AXIAL"]
        ds.PatientPosition = _text(meta, "PatientPosition", data["info"].get("PatientPosition", "HFS"))
        ds.ImageOrientationPatient = [float(value) for value in data["orientation"]]
        ds.ImagePositionPatient = [float(x[0]), float(y[0]), float(z[index])]
        ds.SliceLocation = float(z[index])
        ds.PixelSpacing = [float(dy), float(dx)]
        ds.SliceThickness = float(dz)
        ds.SpacingBetweenSlices = float(dz)
        ds.Rows = ny
        ds.Columns = nx
        ds.SamplesPerPixel = 1
        ds.PhotometricInterpretation = "MONOCHROME2"
        ds.BitsAllocated = 16
        ds.BitsStored = 16
        ds.HighBit = 15
        ds.PixelRepresentation = 1
        ds.RescaleIntercept = 0
        ds.RescaleSlope = 1
        ds.RescaleType = "HU"
        ds.WindowCenter = int((int(data["cube"].min()) + int(data["cube"].max())) / 2)
        ds.WindowWidth = max(1, int(data["cube"].max()) - int(data["cube"].min()))
        ds.ContentDate = now.strftime("%Y%m%d")
        ds.ContentTime = now.strftime("%H%M%S.%f")
        ds.PixelData = np.asarray(data["cube"][index], dtype="<i2").tobytes(order="C")
        pydicom.dcmwrite(str(path), ds, enforce_file_format=True)
        records.append(ds)
    return records, study_uid, frame_uid, series_uid


def _roi_color(index):
    red, green, blue = colorsys.hsv_to_rgb((index * 0.61803398875) % 1.0, 0.75, 1.0)
    return [int(255 * red), int(255 * green), int(255 * blue)]


def _interpreted_type(name, kind):
    normalized = name.strip().upper()
    for roi_type in ("GTV", "CTV", "PTV"):
        if normalized.startswith(roi_type):
            return roi_type
    return "CTV" if kind.upper() == "TARGET" else "ORGAN"


def _mask_contours(mask, x, y, z, spacing, ct_datasets):
    items = []
    for iz in range(mask.shape[0]):
        if not mask[iz].any():
            continue
        padded = np.pad(mask[iz].astype(np.uint8), 1, mode="constant")
        for contour in find_contours(padded, 0.5, fully_connected="high"):
            contour -= 1.0
            if contour.shape[0] < 3:
                continue
            points = []
            for row, column in contour:
                points.extend([
                    float(x[0] + column * spacing[0]),
                    float(y[0] + row * spacing[1]),
                    float(z[iz]),
                ])
            if points[:3] != points[-3:]:
                points.extend(points[:3])
            item = Dataset()
            item.ContourGeometricType = "CLOSED_PLANAR"
            item.NumberOfContourPoints = len(points) // 3
            item.ContourData = points
            image = Dataset()
            image.ReferencedSOPClassUID = CTImageStorage
            image.ReferencedSOPInstanceUID = ct_datasets[iz].SOPInstanceUID
            item.ContourImageSequence = Sequence([image])
            items.append(item)
    return items


def write_rtstruct(data, directory, ct_datasets, study_uid, frame_uid, series_uid, anonymize=False):
    path = directory / "RTSTRUCT.dcm"
    sop_uid = generate_uid()
    ds = _new_file_dataset(path, RTStructureSetStorage, sop_uid)
    ds.SOPClassUID = RTStructureSetStorage
    ds.SOPInstanceUID = sop_uid
    ds.Modality = "RTSTRUCT"
    _apply_patient_study(ds, data["meta"], anonymize, study_uid)
    ds.SeriesInstanceUID = generate_uid()
    ds.FrameOfReferenceUID = frame_uid
    ds.SeriesNumber = 9002
    ds.InstanceNumber = 1
    ds.StructureSetLabel = "MATRAD"
    now = dt.datetime.now()
    ds.StructureSetDate = now.strftime("%Y%m%d")
    ds.StructureSetTime = now.strftime("%H%M%S.%f")

    frame = Dataset()
    frame.FrameOfReferenceUID = frame_uid
    study = Dataset()
    study.ReferencedSOPClassUID = "1.2.840.10008.3.1.2.3.1"
    study.ReferencedSOPInstanceUID = study_uid
    series = Dataset()
    series.SeriesInstanceUID = series_uid
    image_refs = []
    for ct in ct_datasets:
        image = Dataset()
        image.ReferencedSOPClassUID = CTImageStorage
        image.ReferencedSOPInstanceUID = ct.SOPInstanceUID
        image_refs.append(image)
    series.ContourImageSequence = Sequence(image_refs)
    study.RTReferencedSeriesSequence = Sequence([series])
    frame.RTReferencedStudySequence = Sequence([study])
    ds.ReferencedFrameOfReferenceSequence = Sequence([frame])

    roi_items = []
    contour_items = []
    observation_items = []
    x, y, z = data["coordinates"]
    for number, (name, kind, mask) in enumerate(data["structures"], 1):
        roi = Dataset()
        roi.ROINumber = number
        roi.ReferencedFrameOfReferenceUID = frame_uid
        roi.ROIName = name
        roi.ROIGenerationAlgorithm = "SEMIAUTOMATIC"
        roi_items.append(roi)

        roi_contour = Dataset()
        roi_contour.ReferencedROINumber = number
        roi_contour.ROIDisplayColor = _roi_color(number)
        roi_contour.ContourSequence = Sequence(
            _mask_contours(mask, x, y, z, data["spacing"], ct_datasets)
        )
        contour_items.append(roi_contour)

        observation = Dataset()
        observation.ObservationNumber = number
        observation.ReferencedROINumber = number
        observation.RTROIInterpretedType = _interpreted_type(name, kind)
        observation.ROIInterpreter = ""
        observation_items.append(observation)
    ds.StructureSetROISequence = Sequence(roi_items)
    ds.ROIContourSequence = Sequence(contour_items)
    ds.RTROIObservationsSequence = Sequence(observation_items)
    pydicom.dcmwrite(str(path), ds, enforce_file_format=True)


def convert(input_path, output_dir, crop, selected_names=None, anonymize=False, overwrite=False):
    input_path = Path(input_path).resolve()
    output_dir = Path(output_dir).resolve()
    if not input_path.is_file():
        raise ConversionError("input MAT file does not exist: {}".format(input_path))
    if output_dir.exists() and not overwrite:
        raise ConversionError("output already exists; use --overwrite: {}".format(output_dir))
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".{}_".format(output_dir.name), dir=str(output_dir.parent)))
    try:
        data = crop_data(read_matrad(input_path), crop, selected_names)
        ct_datasets, study_uid, frame_uid, series_uid = write_ct_series(data, staging, anonymize)
        write_rtstruct(data, staging, ct_datasets, study_uid, frame_uid, series_uid, anonymize)
        if output_dir.exists():
            if output_dir.is_dir():
                shutil.rmtree(str(output_dir))
            else:
                output_dir.unlink()
        os.replace(str(staging), str(output_dir))
        return data
    except Exception:
        shutil.rmtree(str(staging), ignore_errors=True)
        raise


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="matRad MATLAB v7.3 file")
    parser.add_argument("--output-dir", type=Path, help="destination directory")
    parser.add_argument("--crop-x", nargs=2, type=int, metavar=("LOW", "HIGH"), default=(0, 0))
    parser.add_argument("--crop-y", nargs=2, type=int, metavar=("LOW", "HIGH"), default=(0, 0))
    parser.add_argument("--crop-z", nargs=2, type=int, metavar=("LOW", "HIGH"), default=(0, 0))
    parser.add_argument("--structures", nargs="+", metavar="NAME", help="only convert named structures")
    parser.add_argument("--anonymize", action="store_true", help="remove patient-identifying fields")
    parser.add_argument("--overwrite", action="store_true", help="replace an existing output directory")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    output = args.output_dir or args.input.with_name(args.input.stem + "_dicom")
    try:
        data = convert(
            args.input,
            output,
            (tuple(args.crop_x), tuple(args.crop_y), tuple(args.crop_z)),
            args.structures,
            args.anonymize,
            args.overwrite,
        )
    except (ConversionError, OSError, ValueError, KeyError) as exc:
        print("Error: {}".format(exc), file=sys.stderr)
        return 1
    crop = (tuple(args.crop_x), tuple(args.crop_y), tuple(args.crop_z))
    if any(low or high for low, high in crop):
        before = data["original_dimensions"]
        after = data["dimensions"]
        print("Grid dimensions before cropping: X={}, Y={}, Z={}".format(*before))
        print(
            "Crop margins (low/high voxels): X={}/{}, Y={}/{}, Z={}/{}".format(
                crop[0][0], crop[0][1], crop[1][0], crop[1][1], crop[2][0], crop[2][1]
            )
        )
        print("Grid dimensions after cropping:  X={}, Y={}, Z={}".format(*after))
    print("Wrote {} CT slices and {} structures to {}".format(
        data["dimensions"][2], len(data["structures"]), output
    ))
    if data["omitted"]:
        print("Warning: omitted empty structures after cropping: {}".format(", ".join(data["omitted"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
