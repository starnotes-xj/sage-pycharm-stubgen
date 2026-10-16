#!/usr/bin/env bash
# Sage 升级 + 桩重生成 + 覆盖率测量, 一条命令完成。
# 用法: bash scripts/upgrade_sage_and_measure.sh [目标版本, 默认 10.10]
#
# 前置: conda-forge 已发布目标版本的 sage 包
# (~/miniconda3/bin/conda search -c conda-forge --override-channels sage)。
# 说明: 升级会替换 sage 包, 已安装的桩与 sidecar 随之失效并在本脚本中重建;
# sidecar 写入 sage-api/<新版本>/, 并额外同步一份到 sage-api/10.9/ 作为过渡
# (插件 1.8.30 支持版本目录发现后可去掉)。
set -euo pipefail

TARGET_VERSION="${1:-10.10}"
CONDA="${CONDA:-$HOME/miniconda3/bin/conda}"
ENV_NAME="${ENV_NAME:-sage}"
ENV_PREFIX="$HOME/miniconda3/envs/$ENV_NAME"
ENV_PY="$ENV_PREFIX/bin/python"
STUBGEN_VERSION="${STUBGEN_VERSION:-sage-pycharm-stubgen==0.9.13}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "== [1/6] conda 升级 sage -> $TARGET_VERSION =="
"$CONDA" update -n "$ENV_NAME" -c conda-forge "sage=$TARGET_VERSION" -y

echo "== [2/6] 确认版本与 stubgen 工具 =="
"$ENV_PY" -c "from sage.env import SAGE_VERSION; print('SageMath:', SAGE_VERSION)"
"$ENV_PY" -m pip install --force-reinstall --no-deps -q "$STUBGEN_VERSION"
"$ENV_PY" -m pip show sage-pycharm-stubgen | grep ^Version

echo "== [3/6] 重生成桩(含运行时审计 + sidecar 自动安装) =="
"$ENV_PY" -m sage_pycharm_stubgen --install --runtime-audit

echo "== [4/6] curated 陈旧检测(升级后哪些精确返回与新版本矛盾) =="
"$ENV_PY" -m sage_pycharm_stubgen conformance --runtime 2>&1 | tail -20 || \
  echo "(conformance --runtime 非零: 逐条核对上方 mismatch 条目并更新 SUPPLEMENTAL_DOCS)"

echo "== [5/6] sidecar 过渡同步(新版本目录 -> 10.9 目录, 插件 1.8.30 后可删) =="
SITE_DIR=$(ls -d "$ENV_PREFIX"/lib/python3*/site-packages | head -1)
if [ -d "$SITE_DIR/sage-api/$TARGET_VERSION" ]; then
  mkdir -p "$SITE_DIR/sage-api/10.9"
  cp -f "$SITE_DIR/sage-api/$TARGET_VERSION/"*.json "$SITE_DIR/sage-api/10.9/"
  echo "已同步 sidecar 到 sage-api/10.9/"
fi

echo "== [6/6] 全语料覆盖率测量 =="
cd "$SITE_DIR"
"$ENV_PY" "$SCRIPT_DIR/coverage_full_corpus.py" .
