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
- 桥接 stubgen-pyx 为旧式 Parent 类遗漏的基类，让继承成员
  （`__getitem__`、`_first_ngens` 等）保持可达；
- 补声明静态分析无法发现的 Sage 专属成员（`FiniteField.characteristic`、
  `Integer` 的算术运算符、`CategoryObject._first_ngens`）；
- 为存根补充文档字符串，让 PyCharm 的快速文档（Ctrl+Q）能够说明函数的返回
  值——其中 700 多个 CTF 常用 API（`GF`、`from_integer`/`to_integer`、`log`、
  `discrete_log`、`CRT`、`xgcd` 等）配有中文说明和逐条在 Sage 里实际运行验证
  过的示例；
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
python -m pip install sage-pycharm-stubgen
sage-pycharm-stubgen --install
```

如果要直接安装 GitHub 上的最新开发版本，可以改用：

```bash
python -m pip install "git+https://github.com/starnotes-xj/sage-pycharm-stubgen.git"
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

在 `x.` 后按 `Ctrl+Space`，应能看到 `sqrt` 方法和参数提示；把光标放在函数名上
按 `Ctrl+Q`（快速文档）可以查看它的说明、返回类型和示例。首次安装或升级后若
文档没有生效，执行 **文件 → 使缓存失效/重新启动**。

对于 `.sage` 文件，PyCharm 仍需要合适的文件类型关联；`.py` 文件中的 Sage
语法糖可以通过 [`preparse` 命令](#转换-sage-语法糖)转换为纯 Python。

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
不理解 `R.<x> = PolynomialRing(...)` 等只有 Sage preparser 支持的语法。可以先用
[`preparse` 命令](#转换-sage-语法糖)把这类文件转换为纯 Python。

## 更新与卸载

每次升级 SageMath 后，重新运行同一条命令：

```bash
sage-pycharm-stubgen --install
```

如果不再需要，可以只删除本工具拥有的存根：

```bash
sage-pycharm-stubgen --uninstall
```

如果环境中已有旧工具残留的同名第三方 `.pyi` 文件，安装器默认会保留它们。
需要用 `--install --overwrite-unowned` 显式接管：每个被替换的文件都会备份为
`<名字>.pyi.sps-bak`，执行 `--uninstall` 时自动恢复。

## 转换 Sage 语法糖

Sage preparser 的语法糖（`R.<x> = GF(2)[]`、`F.<a> = GF(2^8, ...)`、`^` 表示
幂、`e^(-1)`）只在 `.sage` 文件里被展开。使用这些语法的 `.py` 文件不是合法
Python，PyCharm 根本无法解析，更不用说建立索引。用一条命令把文件转换为纯
Python：

```bash
sage-pycharm-stubgen preparse test.py
```

文件会被原子地原地改写，并保留 `test.py.preparse-backup` 备份副本。转换会把
生成元声明、幂运算和数字字面量展开成与 Sage 完全一致的语义；当文件中使用了
Sage 符号却没有导入时，还会自动插入 `from sage.all import *`——`.py` 文件没有
`.sage` 文件从 sage 命令获得的隐式命名空间注入。

配合生成的存根，静态分析可以端到端解析转换后的文件：`F` 被识别为
`FiniteField`，`a`、`x` 来自 `_first_ngens`，`from_integer`、`to_integer`、
`polynomial`、`characteristic` 等方法全部可以补全。已用 Pyright 实测：一个
AES 有限域练习文件转换后检查结果为 0 个错误。

选项：

- `--check` — 只报告仍需要转换的文件，有则退出码为 1（适合脚本和 CI）；
- `--output DIR` — 把转换结果写入 `DIR`，不改动原文件；
- `--no-backup` — 不保留 `.preparse-backup` 备份副本。

一次可以转换多个文件：

```bash
sage-pycharm-stubgen preparse a.py b.py c.py
```

## 文档增强

PyCharm 的快速文档（Ctrl+Q）读取的是存根函数的文档字符串**函数体**，因此生成
器会在生成过程中从三个来源（按优先级）修复并填充文档字符串：

1. **精选中文文档**（`supplemental_docs.py`）——700 多个 CTF 常用 API 的中文
   说明、精确返回注解和验证过的 `sage:` 示例（有限域、多项式环、模运算、椭圆
   曲线、矩阵、数论工具），每条例示例都在写入前针对已安装的 Sage 实际运行过。
   用 `python tools/build_supplemental_docs.py <research-output.json>` 重建该
   文件，用 `python tools/merge_supplemental_docs.py <research-output.json>`
   合并新的研究成果。
2. **源码文档字符串**——从已安装的 `.pyx` 源码提取，包括 `cpdef`/`cdef` 函数；
   当存根的导入允许时，Cython 返回类型会把 `-> Any` 升级为具体类型。
3. **运行时文档字符串**——逐个导入活动 Sage 环境中的模块，用
   `inspect.getdoc` 补上源码里没有的文档（继承而来的、装饰器生成的）。这一轮
   导入扫描需要几分钟，可用 `--no-runtime-docs` 关闭。

这一过程还会把 stubgen-pyx 生成在 `def ...: ...` 之后的独立字符串语句移入函数
体——那是 PyCharm 存根索引器唯一认可的文档位置。

## 高级生成选项

如果只想生成一个较小的测试范围而不安装：

```bash
sage-pycharm-stubgen \
  --pattern 'rings/finite_rings/integer_mod.pyx' \
  --output ./sage_typings_test
```

输出目录中包含 `generation-report.json` 和生成的 `sage/` 存根目录树。工厂函数
类型推断详情会写入 `sage/factory-inference.json`，文档增强统计在报告中的
`docstrings` 字段。

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
