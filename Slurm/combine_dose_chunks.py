#!/usr/bin/env python3
"""Combine TOPAS binary dose chunks independently for every field in a profile."""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import numpy as np


FLOAT_BYTES = 8


class CombineError(RuntimeError):
    """A concise manifest, input, or output error."""


class ExistingOutputError(CombineError):
    """A combined result exists and overwrite was not authorized."""


@dataclass(frozen=True)
class ChunkRecord:
    case_id: str
    chunk: int
    chunks: int
    data_path: Path


@dataclass(frozen=True)
class ValidChunk:
    chunk: int
    data_path: Path
    header_path: Path
    header: tuple[str, ...]


@dataclass(frozen=True)
class FieldInputs:
    case_id: str
    expected: int
    valid: tuple[ValidChunk, ...]
    rejected: tuple[str, ...]
    shape: tuple[int, int, int] | None


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    project_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile", choices=("smoke", "production"), required=True,
        help="generated/output profile to combine",
    )
    parser.add_argument(
        "--project-root", type=Path, default=project_root,
        help=f"project root (default: {project_root})",
    )
    parser.add_argument(
        "--block-values", type=int, default=1_000_000,
        help="float64 values processed per block (default: 1000000)",
    )
    parser.add_argument(
        "--shape", type=int, nargs=3, metavar=("NX", "NY", "NZ"),
        help="override grid dimensions when binheaders do not define them",
    )
    parser.add_argument(
        "--validate-only", action="store_true",
        help="validate manifests, headers, sizes, and values without writing output",
    )
    parser.add_argument(
        "--overwrite", action="store_true",
        help="replace an existing combined or partial output",
    )
    return parser.parse_args(argv)


def contained_path(root: Path, value: str, label: str) -> Path:
    path = (root / value).resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise CombineError(f"{label} escapes the project root: {value}") from error
    return path


def read_manifest(root: Path, profile: str) -> dict[str, tuple[ChunkRecord, ...]]:
    root = root.resolve()
    manifest = root / "generated" / profile / "manifest.csv"
    if not manifest.is_file():
        raise CombineError(f"Generated profile manifest does not exist: {manifest}")
    if manifest.stat().st_size == 0:
        raise CombineError(f"Generated profile manifest is empty: {manifest}")

    required = {"case_id", "profile", "chunk", "chunks", "output_path"}
    grouped: dict[str, list[ChunkRecord]] = {}
    seen: set[tuple[str, int]] = set()
    try:
        with manifest.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            missing_columns = required - set(reader.fieldnames or ())
            if missing_columns:
                raise CombineError(
                    f"Manifest lacks required columns: {', '.join(sorted(missing_columns))}"
                )
            for line_number, row in enumerate(reader, 2):
                case_id = (row.get("case_id") or "").strip()
                row_profile = (row.get("profile") or "").strip()
                if not case_id:
                    raise CombineError(f"Manifest line {line_number} has an empty case_id")
                if row_profile != profile:
                    raise CombineError(
                        f"Manifest line {line_number} has profile {row_profile!r}, "
                        f"expected {profile!r}"
                    )
                try:
                    chunk = int(row["chunk"])
                    chunks = int(row["chunks"])
                except (TypeError, ValueError) as error:
                    raise CombineError(
                        f"Manifest line {line_number} has invalid chunk numbering"
                    ) from error
                if chunks <= 0 or not 1 <= chunk <= chunks:
                    raise CombineError(
                        f"Manifest line {line_number} has chunk {chunk} of {chunks}"
                    )
                key = (case_id, chunk)
                if key in seen:
                    raise CombineError(
                        f"Manifest lists {case_id} chunk {chunk:03d} more than once"
                    )
                seen.add(key)
                output_value = (row.get("output_path") or "").strip()
                if not output_value:
                    raise CombineError(
                        f"Manifest line {line_number} has an empty output_path"
                    )
                output_stem = contained_path(root, output_value, "Manifest output_path")
                grouped.setdefault(case_id, []).append(
                    ChunkRecord(case_id, chunk, chunks, output_stem.with_suffix(".bin"))
                )
    except OSError as error:
        raise CombineError(f"Could not read manifest {manifest}: {error}") from error

    if not grouped:
        raise CombineError(f"Generated profile manifest contains no fields: {manifest}")

    result: dict[str, tuple[ChunkRecord, ...]] = {}
    expected_parent = (root / "output" / profile).resolve()
    for case_id, records in grouped.items():
        totals = {record.chunks for record in records}
        if len(totals) != 1:
            raise CombineError(f"Manifest has conflicting chunk totals for {case_id}")
        expected = next(iter(totals))
        actual_ids = {record.chunk for record in records}
        wanted_ids = set(range(1, expected + 1))
        if actual_ids != wanted_ids:
            missing = ", ".join(f"{item:03d}" for item in sorted(wanted_ids - actual_ids))
            raise CombineError(f"Manifest is missing {case_id} chunk(s): {missing}")
        for record in records:
            expected_dir = expected_parent / case_id
            if record.data_path.parent != expected_dir:
                raise CombineError(
                    f"Manifest output for {case_id} is outside {expected_dir}: "
                    f"{record.data_path}"
                )
        result[case_id] = tuple(sorted(records, key=lambda item: item.chunk))
    return result


def read_header(path: Path) -> tuple[str, ...]:
    if not path.is_file():
        raise CombineError(f"missing header {path}")
    if path.stat().st_size == 0:
        raise CombineError(f"empty header {path}")
    try:
        return tuple(path.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError as error:
        raise CombineError(f"could not read header {path}: {error}") from error


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
        raise CombineError(f"header contains conflicting {axis}-bin counts: {sorted(found)}")
    return next(iter(found), None)


def parse_shape(lines: Sequence[str]) -> tuple[int, int, int] | None:
    parsed = tuple(axis_bin_count(lines, axis) for axis in ("X", "Y", "Z"))
    if any(value is None for value in parsed):
        return None
    return tuple(int(value) for value in parsed)  # type: ignore[arg-type, return-value]


def stable_header_signature(lines: Sequence[str]) -> tuple[str, ...]:
    signature: list[str] = []
    for line in lines:
        normalized = " ".join(line.lstrip().lstrip("#").strip().lower().split())
        if not normalized or "output" in normalized or "seriesdescription" in normalized:
            continue
        axis_definition = bool(
            re.search(r"\b[xyz]\b.*\b(?:bins?|voxels?)\b", normalized)
            or re.search(r"\b(?:bins?|voxels?)\b.*\b[xyz]\b", normalized)
        )
        stable_descriptor = any(
            marker in normalized
            for marker in ("scored in component", "dosetomedium", "voxel", "unit:")
        )
        if axis_definition or stable_descriptor:
            signature.append(normalized)
    return tuple(signature)


def validate_values(path: Path, value_count: int, block_values: int) -> None:
    with path.open("rb") as handle:
        remaining = value_count
        offset = 0
        while remaining:
            count = min(remaining, block_values)
            values = np.fromfile(handle, dtype="<f8", count=count)
            if values.size != count:
                raise CombineError(f"unexpected end of binary data at value {offset:,}")
            invalid = np.flatnonzero(~np.isfinite(values))
            if invalid.size:
                raise CombineError(
                    f"non-finite dose at flat value {offset + int(invalid[0]):,}"
                )
            remaining -= count
            offset += count
        if handle.read(1):
            raise CombineError("binary contains extra data")


def inspect_field(
    case_id: str,
    records: Sequence[ChunkRecord],
    shape_override: Sequence[int] | None,
    block_values: int,
) -> FieldInputs:
    expected = records[0].chunks
    rejected: list[str] = []
    preliminary: list[tuple[ChunkRecord, tuple[str, ...], tuple[int, int, int] | None]] = []

    for record in records:
        path = record.data_path
        header_path = path.with_suffix(".binheader")
        try:
            if not path.is_file():
                raise CombineError(f"missing binary {path}")
            if path.stat().st_size == 0:
                raise CombineError(f"empty binary {path}")
            header = read_header(header_path)
            source_shape = parse_shape(header)
            preliminary.append((record, header, source_shape))
        except (OSError, CombineError) as error:
            rejected.append(f"chunk {record.chunk:03d}: {error}")

    if shape_override is not None:
        shape = tuple(shape_override)
        if len(shape) != 3 or any(value <= 0 for value in shape):
            raise CombineError(f"Grid dimensions must be three positive integers, got {shape}")
    else:
        shape = next(
            (source_shape for _, _, source_shape in preliminary if source_shape is not None),
            None,
        )
    if shape is None:
        rejected.append("no usable header defines X/Y/Z grid dimensions; pass --shape")
        return FieldInputs(case_id, expected, (), tuple(rejected), None)

    value_count = shape[0] * shape[1] * shape[2]
    expected_bytes = value_count * FLOAT_BYTES
    reference_signature: tuple[str, ...] | None = None
    valid: list[ValidChunk] = []
    for record, header, source_shape in preliminary:
        try:
            if shape_override is None and source_shape is None:
                raise CombineError("header does not define X/Y/Z grid dimensions")
            if source_shape is not None and source_shape != shape:
                raise CombineError(f"grid shape {source_shape} does not match {shape}")
            signature = stable_header_signature(header)
            if reference_signature and signature and signature != reference_signature:
                raise CombineError("grid/scorer metadata does not match other chunks")
            actual_bytes = record.data_path.stat().st_size
            if actual_bytes != expected_bytes:
                raise CombineError(
                    f"binary size is {actual_bytes:,} bytes; expected {expected_bytes:,}"
                )
            validate_values(record.data_path, value_count, block_values)
            if reference_signature is None:
                reference_signature = signature
            valid.append(
                ValidChunk(
                    record.chunk,
                    record.data_path,
                    record.data_path.with_suffix(".binheader"),
                    header,
                )
            )
        except (OSError, CombineError) as error:
            rejected.append(f"chunk {record.chunk:03d}: {error}")

    return FieldInputs(
        case_id, expected, tuple(valid), tuple(rejected), shape,
    )


def output_paths(field: FieldInputs) -> tuple[Path, Path]:
    directory = field.valid[0].data_path.parent
    if len(field.valid) == field.expected:
        stem = "Dose_combined"
    else:
        stem = f"Dose_partial_{len(field.valid):03d}_of_{field.expected:03d}"
    binary = directory / f"{stem}.bin"
    return binary, binary.with_suffix(".binheader")


def write_header(path: Path, field: FieldInputs) -> None:
    assert field.shape is not None and field.valid
    complete = len(field.valid) == field.expected
    with path.open("w", encoding="utf-8", newline="") as handle:
        label = "Combined" if complete else "PARTIAL"
        handle.write(f"# {label} TOPAS DoseToMedium for field {field.case_id}\n")
        handle.write("# Combination: element-wise sum; no history normalization applied\n")
        handle.write(f"# Accepted chunks: {len(field.valid)} of {field.expected}\n")
        if not complete:
            handle.write("# WARNING: PARTIAL DOSE; missing or invalid chunks were omitted\n")
        handle.write("# Binary output type: IEEE-754 float64 in little-endian order\n")
        handle.write("# Flat ordering: X bin fastest, then Y bin, then Z bin\n")
        handle.write(f"# X = {field.shape[0]} bins\n")
        handle.write(f"# Y = {field.shape[1]} bins\n")
        handle.write(f"# Z = {field.shape[2]} bins\n")
        handle.write("# Accepted source files:\n")
        for source in field.valid:
            handle.write(f"#   chunk {source.chunk:03d}: {source.data_path}\n")
        if field.rejected:
            handle.write("# Missing or rejected chunks:\n")
            for reason in field.rejected:
                handle.write(f"#   {reason}\n")
        handle.write("# BEGIN COPIED TOPAS SOURCE METADATA\n")
        for line in field.valid[0].header:
            handle.write(f"# {line.lstrip('#').lstrip()}\n")
        handle.write("# END COPIED TOPAS SOURCE METADATA\n")


def combine_field(field: FieldInputs, block_values: int, overwrite: bool) -> str:
    assert field.shape is not None and field.valid
    binary, header = output_paths(field)
    if not overwrite and (binary.exists() or header.exists()):
        raise ExistingOutputError(
            f"output already exists for {field.case_id}: {binary} (use --overwrite)"
        )
    binary.parent.mkdir(parents=True, exist_ok=True)
    temporary_binary = binary.with_name(f".{binary.name}.tmp")
    temporary_header = header.with_name(f".{header.name}.tmp")
    for temporary in (temporary_binary, temporary_header):
        if temporary.exists():
            temporary.unlink()

    value_count = field.shape[0] * field.shape[1] * field.shape[2]
    readers = []
    try:
        for source in field.valid:
            readers.append(source.data_path.open("rb"))
        with temporary_binary.open("wb") as output:
            for start in range(0, value_count, block_values):
                count = min(block_values, value_count - start)
                summed = np.zeros(count, dtype=np.float64)
                for source, reader in zip(field.valid, readers):
                    values = np.fromfile(reader, dtype="<f8", count=count)
                    if values.size != count:
                        raise CombineError(
                            f"{source.data_path} changed during combination"
                        )
                    with np.errstate(over="ignore", invalid="ignore"):
                        np.add(summed, values, out=summed)
                if not np.all(np.isfinite(summed)):
                    raise CombineError(
                        f"summed dose became non-finite near flat value {start:,}"
                    )
                summed.astype("<f8", copy=False).tofile(output)
        write_header(temporary_header, field)
        os.replace(temporary_header, header)
        os.replace(temporary_binary, binary)
    except OSError as error:
        raise CombineError(f"I/O failure while combining {field.case_id}: {error}") from error
    finally:
        for reader in readers:
            reader.close()
        for temporary in (temporary_binary, temporary_header):
            if temporary.exists():
                temporary.unlink()
    return "complete" if len(field.valid) == field.expected else "partial"


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.block_values <= 0:
        raise CombineError("--block-values must be a positive integer")
    root = args.project_root.resolve()
    if not root.is_dir():
        raise CombineError(f"Project root does not exist: {root}")
    fields = read_manifest(root, args.profile)

    complete = partial = failed = existing = 0
    for case_id, records in sorted(fields.items()):
        field = inspect_field(case_id, records, args.shape, args.block_values)
        if not field.valid:
            failed += 1
            print(f"FAILED {case_id}: no valid dose chunks", file=sys.stderr)
            for reason in field.rejected:
                print(f"  - {reason}", file=sys.stderr)
            continue

        kind = "complete" if len(field.valid) == field.expected else "partial"
        print(
            f"Validated {case_id}: {len(field.valid)}/{field.expected} chunks, "
            f"shape={field.shape} ({kind})"
        )
        for reason in field.rejected:
            print(f"  - omitted: {reason}")
        if args.validate_only:
            complete += kind == "complete"
            partial += kind == "partial"
            continue
        try:
            result = combine_field(field, args.block_values, args.overwrite)
        except ExistingOutputError as error:
            existing += 1
            print(f"SKIPPED {error}")
            continue
        except CombineError as error:
            failed += 1
            print(f"FAILED {case_id}: {error}", file=sys.stderr)
            continue
        binary, header = output_paths(field)
        print(f"Wrote {result} dose: {binary}")
        print(f"Wrote metadata: {header}")
        complete += result == "complete"
        partial += result == "partial"

    action = "Validated" if args.validate_only else "Combined"
    print(
        f"{action} profile {args.profile}: complete={complete}, partial={partial}, "
        f"failed={failed}, existing={existing}"
    )
    return 2 if failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CombineError as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(2)
