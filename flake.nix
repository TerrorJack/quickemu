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

      nixpkgsFixOverlay =
        final: prev:
        let
          bumpQemu =
            drv:
            drv.overrideAttrs (old: {
              version = "11.1.1";
              src = final.fetchurl {
                url = "https://download.qemu.org/qemu-11.1.1.tar.xz";
                hash = "sha256-B5/7/4pxEbvIkCIQfLq/O7/WFNX8nXzGdZkRlqyhJII=";
              };

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
              cephSupport = false;
              glusterfsSupport = false;
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
