# NaviField Open

**为你的仿真问题找到可用的开源求解路线。**

从你想计算的物理量出发，查找适用的开源求解器、需要准备的输入，以及这条路线已有的验证。找到合适的公开案例后，可以用它的模板在本机运行求解器，保存输入、日志和结果，供自己或他人复算。

[快速开始](docs/quick-start.md) · [浏览公开案例](examples/cli-v01/README.md)

[提交论文或仿真需求](https://github.com/navifield/navifield/issues/new?template=simulation-request.yml) · [反馈 Bug](https://github.com/navifield/navifield/issues/new?template=bug-report.yml)

## 查找求解路线

想算压降、壁温或热应力，可以按目标量查询模型的适用条件、必需输入、场之间传递的量和校核方法。已有论文 DOI 时，可以查作者预测了什么、用什么作对照，以及公开记录复算到了哪一步。陌生工况可能只有部分匹配，选定路线前应核对缺少的输入和验证依据。[查看查询命令](docs/quick-start.md#按目标量或论文查询)

## 在本机运行和复算

当前公开版 v0.2.0 提供 Python 命令行工具、离线知识库和案例脚本。从发行页下载源码包，按案例准备本机求解器，再生成输入、运行和读回目标量。每个运行案例都说明如何检查结果，以及能与哪类参考值比较。[下载公开版](https://github.com/navifield/navifield/releases/tag/v0.2.0) · [求解器获取方式](THIRD_PARTY_NOTICES.md) · [运行记录与监控](docs/local-runs.md)

[开源许可证](LICENSE) · [版权与商标声明](NOTICE)
