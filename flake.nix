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

      # Bumps qemu from 10.2.2 to 11.0.1, drawn from
      # https://github.com/NixOS/nixpkgs/pull/502485 ("qemu: 10.2.2 -> 11.0.0")
      # but pinned to the v11.0.1 release tarball. v11.0.0 contains the fix
      # for the HVF aarch64 SIGIPI-loss race in
      # `hvf_wait_for_ipi` (commits b5f8f7727 + a14afa985 + 6ca499b79 +
      # 7f359375), which causes EDK2 SMP bringup wedges on Apple Silicon
      # hosts running Windows-on-aarch64 guests. v11.0.1 also contains
      # 3b98370b55, the stable backport of the HVF WFI idle-spin fix.
      nixpkgsFixOverlay =
        final: prev:
        let
          # Apply the v11.0.1 src + Meson-test build-input bump to whichever
          # qemu derivation we're given (plain `qemu` and `qemu_full` share a
          # single source).
          bumpQemu =
            drv:
            drv.overrideAttrs (old: {
              version = "11.0.1";
              src = prev.fetchurl {
                url = "https://download.qemu.org/qemu-11.0.1.tar.xz";
                hash = "sha256-DSNfWCAnjZFKMVXsJ6+OQljWl+qJKJVXCAfWnAy4zWQ=";
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
                (old.buildInputs or [ ]) ++ prev.lib.optional prev.stdenv.hostPlatform.isDarwin prev.apple-sdk_26;
            });
        in
        {
          qemu = bumpQemu prev.qemu;
          # Re-derive qemu_full from final.qemu so the overrideAttrs
          # additions in bumpQemu (src, patches, build inputs) are applied
          # exactly once. Going through prev.qemu_full.override then
          # bumpQemu would apply the patches list twice, because the
          # all-packages.nix expression for qemu_full (qemu.override { ... })
          # resolves `qemu` to final.qemu under the overlay and re-applies
          # bumpQemu's overrideAttrs as part of the .override re-evaluation.
          qemu_full = final.qemu.override (
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
          );
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
