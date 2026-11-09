# Debian package

Run `python3 scripts/build_deb.py` from the checkout. It reads the root `VERSION`
and writes `dist/display-desk_<version>_all.deb`. Only the explicit file allowlist
in the builder enters the package. Building requires Python 3 and `dpkg-deb`;
`python3 -m unittest test_packaging.py` also needs `desktop-file-validate` from
`desktop-file-utils`. No build step installs anything on the host.

The package targets Ubuntu 24.04 or later and compatible Debian derivatives with
GTK 4.12 and libadwaita 1.5 or newer. Ubuntu 22.04's standard libraries are too
old. Niri 25.11 or later is required at runtime, but is a `Recommends` rather
than a `Depends`: this permits upstream or manually installed Niri builds that
have no Debian package registration. Installing the application does not
install or configure the compositor.

The desktop launcher and XDG autostart entry are system-wide. The Python
launcher dispatches `--watch` directly to the standard-library-only backend;
outside a session with `NIRI_SOCKET`, it exits successfully without touching
user state. App settings and display profiles remain under the user's XDG
directories. There are no maintainer scripts and no writes to personal Niri
configuration during package install or removal. The runtime include created
when the user uses the app remains on removal, as does its target file, so an
existing Niri include is not broken by removing the package.

Set `SOURCE_DATE_EPOCH` to the release timestamp if desired. The default is the
fixed release timestamp 2026-11-09 UTC. The builder normalizes modes, ownership and
timestamps; builds with identical inputs and epoch are byte-for-byte identical.
Validate on Ubuntu 24.04 with the CI workflow. Package extraction and launcher
tests do not constitute a live Niri session test.
