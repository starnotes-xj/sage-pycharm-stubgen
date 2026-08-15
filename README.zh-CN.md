# Sage PyCharm Stubgen

[English](README.md) | [简体中文](README.zh-CN.md)

为 SageMath 生成并安装静态类型存根，让 PyCharm、Pyright、Jedi 等支持 Python
的编辑器能够理解 `Mod`、`GF`、`PolynomialRing`、`matrix`、`vector` 等动态
Sage API。

**不需要安装 PyCharm 插件。** 生成的 `.pyi` 文件会安装到当前 Python 环境中
对应的 Sage 运行时模块旁边，因此 PyCharm 的 WSL 解释器可以直接建立索引，
不需要在每个项目中单独添加 Sources Root。

目前已在 WSL 的 SageMath 10.9、Python 3.13 环境中测试。生成器会在运行时检查
当前安装的 Sage 版本和内容，不依赖固定版本的符号列表。

## 解决的问题

SageMath 会动态导出许多对象，并且大量代码由 Cython 编译。因此静态分析器可能
报告以下错误：

```text
在 'all' 中找不到引用 'Mod'
在 'persist' 中找不到引用 'load'
```

静态分析器也可能无法补全工厂函数返回对象的方法：

```python
from sage.all import Mod

x = Mod(5, 29)
x.sqrt(all=True)
```

本项目会：

- 使用 [`stubgen-pyx`](https://github.com/jon-edward/stubgen-pyx) 将已安装的
  `.pyx` 文件转换为 `.pyi`，并尽可能合并对应的 `.pxd` 声明；
- 为动态模块 `sage.all` 生成显式公开导出；
- 根据当前 Sage 环境推断动态工厂函数可导入的返回类型；
- 当单个 Cython 扩展无法解析时，从 Cython 源码解析逐级回退到保守的运行时反射；
- 安装前验证每一个生成的存根；
- 保留 Sage 自带或用户自己创建的 `.pyi` 文件；
- 通过安装清单记录本工具拥有的文件，升级和卸载时不会删除其他文件。

## 环境要求

- 已安装在 Python 或 Conda 环境中的 SageMath
- Python 3.10 或更高版本
- PyCharm 或 VS Code 使用同一个解释器，支持 WSL

所有安装命令都必须使用 Sage 的 Python 解释器运行。普通 Windows Python 无法
检查安装在 WSL 中的 Sage 环境。

## 安装

进入 Sage 环境后运行：

```bash
conda activate sage
python -m pip install "git+https://github.com/starnotes-xj/sage-pycharm-stubgen.git"
sage-pycharm-stubgen --install
```

`--install` 会用一条命令完成生成、严格验证和安装。中间生成目录位于
`<当前环境>/sage_typings`，IDE 使用的存根会安装到 Sage 运行时模块旁边，例如：

```text
<环境>/lib/pythonX.Y/site-packages/sage/all.pyi
<环境>/lib/pythonX.Y/site-packages/sage/misc/persist.pyi
```

本工具不会覆盖 `.py`、`.pyx` 或已编译扩展，也不会覆盖不属于本工具的现有
`.pyi` 文件。

## 配置 PyCharm

1. 选择安装存根时使用的同一个 WSL/Conda Python 解释器。
2. 删除项目中以前手动添加的 `sage_typings` Sources Root。
3. 刷新解释器的软件包列表。
4. 如果旧错误仍留在缓存中，执行 **文件 → 使缓存失效/重新启动**。

之后，所有使用该解释器的项目都能共享这些存根，不需要安装自定义 PyCharm
插件。

测试代码：

```python
from sage.all import Mod

x = Mod(5, 29)
x.sqrt(all=True)
```

在 `x.` 后按 `Ctrl+Space`，应能看到 `sqrt` 方法和参数提示。

对于 `.sage` 文件，PyCharm 仍需要合适的文件类型关联或用于运行 Sage preparser
的工具。本项目提供的是 Python 类型信息，不是 Sage preparser 语言插件。

## 配置 VS Code

1. 安装官方的 **WSL**、**Python** 和 **Pylance** 扩展。
2. 在 WSL 窗口中打开项目，例如在 Ubuntu 中运行 `code .`。
3. 选择安装存根时使用的同一个 Sage Python 解释器。
4. 打开 Python 文件，在上面示例的 `x.` 后请求代码补全。

生成的存根已经使用 Pyright 1.1.411 实际验证：它能把 `x` 识别为
`IntegerMod_abstract`，显示 `sqrt` 的函数签名，并且能正确解析
`sage.misc.persist.load`，检查结果为 0 个错误。使用 Pylance 时无需再单独安装
Pyright 命令行程序。

把 `*.sage` 关联为 Python 文件后，可以获得普通 Python API 的补全，但 Pylance
不理解 `R.<x> = PolynomialRing(...)` 等只有 Sage preparser 支持的语法。本项目
是类型存根生成器，不是完整的 `.sage` 语言服务器。

## 更新与卸载

每次升级 SageMath 后，重新运行同一条命令：

```bash
sage-pycharm-stubgen --install
```

如果不再需要，可以只删除本工具拥有的存根：

```bash
sage-pycharm-stubgen --uninstall
```

## 高级生成选项

如果只想生成一个较小的测试范围而不安装：

```bash
sage-pycharm-stubgen \
  --pattern 'rings/finite_rings/integer_mod.pyx' \
  --output ./sage_typings_test
```

输出目录中包含 `generation-report.json` 和生成的 `sage/` 存根目录树。工厂函数
类型推断详情会写入 `sage/factory-inference.json`。

## 限制

任何静态存根生成器都无法保证为所有动态 Python 调用得到唯一且完全精确的类型。
函数可能根据参数值、插件、文件、网络状态或运行时创建的类选择返回类型。本工具
采取的策略是：严格安装时，每个检测到的工厂函数都必须具有可导入的静态返回
类型；如果一个工厂存在多种实现，则使用这些实现共同的、可以导入的基类。

## 开发

```bash
python -m pip install -e .
python -m unittest discover -s tests -v
python -m compileall -q src tests
```

## 许可证

MIT
