#!/usr/bin/env bash
# Build the distributable packages from this checkout:
#
#   bash packaging/build-packages.sh                     # .deb + .tar.gz here
#   bash packaging/build-packages.sh --go-binary /path/to/simurgh-go
#   bash packaging/build-packages.sh --out dist --version 2.8.0
#
# It needs no network and no Python packaging tools: the tree is copied into a
# staging root, the launcher is pointed at the right libdir, and `dpkg-deb` /
# `rpmbuild` (whichever exists) turns that root into a package.  The .tar.gz is
# always built and is the fallback for distributions the other two do not
# cover: unpack it at /.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

VERSION=""
OUT="dist"
PREFIX="/usr"
GO_BINARY=""
NAME="simurgh"
MAINTAINER="${SIMURGH_MAINTAINER:-Simurgh Tunnel contributors <noreply@example.com>}"
BUILD_DEB=1
BUILD_RPM=1
BUILD_TAR=1
ARCH=""

while [ $# -gt 0 ]; do
    case "$1" in
        --version)   VERSION="$2"; shift 2 ;;
        --out)       OUT="$2"; shift 2 ;;
        --prefix)    PREFIX="$2"; shift 2 ;;
        --go-binary) GO_BINARY="$2"; shift 2 ;;
        --name)      NAME="$2"; shift 2 ;;
        --maintainer) MAINTAINER="$2"; shift 2 ;;
        --arch)      ARCH="$2"; shift 2 ;;
        --no-deb)    BUILD_DEB=0; shift ;;
        --no-rpm)    BUILD_RPM=0; shift ;;
        --no-tar)    BUILD_TAR=0; shift ;;
        -h|--help)   sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

if [ -z "$VERSION" ]; then
    VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' simurgh/__init__.py)"
fi
[ -n "$VERSION" ] || { echo "cannot work out the version" >&2; exit 1; }

# ---- what this build is ----------------------------------------------------
GOARCH=""
if [ -n "$GO_BINARY" ]; then
    [ -f "$GO_BINARY" ] || { echo "no such engine binary: $GO_BINARY" >&2; exit 1; }
    GOARCH="${ARCH:-$(uname -m)}"
fi

LIBDIR="$PREFIX/lib/$NAME"
BINDIR="$PREFIX/bin"
UNITDIR="$PREFIX/lib/systemd/system"
DOCDIR="$PREFIX/share/doc/$NAME"

STAGE="$(mktemp -d "${TMPDIR:-/tmp}/simurgh-stage.XXXXXX")"
RPMTOP=""
cleanup() { rm -rf "$STAGE" ${RPMTOP:+"$RPMTOP"}; }
trap cleanup EXIT

log() { printf '  %s\n' "$*" >&2; }

# ---- staging root ---------------------------------------------------------
install -d "$STAGE$LIBDIR/simurgh" "$STAGE$BINDIR" "$STAGE$UNITDIR" "$STAGE$DOCDIR"
install -d -m 0700 "$STAGE/etc/$NAME"

# the Python engine: pure Python, and every dependency it has is bundled
tar -c --exclude='__pycache__' --exclude='*.pyc' -C simurgh . | tar -x -C "$STAGE$LIBDIR/simurgh"

# the launcher: install.sh uses a virtualenv, a package must not
sed "s|@LIBDIR@|$LIBDIR|" packaging/simurgh-launcher.py.in > "$STAGE$BINDIR/$NAME"
chmod 0755 "$STAGE$BINDIR/$NAME"
python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" "$STAGE$BINDIR/$NAME"

if [ -n "$GO_BINARY" ]; then
    install -m 0755 "$GO_BINARY" "$STAGE$BINDIR/$NAME-go"
    log "engine: $(basename "$GO_BINARY") -> $BINDIR/$NAME-go ($GOARCH)"
fi

install -m 0644 packaging/systemd/simurgh-relay.service "$STAGE$UNITDIR/"
install -m 0644 packaging/systemd/simurgh-exit.service "$STAGE$UNITDIR/"

install -m 0644 LICENSE "$STAGE$DOCDIR/LICENSE"
for doc in README.md README.fa.md docs/CONFIGURATION.md docs/CONFIGURATION.fa.md \
           docs/PACKAGING.md docs/PACKAGING.fa.md; do
    [ -f "$doc" ] && install -m 0644 "$doc" "$STAGE$DOCDIR/$(basename "$doc")"
done

# ---- portable tarball ------------------------------------------------------
if [ "$BUILD_TAR" = 1 ]; then
    mkdir -p "$OUT"
    TAR_ARCH="noarch"
    [ -n "$GOARCH" ] && TAR_ARCH="$(uname -m)"
    TARNAME="$NAME-$VERSION-$TAR_ARCH.tar.gz"
    tar -czf "$OUT/$TARNAME" -C "$STAGE" .
    log "wrote $OUT/$TARNAME (unpack at /: tar -xzf $TARNAME -C /)"
fi

# ---- .deb ------------------------------------------------------------------
if [ "$BUILD_DEB" = 1 ]; then
    if command -v dpkg-deb >/dev/null 2>&1; then
        DEB_ARCH="all"
        [ -n "$GOARCH" ] && DEB_ARCH="$(dpkg --print-architecture 2>/dev/null || echo "$GOARCH")"
        DEBSTAGE="$STAGE"
        install -d "$DEBSTAGE/DEBIAN"
        SIZE="$(du -sk "$STAGE" | cut -f1)"
        {
            echo "Package: $NAME"
            echo "Version: $VERSION"
            echo "Architecture: $DEB_ARCH"
            echo "Maintainer: $MAINTAINER"
            echo "Installed-Size: $SIZE"
            echo "Section: net"
            echo "Priority: optional"
            echo "Homepage: https://github.com/Dark-Sky07/Simurgh-Tunnel"
            echo "Depends: python3 (>= 3.9)"
            echo "Recommends: python3-cryptography"
            echo "Suggests: docker.io | podman"
            echo "Description: Transparent Iran <-> foreign TCP/UDP tunnel for VPN panels"
            echo " Simurgh Tunnel carries the TCP and UDP traffic of an Iranian server"
            echo " to a foreign one over a link that survives filtering (four carriers:"
            echo " tls, wss, raw, plain), with a web panel, Prometheus metrics,"
            echo " per-mapping limits and quotas, traffic accounting, alerts and"
            echo " certificate automation.  This package ships the Python engine and"
            echo " systemd units for both roles; the compiled engine is installed as"
            echo " /usr/bin/simurgh-go when this build included it."
        } > "$DEBSTAGE/DEBIAN/control"
        for script in postinst prerm postrm; do
            install -m 0755 "packaging/deb/$script" "$DEBSTAGE/DEBIAN/$script"
        done
        mkdir -p "$OUT"
        DEBNAME="${NAME}_${VERSION}_${DEB_ARCH}.deb"
        dpkg-deb --build --root-owner-group "$DEBSTAGE" "$OUT/$DEBNAME" >/dev/null
        log "wrote $OUT/$DEBNAME"
        # the maintainer scripts live in DEBIAN/, never in the payload
        rm -rf "$DEBSTAGE/DEBIAN"
    else
        log "dpkg-deb not found: skipping the .deb"
    fi
fi

# ---- .rpm ------------------------------------------------------------------
if [ "$BUILD_RPM" = 1 ]; then
    if command -v rpmbuild >/dev/null 2>&1; then
        RPMTOP="$(mktemp -d "${TMPDIR:-/tmp}/simurgh-rpm.XXXXXX")"
        install -d "$RPMTOP"/{BUILD,RPMS,SOURCES,SPECS,BUILDROOT}
        cp packaging/rpm/simurgh.spec "$RPMTOP/SPECS/"
        RPMDEFS=(-D "version $VERSION" -D "release 1" -D "stage $STAGE"
                 -D "_topdir $RPMTOP" -D "_prefix $PREFIX"
                 -D "buildroot $RPMTOP/BUILDROOT")
        [ -n "$GOARCH" ] && RPMDEFS+=(-D "goarch $GOARCH")
        rpmbuild -bb "${RPMDEFS[@]}" "$RPMTOP/SPECS/simurgh.spec" >&2
        mkdir -p "$OUT"
        find "$RPMTOP/RPMS" -name '*.rpm' -exec cp {} "$OUT/" \;
        for rpm in "$OUT"/*.rpm; do log "wrote $rpm"; done
    else
        log "rpmbuild not found: skipping the .rpm"
    fi
fi

ls -l "$OUT" >&2
