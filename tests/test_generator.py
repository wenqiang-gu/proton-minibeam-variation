import tempfile
import unittest
from pathlib import Path

import numpy as np
import pydicom
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, RTStructureSetStorage, generate_uid

import generate_variations as g

ROOT = Path(__file__).resolve().parents[1]


def save(path, modality, sop_class, configure):
    meta = FileMetaDataset()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    meta.MediaStorageSOPClassUID = sop_class
    meta.MediaStorageSOPInstanceUID = generate_uid()
    ds = FileDataset(str(path), {}, file_meta=meta, preamble=b"\0" * 128)
    ds.SOPClassUID = sop_class; ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.Modality = modality; configure(ds)
    pydicom.dcmwrite(path, ds, enforce_file_format=True)
    return ds


def synthetic_series(root, orientation):
    frame, series = generate_uid(), generate_uid()
    cd, rd = np.array(orientation[:3], float), np.array(orientation[3:], float)
    normal, base = np.cross(cd, rd), np.array([10.0, 20.0, 30.0])
    cts = []
    for index in range(3):
        origin = base + normal * 1.5 * index
        def configure(ds, origin=origin, index=index):
            ds.FrameOfReferenceUID = frame; ds.SeriesInstanceUID = series
            ds.Rows = 8; ds.Columns = 9; ds.PixelSpacing = [1.5, 1.5]
            ds.ImageOrientationPatient = orientation
            ds.ImagePositionPatient = origin.tolist(); ds.InstanceNumber = index + 1
        cts.append(save(root / f"CT_{index}.dcm", "CT", CTImageStorage, configure))
    origin = base + normal * 1.5
    points = [origin + cd*c*1.5 + rd*r*1.5 for c,r in [(2,2),(5,2),(5,5),(2,5)]]
    def configure_rt(ds):
        ref = Dataset(); ref.FrameOfReferenceUID = frame
        ds.ReferencedFrameOfReferenceSequence = [ref]
        roi = Dataset(); roi.ROINumber = 1; roi.ROIName = "Target"; roi.ReferencedFrameOfReferenceUID = frame
        ds.StructureSetROISequence = [roi]
        contour = Dataset(); contour.ContourGeometricType = "CLOSED_PLANAR"
        contour.NumberOfContourPoints = 4; contour.ContourData = np.array(points).ravel().tolist()
        image = Dataset(); image.ReferencedSOPClassUID = CTImageStorage; image.ReferencedSOPInstanceUID = cts[1].SOPInstanceUID
        contour.ContourImageSequence = [image]
        group = Dataset(); group.ReferencedROINumber = 1; group.ContourSequence = [contour]
        ds.ROIContourSequence = [group]
    save(root / "RTSTRUCT.dcm", "RTSTRUCT", RTStructureSetStorage, configure_rt)


class GeneratorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.config = g.load_config(ROOT / "study.toml")

    def test_default_matrix_and_apertures(self):
        cases = g.cases(self.config, "smoke")
        sweep = g.table(self.config, "sweep")
        expected_cases = np.prod([len(sweep[key]) for key in ("slit_width_mm", "ctc_mm", "shift_fractions", "angles_deg")])
        expected_apertures = np.prod([len(sweep[key]) for key in ("slit_width_mm", "ctc_mm", "shift_fractions")])
        self.assertEqual(len(cases), expected_cases)
        self.assertEqual(len({g.ap_name(x.aperture) for x in cases}), expected_apertures)
        a = g.table(self.config, "aperture")
        for case in cases:
            self.assertLessEqual(case.aperture.count, 20)
            for x in case.aperture.positions:
                self.assertLessEqual((abs(x)+case.width/2)**2+(a["slit_height_mm"]/2)**2, a["radius_mm"]**2+1e-9)
        output_directories = g.case_output_directories(ROOT, self.config, "smoke")
        self.assertEqual(len(output_directories), expected_cases)
        self.assertTrue(all(str(path).startswith(str(ROOT / "output/smoke")) for path in output_directories))

    def test_actual_ptv_centroid(self):
        ct = g.read_ct(ROOT / "dicom_9306087_fine")
        center = g.roi_center(ct, ROOT / "dicom_9306087_fine/RTSTRUCT.dcm", "PTV2017fw")
        np.testing.assert_allclose(center.patient, [-5.61784, 93.21779, 169.46812], atol=1e-4)
        self.assertEqual(center.voxels, 6963)

    def test_rotated_orientation_and_roi_error(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); synthetic_series(root, [0, 1, 0, -1, 0, 0])
            ct = g.read_ct(root); center = g.roi_center(ct, root / "RTSTRUCT.dcm", "Target")
            self.assertGreater(center.voxels, 0)
            np.testing.assert_allclose(center.local[2], 2.25, atol=1e-9)
            with self.assertRaises(g.Error): g.roi_center(ct, root / "RTSTRUCT.dcm", "Missing")

    def test_histories_split_and_seed(self):
        chunks = g.split_histories([10, 11, 12], 4)
        self.assertEqual([sum(chunk[i] for chunk in chunks) for i in range(3)], [10, 11, 12])
        self.assertEqual(g.seed("smoke", "case", 1), g.seed("smoke", "case", 1))
        self.assertNotEqual(g.seed("smoke", "case", 1), g.seed("smoke", "case", 2))

    def test_reference_beam(self):
        histories = g.beam_histories(ROOT / "reference/beam_1_2e5.txt", 2151)
        self.assertEqual(len(histories), 2151); self.assertTrue(all(x > 0 for x in histories))

    def test_source_nests_time_features_for_topas_39(self):
        source = g.render_source(self.config)
        field = g.render_field(self.config, g.cases(self.config, "smoke")[0], "smoke")
        self.assertIn("includeFile = reference/beam_1_2e5.txt", source)
        self.assertNotIn("includeFile = reference/beam_1_2e5.txt", field)
        self.assertLess(source.index("includeFile"), source.index("Ge/BeamPosition2/RotX"))
        self.assertNotIn("Ge/BeamPosition/", source)
        self.assertIn('So/ProtonSource/Component = "BeamPosition2"', source)

    def test_visualization_is_configurable(self):
        rendered = g.render_visualization(self.config)
        self.assertIn('s:Gr/ViewA/Type = "OpenGL"', rendered)
        self.assertIn('b:Gr/ViewA/Active = "True"', rendered)
        self.assertIn('i:Gr/SwitchOGLtoOGLIifVoxelCountExceeds = 1000000000', rendered)
        self.assertIn('i:Gr/ShowOnlyOutlineIfVoxelCountExceeds = 20000000', rendered)
        self.assertIn('b:Gr/ViewA/IncludeTrajectories = "False"', rendered)
        self.assertIn('d:Gr/ViewA/AxesSize = 200 mm', rendered)
        field = g.render_field(self.config, g.cases(self.config, "smoke")[0], "smoke")
        self.assertNotIn('Gr/ViewA', field)
        vis_test = g.render_vis_test(self.config, "generated/smoke/tasks/example.txt")
        self.assertIn('includeFile = generated/smoke/tasks/example.txt', vis_test)
        self.assertIn('b:Sc/PatientDose/Active = "False"', vis_test)
        self.assertIn('i:Ts/NumberOfThreads = 1', vis_test)
        self.assertIn('iv:Ge/Patient/ShowSpecificSlicesZ = 1 54', vis_test)


if __name__ == "__main__": unittest.main()
