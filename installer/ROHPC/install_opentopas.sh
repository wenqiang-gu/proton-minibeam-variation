#!/usr/bin/env bash
# Install OpenTOPAS and its build/runtime dependencies without root privileges.

set -Eeuo pipefail

SCRIPT_NAME=${0##*/}
SHARED_CONDA="/apps/anaconda3/bin/conda"
if [[ -n ${USER:-} && -d /data/maia/$USER && -w /data/maia/$USER && -x $SHARED_CONDA ]]; then
    DEFAULT_PREFIX="/data/maia/${USER}/Applications"
    DEFAULT_PACKAGE_MANAGER="conda"
    DEFAULT_PROXY_URL="http://proxy.swmed.edu:3128"
else
    DEFAULT_PREFIX="${HOME}/Applications"
    DEFAULT_PACKAGE_MANAGER="auto"
    DEFAULT_PROXY_URL=""
fi
PREFIX="${OPENTOPAS_PREFIX:-$DEFAULT_PREFIX}"
JOBS="${OPENTOPAS_BUILD_JOBS:-8}"
GEANT4_VERSION="11.3.2"
OPENTOPAS_REF="main"
MICROMAMBA_VERSION="2.5.0-2"
PACKAGE_MANAGER="${OPENTOPAS_PACKAGE_MANAGER:-$DEFAULT_PACKAGE_MANAGER}"
CONDA_EXE="${OPENTOPAS_CONDA_EXE:-$SHARED_CONDA}"
PROXY_URL="${OPENTOPAS_PROXY_URL:-$DEFAULT_PROXY_URL}"
WITH_QT=1
CLEAN=0
SKIP_SMOKE_TEST=0

usage() {
    cat <<'EOF'
Install OpenTOPAS locally on Ubuntu without sudo or root access.

Usage:
  install_opentopas.sh [options]

Options:
  --prefix DIR             Installation root (default: writable
                           /data/maia/$USER/Applications, else $HOME/Applications)
  --jobs N                 Parallel build jobs (default: 8)
  --opentopas-ref REF      Git branch, tag, or commit (default: main)
  --geant4-version VERSION Geant4 version (default: 11.3.2)
  --package-manager NAME   auto, conda, or micromamba (managed-server
                           default: conda; elsewhere: auto)
  --conda-executable PATH  Shared Conda executable
                           (default: /apps/anaconda3/bin/conda)
  --proxy-url URL          Proxy used by Conda, curl, Git, and CMake
                           (managed-server default:
                           http://proxy.swmed.edu:3128; elsewhere: unset)
  --micromamba-version VER Rootless toolchain bootstrap (default: 2.5.0-2)
  --headless               Build without Qt/OpenGL visualization
  --clean                  Recreate build and install directories
  --skip-smoke-test        Skip the final dynamic-library and startup checks
  -h, --help               Show this help

On the managed /data/maia server, a no-option invocation uses the shared
/apps/anaconda3/bin/conda, http://proxy.swmed.edu:3128, the user's /data/maia
Applications directory, and 8 build jobs. Elsewhere, the installer prefers the
shared Conda when available and otherwise bootstraps micromamba under PREFIX.
It uses conda-forge for a private compiler, CMake, Qt 6, and required libraries,
then builds Geant4, GDCM, and
OpenTOPAS entirely below PREFIX. It never runs sudo and never edits your shell
startup files.

On clusters with a private certificate authority, set OPENTOPAS_CA_BUNDLE to
the readable PEM CA-bundle path supplied by the administrator before running.

After installation:
  source PREFIX/TOPAS/opentopas-env.sh
  topas main.txt

For a cron job, either source opentopas-env.sh inside the cron command or use:
  PREFIX/bin/topas main.txt
EOF
}

die() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

note() {
    printf '\n[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

while (($#)); do
    case "$1" in
        --prefix)
            (($# >= 2)) || die "--prefix requires a directory"
            PREFIX=$2
            shift 2
            ;;
        --jobs)
            (($# >= 2)) || die "--jobs requires a positive integer"
            JOBS=$2
            shift 2
            ;;
        --opentopas-ref)
            (($# >= 2)) || die "--opentopas-ref requires a value"
            OPENTOPAS_REF=$2
            shift 2
            ;;
        --geant4-version)
            (($# >= 2)) || die "--geant4-version requires a value"
            GEANT4_VERSION=$2
            shift 2
            ;;
        --package-manager)
            (($# >= 2)) || die "--package-manager requires auto, conda, or micromamba"
            PACKAGE_MANAGER=$2
            shift 2
            ;;
        --conda-executable)
            (($# >= 2)) || die "--conda-executable requires a path"
            CONDA_EXE=$2
            shift 2
            ;;
        --proxy-url)
            (($# >= 2)) || die "--proxy-url requires a URL"
            PROXY_URL=$2
            shift 2
            ;;
        --micromamba-version)
            (($# >= 2)) || die "--micromamba-version requires a value"
            MICROMAMBA_VERSION=$2
            shift 2
            ;;
        --headless)
            WITH_QT=0
            shift
            ;;
        --clean)
            CLEAN=1
            shift
            ;;
        --skip-smoke-test)
            SKIP_SMOKE_TEST=1
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
case "$(uname -m)" in
    x86_64) MAMBA_PLATFORM=linux-64 ;;
    aarch64|arm64) MAMBA_PLATFORM=linux-aarch64 ;;
    *) die "unsupported CPU architecture: $(uname -m)" ;;
esac

if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    source /etc/os-release
    if [[ ${ID:-} != ubuntu ]]; then
        printf 'warning: designed for Ubuntu 20.04; detected %s %s\n' \
            "${ID:-unknown}" "${VERSION_ID:-unknown}" >&2
    elif [[ ${VERSION_ID:-} != 20.04 ]]; then
        printf 'warning: designed for Ubuntu 20.04; detected Ubuntu %s\n' \
            "${VERSION_ID:-unknown}" >&2
    fi
fi

if [[ -z $JOBS ]]; then
    JOBS=$(getconf _NPROCESSORS_ONLN 2>/dev/null || printf '1')
    ((JOBS > 8)) && JOBS=8
fi
[[ $JOBS =~ ^[1-9][0-9]*$ ]] || die "--jobs must be a positive integer"
[[ $GEANT4_VERSION =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || \
    die "--geant4-version must look like 11.3.2"
[[ $MICROMAMBA_VERSION =~ ^[0-9]+\.[0-9]+\.[0-9]+-[0-9]+$ ]] || \
    die "--micromamba-version must look like 2.5.0-2"
[[ -n $OPENTOPAS_REF ]] || die "--opentopas-ref cannot be empty"
if [[ -n $PROXY_URL && ! $PROXY_URL =~ ^https?://[^[:space:]]+$ ]]; then
    die "--proxy-url must be an http:// or https:// URL without whitespace"
fi
case "$PACKAGE_MANAGER" in
    auto)
        if [[ -x $CONDA_EXE ]]; then
            PACKAGE_MANAGER=conda
        else
            PACKAGE_MANAGER=micromamba
        fi
        ;;
    conda)
        [[ -x $CONDA_EXE ]] || die "Conda executable not found: $CONDA_EXE"
        ;;
    micromamba) ;;
    *) die "--package-manager must be auto, conda, or micromamba" ;;
esac

PREFIX=$(mkdir -p "$PREFIX" && cd "$PREFIX" && pwd -P)
MAMBA_ROOT="$PREFIX/.micromamba"
MAMBA_EXE="$MAMBA_ROOT/bin/micromamba"
TOOLCHAIN="$PREFIX/opentopas-toolchain"
GEANT4_ROOT="$PREFIX/GEANT4"
GEANT4_SOURCE="$GEANT4_ROOT/geant4-v${GEANT4_VERSION}"
GEANT4_BUILD="$GEANT4_ROOT/geant4-build"
GEANT4_INSTALL="$GEANT4_ROOT/geant4-install"
GDCM_ROOT="$PREFIX/GDCM"
GDCM_SOURCE="$GDCM_ROOT/gdcm-2.6.8"
GDCM_BUILD="$GDCM_ROOT/gdcm-build"
GDCM_INSTALL="$GDCM_ROOT/gdcm-install"
TOPAS_ROOT="$PREFIX/TOPAS"
TOPAS_SOURCE="$TOPAS_ROOT/OpenTOPAS"
TOPAS_BUILD="$TOPAS_ROOT/OpenTOPAS-build"
TOPAS_INSTALL="$TOPAS_ROOT/OpenTOPAS-install"
ENV_SCRIPT="$TOPAS_ROOT/opentopas-env.sh"
WRAPPER_DIR="$PREFIX/bin"
WRAPPER="$WRAPPER_DIR/topas"
MANIFEST="$TOPAS_ROOT/install-manifest.txt"
LOG_FILE="$TOPAS_ROOT/install-opentopas.log"

if [[ -n $PROXY_URL ]]; then
    export http_proxy="$PROXY_URL"
    export https_proxy="$PROXY_URL"
    export HTTP_PROXY="$PROXY_URL"
    export HTTPS_PROXY="$PROXY_URL"
fi

mkdir -p "$TOPAS_ROOT"
exec > >(tee -a "$LOG_FILE") 2>&1

note "Rootless OpenTOPAS installation"
printf 'Prefix: %s\nJobs: %s\nGeant4: %s\nOpenTOPAS ref: %s\nPackage manager: %s\n' \
    "$PREFIX" "$JOBS" "$GEANT4_VERSION" "$OPENTOPAS_REF" "$PACKAGE_MANAGER"
if [[ $PACKAGE_MANAGER == conda ]]; then
    printf 'Conda executable: %s\n' "$CONDA_EXE"
else
    printf 'Micromamba: %s\n' "$MICROMAMBA_VERSION"
fi
if [[ -n $PROXY_URL ]]; then
    printf 'Explicit proxy: configured (value hidden)\n'
else
    inherited_https_proxy=${https_proxy:-${HTTPS_PROXY:-}}
    if [[ $inherited_https_proxy == https://* ]]; then
        printf '%s\n' \
            'warning: the inherited HTTPS proxy URL begins with https://.' \
            'If this is an HTTP CONNECT proxy, rerun with --proxy-url http://HOST:PORT.' >&2
    fi
fi
printf 'Qt visualization: %s\nLog: %s\n' \
    "$([[ $WITH_QT == 1 ]] && printf enabled || printf disabled)" "$LOG_FILE"

available_kb=$(df -Pk "$PREFIX" | awk 'NR==2 {print $4}')
if [[ $available_kb =~ ^[0-9]+$ ]] && ((available_kb < 10 * 1024 * 1024)); then
    die "less than 10 GiB is available below $PREFIX; OpenTOPAS builds need substantial temporary space"
fi

try_download() {
    local url=$1
    local destination=$2
    local temporary="${destination}.part"
    local found_client=0

    rm -f "$temporary"
    if command -v curl >/dev/null 2>&1; then
        found_client=1
        if curl --fail --location --retry 4 --retry-delay 3 \
            --output "$temporary" "$url"; then
            mv "$temporary" "$destination"
            return 0
        fi
        printf 'warning: curl could not download %s; trying another client/source\n' \
            "$url" >&2
        rm -f "$temporary"
    fi
    if command -v wget >/dev/null 2>&1; then
        found_client=1
        if wget --tries=4 --waitretry=3 --output-document="$temporary" "$url"; then
            mv "$temporary" "$destination"
            return 0
        fi
        printf 'warning: wget could not download %s\n' "$url" >&2
        rm -f "$temporary"
    fi
    ((found_client == 1)) || \
        printf 'warning: neither curl nor wget is installed\n' >&2
    return 1
}

show_network_advice() {
    local name value
    local proxy_found=0
    local https_proxy_scheme=0
    for name in HTTPS_PROXY https_proxy HTTP_PROXY http_proxy ALL_PROXY all_proxy; do
        value=${!name-}
        if [[ -n $value ]]; then
            proxy_found=1
            printf '  %s is set (value hidden)\n' "$name" >&2
            if [[ $name == HTTPS_PROXY || $name == https_proxy ]] && \
                    [[ $value == https://* ]]; then
                https_proxy_scheme=1
            fi
        fi
    done

    if ((proxy_found == 1)); then
        printf '%s\n' \
            'A proxy is configured. curl error 35 with "wrong version number" often' \
            'means the proxy expects an http:// proxy URL even for HTTPS downloads.' \
            'Ask the cluster administrator for the correct proxy URL and CA settings.' >&2
        if ((https_proxy_scheme == 1)); then
            printf '%s\n' \
                'At least one HTTPS proxy variable begins with https://; this is the' \
                'likely problem. Change only the proxy URL scheme to http:// if your' \
                'cluster documentation confirms that it is an HTTP CONNECT proxy.' >&2
        fi
    else
        printf '%s\n' \
            'No proxy environment variables were detected. The compute node may block' \
            'outbound HTTPS or require a site CA/proxy. Ask the cluster administrator,' \
            'or download micromamba on a login host and copy it to:' \
            "  $MAMBA_EXE" >&2
    fi
}

select_ca_bundle() {
    local candidate

    if [[ -n ${OPENTOPAS_CA_BUNDLE:-} ]]; then
        [[ -r $OPENTOPAS_CA_BUNDLE ]] || \
            die "OPENTOPAS_CA_BUNDLE is not a readable file: $OPENTOPAS_CA_BUNDLE"
        CA_BUNDLE=$OPENTOPAS_CA_BUNDLE
    else
        CA_BUNDLE=""
        for candidate in \
                "${SSL_CERT_FILE:-}" \
                "${CURL_CA_BUNDLE:-}" \
                /etc/ssl/certs/ca-certificates.crt \
                /etc/pki/tls/certs/ca-bundle.crt \
                /etc/ssl/cert.pem; do
            if [[ -n $candidate && -r $candidate && -f $candidate ]]; then
                CA_BUNDLE=$candidate
                break
            fi
        done
    fi

    if [[ -n $CA_BUNDLE ]]; then
        export SSL_CERT_FILE="$CA_BUNDLE"
        export CURL_CA_BUNDLE="$CA_BUNDLE"
        export REQUESTS_CA_BUNDLE="$CA_BUNDLE"
        export MAMBA_SSL_VERIFY="$CA_BUNDLE"
        note "Using TLS CA bundle: $CA_BUNDLE"
    else
        printf '%s\n' \
            'warning: no readable CA bundle was found; micromamba will use its' \
            'compiled default. Set OPENTOPAS_CA_BUNDLE if that default fails.' >&2
    fi
}

if [[ $PACKAGE_MANAGER == conda && -z ${OPENTOPAS_CA_BUNDLE:-} ]]; then
    CA_BUNDLE=""
    note "Using the shared Conda and its configured TLS trust"
else
    select_ca_bundle
fi
if [[ $PACKAGE_MANAGER == micromamba ]]; then
    mamba_tls_options=()
    if [[ -n $CA_BUNDLE ]]; then
        mamba_tls_options=(--ssl-verify "$CA_BUNDLE")
    fi

    required_micromamba_runtime=${MICROMAMBA_VERSION%-*}
    installed_micromamba_runtime=""
    micromamba_replaced=0
    if [[ -x $MAMBA_EXE ]]; then
        installed_micromamba_runtime=$($MAMBA_EXE --version 2>/dev/null || true)
    fi
    if [[ $installed_micromamba_runtime != "$required_micromamba_runtime" ]]; then
        if [[ -n $installed_micromamba_runtime ]]; then
            note "Replacing micromamba $installed_micromamba_runtime with pinned $MICROMAMBA_VERSION"
        fi
        rm -f "$MAMBA_EXE"
        micromamba_replaced=1
    fi

    if [[ ! -x $MAMBA_EXE ]]; then
        note "Bootstrapping micromamba"
        command -v tar >/dev/null 2>&1 || die "tar is required"
        archive="$PREFIX/.micromamba-${MAMBA_PLATFORM}.tar.bz2"
        mkdir -p "$MAMBA_ROOT"
        if ! try_download \
                "https://github.com/mamba-org/micromamba-releases/releases/download/${MICROMAMBA_VERSION}/micromamba-${MAMBA_PLATFORM}" \
                "$MAMBA_EXE"; then
            printf '%s\n' \
                'warning: the pinned GitHub binary download failed; trying the official' \
                'micromamba package endpoint' >&2
            if ! try_download \
                    "https://micro.mamba.pm/api/micromamba/${MAMBA_PLATFORM}/${MICROMAMBA_VERSION}" \
                    "$archive" || \
                    ! tar -xjf "$archive" -C "$MAMBA_ROOT" bin/micromamba; then
                rm -f "$archive" "$MAMBA_EXE"
                show_network_advice
                die "could not download micromamba from either official source"
            fi
            rm -f "$archive"
        fi
        [[ -x $MAMBA_EXE ]] || chmod 0755 "$MAMBA_EXE"
    fi
    installed_micromamba_runtime=$($MAMBA_EXE --version 2>/dev/null || true)
    [[ $installed_micromamba_runtime == "$required_micromamba_runtime" ]] || \
        die "expected micromamba $required_micromamba_runtime, got ${installed_micromamba_runtime:-unusable}"
    printf 'Micromamba executable: %s (version %s)\n' \
        "$MAMBA_EXE" "$installed_micromamba_runtime"

    export MAMBA_ROOT_PREFIX="$MAMBA_ROOT"
    if ((micromamba_replaced == 1)); then
        note "Removing index metadata cached by the incompatible micromamba build"
        "$MAMBA_EXE" clean --index-cache --yes || \
            printf 'warning: could not clear micromamba index cache; continuing\n' >&2
    fi
fi
base_packages=(
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
qt_packages=(
    "qt6-main>=6.4,<7"
    harfbuzz
    libgl-devel
    libglu
    xorg-libx11
    xorg-libxext
    xorg-libxt
    xorg-libxmu
)
if [[ $WITH_QT == 1 ]]; then
    base_packages+=("${qt_packages[@]}")
fi

note "Creating/updating the private build toolchain"
if [[ $PACKAGE_MANAGER == conda ]]; then
    conda_version=$($CONDA_EXE --version 2>&1) || \
        die "could not run shared Conda: $CONDA_EXE"
    printf 'Shared Conda: %s\n' "$conda_version"
    if [[ -d $TOOLCHAIN/conda-meta ]]; then
        "$CONDA_EXE" install --yes --prefix "$TOOLCHAIN" \
            --override-channels --channel conda-forge "${base_packages[@]}"
    else
        "$CONDA_EXE" create --yes --prefix "$TOOLCHAIN" \
            --override-channels --channel conda-forge "${base_packages[@]}"
    fi
else
    if [[ -d $TOOLCHAIN/conda-meta ]]; then
        "$MAMBA_EXE" install --yes --prefix "$TOOLCHAIN" \
            --channel conda-forge --channel-priority strict \
            "${mamba_tls_options[@]}" "${base_packages[@]}"
    else
        "$MAMBA_EXE" create --yes --prefix "$TOOLCHAIN" \
            --channel conda-forge --channel-priority strict \
            "${mamba_tls_options[@]}" "${base_packages[@]}"
    fi
fi

if [[ $PACKAGE_MANAGER == conda ]]; then
    # Conda 4.9's activation hook reads interactive-shell variables such as
    # PS1 even in a noninteractive script. Temporarily disable nounset only
    # while evaluating the trusted shared-Conda hook, then restore it.
    set +u
    conda_hook=$($CONDA_EXE shell.bash hook 2>/dev/null) || \
        die "could not initialize the shared Conda shell hook"
    eval "$conda_hook"
    conda activate "$TOOLCHAIN"
    set -u
    mrun() {
        command "$@"
    }
    write_explicit_packages() {
        "$CONDA_EXE" list --prefix "$TOOLCHAIN" --explicit
    }
else
    mrun() {
        "$MAMBA_EXE" run --prefix "$TOOLCHAIN" "$@"
    }
    write_explicit_packages() {
        "$MAMBA_EXE" list --prefix "$TOOLCHAIN" --explicit
    }
fi

export CMAKE_PREFIX_PATH="$TOOLCHAIN${CMAKE_PREFIX_PATH:+:$CMAKE_PREFIX_PATH}"
export PKG_CONFIG_PATH="$TOOLCHAIN/lib/pkgconfig:$TOOLCHAIN/share/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"

if [[ $CLEAN == 1 ]]; then
    note "Removing prior build and install directories (--clean)"
    rm -rf "$GEANT4_BUILD" "$GEANT4_INSTALL" \
        "$GDCM_BUILD" "$GDCM_INSTALL" "$TOPAS_BUILD" "$TOPAS_INSTALL"
fi

mkdir -p "$GEANT4_ROOT"
if [[ ! -f $GEANT4_SOURCE/CMakeLists.txt ]]; then
    note "Downloading Geant4 ${GEANT4_VERSION}"
    geant4_archive="$GEANT4_ROOT/geant4-v${GEANT4_VERSION}.tar.gz"
    if [[ ! -f $geant4_archive ]]; then
        try_download \
            "https://gitlab.cern.ch/geant4/geant4/-/archive/v${GEANT4_VERSION}/geant4-v${GEANT4_VERSION}.tar.gz" \
            "$geant4_archive" || die "could not download Geant4 ${GEANT4_VERSION}"
    fi
    tar -xzf "$geant4_archive" -C "$GEANT4_ROOT"
fi

geant4_options=(
    -DCMAKE_BUILD_TYPE=Release
    "-DCMAKE_INSTALL_PREFIX=$GEANT4_INSTALL"
    "-DCMAKE_PREFIX_PATH=$TOOLCHAIN"
    -DGEANT4_INSTALL_DATA=ON
    -DGEANT4_BUILD_MULTITHREADED=ON
    -DGEANT4_BUILD_VERBOSE_CODE=OFF
    -DBUILD_SHARED_LIBS=ON
)
if [[ $WITH_QT == 1 ]]; then
    geant4_options+=(
        -DGEANT4_USE_QT=ON
        -DGEANT4_USE_QT_QT6=ON
        -DGEANT4_USE_OPENGL_X11=ON
    )
else
    geant4_options+=(
        -DGEANT4_USE_QT=OFF
        -DGEANT4_USE_OPENGL_X11=OFF
    )
fi

note "Configuring Geant4 ${GEANT4_VERSION}"
mrun cmake -S "$GEANT4_SOURCE" -B "$GEANT4_BUILD" -G Ninja \
    "${geant4_options[@]}"
note "Building and installing Geant4"
mrun cmake --build "$GEANT4_BUILD" --parallel "$JOBS"
mrun cmake --install "$GEANT4_BUILD"

note "Obtaining OpenTOPAS source"
mkdir -p "$TOPAS_ROOT"
if [[ ! -d $TOPAS_SOURCE/.git ]]; then
    mrun git clone https://github.com/OpenTOPAS/OpenTOPAS.git "$TOPAS_SOURCE"
fi
mrun git -C "$TOPAS_SOURCE" checkout "$OPENTOPAS_REF"
TOPAS_COMMIT=$(mrun git -C "$TOPAS_SOURCE" rev-parse HEAD)

mkdir -p "$GDCM_ROOT"
if [[ ! -f $GDCM_SOURCE/CMakeLists.txt ]]; then
    gdcm_archive="$TOPAS_SOURCE/gdcm-2.6.8.tar.gz"
    [[ -f $gdcm_archive ]] || die "OpenTOPAS source does not contain gdcm-2.6.8.tar.gz"
    note "Extracting bundled GDCM 2.6.8"
    tar -xzf "$gdcm_archive" -C "$GDCM_ROOT"
fi

note "Configuring GDCM 2.6.8"
mrun cmake -S "$GDCM_SOURCE" -B "$GDCM_BUILD" -G Ninja \
    -DCMAKE_BUILD_TYPE=Release \
    "-DCMAKE_INSTALL_PREFIX=$GDCM_INSTALL" \
    "-DCMAKE_PREFIX_PATH=$TOOLCHAIN" \
    -DGDCM_BUILD_SHARED_LIBS=ON \
    -DGDCM_BUILD_DOCBOOK_MANPAGES=OFF \
    -DGDCM_BUILD_APPLICATIONS=OFF \
    -DGDCM_BUILD_TESTING=OFF
note "Building and installing GDCM"
mrun cmake --build "$GDCM_BUILD" --parallel "$JOBS"
mrun cmake --install "$GDCM_BUILD"

GEANT4_CONFIG=$(find "$GEANT4_INSTALL" -type f -name Geant4Config.cmake \
    -print -quit)
GDCM_CONFIG=$(find "$GDCM_INSTALL" -type f -name GDCMConfig.cmake \
    -print -quit)
[[ -n $GEANT4_CONFIG ]] || die "Geant4Config.cmake was not installed"
[[ -n $GDCM_CONFIG ]] || die "GDCMConfig.cmake was not installed"
GEANT4_CMAKE_DIR=$(dirname "$GEANT4_CONFIG")
GDCM_CMAKE_DIR=$(dirname "$GDCM_CONFIG")

topas_options=(
    -DCMAKE_BUILD_TYPE=Release
    "-DCMAKE_INSTALL_PREFIX=$TOPAS_INSTALL"
    "-DCMAKE_PREFIX_PATH=$TOOLCHAIN;$GEANT4_INSTALL;$GDCM_INSTALL"
    "-DGeant4_DIR=$GEANT4_CMAKE_DIR"
    "-DGDCM_DIR=$GDCM_CMAKE_DIR"
)
if [[ $WITH_QT == 1 ]]; then
    topas_options+=( -DTOPAS_USE_QT=ON -DTOPAS_USE_QT6=ON )
else
    topas_options+=( -DTOPAS_USE_QT=OFF -DTOPAS_USE_QT6=OFF )
fi

note "Configuring OpenTOPAS at commit $TOPAS_COMMIT"
mrun cmake -S "$TOPAS_SOURCE" -B "$TOPAS_BUILD" -G Ninja \
    "${topas_options[@]}"
note "Building and installing OpenTOPAS"
mrun cmake --build "$TOPAS_BUILD" --parallel "$JOBS"
mrun cmake --install "$TOPAS_BUILD"

TOPAS_EXE="$TOPAS_INSTALL/bin/topas"
[[ -x $TOPAS_EXE ]] || die "OpenTOPAS executable was not installed at $TOPAS_EXE"
G4_DATA_DIR="$GEANT4_INSTALL/share/Geant4/data"
[[ -d $G4_DATA_DIR ]] || die "Geant4 datasets were not installed at $G4_DATA_DIR"
QT_PLUGIN_DIR=""
QT_PLATFORM_PLUGIN_DIR=""
if [[ $WITH_QT == 1 ]]; then
    QT_XCB_PLUGIN=$(find "$TOOLCHAIN" -type f -name libqxcb.so -print -quit)
    [[ -n $QT_XCB_PLUGIN ]] || die "the Qt XCB platform plugin was not installed"
    QT_PLATFORM_PLUGIN_DIR=$(dirname "$QT_XCB_PLUGIN")
    QT_PLUGIN_DIR=$(dirname "$QT_PLATFORM_PLUGIN_DIR")
fi

mkdir -p "$WRAPPER_DIR"
{
    printf '#!/usr/bin/env bash\n'
    printf '# Generated by %s. Source this file before running OpenTOPAS.\n' "$SCRIPT_NAME"
    printf 'export OPENTOPAS_ROOT=%q\n' "$TOPAS_INSTALL"
    printf 'export GEANT4_ROOT=%q\n' "$GEANT4_INSTALL"
    printf 'export GDCM_ROOT=%q\n' "$GDCM_INSTALL"
    printf 'export TOPAS_G4_DATA_DIR=%q\n' "$G4_DATA_DIR"
    printf 'export PATH=%q:%q:${PATH}\n' "$TOPAS_INSTALL/bin" "$TOOLCHAIN/bin"
    printf 'export LD_LIBRARY_PATH=%q:%q:%q:%q${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}\n' \
        "$TOPAS_INSTALL/lib" "$GEANT4_INSTALL/lib" "$GDCM_INSTALL/lib" "$TOOLCHAIN/lib"
    if [[ $WITH_QT == 1 ]]; then
        printf 'export QT_PLUGIN_PATH=%q\n' "$QT_PLUGIN_DIR"
        printf 'export QT_QPA_PLATFORM_PLUGIN_PATH=%q\n' \
            "$QT_PLATFORM_PLUGIN_DIR"
    fi
} > "$ENV_SCRIPT"
chmod 0644 "$ENV_SCRIPT"

{
    printf '#!/usr/bin/env bash\n'
    printf 'set -e\n'
    printf 'source %q\n' "$ENV_SCRIPT"
    printf 'exec %q "$@"\n' "$TOPAS_EXE"
} > "$WRAPPER"
chmod 0755 "$WRAPPER"

{
    printf 'Installed: %s\n' "$(date --iso-8601=seconds)"
    printf 'Prefix: %s\n' "$PREFIX"
    printf 'Ubuntu: %s\n' "${PRETTY_NAME:-unknown}"
    printf 'Architecture: %s\n' "$(uname -m)"
    printf 'Geant4 version: %s\n' "$GEANT4_VERSION"
    printf 'OpenTOPAS ref: %s\n' "$OPENTOPAS_REF"
    printf 'OpenTOPAS commit: %s\n' "$TOPAS_COMMIT"
    printf 'Package manager: %s\n' "$PACKAGE_MANAGER"
    if [[ $PACKAGE_MANAGER == conda ]]; then
        printf 'Conda executable: %s\n' "$CONDA_EXE"
    else
        printf 'Micromamba version: %s\n' "$installed_micromamba_runtime"
    fi
    printf 'Qt enabled: %s\n' "$WITH_QT"
    printf 'Build jobs: %s\n' "$JOBS"
    printf 'Toolchain explicit packages:\n'
    write_explicit_packages
} > "$MANIFEST"

if [[ $SKIP_SMOKE_TEST == 0 ]]; then
    note "Checking runtime library resolution"
    ldd_output=$(bash -c 'source "$1"; ldd "$2"' bash "$ENV_SCRIPT" "$TOPAS_EXE")
    printf '%s\n' "$ldd_output"
    if grep -q 'not found' <<< "$ldd_output"; then
        die "one or more OpenTOPAS runtime libraries could not be resolved"
    fi

    note "Starting OpenTOPAS briefly to verify that the executable loads"
    set +e
    if command -v timeout >/dev/null 2>&1; then
        startup_output=$(bash -c 'source "$1"; timeout 30 "$2"' bash \
            "$ENV_SCRIPT" "$TOPAS_EXE" 2>&1)
    else
        startup_output=$(bash -c 'source "$1"; "$2"' bash \
            "$ENV_SCRIPT" "$TOPAS_EXE" 2>&1)
    fi
    startup_status=$?
    set -e
    printf '%s\n' "$startup_output" | sed -n '1,20p'
    if [[ $startup_output != *TOPAS* ]]; then
        die "OpenTOPAS did not produce its startup banner (exit $startup_status)"
    fi
fi

note "Installation complete"
printf 'Environment: %s\nWrapper:     %s\nExecutable:  %s\nG4 data:     %s\nManifest:    %s\n\n' \
    "$ENV_SCRIPT" "$WRAPPER" "$TOPAS_EXE" "$G4_DATA_DIR" "$MANIFEST"
printf 'For this shell, run:\n  source %q\n\n' "$ENV_SCRIPT"
printf 'Then run TOPAS normally:\n  topas main.txt\n\n'
printf 'For cron or scripts, use the absolute wrapper:\n  %q main.txt\n' "$WRAPPER"
