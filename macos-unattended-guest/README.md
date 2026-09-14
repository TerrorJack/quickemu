These files supply the guest half of unattended macOS Tahoe installation.
`build.py` creates `recovery.sh`, a scripts-only `setup.pkg`, and an empty
`state/` directory. It uses Python's standard library to write the XAR product
archive and gzip/cpio component scripts, so no macOS packaging tools are needed
on the host.

```sh
python3 macos-unattended-guest/build.py \
    --output /path/to/seed-files \
    --username quickemu \
    --password-file /path/to/password \
    --public-key-file /path/to/id_ed25519.pub
```

Copy the generated files into a writable FAT volume named `QUICKEMU`. Attach
the blank installation disk with QEMU serial `QUICKEMU_SYSTEM`. From Recovery
Terminal, run:

```sh
/bin/bash /Volumes/QUICKEMU/recovery.sh
```

The recovery script identifies the whole target disk through its IORegistry
serial, refuses existing partition maps and repeated erasure, formats APFS,
and invokes Tahoe's `startosinstall --volume` with the generated package.
An interrupted installation can resume only with its installation marker and
recorded APFS container UUID. The script verifies that container belongs to
the identified physical disk and reruns the installer on its existing
`Macintosh HD` volume without erasing it. It refuses completed or failed
provisioning stages, a different container, ambiguous volumes, and an
interruption before the container UUID was recorded.
The package installs a first-boot LaunchDaemon on the destination volume and
suppresses Setup Assistant. It requires a restart so launchd loads the daemon
even when the package is installed after launchd's initial scan. When running
inside the installed system, it also activates the daemon immediately; Recovery
and offline targets are excluded. On the installed system, that daemon creates the
administrator, enables SSH with the supplied key and password, configures
passwordless sudo and automatic login, and disables sleeping.

`state/status` on the seed volume progresses through `recovery-ready`,
`installing`, `provisioned`, `configuring`, and `complete`. Failures are reported
as `recovery-failed` or `firstboot-failed`. Recovery logs are written to
`state/recovery.log`, and Apple's installer log is mirrored to `state/install.log`;
`state/postinstall.log` records the package target and activation context. The installed system writes
`/var/log/quickemu-firstboot.log` and copies it to `state/firstboot.log` on
completion or failure. Successful provisioning also creates
`/var/db/quickemu-unattended-complete` containing `sw_vers` output. The host
should verify that marker and the running version through SSH before declaring
installation successful.

The seed package contains the configured password. The generated directory is
private to the host user; the seed image must also be private and should be
detached after installation. The guest removes its temporary password settings
after provisioning; macOS retains its normal automatic-login password file.

Run the package-format and disk-erasure guard regressions with
`python3 macos-unattended-guest/test_guest.py`.
