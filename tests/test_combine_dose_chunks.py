import csv
import tempfile
import unittest
from pathlib import Path

import numpy as np

from Slurm import combine_dose_chunks as combine


HEADER = """# TOPAS binary dose output
# X = 2 bins
# Y = 2 bins
# Z = 2 bins
# Scored in component: Patient/RTDoseGrid
# Quantity: DoseToMedium
# Unit: Gy
"""


class CombineDoseChunksTests(unittest.TestCase):
    def make_project(self, chunks=2, case_id="smoke_field", profile="smoke"):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        manifest = root / "generated" / profile / "manifest.csv"
        manifest.parent.mkdir(parents=True)
        rows = []
        field_dir = root / "output" / profile / case_id
        field_dir.mkdir(parents=True)
        for chunk in range(1, chunks + 1):
            stem = field_dir / f"Dose_chunk_{chunk:03d}_of_{chunks:03d}"
            rows.append({
                "case_id": case_id,
                "profile": profile,
                "chunk": chunk,
                "chunks": chunks,
                "output_path": str(stem.relative_to(root)),
            })
        self.write_manifest(manifest, rows)
        return temporary, root, field_dir, rows

    @staticmethod
    def write_manifest(path, rows):
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=("case_id", "profile", "chunk", "chunks", "output_path"),
            )
            writer.writeheader()
            writer.writerows(rows)

    @staticmethod
    def write_chunk(field_dir, chunk, chunks, values, header=HEADER):
        path = field_dir / f"Dose_chunk_{chunk:03d}_of_{chunks:03d}.bin"
        np.asarray(values, dtype="<f8").tofile(path)
        path.with_suffix(".binheader").write_text(header)
        return path

    def test_complete_blockwise_sum_and_header(self):
        temporary, root, field_dir, _ = self.make_project()
        with temporary:
            first = np.arange(8, dtype=float)
            second = np.arange(8, dtype=float) * 10
            self.write_chunk(field_dir, 1, 2, first)
            self.write_chunk(field_dir, 2, 2, second)

            result = combine.main([
                "--profile", "smoke", "--project-root", str(root),
                "--block-values", "3",
            ])

            self.assertEqual(result, 0)
            output = field_dir / "Dose_combined.bin"
            np.testing.assert_array_equal(np.fromfile(output, dtype="<f8"), first + second)
            metadata = output.with_suffix(".binheader").read_text()
            self.assertIn("Accepted chunks: 2 of 2", metadata)
            self.assertIn("X = 2 bins", metadata)
            reshaped = np.fromfile(output, dtype="<f8").reshape(2, 2, 2)
            np.testing.assert_array_equal(reshaped[1, 0], (first + second)[4:6])

    def test_missing_chunk_writes_clearly_named_partial(self):
        temporary, root, field_dir, _ = self.make_project(chunks=3)
        with temporary:
            values = np.arange(8, dtype=float)
            self.write_chunk(field_dir, 1, 3, values)
            self.write_chunk(field_dir, 3, 3, values + 1)

            result = combine.main([
                "--profile", "smoke", "--project-root", str(root),
            ])

            self.assertEqual(result, 0)
            output = field_dir / "Dose_partial_002_of_003.bin"
            np.testing.assert_array_equal(
                np.fromfile(output, dtype="<f8"), values + values + 1,
            )
            metadata = output.with_suffix(".binheader").read_text()
            self.assertIn("WARNING: PARTIAL DOSE", metadata)
            self.assertIn("chunk 002", metadata)

    def test_invalid_chunks_are_omitted_but_valid_chunk_is_combined(self):
        temporary, root, field_dir, _ = self.make_project(chunks=5)
        with temporary:
            values = np.arange(8, dtype=float)
            self.write_chunk(field_dir, 1, 5, values)
            self.write_chunk(field_dir, 2, 5, values[:-1])
            nonfinite = values.copy(); nonfinite[3] = np.nan
            self.write_chunk(field_dir, 3, 5, nonfinite)
            mismatch = HEADER.replace("Patient/RTDoseGrid", "OtherGrid")
            self.write_chunk(field_dir, 4, 5, values, mismatch)
            empty = field_dir / "Dose_chunk_005_of_005.bin"
            empty.touch(); empty.with_suffix(".binheader").touch()

            records = combine.read_manifest(root, "smoke")["smoke_field"]
            inspected = combine.inspect_field("smoke_field", records, None, 2)

            self.assertEqual([item.chunk for item in inspected.valid], [1])
            reasons = "\n".join(inspected.rejected)
            self.assertIn("binary size", reasons)
            self.assertIn("non-finite", reasons)
            self.assertIn("metadata does not match", reasons)
            self.assertIn("empty binary", reasons)

    def test_shape_override_accepts_header_without_dimensions(self):
        temporary, root, field_dir, _ = self.make_project(chunks=1)
        with temporary:
            values = np.arange(8, dtype=float)
            self.write_chunk(field_dir, 1, 1, values, "# Quantity: DoseToMedium\n")
            result = combine.main([
                "--profile", "smoke", "--project-root", str(root),
                "--shape", "2", "2", "2",
            ])
            self.assertEqual(result, 0)
            np.testing.assert_array_equal(
                np.fromfile(field_dir / "Dose_combined.bin", dtype="<f8"), values,
            )

    def test_validate_only_writes_nothing(self):
        temporary, root, field_dir, _ = self.make_project(chunks=1)
        with temporary:
            self.write_chunk(field_dir, 1, 1, np.arange(8))
            result = combine.main([
                "--profile", "smoke", "--project-root", str(root),
                "--validate-only",
            ])
            self.assertEqual(result, 0)
            self.assertFalse((field_dir / "Dose_combined.bin").exists())

    def test_production_manifest_discovery_and_other_fields_continue(self):
        temporary, root, good_dir, rows = self.make_project(
            chunks=1, case_id="production_good", profile="production",
        )
        with temporary:
            self.write_chunk(good_dir, 1, 1, np.arange(8))
            bad_stem = root / "output/production/production_bad/Dose_chunk_001_of_001"
            bad_stem.parent.mkdir(parents=True)
            rows.append({
                "case_id": "production_bad",
                "profile": "production",
                "chunk": 1,
                "chunks": 1,
                "output_path": str(bad_stem.relative_to(root)),
            })
            self.write_manifest(root / "generated/production/manifest.csv", rows)

            result = combine.main([
                "--profile", "production", "--project-root", str(root),
            ])

            self.assertEqual(result, 2)
            self.assertTrue((good_dir / "Dose_combined.bin").is_file())

    def test_no_valid_chunk_returns_nonzero(self):
        temporary, root, _, _ = self.make_project(chunks=1)
        with temporary:
            result = combine.main([
                "--profile", "smoke", "--project-root", str(root),
            ])
            self.assertEqual(result, 2)

    def test_existing_output_requires_overwrite(self):
        temporary, root, field_dir, _ = self.make_project(chunks=1)
        with temporary:
            values = np.arange(8, dtype=float)
            self.write_chunk(field_dir, 1, 1, values)
            output = field_dir / "Dose_combined.bin"
            output.write_bytes(b"old")
            result = combine.main([
                "--profile", "smoke", "--project-root", str(root),
            ])
            self.assertEqual(result, 0)
            self.assertEqual(output.read_bytes(), b"old")
            combine.main([
                "--profile", "smoke", "--project-root", str(root), "--overwrite",
            ])
            np.testing.assert_array_equal(np.fromfile(output, dtype="<f8"), values)

    def test_sum_failure_cleans_temporary_files(self):
        temporary, root, field_dir, _ = self.make_project(chunks=1)
        with temporary:
            self.write_chunk(field_dir, 1, 1, np.full(8, 1e308))
            records = combine.read_manifest(root, "smoke")["smoke_field"]
            field = combine.inspect_field("smoke_field", records, None, 8)
            # Duplicate the valid source so finite inputs overflow during summation.
            overflowing = combine.FieldInputs(
                field.case_id, 2, field.valid + field.valid, (), field.shape,
            )
            with self.assertRaisesRegex(combine.CombineError, "became non-finite"):
                combine.combine_field(overflowing, 8, False)
            self.assertFalse((field_dir / ".Dose_combined.bin.tmp").exists())
            self.assertFalse((field_dir / ".Dose_combined.binheader.tmp").exists())

    def test_manifest_duplicate_and_malformed_chunk_sets_fail(self):
        temporary, root, _, rows = self.make_project(chunks=2)
        with temporary:
            manifest = root / "generated/smoke/manifest.csv"
            self.write_manifest(manifest, rows + [rows[0]])
            with self.assertRaisesRegex(combine.CombineError, "more than once"):
                combine.read_manifest(root, "smoke")

            self.write_manifest(manifest, rows[:1])
            with self.assertRaisesRegex(combine.CombineError, "missing .* chunk"):
                combine.read_manifest(root, "smoke")

    def test_empty_header_is_rejected(self):
        temporary, root, field_dir, _ = self.make_project(chunks=1)
        with temporary:
            path = self.write_chunk(field_dir, 1, 1, np.arange(8))
            path.with_suffix(".binheader").write_text("")
            records = combine.read_manifest(root, "smoke")["smoke_field"]
            inspected = combine.inspect_field("smoke_field", records, None, 8)
            self.assertFalse(inspected.valid)
            self.assertIn("empty header", "\n".join(inspected.rejected))


if __name__ == "__main__":
    unittest.main()
