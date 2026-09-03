import tempfile
import unittest
import os
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

    def test_omitted_slice_computes_maximum_along_z(self):
        with tempfile.TemporaryDirectory() as directory:
            path, volume = self.make_dose(Path(directory))
            plane = plot.read_xy_plane(path, (4, 3, 2), None)
            np.testing.assert_array_equal(plane, np.max(volume, axis=0))

            volume[0, 0, 0] = -np.inf
            volume.astype("<f8").tofile(path)
            with self.assertRaisesRegex(plot.PlotError, "Dose volume contains non-finite"):
                plot.read_xy_plane(path, (4, 3, 2), None)

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

    def test_output_selection_for_static_and_interactive_modes(self):
        input_path = Path("/tmp/Dose.bin")
        self.assertEqual(
            plot.output_path(input_path, None, 54, False),
            Path("/tmp/Dose_z_slice_54.png"),
        )
        self.assertIsNone(plot.output_path(input_path, None, 54, True))
        self.assertEqual(
            plot.output_path(input_path, None, None, False),
            Path("/tmp/Dose_z_max.png"),
        )
        self.assertIsNone(plot.output_path(input_path, None, None, True))
        self.assertEqual(
            plot.output_path(input_path, Path("/tmp/custom.png"), 54, True),
            Path("/tmp/custom.png").resolve(),
        )

    def test_interactive_main_does_not_select_default_output(self):
        with tempfile.TemporaryDirectory() as directory:
            path, _ = self.make_dose(Path(directory))
            with mock.patch.object(plot, "plot_xy") as plot_xy:
                result = plot.main([str(path), "--z-slice", "1", "--interactive"])
            self.assertEqual(result, 0)
            self.assertIsNone(plot_xy.call_args.args[0])
            self.assertTrue(plot_xy.call_args.args[-1])

    def test_projection_main_uses_z_max_and_explicit_output_rules(self):
        with tempfile.TemporaryDirectory() as directory:
            path, volume = self.make_dose(Path(directory))
            with mock.patch.object(plot, "plot_xy") as plot_xy:
                result = plot.main([str(path)])
            self.assertEqual(result, 0)
            self.assertEqual(
                plot_xy.call_args.args[0],
                path.with_name("Dose_z_max.png").resolve(),
            )
            self.assertIsNone(plot_xy.call_args.args[2])
            np.testing.assert_array_equal(plot_xy.call_args.args[1], np.max(volume, axis=0))

            output = Path(directory) / "both.png"
            with mock.patch.object(plot, "plot_xy") as plot_xy:
                plot.main([str(path), "--interactive", "--output", str(output)])
            self.assertEqual(plot_xy.call_args.args[0], output.resolve())
            self.assertTrue(plot_xy.call_args.args[-1])

    def test_interactive_plot_requests_blocking_show(self):
        plane = np.array([[0.0, 1.0], [2.0, 3.0]])
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.dict(os.environ, {
                 "MPLCONFIGDIR": directory,
                 "XDG_CACHE_HOME": directory,
             }), \
             mock.patch.object(plot, "ensure_interactive_backend"):
            import matplotlib
            matplotlib.use("Agg", force=True)
            import matplotlib.pyplot as plt
            with mock.patch.object(plt, "show") as show:
                plot.plot_xy(None, plane, 1, "max", "viridis", None, 72, True)
            show.assert_called_once_with(block=True)

    def test_interactive_mode_rejects_noninteractive_backend(self):
        backend = mock.Mock()
        backend.get_backend.return_value = "Agg"
        with self.assertRaisesRegex(plot.PlotError, "graphical Matplotlib backend"):
            plot.ensure_interactive_backend(backend)


if __name__ == "__main__":
    unittest.main()
