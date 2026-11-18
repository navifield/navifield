# 第三方程序与许可证

NaviField CLI 的许可见 `LICENSE` 和 `NOTICE`。本工具箱提供案例输入、结果检查和环境配置脚本。运行案例前，请从相应发行方下载所需程序，保留原发行包的许可、声明和配套运行库。CLI 将求解器作为独立程序调用。

## Windows 示例使用的原生程序

| 组件 | 获取方式 | 许可和用法 |
|---|---|---|
| OpenFOAM v2606，Windows MinGW x64 | 从 OpenFOAM 下载 `OpenFOAM-v2606-windows-mingw.exe`。[官方安装程序](https://dl.openfoam.com/source/v2606/OpenFOAM-v2606-windows-mingw.exe) · [同版源码](https://dl.openfoam.com/source/v2606/) | OpenFOAM 采用 GNU GPL v3 或更高版本。方腔、圆管使用此发行版的可执行程序和配套 DLL。[上游许可说明](https://www.openfoam.com/documentation/licencing) |
| CalculiX CrunchiX 2.22，Windows x64 | 梁例使用 FreeCAD 官方 1.1.1 压缩包 `FreeCAD_1.1.1-Windows-x86_64-py311.7z` 中的 `bin/ccx.exe` 和同目录依赖。[官方压缩包](https://github.com/FreeCAD/FreeCAD/releases/download/1.1.1/FreeCAD_1.1.1-Windows-x86_64-py311.7z) · [发行页](https://github.com/FreeCAD/FreeCAD/releases/tag/1.1.1) | CalculiX 采用 GNU GPL v2 或更高版本。解压后设置 `NAVIFIELD_CCX_BIN` 即可，运行梁例无需启动 FreeCAD。[CalculiX 上游与源码](https://www.dhondt.de/) |
| Microsoft MPI Runtime v10.1.2（10.1.12498.18），Windows x64 | OpenFOAM 上述安装程序含 `thirdParty/msmpisetup.exe`。微软也提供运行时 `msmpisetup.exe`；仅运行八进程例子时请选择 Runtime。[Microsoft 官方下载](https://www.microsoft.com/en-us/download/details.aspx?id=100593) | 使用安装程序附带的 Microsoft 许可条款和第三方声明。配置时使用同版的 `mpiexec.exe` 和 `msmpi.dll`。 |
| MinGW 运行库 | OpenFOAM 安装程序和 FreeCAD 压缩包各自包含其构建使用的运行库。使用上面对应发行包提供的 DLL，保留各自的目录。 | 各库的条款见各自发行包，包括适用的 GCC Runtime Library Exception 与 mingw-w64 各组件的许可。[GCC 运行库例外](https://www.gnu.org/licenses/gcc-exception-3.1.html) · [mingw-w64 许可](https://www.mingw-w64.org/support/#licensing) |

Windows MinGW 发行文件标为 v2606。该发行文件中的求解器在实测时同时报告了 Build `34e226dfe3-20211220`。复现实例时请记录两个标识；与 v2606 源码逐项对应的构建配方尚无记录。

串行算例可直接使用对应的安装目录。若 OpenFOAM 默认使用 dummy 通信库，按 README 的八进程配置在自己的电脑上选择同一发行包提供的 MS-MPI 变体。此 ZIP 不含求解器、MPI、MinGW 二进制文件或它们的安装程序。

## Python 及其依赖

| 组件 | 用途 | 上游许可 |
|---|---|---|
| Python | 解释器，由用户安装 | [Python 软件基金会许可证及随附声明](https://docs.python.org/3/license.html) |
| psutil | 本地作业与进程管理 | [BSD-3-Clause](https://github.com/giampaolo/psutil/blob/master/LICENSE) |
| Pydantic / pydantic-core | 运行记录模型 | [Pydantic MIT](https://github.com/pydantic/pydantic/blob/main/LICENSE) · [pydantic-core MIT](https://github.com/pydantic/pydantic-core/blob/main/LICENSE) |
| NumPy | 可选，场数据数组 | [BSD-3-Clause 及随附组件声明](https://numpy.org/doc/stable/license.html) |
| PyVista | 可选，读取与绘制网格 | [MIT](https://github.com/pyvista/pyvista/blob/main/LICENSE) |
| VTK | 可选，网格读取、截面与渲染 | [BSD-3-Clause 及第三方组件说明](https://vtk.org/about/) |
| Matplotlib | 可选，绘图 | [Matplotlib 许可证及历史版权声明](https://matplotlib.org/stable/project/license.html) |

安装基础版或 `plots` 可选组时，包管理器会解析相应的 Python 依赖。各安装包的许可证适用于其中的代码及其随附组件。OpenFOAM® 为其商标持有方的商标，本项目与其无关联，未获其认可。
