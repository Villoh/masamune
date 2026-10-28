{
  description = "Masamune terminal UI for building Morphe artifacts from local APKs";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    goopdl = {
      url = "github:Villoh/goopdl";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    { nixpkgs, ... }@inputs:
    let
      forAllSystems =
        f:
        nixpkgs.lib.genAttrs [ "x86_64-linux" "aarch64-linux" ] (
          system:
          f (
            import nixpkgs {
              inherit system;
              overlays = [ inputs.goopdl.overlays.default ];
            }
          )
        );
    in
    {
      packages = forAllSystems (pkgs: rec {
        masamune =
          let
            # Not in nixpkgs.
            axml = pkgs.python313Packages.buildPythonPackage rec {
              pname = "axml";
              version = "0.0.2";
              pyproject = true;
              src = pkgs.fetchPypi {
                inherit pname version;
                hash = "sha256-TwJEr63aKZe2zgRdvaRI78wSCs0Fr4W/iqS4bPz56z4=";
              };
              build-system = [ pkgs.python313Packages.setuptools ];
              dependencies = with pkgs.python313Packages; [
                rich
                lxml
              ];
              pythonImportsCheck = [ "axml" ];
            };
            # The TUI is written against this exact Textual; nixpkgs' newer one breaks it.
            textual = pkgs.python313Packages.textual.overridePythonAttrs (old: rec {
              version = "6.6.0";
              src = pkgs.fetchFromGitHub {
                owner = "Textualize";
                repo = "textual";
                tag = "v${version}";
                hash = "sha256-gJWgGyPfSBxZRVuWvcehDs+hr2gmRsL6tleYCVPOPg4=";
              };
              doCheck = false;
            });
          in
          pkgs.python313Packages.buildPythonApplication {
            pname = "masamune-tui";
            # One source of truth for the version: pyproject.toml.
            version = (builtins.fromTOML (builtins.readFile ./pyproject.toml)).project.version;
            pyproject = true;
            src = ./.;
            build-system = [ pkgs.python313Packages.setuptools ];
            # pyproject.toml pins exact versions for uv.lock; nixpkgs has its own.
            pythonRelaxDeps = [
              "goopdl"
              "tomlkit"
            ];
            dependencies = with pkgs.python313Packages; [
              axml
              goopdl
              rich
              textual
              tomlkit
            ];
            # Morphe CLI, APKEditor and uber-apk-signer are jars run with `java`.
            makeWrapperArgs = [
              "--prefix"
              "PATH"
              ":"
              (pkgs.lib.makeBinPath [ pkgs.jdk21_headless ])
            ];
            checkPhase = "python -m unittest discover -s tests";
            pythonImportsCheck = [ "masamune" ];
            meta.mainProgram = "masamune";
          };
        default = masamune;
      });
    };
}
