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

## Convert matRad CT and structures

Convert a MATLAB v7.3 matRad file containing `ct` and `cst` into a DICOM CT
series and `RTSTRUCT.dcm`:

```sh
python matRad/convert_matrad_to_dicom.py matRad/9306087_fine.mat
```

By default this writes `matRad/9306087_fine_dicom`, preserves patient, study,
and frame-of-reference metadata, and assigns new Series and SOP Instance UIDs
to the derived DICOM objects. Choose another destination or anonymize the
patient fields with:

```sh
python matRad/convert_matrad_to_dicom.py matRad/9306087_fine.mat \
  --output-dir dicom_converted --anonymize
```

Crop by giving the numbers of voxels to remove from the low and high sides of
each axis. For example:

```sh
python matRad/convert_matrad_to_dicom.py matRad/9306087_fine.mat \
  --crop-x 10 10 --crop-y 20 20 --crop-z 5 5
```

All structures are included unless `--structures` names a subset. Quote names
that contain spaces:

```sh
python matRad/convert_matrad_to_dicom.py matRad/9306087_fine.mat \
  --structures "PTV2017fw" "Brainstem DS"
```

An existing output directory is protected; pass `--overwrite` to replace it.
Structures that become empty after cropping are omitted with a warning.

Plot a one-based CT Z slice with every RTSTRUCT contour present on that slice:

```sh
python matRad/plot_dicom_rtstruct_slice.py \
  matRad/9306087_fine_dicom --z-slice 54
```

The plot uses one-based CT voxel indices on its X and Y axes and is written as
`matRad/9306087_fine_dicom_z_slice_54.png` by default. Restrict the overlay to
named structures, choose an output path, or override the DICOM display window
with:

```sh
python matRad/plot_dicom_rtstruct_slice.py \
  matRad/9306087_fine_dicom --z-slice 54 \
  --structures "PTV2017fw" "Brainstem DS" \
  --window 40 400 --output slice_54.png
```

The viewer writes a PNG using Matplotlib's noninteractive backend, so it also
works in a headless environment.

To build a private OpenTOPAS installation on BioHPC, where Conda is already
initialized, run:

```sh
Slurm/install_opentopas.sh
source "$HOME/Applications/TOPAS/opentopas-env.sh"
```

The installer enables Qt/OpenGL by default. Use `--headless` on systems where
only batch simulation is needed, or `--prefix DIR` to select another location.

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
also configurable. `aperture.downstream_surface_distance_mm` contains one
positive distance for each `sweep.angles_deg` entry in the same order, allowing
the aperture-to-isocenter distance to vary by beam angle.

Interactive OpenGL visualization is controlled by `[visualization]`. Each
profile always gets a separate `visTest.txt` that uses the first generated task,
disables dose scoring, replaces the scoring grid with a coarse 10 mm visualization-only
grid, adds a white box marking the otherwise invisible proton source group, and runs
one spot with one history. The box is only a visual marker, not the physical particle
source. Production fields and tasks retain the
configured scoring grid and contain no graphics settings. The configured OpenGL voxel threshold is
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

TOPAS can restrict the patient voxels without creating another DICOM series.
Crop margins are counts removed from the low and high sides of each original
DICOM axis:

```toml
[patient]
crop_x_voxels = [108, 108]
crop_y_voxels = [108, 108]
crop_z_voxels = [0, 0]
```

The generator writes one-based inclusive `RestrictVoxels*Min/Max` parameters,
compensates the patient translation so the isocenter remains fixed, and rejects
a crop that removes any voxel of the configured ROI. Visualization Z slices
remain numbered in the original DICOM series and are remapped automatically.
Cropping changes the simulated patient geometry: use enough margin to retain
material relevant to the beam path and scattered particles.

The brass aperture stays 45 mm in radius and 60 mm thick; the configured slits
are 36 mm high. The generator transports each spot's L9-L14 Gaussian phase
space from the source to both aperture faces and requires the slit lattice to
span the combined one-sigma X/Y envelope. For each width/CTC pair, it selects
the largest even count up to `max_slits` that fits inside the circular aperture
at every shift. The current 0.4 mm-width designs use 18, 14, and 10 slits for
3, 5, and 7 mm CTC respectively. At zero shift an even lattice leaves brass at
X=0; a 50% CTC shift moves a slit onto X=0. Coverage bounds and limiting-corner
margins are recorded in `summary.json`.

## Run TOPAS and Slurm

Run a task from the repository root:

```sh
topas generated/smoke/tasks/smoke_sw040_ctc300_shift000_angle045_chunk_001_of_001.txt
```

Submit a profile on ROHPC by naming its OpenTOPAS environment explicitly:

```sh
Slurm/submit_topas_array.sh \
  --topas-env "/data/maia/$USER/Applications/TOPAS/opentopas-env.sh" \
  --manifest generated/smoke/inputs.txt \
  --throttle 5
```

On BioHPC, supply the installation on project storage instead:

```sh
Slurm/submit_topas_array.sh \
  --topas-env "/project/radiology/HGao_lab/$USER/Applications/TOPAS/opentopas-env.sh" \
  --manifest generated/production/inputs.txt \
  --throttle 5
```

Alternatively, export `TOPAS_ENV` once and omit `--topas-env` from later
submissions. The CLI option takes precedence when both are present. Optional
`--account`, `--qos`, `--partition`, `--time`, and `--mem` arguments are passed
to `sbatch`; inspect the complete command first with `--dry-run`.

The default full-CT grid is 0.4 x 0.4 x 3 mm and can produce very large dose
files; verify disk, memory, and scheduler limits with smoke runs before
submitting production.

Combine each field's available binary chunks independently:

```sh
python Slurm/combine_dose_chunks.py --profile production
```

Complete fields produce `Dose_combined.bin`; fields with missing or invalid
chunks produce a clearly labeled `Dose_partial_K_of_N.bin`. Both receive a
companion `.binheader` in the same field directory. Inspect everything without
writing output, or replace an existing combined result, with:

```sh
python Slurm/combine_dose_chunks.py --profile production --validate-only
python Slurm/combine_dose_chunks.py --profile production --overwrite
```

Combination is an element-wise sum with no history normalization. The generated
profile manifest determines the expected fields and chunks. Invalid fields are
reported without preventing other fields from being processed.

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
