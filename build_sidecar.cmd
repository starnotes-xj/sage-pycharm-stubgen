@echo off
cd /d G:\Projects\sage-math-ctf-ide
python tools\sage-api-index\import_wsl_artifact.py ^
  --distro Ubuntu ^
  --conda auto ^
  --conda-env sage ^
  --source-root "\\wsl.localhost\Ubuntu\home\starnotes\miniconda3\envs\sage\sage_typings" ^
  --generation-report "\\wsl.localhost\Ubuntu\home\starnotes\miniconda3\envs\sage\sage_typings\generation-report.json" ^
  --output-dir build\sage-api-real\refresh-0909
