# 运行记录与监控

先按[快速开始](quick-start.md)安装，并按[公开案例](../examples/cli-v01/README.md)配置本机求解器。下列 Windows 命令从发行源码根目录执行。

## 记录、报告和作业控制

```bat
navifield.cmd run --project work\my-project --cwd case --background --cores 8 --wall-seconds 900 -- solver.exe argument1
navifield.cmd wait --project work\my-project --rid r001
navifield.cmd status --project work\my-project --rid r001
navifield.cmd show --project work\my-project --rid r001
navifield.cmd report --project work\my-project --rid r001
navifield.cmd self-check --project work\my-project --rid r001
navifield.cmd monitor --project work\my-project
navifield.cmd stop --project work\my-project --rid r001
```

先在项目下创建 `case` 并放入输入；`--` 后每项为程序和参数，不经过 shell。`--input 项目相对路径` 可重复指定其他项目内输入。运行号以实际输出为准。`report` 和 `self-check` 返回 HTML 路径；`self-check` 可重复 `--rid` 合看多个运行，并以 `--facts`、`--monitor` 或 `--absolute-pressure` 传入用户明确提供的数据，缺失项不会被推断。

`status` 和 `show` 默认返回简短 JSON 及记录/日志路径，`status` 不修改项目；加 `--full` 输出完整机器可读结果。`wait` 通常返回完整一行 JSON；表或残差很多时返回代表性末值及完整 JSON 路径，加 `--full` 输出全部内容。可加 `--timeout 秒`、`--residual-below 值`、`--then-stop` 或 `--stop-on-divergence`；停止只是请求，须确认终态。

`wait` 的退出码为 **0**（运行成功完成）、**3**（返回时仍在运行，例如等待超时或刚发出停止请求）、**1**（其他终态或无法确认状态）。同时读 JSON 的 `state` 和 `reason`。

### 运行监控

`wait` 和 `monitor` 只识别 OpenFOAM 样式日志中的残差。其他求解器仍可查看可识别的监测表和日志末尾。网页最多绘制 8 条残差曲线，其余方程列在数值表中。`monitor` 只监听本机 `127.0.0.1:8790`，按 Ctrl+C 结束；在其他电脑浏览时可通过 SSH 转发该端口。

## 转换 Gmsh 网格

```bat
"%NAVIFIELD_PYTHON%" tools\mesh_convert.py mesh.msh --case work\my-case
```

案例须先有 `system/controlDict` 且不存在 `constant/polyMesh`。转换器默认是 PATH 中的 `gmshToFoam`，可用 `--converter` 指定；完整原生日志写入案例的 `gmshToFoam.log`，已有日志不覆盖，可用 `--log` 指定新文件。检查退出码、网格质量及边界名称、类型和面数。

## 已知局限

- 停止采用进程树终止请求，求解器可能不会写出最新场或安全检查点；状态 `unknown` 不是成功。
- OpenFOAM 残差低于阈值、程序退出0或自检有结果，都不等于结果可交付。仍需检查物理输入、网格、监控量、守恒、网格敏感性及适用参考。
- 运行时仅复制不超过1 MiB的案例文件；大文件记录大小和时间，复制失败会提示。监测表单位不自动推断。
- 梁的解析解、方腔的文献数据和圆管经验关联分别有适用条件；单一网格不能证明网格无关。报告和自检列事实及缺项，不作工程通过判定。
