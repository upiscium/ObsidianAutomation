{
  description = "Development environment for ObsidianAutomation";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { nixpkgs, ... }:
    let
      supportedSystems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      forAllSystems = nixpkgs.lib.genAttrs supportedSystems;
    in
    {
      devShells = forAllSystems (
        system:
        let
          pkgs = import nixpkgs { inherit system; };
          python = pkgs.python311;
          pythonEnv = python.withPackages (ps: [
            ps.pip
            ps.pytest
            ps.setuptools
          ]);
          publicExporter = pkgs.writeShellApplication {
            name = "obsidian-public-export";
            runtimeInputs = [
              pkgs.git
              pythonEnv
            ];
            text = ''
              repo_root="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
              export PYTHONPATH="$repo_root/src''${PYTHONPATH:+:$PYTHONPATH}"
              exec python -m obsidian_automation.public_export "$@"
            '';
          };
        in
        {
          default = pkgs.mkShellNoCC {
            packages = [
              pkgs.git
              pythonEnv
              publicExporter
            ];

            shellHook = ''
              export PYTHONPATH="$PWD/src''${PYTHONPATH:+:$PYTHONPATH}"
            '';
          };
        }
      );
    };
}
