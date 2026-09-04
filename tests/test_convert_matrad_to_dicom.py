import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import h5py
import numpy as np
import pydicom


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "convert_matrad_to_dicom", ROOT / "matRad" / "convert_matrad_to_dicom.py"
)
converter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(converter)


def add_char(refs, name, value):
    dataset = refs.create_dataset(
        name, data=np.array([ord(c) for c in value], dtype=np.uint16).reshape(-1, 1)
    )
    dataset.attrs["MATLAB_class"] = np.bytes_(b"char")
    return dataset


def make_fixture(path, bad_index=False):
    nx, ny, nz = 4, 3, 2
    cube = np.arange(nx * ny * nz, dtype=float).reshape((nz, ny, nx)) - 10
    with h5py.File(str(path), "w") as handle:
        refs = handle.create_group("#refs#")
        ct = handle.create_group("ct")
        ct.create_dataset("cubeDim", data=np.array([[nx], [ny], [nz]], dtype=float))
        for name, values in (
            ("x", [10, 11.5, 13, 14.5]),
            ("y", [20, 22, 24]),
            ("z", [30, 32.5]),
        ):
            ct.create_dataset(name, data=np.asarray(values, dtype=float).reshape(-1, 1))
        cube_ds = refs.create_dataset("cube", data=cube.transpose(0, 2, 1))
        cube_cell = ct.create_dataset("cubeHU", shape=(1, 1), dtype=h5py.ref_dtype)
        cube_cell[0, 0] = cube_ds.ref
        info = ct.create_group("dicomInfo")
        info.create_dataset("ImageOrientationPatient", data=np.array([[1, 0, 0, 0, 1, 0]], dtype=float))
        patient_position = info.create_dataset(
            "PatientPosition", data=np.array([ord(c) for c in "HFS"], dtype=np.uint16).reshape(-1, 1)
        )
        patient_position.attrs["MATLAB_class"] = np.bytes_(b"char")
        meta = ct.create_group("dicomMeta")
        for name, value in (
            ("PatientName", "Test^Patient"),
            ("PatientID", "P123"),
            ("StudyInstanceUID", "1.2.826.0.1.3680043.10.999.1"),
            ("FrameOfReferenceUID", "1.2.826.0.1.3680043.10.999.2"),
        ):
            target = add_char(refs, "meta_" + name, value)
            cell = meta.create_dataset(name, shape=(1, 1), dtype=h5py.ref_dtype)
            cell[0, 0] = target.ref

        cst = handle.create_dataset("cst", shape=(6, 2), dtype=h5py.ref_dtype)
        for col, (name, kind, indices) in enumerate(
            (("Target", "TARGET", [1, 2, 17]), ("Edge", "OAR", [24]))
        ):
            number = refs.create_dataset("num{}".format(col), data=[[float(col)]])
            name_ds = add_char(refs, "name{}".format(col), name)
            kind_ds = add_char(refs, "kind{}".format(col), kind)
            values = [nx * ny * nz + 1] if bad_index and col == 0 else indices
            indices_ds = refs.create_dataset("indices{}".format(col), data=np.asarray([values], dtype=float))
            scenario = refs.create_dataset("scenario{}".format(col), shape=(1, 1), dtype=h5py.ref_dtype)
            scenario[0, 0] = indices_ds.ref
            empty = refs.create_dataset("empty{}".format(col), data=np.zeros((2,), dtype=np.uint64))
            for row, obj in enumerate((number, name_ds, kind_ds, scenario, empty, empty)):
                cst[row, col] = obj.ref
    return cube


class ConverterTests(unittest.TestCase):
    def test_reads_cube_and_matlab_linear_indices(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.mat"
            cube = make_fixture(path)
            data = converter.read_matrad(path)
            self.assertEqual(data["cube"].shape, (2, 3, 4))
            np.testing.assert_array_equal(data["cube"], cube.astype(np.int16))
            cropped = converter.crop_data(data, ((0, 0), (0, 0), (0, 0)))
            target = cropped["structures"][0][2]
            self.assertTrue(target[0, 0, 0])
            self.assertTrue(target[0, 1, 0])
            self.assertTrue(target[1, 1, 1])
            self.assertEqual(int(target.sum()), 3)

    def test_asymmetric_crop_and_empty_structure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.mat"
            make_fixture(path)
            data = converter.crop_data(
                converter.read_matrad(path), ((1, 0), (0, 1), (0, 0))
            )
            self.assertEqual(data["dimensions"], (3, 2, 2))
            self.assertEqual(data["coordinates"][0][0], 11.5)
            self.assertIn("Edge", data["omitted"])

    def test_crop_rejects_negative_and_full_axis(self):
        with self.assertRaises(converter.ConversionError):
            converter.validate_crop((4, 3, 2), ((-1, 0), (0, 0), (0, 0)))
        with self.assertRaises(converter.ConversionError):
            converter.validate_crop((4, 3, 2), ((2, 2), (0, 0), (0, 0)))

    def test_unknown_structure_and_invalid_index(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.mat"
            make_fixture(path)
            data = converter.read_matrad(path)
            with self.assertRaises(converter.ConversionError):
                converter.crop_data(data, ((0, 0),) * 3, ["Missing"])
            bad = Path(directory) / "bad.mat"
            make_fixture(bad, bad_index=True)
            with self.assertRaises(converter.ConversionError):
                converter.read_matrad(bad)

    def test_missing_fields_irregular_coordinates_and_nonfinite_hu(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing = root / "missing.mat"
            with h5py.File(str(missing), "w") as handle:
                handle.create_group("ct")
            with self.assertRaisesRegex(converter.ConversionError, "both 'ct' and 'cst'"):
                converter.read_matrad(missing)

            irregular = root / "irregular.mat"
            make_fixture(irregular)
            with h5py.File(str(irregular), "r+") as handle:
                handle["ct/x"][2, 0] = 13.5
            with self.assertRaisesRegex(converter.ConversionError, "regularly increasing"):
                converter.read_matrad(irregular)

            nonfinite = root / "nonfinite.mat"
            make_fixture(nonfinite)
            with h5py.File(str(nonfinite), "r+") as handle:
                handle["#refs#/cube"][0, 0, 0] = np.nan
            with self.assertRaisesRegex(converter.ConversionError, "finite numeric HU"):
                converter.read_matrad(nonfinite)

    def test_conversion_writes_linked_dicom_and_rtstruct(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.mat"
            make_fixture(path)
            output = Path(directory) / "dicom"
            result = converter.convert(path, output, ((0, 0),) * 3)
            self.assertEqual(result["dimensions"], (4, 3, 2))
            ct1 = pydicom.dcmread(str(output / "CT_0001.dcm"))
            ct2 = pydicom.dcmread(str(output / "CT_0002.dcm"))
            rt = pydicom.dcmread(str(output / "RTSTRUCT.dcm"))
            self.assertEqual((ct1.Columns, ct1.Rows), (4, 3))
            self.assertEqual(list(ct1.ImagePositionPatient), [10.0, 20.0, 30.0])
            np.testing.assert_array_equal(ct1.pixel_array, result["cube"][0])
            self.assertEqual(ct1.SeriesInstanceUID, ct2.SeriesInstanceUID)
            self.assertNotEqual(ct1.SOPInstanceUID, ct2.SOPInstanceUID)
            self.assertEqual(rt.StudyInstanceUID, ct1.StudyInstanceUID)
            self.assertEqual(
                rt.ReferencedFrameOfReferenceSequence[0].FrameOfReferenceUID,
                ct1.FrameOfReferenceUID,
            )
            names = [str(item.ROIName) for item in rt.StructureSetROISequence]
            self.assertEqual(names, ["Target", "Edge"])
            referenced = {
                str(contour.ContourImageSequence[0].ReferencedSOPInstanceUID)
                for roi in rt.ROIContourSequence
                for contour in roi.ContourSequence
            }
            self.assertEqual(referenced, {ct1.SOPInstanceUID, ct2.SOPInstanceUID})

    def test_anonymization_and_existing_output_protection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.mat"
            make_fixture(path)
            output = Path(directory) / "dicom"
            converter.convert(path, output, ((0, 0),) * 3, anonymize=True)
            ct = pydicom.dcmread(str(output / "CT_0001.dcm"), stop_before_pixels=True)
            self.assertEqual(str(ct.PatientName), "")
            self.assertEqual(str(ct.PatientID), "")
            with self.assertRaises(converter.ConversionError):
                converter.convert(path, output, ((0, 0),) * 3)

    def test_named_structure_subset(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.mat"
            make_fixture(path)
            output = Path(directory) / "dicom"
            converter.convert(path, output, ((0, 0),) * 3, selected_names=["Target"])
            rt = pydicom.dcmread(str(output / "RTSTRUCT.dcm"), stop_before_pixels=True)
            self.assertEqual([str(item.ROIName) for item in rt.StructureSetROISequence], ["Target"])

    def test_cli_reports_dimensions_when_cropping(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.mat"
            output = Path(directory) / "dicom"
            make_fixture(path)
            stdout = io.StringIO()
            with mock.patch("sys.stdout", stdout):
                result = converter.main([
                    str(path), "--output-dir", str(output),
                    "--crop-x", "1", "0", "--crop-y", "0", "1",
                ])
            self.assertEqual(result, 0)
            message = stdout.getvalue()
            self.assertIn("before cropping: X=4, Y=3, Z=2", message)
            self.assertIn("X=1/0, Y=0/1, Z=0/0", message)
            self.assertIn("after cropping:  X=3, Y=2, Z=2", message)

    def test_cli_omits_crop_summary_without_crop(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.mat"
            output = Path(directory) / "dicom"
            make_fixture(path)
            stdout = io.StringIO()
            with mock.patch("sys.stdout", stdout):
                result = converter.main([str(path), "--output-dir", str(output)])
            self.assertEqual(result, 0)
            self.assertNotIn("before cropping", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
