{
  description = "feedzine — RSS/Atom фиды в периодические «журналы» EPUB";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs =
    {
      self,
      nixpkgs,
      flake-utils,
    }:
    flake-utils.lib.eachDefaultSystem (
      system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
        feedzine = pkgs.stdenv.mkDerivation rec {
          pname = "feedzine";
          version = "0.3.0";

          src = self;

          nativeBuildInputs = [ pkgs.makeWrapper ];

          installPhase = ''
            runHook preInstall

            install -Dm755 bin/feedzine.py $out/share/feedzine/feedzine.py
            install -Dm644 bin/eink.css $out/share/feedzine/eink.css
            makeWrapper ${pkgs.python3.withPackages (ps: [ ps.pillow ])}/bin/python3 $out/bin/feedzine \
              --add-flags "$out/share/feedzine/feedzine.py" \
              --prefix PATH : ${pkgs.lib.makeBinPath [
                pkgs.pandoc
              ]}

            runHook postInstall
          '';

          meta = with pkgs.lib; {
            description = "RSS/Atom feeds to periodic EPUB journals";
            license = licenses.mit;
            mainProgram = "feedzine";
            platforms = platforms.unix;
          };
        };
      in
      {
        packages.default = feedzine;
        packages.feedzine = feedzine;

        apps.default = {
          type = "app";
          program = "${feedzine}/bin/feedzine";
        };

        checks.unit-tests = pkgs.runCommand "feedzine-unit-tests" {
          nativeBuildInputs = [
            (pkgs.python3.withPackages (ps: [
              ps.pytest
              ps.pillow
            ]))
            pkgs.pandoc
          ];
        } ''
          cp -r ${self} src
          chmod -R u+w src
          cd src && pytest tests -q --no-header
          touch $out
        '';

        devShells.default = pkgs.mkShell {
          packages = [
            (pkgs.python3.withPackages (ps: [
              ps.pytest
              ps.pillow
            ]))
            pkgs.pandoc
          ];
        };
      }
    );
}
