%global _build_id_links none
# The Python side is pure Python (deps are bundled), so the package is noarch
# unless the caller staged a compiled engine next to it; then it is that
# machine's architecture:
#   rpmbuild -bb packaging/rpm/simurgh.spec -D "version 2.8.0" -D "stage /tmp/stage"
#   rpmbuild -bb ... -D "goarch x86_64"      # when /usr/bin/simurgh-go is staged

%if %{defined goarch}
BuildArch: %{goarch}
%else
BuildArch: noarch
%endif

Name:           simurgh
Version:        %{version}
Release:        %{release}%{?dist}
Summary:        Transparent Iran <-> foreign TCP/UDP tunnel for VPN panels
License:        MIT
URL:            https://github.com/Dark-Sky07/Simurgh-Tunnel
Source0:        simurgh-%{version}.tar.gz
Requires:       python3 >= 3.9
Recommends:     python3-cryptography
# The panel is served over plain HTTP and meant to be reached through the
# tunnel or an SSH port-forward; the units keep that promise off the public
# interface by binding what the operator configured, nothing more.

%description
Simurgh Tunnel carries the TCP and UDP traffic of an Iranian server to a
foreign one, so VPN panels (3x-ui, OMEGA, Hiddify, X-UI) work over a link that
survives filtering.  It has four carriers (tls, wss, raw, plain), both engines
speak the same protocol (so one side may run the compiled engine), a web panel
with metrics, Prometheus output, per-mapping bandwidth limits and quotas, live
traffic accounting that survives restarts, Telegram/webhook alerts, certificate
automation and one-command install/update.

This package installs the Python engine (the whole operator surface: CLI, menu,
panel, metrics, alerts) and ships systemd units for both roles.  /etc/simurgh
is created by the package and only ever written by `simurgh` itself.

%prep
# Nothing to unpack: the build script hands us a staged tree.

%build
# Nothing to compile: the Python engine is pure Python, and the optional Go
# engine is built by tools/build-go.sh (the release packages ship it already
# compiled for this architecture).

%install
rm -rf %{buildroot}
mkdir -p %{buildroot}
if [ -z "%{stage}" ]; then
    echo "pass -D \"stage <staging dir>\" (see packaging/build-packages.sh)" >&2
    exit 1
fi
cp -a %{stage}/. %{buildroot}/

%post
mkdir -p /etc/simurgh
chmod 700 /etc/simurgh
if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
    systemctl daemon-reload >/dev/null 2>&1 || true
    for role in relay exit; do
        if [ -f "/etc/simurgh/$role.toml" ]; then
            systemctl enable --now "simurgh-$role.service" >/dev/null 2>&1 || true
        fi
    done
fi
echo "Simurgh Tunnel installed: 'simurgh init --role relay|exit', or 'simurgh join <link>'."

%preun
if [ "$1" = "0" ] && command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
    for role in relay exit; do
        systemctl stop "simurgh-$role.service" >/dev/null 2>&1 || true
    done
fi

%postun
if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
    systemctl daemon-reload >/dev/null 2>&1 || true
fi

%files
%dir %{_sysconfdir}/simurgh
%license %{_prefix}/share/doc/simurgh/LICENSE
%{_prefix}/share/doc/simurgh/README.md
%{_prefix}/share/doc/simurgh/README.fa.md
%{_prefix}/share/doc/simurgh/CONFIGURATION.md
%{_prefix}/share/doc/simurgh/CONFIGURATION.fa.md
%{_prefix}/share/doc/simurgh/PACKAGING.md
%{_bindir}/simurgh
%if %{defined goarch}
%{_bindir}/simurgh-go
%endif
%{_prefix}/lib/simurgh
%{_prefix}/lib/systemd/system/simurgh-relay.service
%{_prefix}/lib/systemd/system/simurgh-exit.service

%changelog
* Fri Oct 16 2026 Simurgh Tunnel contributors - %{version}-%{release}
- Initial package: CLI, menu, web panel, metrics, alerts, certificates,
  both systemd roles, and the compiled engine when it is staged.
