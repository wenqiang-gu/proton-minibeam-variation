import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from Slurm import plot_dose_xy as plot


HEADER = """# TOPAS binary dose output
# X = 4 bins
# Y = 3 bins
# Z = 2 bins
"""


class PlotDoseXYTests(unittest.TestCase):
    def make_dose(self, root: Path, values=None):
        path = root / "Dose.bin"
        volume = np.arange(24, dtype="<f8").reshape(2, 3, 4)
        if values is not None:
            volume = np.asarray(values, dtype="<f8").reshape(2, 3, 4)
        volume.tofile(path)
        path.with_suffix(".binheader").write_text(HEADER)
        return path, volume

    def test_header_shape_and_one_based_slice_selection(self):
        self.assertEqual(plot.grid_shape(HEADER.splitlines(), None), (4, 3, 2))
        self.assertEqual(plot.select_z_slice(1, 2), 0)
        self.assertEqual(plot.select_z_slice(2, 2), 1)
        with self.assertRaisesRegex(plot.PlotError, "from 1 to 2"):
            plot.select_z_slice(0, 2)

    def test_reads_requested_xy_plane_in_topas_order(self):
        with tempfile.TemporaryDirectory() as directory:
            path, volume = self.make_dose(Path(directory))
            plane = plot.read_xy_slice(path, (4, 3, 2), 1)
            np.testing.assert_array_equal(plane, volume[1])

    def test_validates_binary_size_and_nonfinite_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, _ = self.make_dose(root)
            with self.assertRaisesRegex(plot.PlotError, "expected"):
                plot.read_xy_slice(path, (5, 3, 2), 0)
            values = np.arange(24, dtype=float)
            values[3] = np.nan
            path, _ = self.make_dose(root, values)
            with self.assertRaisesRegex(plot.PlotError, "non-finite"):
                plot.read_xy_slice(path, (4, 3, 2), 0)

    def test_relative_and_raw_scaling(self):
        plane = np.array([[0.0, 2.0], [5.0, 10.0]])
        relative, label, vmin, vmax = plot.scale_plane(plane, "max")
        np.testing.assert_allclose(relative, [[0.0, 20.0], [50.0, 100.0]])
        self.assertIn("%", label); self.assertEqual((vmin, vmax), (0.0, 100.0))
        raw, label, _, _ = plot.scale_plane(plane, "none")
        np.testing.assert_array_equal(raw, plane); self.assertIn("Gy", label)
        with self.assertRaisesRegex(plot.PlotError, "no positive dose"):
            plot.scale_plane(np.zeros((2, 2)), "max")

    def test_missing_and_empty_inputs_are_clear(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); path = root / "Dose.bin"
            with self.assertRaisesRegex(plot.PlotError, "companion header"):
                plot.read_header(path)
            path.with_suffix(".binheader").touch()
            with self.assertRaisesRegex(plot.PlotError, "header is empty"):
                plot.read_header(path)
            path.write_bytes(b"")
            path.with_suffix(".binheader").write_text(HEADER)
            with self.assertRaisesRegex(plot.PlotError, "Dose input is empty"):
                plot.read_xy_slice(path, (4, 3, 2), 0)

    def test_shape_override_allows_missing_header(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "Dose.bin"
            np.arange(24, dtype="<f8").tofile(path)
            output = root / "slice.png"
            with mock.patch.object(plot, "plot_xy") as plot_xy:
                result = plot.main([
                    str(path), "--z-slice", "2", "--shape", "4", "3", "2",
                    "--output", str(output),
                ])
            self.assertEqual(result, 0)
            np.testing.assert_array_equal(plot_xy.call_args.args[1], np.arange(12, 24).reshape(3, 4))


if __name__ == "__main__":
    unittest.main()
