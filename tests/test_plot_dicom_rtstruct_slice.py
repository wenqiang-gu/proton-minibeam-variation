import importlib.util
import io
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

import numpy as np
import pydicom
from pydicom.uid import generate_uid

from test_convert_matrad_to_dicom import converter, make_fixture


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "plot_dicom_rtstruct_slice", ROOT / "matRad" / "plot_dicom_rtstruct_slice.py"
)
plotter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(plotter)


def make_dicom(directory):
    mat_path = directory / "input.mat"
    dicom_path = directory / "dicom"
    make_fixture(mat_path)
    converter.convert(mat_path, dicom_path, ((0, 0),) * 3)
    return dicom_path


class DicomRTStructPlotTests(unittest.TestCase):
    def test_reads_geometry_and_one_based_slice(self):
        with tempfile.TemporaryDirectory() as temporary:
            dicom = make_dicom(Path(temporary))
            series = plotter.read_dicom_directory(dicom)
            self.assertEqual((series["columns"], series["rows"], len(series["ct"])), (4, 3, 2))
            self.assertEqual([float(value) for value in series["projections"]], [30.0, 32.5])
            self.assertEqual(plotter.select_z_slice(2, 2), 1)
            with self.assertRaises(plotter.PlotError):
                plotter.select_z_slice(0, 2)
            with self.assertRaises(plotter.PlotError):
                plotter.select_z_slice(3, 2)

    def test_hu_rescaling_and_window_selection(self):
        with tempfile.TemporaryDirectory() as temporary:
            dicom = make_dicom(Path(temporary))
            series = plotter.read_dicom_directory(dicom)
            dataset = series["ct"][0]
            dataset.RescaleSlope = 2
            dataset.RescaleIntercept = -100
            plane = plotter.ct_plane(dataset)
            expected = dataset.pixel_array.astype(float) * 2 - 100
            np.testing.assert_array_equal(plane, expected)
            self.assertEqual(plotter.display_window(dataset, plane, (40, 400)), (-160.0, 240.0))
            with self.assertRaises(plotter.PlotError):
                plotter.display_window(dataset, plane, (40, 0))

    def test_contours_map_to_one_based_indices_and_filter(self):
        with tempfile.TemporaryDirectory() as temporary:
            dicom = make_dicom(Path(temporary))
            series = plotter.read_dicom_directory(dicom)
            contours = plotter.contours_for_slice(series, 0)
            self.assertEqual([item[0] for item in contours], ["Target"])
            columns, rows = contours[0][2][0]
            self.assertAlmostEqual(float(columns.min()), 0.5)
            self.assertAlmostEqual(float(columns.max()), 1.5)
            self.assertAlmostEqual(float(rows.min()), 0.5)
            self.assertAlmostEqual(float(rows.max()), 2.5)
            self.assertEqual(plotter.contours_for_slice(series, 0, ["Edge"]), [])
            with self.assertRaisesRegex(plotter.PlotError, "unknown structure"):
                plotter.contours_for_slice(series, 0, ["Missing"])

    def test_missing_references_fall_back_to_contour_plane(self):
        with tempfile.TemporaryDirectory() as temporary:
            dicom = make_dicom(Path(temporary))
            rt_path = dicom / "RTSTRUCT.dcm"
            rt = pydicom.dcmread(str(rt_path))
            for group in rt.ROIContourSequence:
                for contour in group.ContourSequence:
                    if hasattr(contour, "ContourImageSequence"):
                        del contour.ContourImageSequence
            pydicom.dcmwrite(str(rt_path), rt, enforce_file_format=True)
            series = plotter.read_dicom_directory(dicom)
            self.assertEqual([item[0] for item in plotter.contours_for_slice(series, 1)], ["Target", "Edge"])

    def test_default_output_and_png_plot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dicom = make_dicom(root)
            expected = root / "dicom_z_slice_1.png"
            stdout = io.StringIO()
            with mock.patch("sys.stdout", stdout):
                result = plotter.main([str(dicom), "--z-slice", "1"])
            self.assertEqual(result, 0)
            self.assertTrue(expected.is_file())
            self.assertIn("CT dimensions: X=4, Y=3, Z=2", stdout.getvalue())
            self.assertIn("Plotted structures: Target", stdout.getvalue())

    def test_empty_overlay_still_writes_png(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dicom = make_dicom(root)
            output = root / "custom.png"
            stdout = io.StringIO()
            with mock.patch("sys.stdout", stdout):
                result = plotter.main([
                    str(dicom), "--z-slice", "1", "--structures", "Edge",
                    "--output", str(output), "--window", "0", "1000",
                ])
            self.assertEqual(result, 0)
            self.assertTrue(output.is_file())
            self.assertIn("No selected structures intersect", stdout.getvalue())

    def test_missing_or_multiple_rtstruct_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dicom = make_dicom(root)
            rt = dicom / "RTSTRUCT.dcm"
            saved = root / "saved.dcm"
            rt.rename(saved)
            with self.assertRaisesRegex(plotter.PlotError, "exactly one RTSTRUCT"):
                plotter.read_dicom_directory(dicom)
            saved.rename(rt)
            shutil.copyfile(str(rt), str(dicom / "RTSTRUCT_copy.dcm"))
            with self.assertRaisesRegex(plotter.PlotError, "found 2"):
                plotter.read_dicom_directory(dicom)

    def test_frame_series_and_contour_reference_validation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dicom = make_dicom(root)
            ct_path = dicom / "CT_0002.dcm"
            ct = pydicom.dcmread(str(ct_path))
            ct.SeriesInstanceUID = generate_uid()
            pydicom.dcmwrite(str(ct_path), ct, enforce_file_format=True)
            with self.assertRaisesRegex(plotter.PlotError, "multiple CT series"):
                plotter.read_dicom_directory(dicom)

            shutil.rmtree(str(dicom))
            dicom = make_dicom(root)
            rt_path = dicom / "RTSTRUCT.dcm"
            rt = pydicom.dcmread(str(rt_path))
            rt.ROIContourSequence[0].ContourSequence[0].ContourImageSequence[
                0
            ].ReferencedSOPInstanceUID = generate_uid()
            pydicom.dcmwrite(str(rt_path), rt, enforce_file_format=True)
            series = plotter.read_dicom_directory(dicom)
            with self.assertRaisesRegex(plotter.PlotError, "outside this series"):
                plotter.contours_for_slice(series, 0)


if __name__ == "__main__":
    unittest.main()
