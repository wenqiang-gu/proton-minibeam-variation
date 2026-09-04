import copy
import re
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
    def setUpClass(cls):
        cls.config = g.load_config(ROOT / "study.toml")
        cls.beam_path = ROOT / cls.config["beam"]["time_feature_file"]
        cls.envelope = g.beam_envelope(cls.beam_path,cls.config)

    def test_default_matrix_and_apertures(self):
        cases = g.cases(self.config, "smoke",self.envelope)
        sweep = g.table(self.config, "sweep")
        expected_cases = np.prod([len(sweep[key]) for key in ("slit_width_mm", "ctc_mm", "shift_fractions", "angles_deg")])
        expected_apertures = np.prod([len(sweep[key]) for key in ("slit_width_mm", "ctc_mm", "shift_fractions")])
        self.assertEqual(len(cases), expected_cases)
        self.assertEqual(len({g.ap_name(x.aperture) for x in cases}), expected_apertures)
        a = g.table(self.config, "aperture")
        for case in cases:
            self.assertLessEqual(case.aperture.count, self.config["aperture"]["max_slits"])
            self.assertEqual(case.aperture.count % 2, 0)
            for x in case.aperture.positions:
                self.assertLessEqual((abs(x)+case.width/2)**2+(a["slit_height_mm"]/2)**2, a["radius_mm"]**2+1e-9)
        output_directories = g.case_output_directories(ROOT, self.config, "smoke")
        self.assertEqual(len(output_directories), expected_cases)
        self.assertTrue(all(str(path).startswith(str(ROOT / "output/smoke")) for path in output_directories))

    def test_one_sigma_envelope_and_even_counts(self):
        self.assertEqual(self.envelope.sigma,1.0)
        self.assertAlmostEqual(self.envelope.x_min,-23.3844558688)
        self.assertAlmostEqual(self.envelope.x_max,22.4313947453)
        self.assertAlmostEqual(self.envelope.y_min,-14.7053883720)
        self.assertAlmostEqual(self.envelope.y_max,17.9603007820)
        self.assertAlmostEqual(2*max(abs(self.envelope.y_min),abs(self.envelope.y_max)),35.9206015640)

        config=copy.deepcopy(self.config)
        config["sweep"]["slit_width_mm"]=[0.4]
        config["sweep"]["ctc_mm"]=[3.0,5.0,7.0]
        generated_cases=g.cases(config,"smoke",self.envelope)
        counts={ctc:{case.aperture.count for case in generated_cases if case.ctc==ctc} for ctc in (3.0,5.0,7.0)}
        self.assertEqual(counts,{3.0:{18},5.0:{14},7.0:{10}})
        shifted=next(case for case in generated_cases if case.ctc==7.0 and case.shift==0.75)
        low,high=g.slit_lattice_bounds(shifted.width,shifted.ctc,shifted.shift,shifted.aperture.count)
        self.assertAlmostEqual(low,-26.45); self.assertAlmostEqual(high,36.95)
        self.assertAlmostEqual(np.hypot(max(abs(low),abs(high)),18.0),41.1011252887)
        zero=next(case for case in generated_cases if case.ctc==3.0 and case.shift==0.0)
        half=next(case for case in generated_cases if case.ctc==3.0 and case.shift==0.5)
        self.assertNotIn(0.0,zero.aperture.positions)
        self.assertIn(0.0,half.aperture.positions)

        with self.assertRaisesRegex(g.Error,"minimum centered height"):
            g.slit_count(0.4,3.0,[0.0,0.25,0.5,0.75],45.0,20.0,18,self.envelope)
        with self.assertRaisesRegex(g.Error,"does not cover"):
            g.slit_count(0.4,3.0,[0.0,0.25,0.5,0.75],45.0,36.0,16,self.envelope)

    def test_per_angle_downstream_surface_distances(self):
        config = copy.deepcopy(self.config)
        config["sweep"]["angles_deg"] = [0.0, 45.0, 90.0]
        config["aperture"]["downstream_surface_distance_mm"] = [180.0, 160.0, 140.0]
        generated_cases = g.cases(config, "smoke",g.beam_envelope(self.beam_path,config))
        expected = {0.0: 180.0, 45.0: 160.0, 90.0: 140.0}
        self.assertTrue(generated_cases)
        for case in generated_cases:
            self.assertEqual(case.downstream_surface_distance, expected[case.angle])
            field = g.render_field(config, case, "smoke")
            self.assertIn(
                f"d:Ge/Snout/TransZ = {g.fmt(-(expected[case.angle] + 30.0))} mm",
                field,
            )
        self.assertNotIn("Ge/Snout/TransZ", g.render_aperture(config, generated_cases[0].aperture))

        config["sweep"]["angles_deg"] = [45.0]
        config["aperture"]["downstream_surface_distance_mm"] = [180.0]
        single_angle_cases = g.cases(config, "smoke",g.beam_envelope(self.beam_path,config))
        self.assertTrue(all(case.angle == 45.0 for case in single_angle_cases))
        self.assertTrue(all(case.downstream_surface_distance == 180.0 for case in single_angle_cases))

    def test_downstream_surface_distance_array_validation(self):
        invalid = {
            "scalar": ("downstream_surface_distance_mm = 150.0", "non-empty numeric array"),
            "empty": ("downstream_surface_distance_mm = []", "non-empty numeric array"),
            "short": ("downstream_surface_distance_mm = [150.0]", "same length"),
            "boolean": ("downstream_surface_distance_mm = [true, true, true, true, true, true, true, true]", "non-empty numeric array"),
            "nonnumeric": ('downstream_surface_distance_mm = ["x", "x", "x", "x", "x", "x", "x", "x"]', "non-empty numeric array"),
            "zero": ("downstream_surface_distance_mm = [0.0, 150.0, 150.0, 150.0, 150.0, 150.0, 150.0, 150.0]", "values must be positive"),
            "negative": ("downstream_surface_distance_mm = [-1.0, 150.0, 150.0, 150.0, 150.0, 150.0, 150.0, 150.0]", "values must be positive"),
        }
        source = (ROOT / "study.toml").read_text()
        pattern = r"downstream_surface_distance_mm\s*=\s*\[[^\]]*\]"
        self.assertEqual(len(re.findall(pattern,source,re.S)),1)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "study.toml"
            for name, (replacement, message) in invalid.items():
                with self.subTest(name=name):
                    path.write_text(re.sub(pattern,replacement,source,count=1,flags=re.S))
                    with self.assertRaisesRegex(g.Error, message):
                        g.load_config(path)

    def test_actual_ptv_centroid(self):
        ct = g.read_ct(ROOT / "dicom_9306087_fine")
        center = g.roi_center(ct, ROOT / "dicom_9306087_fine/RTSTRUCT.dcm", "PTV2017fw")
        np.testing.assert_allclose(center.patient, [-5.61784, 93.21779, 169.46812], atol=1e-4)
        self.assertEqual(center.voxels, 6963)
        np.testing.assert_array_equal(center.index_min, [150, 162, 40])
        np.testing.assert_array_equal(center.index_max, [186, 186, 66])

    def test_topas_native_patient_crop(self):
        ct = g.read_ct(ROOT / "dicom_9306087_fine")
        center = g.roi_center(ct, ROOT / "dicom_9306087_fine/RTSTRUCT.dcm", "PTV2017fw")
        crop = g.patient_crop(self.config, ct, center)
        configured_low = np.array([
            self.config["patient"][f"crop_{axis}_voxels"][0] for axis in "xyz"
        ])
        configured_high = np.array([
            self.config["patient"][f"crop_{axis}_voxels"][1] for axis in "xyz"
        ])
        source_shape = np.array([ct.cols, ct.rows, len(ct.origins)])
        np.testing.assert_array_equal(crop.low, configured_low)
        np.testing.assert_array_equal(crop.high, configured_high)
        np.testing.assert_array_equal(crop.shape, source_shape-configured_low-configured_high)
        patient = g.render_patient(self.config, ct, center)
        for index, axis in enumerate("XYZ"):
            self.assertIn(f"RestrictVoxels{axis}Min = {configured_low[index]+1}", patient)
            self.assertIn(f"RestrictVoxels{axis}Max = {source_shape[index]-configured_high[index]}", patient)
        spacing = np.array([ct.col_spacing, ct.row_spacing, ct.slice_spacing])
        cropped_local = center.local-configured_low*spacing
        expected_translation = crop.shape*spacing/2-cropped_local
        for axis, value in zip("XYZ", expected_translation):
            self.assertIn(f"d:Ge/Patient/Trans{axis} = {g.fmt(value)} mm", patient)

    def test_asymmetric_crop_compensates_translation_and_rejects_roi_clipping(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); synthetic_series(root, [1, 0, 0, 0, 1, 0])
            ct = g.read_ct(root); center = g.roi_center(ct, root / "RTSTRUCT.dcm", "Target")
            uncropped = copy.deepcopy(self.config)
            for axis in "xyz": uncropped["patient"].pop(f"crop_{axis}_voxels", None)
            cropped = copy.deepcopy(uncropped)
            cropped["patient"]["crop_x_voxels"] = [1, 2]
            cropped["patient"]["crop_y_voxels"] = [1, 1]
            cropped["patient"]["crop_z_voxels"] = [0, 1]
            before = ct.size/2-center.local
            spacing = np.array([ct.col_spacing,ct.row_spacing,ct.slice_spacing])
            expected = before+(np.array([1,1,0])-np.array([2,1,1]))*spacing/2
            rendered = g.render_patient(cropped,ct,center)
            for axis,value in zip("XYZ",expected): self.assertIn(f"d:Ge/Patient/Trans{axis} = {g.fmt(value)} mm",rendered)
            clipping = copy.deepcopy(uncropped); clipping["patient"]["crop_x_voxels"]=[3,0]
            with self.assertRaisesRegex(g.Error,"removes voxels from the configured ROI"):
                g.patient_crop(clipping,ct,center)

    def test_crop_validation_and_visualization_slice_remapping(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/"study.toml"; text=(ROOT/"study.toml").read_text()
            path.write_text(text.replace("crop_x_voxels = [108, 108]","crop_x_voxels = [-1, 0]"))
            with self.assertRaisesRegex(g.Error,"two nonnegative integers"): g.load_config(path)
        vis=g.render_vis_test(self.config,"generated/smoke/tasks/example.txt",10)
        self.assertIn("ShowSpecificSlicesZ = 1 44",vis)

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
        field = g.render_field(self.config, g.cases(self.config, "smoke",self.envelope)[0], "smoke")
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
        field = g.render_field(self.config, g.cases(self.config, "smoke",self.envelope)[0], "smoke")
        self.assertNotIn('Gr/ViewA', field)
        self.assertNotIn('BeamPosition20', field)
        vis_test = g.render_vis_test(self.config, "generated/smoke/tasks/example.txt")
        self.assertIn('includeFile = generated/smoke/tasks/example.txt', vis_test)
        self.assertIn('dv:Ge/Patient/CloneRTDoseGridSize = 3 10 10 10 mm', vis_test)
        for source_marker_parameter in (
            's:Ge/BeamPosition20/Parent = "BeamPosition2"',
            's:Ge/BeamPosition20/Type = "TsBox"',
            's:Ge/BeamPosition20/Material = "Air"',
            'd:Ge/BeamPosition20/HLX = 10 mm',
            'd:Ge/BeamPosition20/HLY = 10 mm',
            'd:Ge/BeamPosition20/HLZ = 20 mm',
            'd:Ge/BeamPosition20/TransX = 0 mm',
            'd:Ge/BeamPosition20/TransY = 0 mm',
            'd:Ge/BeamPosition20/TransZ = 0 mm',
            'd:Ge/BeamPosition20/RotX = 0 deg',
            'd:Ge/BeamPosition20/RotY = 0 deg',
            'd:Ge/BeamPosition20/RotZ = 0 deg',
            's:Ge/BeamPosition20/Color = "white"',
        ):
            self.assertIn(source_marker_parameter, vis_test)
        self.assertIn('b:Sc/PatientDose/Active = "False"', vis_test)
        self.assertIn('i:Ts/NumberOfThreads = 1', vis_test)
        self.assertIn('iv:Ge/Patient/ShowSpecificSlicesZ = 1 54', vis_test)
        task, _, _ = g.render_task(
            self.config,
            g.cases(self.config, "smoke",self.envelope)[0],
            "smoke",
            1,
            1,
            [1] * g.integer(g.table(self.config, "beam"), "spot_count"),
        )
        self.assertNotIn('BeamPosition20', task)


if __name__ == "__main__": unittest.main()
