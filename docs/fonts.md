# 本地字体

界面使用 Noto Sans SC 400、500，标题和阅读正文使用 Noto Serif SC 700，数字与代码使用 Fira Code 400、500。字体家族和回退顺序沿用 DESIGN.md §2，字体定义由 `src/frontend/src/font-faces.css` 提供。所有字体均随安装包提供，页面通过同源地址读取，运行时没有外部字体请求。

中文字体的常用片包含 GB2312 一级汉字、拉丁字符及常用标点。其余原有字符每 512 个字形成一片，每片的 `unicode-range` 与实际 cmap 一致，浏览器只在页面含对应字符时请求。原 Noto Sans SC 每个字重的 30,890 个字符、Noto Serif SC 的 30,928 个字符全部保留，原有提示、字宽和 OpenType 布局特性继续保留。

原始中文字体没有 U+20BB7 𠮷。对应的 Noto Sans CJK SC Regular、Medium 和 Noto Serif CJK SC Bold 完整字体提供该字符，各生成一个按需加载的单字补充片。U+9F98 龘仍来自原有字体的非常用片。未被这些字体覆盖的字符继续按原 CSS 字体顺序读取系统字体。

原 Fira Code 两份静态文件的字节、轮廓和左边距存在差异。现在使用官方同版本 5.002 的可变字体，将字重范围限制为 400 至 500，生成一个 WOFF2 文件，两个设计字重共用该文件。Fraunces 没有生产引用，对应字体及专用许可已移除。其余原许可继续随包提供，CJK 补充字体许可见 `public/fonts/OFL-Noto-CJK.txt`。

生成后的三个中文样式共 92,708 个原字符映射已逐项比较轮廓与字宽，结果与原 TTF 一致。Fira Code 400 的 1,551 个字符映射也完全一致；500 的全部字宽与轮廓结构一致，1,225 个字符存在可变字体坐标精度差异，最大为 2.4062 个字体单位，以 15px 字号计算约为 0.0181px。外观对照仍列入集中验收。

## 生成与核验

生成工具使用 fontTools 和 Brotli，不属于应用运行依赖。请在独立工具环境中安装，避免修改项目公共依赖：

```powershell
uv venv work/font-tools
uv pip install --python work/font-tools/Scripts/python.exe fonttools==4.67.0 brotli==1.2.1
```

源文件放在专用工作目录，通过 `--source-dir` 指定。原有三个 Noto TTF 可以从 R3 修改前的 Git 版本导出；仓库中保留生成后的 WOFF2，不重复保存原始大文件。生成所需源文件如下：

| 文件名 | 来源 |
| --- | --- |
| `noto-sans-sc-400.ttf`、`noto-sans-sc-500.ttf`、`noto-serif-sc-700.ttf` | 本项目 R3 修改前的 `src/frontend/public/fonts` |
| `fira-code-variable.ttf` | [Google Fonts 的 Fira Code 可变字体](https://github.com/google/fonts/blob/main/ofl/firacode/FiraCode%5Bwght%5D.ttf)，Version 5.002 |
| `noto-sans-cjk-sc-400.otf` | [Noto Sans CJK SC Regular](https://github.com/notofonts/noto-cjk/blob/main/Sans/OTF/SimplifiedChinese/NotoSansCJKsc-Regular.otf)，Version 2.004 |
| `noto-sans-cjk-sc-500.otf` | [Noto Sans CJK SC Medium](https://github.com/notofonts/noto-cjk/blob/main/Sans/OTF/SimplifiedChinese/NotoSansCJKsc-Medium.otf)，Version 2.004 |
| `noto-serif-cjk-sc-700.otf` | [Noto Serif CJK SC Bold](https://github.com/notofonts/noto-cjk/blob/main/Serif/OTF/SimplifiedChinese/NotoSerifCJKsc-Bold.otf)，Version 2.003 |

```powershell
work/font-tools/Scripts/python.exe tools/subset_fonts.py --source-dir work/font-source
work/font-tools/Scripts/python.exe tools/subset_fonts.py --check
```

工具逐片验证实际 cmap 与 CSS 范围相等，检查范围之间没有重复，核对全部原有字符仍然存在，并要求三个中文字重都包含 龘、𠮷。Fira Code 的可变字重范围也会核验。`manifest.json` 保存源覆盖范围、每片字符范围和文件大小，生成结果直接参与前端构建。

只重新生成某个家族时可以添加 `--only-family "Fira Code"`，其余家族的文件必须已经存在。工具仍核验最终全部文件和全部覆盖范围。

更换源字体版本或切片规则后，应在新的输出目录或干净工作树生成，避免旧切片残留。检查不会自动删除文件；目录与清单不一致时会抛出错误。

## 下载量的统计条件

字体目录总大小表示安装包需要携带的静态资产，页面实际字体下载量由可见文字和所用字重决定。测量实际下载量时，应固定页面内容、视口、主题、缓存条件和网络压缩条件，统计字体响应体或 Resource Timing 数据。使用全新浏览器上下文测量首次读取，并另行核对罕见字触发的增量请求。

浏览器 FontFaceSet 就绪只代表加载流程结束。罕见字验证还应结合字体 cmap 及浏览器实际渲染字体信息。未连接后台的设备配对页面与已登录的工作台应分别记录，前者的数据不能代替后者。

2026-10-24 的实际核验使用 Edge 153.0.4234.48、390×844、浅色、全新浏览器上下文并禁用 HTTP 缓存。静态生产构建经 loopback 未压缩 HTTP 提供，浏览器固定模拟 200ms 延迟、吞吐不限，后台未连接。页面显示设备配对读取未完成；字体请求包含载入过程中的使用量。

| 指标 | 修改前 | 当前 |
| --- | ---: | ---: |
| `public/fonts` 全部文件总大小 | 76,619,078 字节 | 17,405,908 字节 |
| 字体文件数 | 12 个 TTF | 166 个 WOFF2 |
| 此页面首次读取的字体请求数 | 3 | 3 |
| 此页面首次读取的字体响应体 | 25,498,652 字节 | 1,374,368 字节 |

此次页面的字体响应体减少 94.61%。向 DOM 添加 龘、𠮷 两个字符后才产生对应的非常用片和补充片请求。浏览器实际字体信息确认三个中文样式的两个字符都使用自带字体。已登录工作台的首次读取量和不同资料的请求分布仍需在对应页面单独测量。

外观对照在集中验收执行，覆盖桌面与手机尺寸、明暗两种主题，以及工作台、资料库正文与设置页。当前任务的非视觉核验不代替该项检查。

工具接口依据 [fontTools subset 文档](https://fonttools.readthedocs.io/en/latest/subset/index.html) 和 [WOFF2 文档](https://fonttools.readthedocs.io/en/latest/ttLib/woff2.html)。
