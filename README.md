# Sage PyCharm Stubgen

[English](README.md) | [简体中文](README.zh-CN.md)

Generate and install static type stubs for SageMath so that PyCharm, Pyright,
Jedi, and other Python-aware editors can understand dynamic Sage APIs such as
`Mod`, `GF`, `PolynomialRing`, `matrix`, and `vector`.

**No PyCharm plugin is required.** The generated `.pyi` files are installed
next to the Sage runtime modules in the active Python environment, which makes
them visible to PyCharm's WSL interpreter indexer without adding a Sources
Root to every project.

Tested with SageMath 10.9 on Python 3.13 in WSL. The generator discovers the
installed Sage version at runtime instead of relying on a fixed symbol list.

## What it fixes

SageMath exposes many objects dynamically and ships a large amount of compiled
Cython code. A static analyzer may therefore report errors such as:

```text
Cannot find reference 'Mod' in 'all'
Cannot find reference 'load' in 'persist'
```

It may also fail to complete methods returned by factories:

```python
from sage.all import Mod

x = Mod(5, 29)
x.sqrt(all=True)
```

This project:

- converts installed `.pyx` files, with matching `.pxd` declarations when
  possible, into `.pyi` files using
  [`stubgen-pyx`](https://github.com/jon-edward/stubgen-pyx);
- generates explicit exports for the dynamic `sage.all` module;
- infers importable return types for dynamic factories from the active Sage
  environment;
- falls back from Cython source parsing to conservative runtime reflection when
  an individual extension cannot be parsed;
- validates every generated stub before installation;
- preserves existing Sage-owned or user-owned `.pyi` files;
- tracks its own files in a manifest so updates and uninstallations only touch
  files owned by this tool.

## Requirements

- SageMath installed in a Python/Conda environment
- Python 3.10 or newer
- PyCharm configured to use that same interpreter (WSL is supported)

Run all installation commands with Sage's Python interpreter. A normal Windows
Python installation cannot inspect a Sage environment installed in WSL.

## Install

Inside the Sage environment:

```bash
conda activate sage
python -m pip install sage-pycharm-stubgen
sage-pycharm-stubgen --install
```

To install the current development version directly from GitHub instead, use:

```bash
python -m pip install "git+https://github.com/starnotes-xj/sage-pycharm-stubgen.git"
```

`--install` performs generation, strict validation, and installation in one
command. The generated build tree is stored at
`<current-environment>/sage_typings`, while the stubs used by the IDE are
placed beside the installed Sage modules, for example:

```text
<environment>/lib/pythonX.Y/site-packages/sage/all.pyi
<environment>/lib/pythonX.Y/site-packages/sage/misc/persist.pyi
```

The tool never overwrites `.py`, `.pyx`, or compiled extension files. It also
does not overwrite pre-existing `.pyi` files that it does not own.

## Configure PyCharm

1. Select the same WSL/Conda Python interpreter used above.
2. Remove any manually added `sage_typings` Sources Root from the project.
3. Refresh the interpreter package list.
4. If an old error remains cached, use **File → Invalidate Caches / Restart**.

The stubs then apply to every project using that interpreter. No custom
PyCharm plugin is needed. For `.sage` files, PyCharm still needs an appropriate
file-type association; this project provides Python type information rather
than a Sage preparser language plugin.

## Configure VS Code

1. Install the official **WSL**, **Python**, and **Pylance** extensions.
2. Open the project in a WSL window, for example by running `code .` in Ubuntu.
3. Select the same Sage Python interpreter used to install the stubs.
4. Open a Python file and request completion after `x.` in the example above.

The generated stubs have been verified with Pyright 1.1.411: it resolves `x`
as `IntegerMod_abstract`, exposes the `sqrt` signature, and resolves
`sage.misc.persist.load` without errors. VS Code users do not need to install
the Pyright CLI separately when using Pylance.

Associating `*.sage` with the Python language can provide ordinary Python API
completion, but Pylance does not understand Sage preparser-only syntax such as
`R.<x> = PolynomialRing(...)`. This project is a type-stub generator, not a
complete `.sage` language server.

## Update and uninstall

Run the same command after upgrading SageMath:

```bash
sage-pycharm-stubgen --install
```

Remove only the stubs owned by this tool:

```bash
sage-pycharm-stubgen --uninstall
```

## Advanced generation

To generate a small test subset without installing it:

```bash
sage-pycharm-stubgen \
  --pattern 'rings/finite_rings/integer_mod.pyx' \
  --output ./sage_typings_test
```

The output contains a `generation-report.json` and the generated `sage/` stub
tree. Factory inference details are written to
`sage/factory-inference.json`.

## Limitations

No static stub generator can promise one perfectly precise type for every
possible dynamic Python call. A function may choose its return type from
argument values, plugins, files, network state, or classes created at runtime.
This tool instead requires every detected factory to have an importable static
return type during strict installation. Factories with several implementations
use a common importable base class whose members are valid for the observed
implementations.

## Development

```bash
python -m pip install -e .
python -m unittest discover -s tests -v
python -m compileall -q src tests
```

## License

MIT
