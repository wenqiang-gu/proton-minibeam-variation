#!/usr/bin/env bash
# Rootless OpenTOPAS installation for BioHPC systems with Conda available.

set -Eeuo pipefail

script_name=${0##*/}
prefix=${OPENTOPAS_PREFIX:-"${HOME}/Applications"}
jobs=${OPENTOPAS_BUILD_JOBS:-8}
geant4_version=${OPENTOPAS_GEANT4_VERSION:-11.3.2}
opentopas_ref=${OPENTOPAS_REF:-main}
conda_executable=${OPENTOPAS_CONDA_EXE:-${CONDA_EXE:-}}
with_qt=1
clean=0
skip_smoke_test=0

usage() {
    cat <<'EOF'
Install OpenTOPAS and its dependencies below a user-owned directory on BioHPC.

Usage:
  Slurm/install_opentopas.sh [options]

Options:
  --prefix DIR             Installation root (default: $HOME/Applications)
  --jobs N                 Parallel build jobs (default: 8)
  --opentopas-ref REF      OpenTOPAS branch, tag, or commit (default: main)
  --geant4-version VER     Geant4 version (default: 11.3.2)
  --conda-executable PATH  Underlying Conda executable; normally auto-detected
  --headless               Build without Qt/OpenGL visualization
  --clean                  Recreate build and install directories
  --skip-smoke-test        Skip runtime-library and startup checks
  -h, --help               Show this help

The script uses the site's existing Conda installation to create a private
conda-forge build toolchain. It builds Geant4, bundled GDCM, and OpenTOPAS
without sudo and does not edit shell startup files.

After installation:
  source PREFIX/TOPAS/opentopas-env.sh
  topas main.txt

Environment overrides:
  OPENTOPAS_PREFIX, OPENTOPAS_BUILD_JOBS, OPENTOPAS_GEANT4_VERSION,
  OPENTOPAS_REF, and OPENTOPAS_CONDA_EXE.
EOF
}

die() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

note() {
    printf '\n[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

while (( $# )); do
    case $1 in
        --prefix)
            (( $# >= 2 )) || die "--prefix requires a directory"
            prefix=$2
            shift 2
            ;;
        --jobs)
            (( $# >= 2 )) || die "--jobs requires a positive integer"
            jobs=$2
            shift 2
            ;;
        --opentopas-ref)
            (( $# >= 2 )) || die "--opentopas-ref requires a value"
            opentopas_ref=$2
            shift 2
            ;;
        --geant4-version)
            (( $# >= 2 )) || die "--geant4-version requires a value"
            geant4_version=$2
            shift 2
            ;;
        --conda-executable)
            (( $# >= 2 )) || die "--conda-executable requires a path"
            conda_executable=$2
            shift 2
            ;;
        --headless)
            with_qt=0
            shift
            ;;
        --clean)
            clean=1
            shift
            ;;
        --skip-smoke-test)
            skip_smoke_test=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            die "unknown option: $1 (use --help)"
            ;;
    esac
done

[[ $(uname -s) == Linux ]] || die "this installer supports Linux only"
[[ $jobs =~ ^[1-9][0-9]*$ ]] || die "--jobs must be a positive integer"
[[ $geant4_version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || \
    die "--geant4-version must look like 11.3.2"
[[ -n $opentopas_ref ]] || die "--opentopas-ref cannot be empty"

resolve_conda_executable() {
    local conda_base candidate

    if [[ -n $conda_executable && -x $conda_executable ]]; then
        printf '%s\n' "$conda_executable"
        return
    fi

    # BioHPC initializes `conda` as a shell function. Ask it for the base
    # installation and then use the real executable in noninteractive calls.
    if command -v conda >/dev/null 2>&1; then
        conda_base=$(conda info --base 2>/dev/null || true)
        candidate=$conda_base/bin/conda
        if [[ -n $conda_base && -x $candidate ]]; then
            printf '%s\n' "$candidate"
            return
        fi
    fi

    for candidate in /apps/anaconda3/bin/conda /opt/conda/bin/conda; do
        if [[ -x $candidate ]]; then
            printf '%s\n' "$candidate"
            return
        fi
    done

    die "could not find the Conda executable; pass --conda-executable PATH"
}

conda_executable=$(resolve_conda_executable)
prefix=$(mkdir -p "$prefix" && cd "$prefix" && pwd -P)

toolchain=$prefix/opentopas-toolchain
geant4_root=$prefix/GEANT4
geant4_source=$geant4_root/geant4-v${geant4_version}
geant4_build=$geant4_root/geant4-build
geant4_install=$geant4_root/geant4-install
gdcm_root=$prefix/GDCM
gdcm_source=$gdcm_root/gdcm-2.6.8
gdcm_build=$gdcm_root/gdcm-build
gdcm_install=$gdcm_root/gdcm-install
topas_root=$prefix/TOPAS
topas_source=$topas_root/OpenTOPAS
topas_build=$topas_root/OpenTOPAS-build
topas_install=$topas_root/OpenTOPAS-install
environment_script=$topas_root/opentopas-env.sh
wrapper_dir=$prefix/bin
wrapper=$wrapper_dir/topas
manifest=$topas_root/install-manifest.txt
log_file=$topas_root/install-opentopas.log

mkdir -p "$topas_root"
exec > >(tee -a "$log_file") 2>&1

note "Rootless OpenTOPAS installation for BioHPC"
printf 'Prefix: %s\nJobs: %s\nGeant4: %s\nOpenTOPAS ref: %s\nConda: %s\nQt: %s\nLog: %s\n' \
    "$prefix" "$jobs" "$geant4_version" "$opentopas_ref" \
    "$conda_executable" \
    "$([[ $with_qt == 1 ]] && printf enabled || printf disabled)" "$log_file"

available_kb=$(df -Pk "$prefix" | awk 'NR==2 {print $4}')
if [[ $available_kb =~ ^[0-9]+$ ]] && (( available_kb < 10 * 1024 * 1024 )); then
    die "less than 10 GiB is available below $prefix"
fi

packages=(
    c-compiler
    cxx-compiler
    "cmake>=3.24,<4"
    ninja
    make
    git
    curl
    pkg-config
    expat
    xerces-c
    zlib
    bzip2
    libpng
    libjpeg-turbo
    freetype
)
if [[ $with_qt == 1 ]]; then
    packages+=(
        "qt6-main>=6.4,<7"
        harfbuzz
        libgl-devel
        libglu
        xorg-libx11
        xorg-libxext
        xorg-libxt
        xorg-libxmu
    )
fi

note "Creating/updating private Conda toolchain"
"$conda_executable" --version
if [[ -d $toolchain/conda-meta ]]; then
    "$conda_executable" install --yes --prefix "$toolchain" \
        --override-channels --channel conda-forge "${packages[@]}"
else
    "$conda_executable" create --yes --prefix "$toolchain" \
        --override-channels --channel conda-forge "${packages[@]}"
fi

run_in_toolchain() {
    "$conda_executable" run --prefix "$toolchain" "$@"
}

export CMAKE_PREFIX_PATH="$toolchain${CMAKE_PREFIX_PATH:+:$CMAKE_PREFIX_PATH}"
export PKG_CONFIG_PATH="$toolchain/lib/pkgconfig:$toolchain/share/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"

if [[ $clean == 1 ]]; then
    note "Removing prior build and install directories (--clean)"
    rm -rf -- "$geant4_build" "$geant4_install" \
        "$gdcm_build" "$gdcm_install" "$topas_build" "$topas_install"
fi

mkdir -p "$geant4_root"
if [[ ! -f $geant4_source/CMakeLists.txt ]]; then
    note "Downloading Geant4 $geant4_version"
    geant4_archive=$geant4_root/geant4-v${geant4_version}.tar.gz
    if [[ ! -f $geant4_archive ]]; then
        run_in_toolchain curl --fail --location --retry 4 --retry-delay 3 \
            --output "$geant4_archive.part" \
            "https://gitlab.cern.ch/geant4/geant4/-/archive/v${geant4_version}/geant4-v${geant4_version}.tar.gz"
        mv "$geant4_archive.part" "$geant4_archive"
    fi
    tar -xzf "$geant4_archive" -C "$geant4_root"
fi

geant4_options=(
    -DCMAKE_BUILD_TYPE=Release
    "-DCMAKE_INSTALL_PREFIX=$geant4_install"
    "-DCMAKE_PREFIX_PATH=$toolchain"
    -DGEANT4_INSTALL_DATA=ON
    -DGEANT4_BUILD_MULTITHREADED=ON
    -DGEANT4_BUILD_VERBOSE_CODE=OFF
    -DBUILD_SHARED_LIBS=ON
)
if [[ $with_qt == 1 ]]; then
    geant4_options+=(
        -DGEANT4_USE_QT=ON
        -DGEANT4_USE_QT_QT6=ON
        -DGEANT4_USE_OPENGL_X11=ON
    )
else
    geant4_options+=( -DGEANT4_USE_QT=OFF -DGEANT4_USE_OPENGL_X11=OFF )
fi

note "Configuring Geant4 $geant4_version"
run_in_toolchain cmake -S "$geant4_source" -B "$geant4_build" -G Ninja \
    "${geant4_options[@]}"
note "Building and installing Geant4"
run_in_toolchain cmake --build "$geant4_build" --parallel "$jobs"
run_in_toolchain cmake --install "$geant4_build"

note "Obtaining OpenTOPAS source"
mkdir -p "$topas_root"
if [[ ! -d $topas_source/.git ]]; then
    run_in_toolchain git clone https://github.com/OpenTOPAS/OpenTOPAS.git \
        "$topas_source"
fi
run_in_toolchain git -C "$topas_source" fetch --tags --prune
run_in_toolchain git -C "$topas_source" checkout "$opentopas_ref"
topas_commit=$(run_in_toolchain git -C "$topas_source" rev-parse HEAD)

mkdir -p "$gdcm_root"
if [[ ! -f $gdcm_source/CMakeLists.txt ]]; then
    gdcm_archive=$topas_source/gdcm-2.6.8.tar.gz
    [[ -f $gdcm_archive ]] || \
        die "OpenTOPAS source does not contain gdcm-2.6.8.tar.gz"
    note "Extracting bundled GDCM 2.6.8"
    tar -xzf "$gdcm_archive" -C "$gdcm_root"
fi

note "Configuring and installing GDCM 2.6.8"
run_in_toolchain cmake -S "$gdcm_source" -B "$gdcm_build" -G Ninja \
    -DCMAKE_BUILD_TYPE=Release \
    "-DCMAKE_INSTALL_PREFIX=$gdcm_install" \
    "-DCMAKE_PREFIX_PATH=$toolchain" \
    -DGDCM_BUILD_SHARED_LIBS=ON \
    -DGDCM_BUILD_DOCBOOK_MANPAGES=OFF \
    -DGDCM_BUILD_APPLICATIONS=OFF \
    -DGDCM_BUILD_TESTING=OFF
run_in_toolchain cmake --build "$gdcm_build" --parallel "$jobs"
run_in_toolchain cmake --install "$gdcm_build"

geant4_config=$(find "$geant4_install" -type f -name Geant4Config.cmake -print -quit)
gdcm_config=$(find "$gdcm_install" -type f -name GDCMConfig.cmake -print -quit)
[[ -n $geant4_config ]] || die "Geant4Config.cmake was not installed"
[[ -n $gdcm_config ]] || die "GDCMConfig.cmake was not installed"

topas_options=(
    -DCMAKE_BUILD_TYPE=Release
    "-DCMAKE_INSTALL_PREFIX=$topas_install"
    "-DCMAKE_PREFIX_PATH=$toolchain;$geant4_install;$gdcm_install"
    "-DGeant4_DIR=$(dirname "$geant4_config")"
    "-DGDCM_DIR=$(dirname "$gdcm_config")"
)
if [[ $with_qt == 1 ]]; then
    topas_options+=( -DTOPAS_USE_QT=ON -DTOPAS_USE_QT6=ON )
else
    topas_options+=( -DTOPAS_USE_QT=OFF -DTOPAS_USE_QT6=OFF )
fi

note "Configuring OpenTOPAS at commit $topas_commit"
run_in_toolchain cmake -S "$topas_source" -B "$topas_build" -G Ninja \
    "${topas_options[@]}"
note "Building and installing OpenTOPAS"
run_in_toolchain cmake --build "$topas_build" --parallel "$jobs"
run_in_toolchain cmake --install "$topas_build"

topas_executable=$topas_install/bin/topas
g4_data_dir=$geant4_install/share/Geant4/data
[[ -x $topas_executable ]] || \
    die "OpenTOPAS executable was not installed at $topas_executable"
[[ -d $g4_data_dir ]] || die "Geant4 datasets were not installed at $g4_data_dir"

qt_plugin_dir=
qt_platform_plugin_dir=
if [[ $with_qt == 1 ]]; then
    qt_xcb_plugin=$(find "$toolchain" -type f -name libqxcb.so -print -quit)
    [[ -n $qt_xcb_plugin ]] || die "the Qt XCB platform plugin was not installed"
    qt_platform_plugin_dir=$(dirname "$qt_xcb_plugin")
    qt_plugin_dir=$(dirname "$qt_platform_plugin_dir")
fi

mkdir -p "$wrapper_dir"
{
    printf '#!/usr/bin/env bash\n'
    printf '# Generated by %s. Source before running OpenTOPAS.\n' "$script_name"
    printf 'export OPENTOPAS_ROOT=%q\n' "$topas_install"
    printf 'export GEANT4_ROOT=%q\n' "$geant4_install"
    printf 'export GDCM_ROOT=%q\n' "$gdcm_install"
    printf 'export TOPAS_G4_DATA_DIR=%q\n' "$g4_data_dir"
    printf 'export PATH=%q:%q:${PATH}\n' "$topas_install/bin" "$toolchain/bin"
    printf 'export LD_LIBRARY_PATH=%q:%q:%q:%q${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}\n' \
        "$topas_install/lib" "$geant4_install/lib" "$gdcm_install/lib" "$toolchain/lib"
    if [[ $with_qt == 1 ]]; then
        printf 'export QT_PLUGIN_PATH=%q\n' "$qt_plugin_dir"
        printf 'export QT_QPA_PLATFORM_PLUGIN_PATH=%q\n' "$qt_platform_plugin_dir"
    fi
} > "$environment_script"
chmod 0644 "$environment_script"

{
    printf '#!/usr/bin/env bash\nset -e\n'
    printf 'source %q\n' "$environment_script"
    printf 'exec %q "$@"\n' "$topas_executable"
} > "$wrapper"
chmod 0755 "$wrapper"

{
    printf 'Installed: %s\n' "$(date --iso-8601=seconds)"
    printf 'Prefix: %s\n' "$prefix"
    printf 'Architecture: %s\n' "$(uname -m)"
    printf 'Geant4 version: %s\n' "$geant4_version"
    printf 'OpenTOPAS ref: %s\n' "$opentopas_ref"
    printf 'OpenTOPAS commit: %s\n' "$topas_commit"
    printf 'Conda executable: %s\n' "$conda_executable"
    printf 'Qt enabled: %s\n' "$with_qt"
    printf 'Build jobs: %s\n' "$jobs"
    printf 'Toolchain explicit packages:\n'
    "$conda_executable" list --prefix "$toolchain" --explicit
} > "$manifest"

if [[ $skip_smoke_test == 0 ]]; then
    note "Checking runtime library resolution"
    ldd_output=$(bash -c 'source "$1"; ldd "$2"' bash \
        "$environment_script" "$topas_executable")
    printf '%s\n' "$ldd_output"
    if grep -q 'not found' <<< "$ldd_output"; then
        die "one or more OpenTOPAS runtime libraries could not be resolved"
    fi

    note "Starting OpenTOPAS briefly to verify executable loading"
    set +e
    startup_output=$(bash -c 'source "$1"; timeout 30 "$2"' bash \
        "$environment_script" "$topas_executable" 2>&1)
    startup_status=$?
    set -e
    printf '%s\n' "$startup_output" | sed -n '1,20p'
    [[ $startup_output == *TOPAS* ]] || \
        die "OpenTOPAS did not produce its startup banner (exit $startup_status)"
fi

note "Installation complete"
printf 'Environment: %s\nWrapper:     %s\nExecutable:  %s\nG4 data:     %s\nManifest:    %s\n\n' \
    "$environment_script" "$wrapper" "$topas_executable" "$g4_data_dir" "$manifest"
printf 'For this shell, run:\n  source %q\n\n' "$environment_script"
printf 'Then run TOPAS normally:\n  topas main.txt\n'
