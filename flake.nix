{
  description = "Quickemu flake";
  inputs = {
    flake-schemas.url = "https://flakehub.com/f/DeterminateSystems/flake-schemas/*.tar.gz";
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs =
    {
      self,
      flake-schemas,
      nixpkgs,
    }:
    let
      supportedSystems = [
        "x86_64-linux"
        "x86_64-darwin"
        "aarch64-darwin"
        "aarch64-linux"
      ];

      # Inline backport of https://github.com/NixOS/nixpkgs/pull/513104
      # (acpica-tools + OVMF: fix build on aarch64-darwin) so OVMF/OVMFFull
      # become available on every supported system, not just Linux.
      #
      # Also bumps qemu from 10.2.2 to 11.0.0, drawn from
      # https://github.com/NixOS/nixpkgs/pull/502485 ("qemu: 10.2.2 -> 11.0.0")
      # but pinned to the v11.0.0 release tarball instead of -rc4. v11.0.0
      # contains the fix for the HVF aarch64 SIGIPI-loss race in
      # `hvf_wait_for_ipi` (commits b5f8f7727 + a14afa985 + 6ca499b79 +
      # 7f359375), which causes EDK2 SMP bringup wedges on Apple Silicon
      # hosts running Windows-on-aarch64 guests.
      nixpkgsFixOverlay =
        final: prev:
        let
          # Apply the v11.0.0 src + Meson-test build-input bump to whichever
          # qemu derivation we're given (plain `qemu` and `qemu_full` share a
          # single source).
          bumpQemu = drv: drv.overrideAttrs (old: {
            version = "11.0.0";
            src = prev.fetchurl {
              url = "https://download.qemu.org/qemu-11.0.0.tar.xz";
              hash = "sha256-wEyjYBJlPzLRHGdNNwz1KnEOfT8Ywti2PkkyBSpIVNY=";
            };
            # v11.0.0 reorganised the test harness around Meson; the build
            # now needs setuptools+wheel at build time and pygdbmi+qemu-qmp
            # for the check phase. Same additions as PR #502485.
            nativeBuildInputs = (old.nativeBuildInputs or [ ]) ++ [
              prev.python3Packages.setuptools
              prev.python3Packages.wheel
            ];
            checkInputs = (old.checkInputs or [ ]) ++ [
              prev.python3Packages.pygdbmi
              prev.python3Packages.qemu-qmp
            ];
            # On Darwin, build against macOS SDK >= 15.2 so qemu picks up the
            # real Hypervisor.framework SME2 declarations from
            # target/arm/hvf_arm.h instead of falling back to the stub in
            # target/arm/hvf/hvf_sme_stubs.h. With the stub, qemu's
            # hvf_arch_init_vcpu g_assert (sysreg.c.inc:149) compares
            # HV_SYS_REG_SMCR_EL1 (= 0 from the stub enum) against the real
            # KVMID-derived value (non-zero) and aborts on every M1/M2 host
            # running macOS >= 15.2.
            buildInputs =
              (old.buildInputs or [ ])
              ++ prev.lib.optional prev.stdenv.hostPlatform.isDarwin prev.apple-sdk_26;
            patches = (old.patches or [ ]) ++ [
              # qemu commit b5f8f77271 ("Implement WFI without using
              # pselect()") fixed a SIGIPI-loss race but introduced a
              # regression: aarch64 hvf_wfi() returns EXCP_HLT without
              # setting cpu->halted=1, and there is no host-side wakeup
              # mechanism for the guest virtual timer (HVF only delivers
              # HV_EXIT_REASON_VTIMER_ACTIVATED inside hv_vcpu_run, which
              # an idle vCPU never re-enters). Result: 100% host CPU per
              # vCPU on an otherwise idle guest.
              #
              # Tracked upstream as
              #   https://gitlab.com/qemu-project/qemu/-/issues/3433
              # Patch in flight (reviewed by Peter Maydell, not merged as
              # of v11.0.0; queued in philmd's pre-v11.1 collection
              # message-id 20260423170229.64655-1-philmd@linaro.org as
              # 13/16). Source mbox:
              #   https://patchew.org/QEMU/20260427195516.46256-1-scottjgo@gmail.com/
              #   "[PATCH v3] target/arm/hvf: Fix WFI halting to stop
              #    idle vCPU spinning" -- Scott J. Goldman, 2026-04-27.
              ./patches/qemu-hvf-arm-wfi-halt.patch
            ];
          });
        in
        {
          acpica-tools = prev.acpica-tools.overrideAttrs (old: {
            env = (old.env or { }) // {
              NIX_LDFLAGS =
                ((old.env or { }).NIX_LDFLAGS or "")
                + final.lib.optionalString
                  (final.stdenv.hostPlatform.isDarwin && final.stdenv.hostPlatform.isAarch64)
                  " -no_fixup_chains";
              INSTALLFLAGS = final.lib.optionalString (!final.stdenv.hostPlatform.isDarwin) "-m 555";
            };
          });
          qemu = bumpQemu prev.qemu;
          # Re-derive qemu_full from final.qemu so the overrideAttrs
          # additions in bumpQemu (src, patches, build inputs) are applied
          # exactly once. Going through prev.qemu_full.override then
          # bumpQemu would apply the patches list twice, because the
          # all-packages.nix expression for qemu_full (qemu.override { ... })
          # resolves `qemu` to final.qemu under the overlay and re-applies
          # bumpQemu's overrideAttrs as part of the .override re-evaluation.
          qemu_full = prev.lib.lowPrio (
            final.qemu.override (
              {
                cephSupport = prev.lib.meta.availableOn prev.stdenv.hostPlatform prev.ceph;
                glusterfsSupport =
                  prev.lib.meta.availableOn prev.stdenv.hostPlatform prev.glusterfs
                  && prev.lib.meta.availableOn prev.stdenv.hostPlatform prev.libuuid;
              }
              // (
                if final.stdenv.hostPlatform.isDarwin then
                  { smbdSupport = false; }
                else
                  { smbdSupport = prev.lib.meta.availableOn prev.stdenv.hostPlatform prev.samba; }
              )
            )
          );
          OVMF = prev.OVMF.overrideAttrs (old: {
            meta = old.meta // {
              broken = false;
            };
          });
          OVMFFull = prev.OVMFFull.overrideAttrs (old: {
            meta = old.meta // {
              broken = false;
            };
          });
        };

      pkgsFor =
        system:
        import nixpkgs {
          inherit system;
          overlays = [ nixpkgsFixOverlay ];
        };

      forEachSupportedSystem =
        f:
        nixpkgs.lib.genAttrs supportedSystems (
          system:
          f {
            system = system;
            pkgs = pkgsFor system;
          }
        );
    in
    {
      schemas = flake-schemas.schemas;

      overlays = {
        default = nixpkgs.lib.composeManyExtensions [
          nixpkgsFixOverlay
          (final: prev: {
            quickemu = final.callPackage ./package.nix { };
          })
        ];
      };

      packages = forEachSupportedSystem (
        { pkgs, system, ... }:
        rec {
          quickemu = pkgs.callPackage ./package.nix { };
          default = quickemu;
        }
      );

      devShells = forEachSupportedSystem (
        { pkgs, system, ... }:
        {
          default = pkgs.callPackage ./devshell.nix { };
        }
      );
    };
}
