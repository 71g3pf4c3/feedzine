# Home Manager module для feedzine.
# Принимает flake (self), чтобы default package брался из этого flake
# без IFD и без требования прокидывать пакет через specialArgs.
self:
{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.programs.feedzine;

  # ~/.config/feedzine/config.toml
  tomlFormat = pkgs.formats.toml { };
in
{
  options.programs.feedzine = {
    enable = lib.mkEnableOption "feedzine — RSS/Atom фиды в периодические «журналы» EPUB";

    package = lib.mkOption {
      type = lib.types.package;
      default = self.packages.${pkgs.system}.feedzine;
      defaultText = lib.literalExpression "packages.\${system}.feedzine этого flake";
      description = "Пакет feedzine.";
    };

    settings = lib.mkOption {
      type = tomlFormat.type;
      default = { };
      example = lib.literalExpression ''
        {
          title = "Мой журнал";
          out = "~/Books/feedzine";
          preset = "reader";
          full_text = "auto";
          max_per_feed = 10;
          feed = [
            { name = "Хабр · статьи"; url = "https://habr.com/ru/rss/articles/"; }
            { name = "Хабр · Python"; url = "https://habr.com/ru/rss/hubs/python/"; max = 5; section = "Хабр"; }
            { name = "DTF · главное"; url = "https://dtf.ru/rss/all"; full_text = false; }
          ];
        }
      '';
      description = ''
        Содержимое ~/.config/feedzine/config.toml.
        Список `feed` сериализуется в `[[feed]]` и соответствует полям
        `load_config` из bin/feedzine.py: title, out, workdir, preset,
        full_text, max_per_feed, text_only, feed (name/url/max/section/full_text).
        Пути `out`/`workdir` понимают `~`.

        Пока модуль включён, `feedzine init` запускать не нужно (и не стоит:
        он перепишет управляемый модулем файл).
      '';
    };

    timer = {
      enable = lib.mkEnableOption "systemd user timer — периодические выпуски `feedzine issue`";

      onCalendar = lib.mkOption {
        type = lib.types.str;
        default = "*-*-* 07:00:00";
        example = "hourly";
        description = ''
          systemd-календарь запуска (systemd.time(7)), например
          `*-*-* 07:00:00`, `Mon *-*-* 09:00:00`, `hourly`.
        '';
      };

      persistent = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = "Навёрстывать пропущенные запуски (Persistent=).";
      };
    };
  };

  config = lib.mkIf cfg.enable (
    lib.mkMerge [
      {
        home.packages = [ cfg.package ];

        # Пишем конфиг только если задан settings: пустой {} не должен
        # затирать конфиг, ведённый вручную.
        xdg.configFile."feedzine/config.toml" = lib.mkIf (cfg.settings != { }) {
          source = tomlFormat.generate "config.toml" cfg.settings;
        };

        # Сервис объявляем всегда при enable — выпуск можно дёрнуть и
        # вручную: systemctl --user start feedzine
        systemd.user.services.feedzine = {
          Unit.Description = "feedzine issue";
          Service = {
            Type = "oneshot";
            ExecStart = "${cfg.package}/bin/feedzine issue";
          };
        };
      }

      (lib.mkIf cfg.timer.enable {
        systemd.user.timers.feedzine = {
          Unit.Description = "feedzine issue";
          Timer = {
            OnCalendar = cfg.timer.onCalendar;
            Persistent = cfg.timer.persistent;
          };
          Install.WantedBy = [ "timers.target" ];
        };
      })
    ]
  );
}
