{
  description = "feedzine — RSS/Atom фиды в периодические «журналы» EPUB";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
    home-manager = {
      url = "github:nix-community/home-manager";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    {
      self,
      nixpkgs,
      flake-utils,
      home-manager,
    }:
    flake-utils.lib.eachDefaultSystem (
      system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
        # рантайм-зависимости питона: единый список для пакета, тестов и devShell
        pyEnv = ps: with ps; [
          pillow
          typer
          rich
          textual
          httpx
          pydantic
          fonttools              # покрытие глифов шрифта обложки (нет tofu)
        ];
        feedzine = pkgs.stdenv.mkDerivation rec {
          pname = "feedzine";
          version = "0.11.0";

          src = self;

          nativeBuildInputs = [ pkgs.makeWrapper ];

          installPhase = ''
            runHook preInstall

            install -Dm755 bin/feedzine.py $out/share/feedzine/feedzine.py
            install -Dm644 bin/eink.css $out/share/feedzine/eink.css
            install -Dm644 ${pkgs.dejavu_fonts}/share/fonts/truetype/DejaVuSerif-Bold.ttf \
              $out/share/feedzine/fonts/cover.ttf
            makeWrapper ${pkgs.python3.withPackages pyEnv}/bin/python3 $out/bin/feedzine \
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

        # Минимальная конфигурация Home Manager с включённым модулем:
        # проверяет eval модуля и сборку сгенерированного config.toml
        # (файл попадает в home-files, значит реально собирается).
        hm-module-check = home-manager.lib.homeManagerConfiguration {
          inherit pkgs;
          modules = [
            self.homeManagerModules.feedzine
            {
              home.username = "ci";
              home.homeDirectory = "/home/ci";
              home.stateVersion = "25.11";
              programs.feedzine = {
                enable = true;
                settings = {
                  title = "CI";
                  out = "/tmp/feedzine-out";
                  feed = [
                    {
                      name = "Хабр · тест";
                      url = "https://habr.com/ru/rss/articles/";
                    }
                  ];
                };
                timer.enable = true;
              };
            }
          ];
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
            (pkgs.python3.withPackages (ps:
              pyEnv ps ++ [ ps.pytest ]
            ))
            pkgs.pandoc
          ];
        } ''
          cp -r ${self} src
          chmod -R u+w src
          cd src && pytest tests -q --no-header
          touch $out
        '';

        checks.hm-module = hm-module-check.activationPackage;

        devShells.default = pkgs.mkShell {
          packages = [
            (pkgs.python3.withPackages (ps:
              pyEnv ps ++ [ ps.pytest ]
            ))
            pkgs.pandoc
          ];
          # чтобы make_cover в dev-окружении рендерил тем же шрифтом, что и пакет
          FEEDZINE_FONT = "${pkgs.dejavu_fonts}/share/fonts/truetype/DejaVuSerif-Bold.ttf";
        };
      }
    )
    // {
      homeManagerModules.feedzine = import ./nix/hm-module.nix self;
      homeManagerModules.default = self.homeManagerModules.feedzine;
    };
}
