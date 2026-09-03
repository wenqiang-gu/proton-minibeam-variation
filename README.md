# Proton minibeam variation generator

This repository generates independent TOPAS fields from `study.toml`. The
default full factorial contains 240 fields: slit widths 0.4/0.6/0.8 mm, CTCs
3/4/5/6/7 mm, gantry angles 45/135/225/315 degrees, and aperture shifts
0/25/50/75% of CTC in gantry-local +X. Angles remain separate; doses are not
summed. Every angle reuses the imported 2,151-spot beam-1 model.

## Setup

Use Python 3.8 or newer. Python versions before 3.11 use the `tomli`
compatibility package because `tomllib` is not yet part of the standard library:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
```

The local `dicom_9306087_fine` directory must contain the CT series and
`RTSTRUCT.dcm`. Patient data are ignored by Git and never modified.

## Generate and validate

```sh
python generate_variations.py --config study.toml --profile smoke
python generate_variations.py --config study.toml --profile smoke --check

python generate_variations.py --config study.toml --profile production
python generate_variations.py --config study.toml --profile production --check
```

Smoke uses 100 histories per spot. Production scales the original L4 weights
and splits each spot exactly across the configured chunk count. Existing output
must match exactly; use `--force` to replace only a stale generated profile or
`--clean` to remove only that profile. DICOM and dose outputs are untouched.
Generation also creates every case's empty directory below `output/`, allowing
the task files to run directly in TOPAS. Existing dose files are never removed.

Shorten any sweep list in `study.toml` to generate a subset. The scorer voxel
size, history scale, chunks, threads, patient-frame rotation, and aperture are
also configurable.

Interactive OpenGL visualization is controlled by `[visualization]`. Each
profile always gets a separate `visTest.txt` that uses the first generated task,
disables dose scoring, and runs one spot with one history. Production fields and
tasks contain no graphics settings. The configured OpenGL voxel threshold is
deliberately high so the interactive view remains in stored mode instead of
switching to the slower immediate mode. `slices_z` selects one or more one-based
DICOM patient slices to display in the visualization input. The outline
threshold must exceed the CT voxel count for those selected slices to be drawn.

## Generated layout

Each profile has `common/`, 60 reusable `apertures/`, 240 angle-specific
`fields/`, runnable `tasks/`, a standalone `visTest.txt`, `inputs.txt`, CSV/JSON manifests, and a geometry
`summary.json`. A name such as `smoke_sw040_ctc300_shift025_angle135` means
0.40 mm width, 3.00 mm CTC, 25% shift, and 135 degrees.

The generator validates CT dimensions, spacing, orientation, slice positions,
frame references, and all beam vectors. It rasterizes the exact `PTV2017fw`
contours on the CT grid and uses the included-voxel centroid as isocenter. The
supplied dataset resolves to approximately
`(-5.61784, 93.21779, 169.46812) mm` in DICOM patient coordinates.

The brass aperture stays 45 mm in radius and 60 mm thick; slits are 20 mm high.
For each width/CTC pair, the largest count up to 20 that fits at every shift is
used, keeping phase comparisons at a common slit count.

## Run TOPAS and Slurm

Run a task from the repository root:

```sh
topas generated/smoke/tasks/smoke_sw040_ctc300_shift000_angle045_chunk_001_of_001.txt
```

Submit a profile on the original cluster:

```sh
Slurm/submit_topas_array.sh --manifest generated/smoke/inputs.txt --dry-run
Slurm/submit_topas_array.sh --manifest generated/smoke/inputs.txt --throttle 5
```

The worker uses `/data/maia/$USER/Applications/TOPAS/opentopas-env.sh` unless
`TOPAS_ENV` is set. The default full-CT grid is 0.4 x 0.4 x 3 mm and can produce
very large dose files; verify disk, memory, and scheduler limits with smoke runs
before submitting production.

Plot the maximum dose projection along Z in the X-Y plane:

```sh
python Slurm/plot_dose_xy.py \
  output/smoke/smoke_sw040_ctc300_shift000_angle045/Dose_chunk_001_of_001.bin
```

To plot a specific one-based patient-grid Z slice instead:

```sh
python Slurm/plot_dose_xy.py \
  output/smoke/smoke_sw040_ctc300_shift000_angle045/Dose_chunk_001_of_001.bin \
  --z-slice 54
```

The script reads grid dimensions from the companion `.binheader`, uses voxel
indices on the axes, and normalizes the plotted map's maximum to 100% by
default. Pass `--normalization none` to retain raw DoseToMedium values in Gy, or
`--shape NX NY NZ` if dimensions cannot be parsed from the header.

For an interactive window with zoom, pan, and coordinate inspection, run:

```sh
python Slurm/plot_dose_xy.py DOSE.bin --interactive
```

Interactive-only mode does not write a PNG. Add `--output slice.png` to both
save and display the figure. Interactive mode requires a graphical Matplotlib
backend and normally will not work on a headless Slurm compute node; omit
`--interactive` there to use the PNG-producing `Agg` backend.

## Tests

```sh
python -m unittest discover -s tests -v
```

Tests cover real and rotated DICOM geometry, ROI errors, the case matrix,
aperture containment, history reconstruction, stable seeds, beam vectors,
deterministic generation, and binary dose-slice parsing. TOPAS transport must be
checked on a TOPAS host.
