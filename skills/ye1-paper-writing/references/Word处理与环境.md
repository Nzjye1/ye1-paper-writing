# Word 处理与运行环境

由 Agent 在用户当前任务的授权与宿主权限内处理；不要让医生编写命令、JSON 或排查环境。用本 Skill 自带 `scripts/ye1_word.py`，不依赖其他 Skill。先检查宿主已有文档工具和 Python；WorkBuddy、Codex 或其他 Agent 可使用其实际提供的终端、文档工具及运行权限，不能假定所有宿主拥有相同接口。

## 准备 Python

仅在需要 Python Word 工具时，优先使用宿主可用的 CPython 3.13；先探测 `python3.13`、Windows 的 `py -3.13` 或已知可复用的 3.13 环境，不假定 `python3` 就是 3.13。已有经验证能完成当前任务的 3.12 环境可直接复用，其他版本须先核实依赖和功能。检查 `python-docx` 是否支持 `Document.add_comment`（1.2 或以上）。已有符合要求的环境就直接使用，不为重复诊断或安装耽误任务。缺少库时，Agent 根据当前宿主支持的方式在任务工作目录或临时目录建立隔离环境，安装 `python-docx>=1.2,<2`；不在 Skill 文件夹内创建虚拟环境，避免污染 Skill 包。Python 不存在时，先使用宿主提供的运行环境或可用的常规安装方式；宿主限制或联网失败时，完成不依赖该工具的部分并说明实际缺口。遵守宿主的权限和网络限制，不自动安装其他 Skill，不改用户全局配置。

例如，Unix/macOS 的隔离环境可用以下形式；Windows 可用 `py -3.13` 和环境中的 `Scripts/python.exe`。环境名称和路径由 Agent 决定，不要求医生输入：

```sh
python3.13 -m venv /任务工作目录/.ye1-docx-env
/任务工作目录/.ye1-docx-env/bin/python -m pip install 'python-docx>=1.2,<2'
```

安装依赖使用公开软件包及宿主授权网络，按可用性处理失败；不要把稿件、审稿意见或患者数据上传到包管理服务。文献检索只发送必要的公开题录、DOI、PMID 或检索词。

需要读取 PDF 时，先用宿主已有的本地 PDF 工具；没有时可在同一隔离环境安装 `pypdf`，运行 `python scripts/ye1_pdf.py input.pdf`，得到按页提取的文字及无文字页面提示。扫描页需要宿主可用的 OCR；图表和版面仍需按实际内容另行查看。不能提取或解密时说明原因并继续处理已有材料。

## 使用随包工具

工具参数中的路径由 Agent 换成真实文件路径。下面的 JSON 是 Agent 在任务工作目录临时生成的操作数据，不是让医生填写的表单。输出用新文件名，工具不会覆盖原件或已有输出。

### 读取正文、表格段落与已有批注

```sh
python scripts/ye1_word.py inspect input.docx
```

返回主文档按顺序编号的段落、是否位于表格、现有批注文本和能定位到的锚点、已有修订元素数。编号从 1 开始。**批注和修订的 paragraph 编号必须直接对应 inspect 的结果；inspect 把表格单元格中的段落也计入顺序，不能用 python-docx 的 Document.paragraphs 下标加一代替。** 每次按当前基准稿重新读取编号和原文，生成后核对锚点与变更。显示的是当前正文视图：保留原稿中的既有修订，不把删除文本当作当前文字。**页眉、页脚、脚注、绘图中的文字与现代批注线程不在此工具完整读取范围内**；需要时用宿主文档工具读取并说明实际范围。

### 生成新的 Word 文档

Agent 生成 UTF-8 JSON，`title` 可省略，`blocks` 支持 `heading`、`paragraph`、`table`、`figure`。例如：

```json
{"title":"论文标题","blocks":[
  {"type":"heading","text":"Results","level":1},
  {"type":"paragraph","text":"依据真实结果写出的正文 [1]。"},
  {"type":"table","caption":"Table 1","rows":[["Group","n"],["A","20"]]},
  {"type":"figure","path":"figure.png","caption":"Figure 1"}
]}
```

`figure.path` 为图片实际路径或相对 JSON 文件的路径。生成：`python scripts/ye1_word.py create --spec draft.json --out manuscript.docx`。工具适合普通段落、表和常见图片；复杂数学公式、特殊引文域和模板排版应使用宿主的兼容文档工具，且按输出实际情况核对。生成后检查表格列宽、表头和英文长词是否拥挤或截断；必要时在不改变含义的前提下简化表头、调整列宽或使用适合的横向布局，不能仅凭文件创建成功就认定版面可用。

### 给已有 Word 加原生批注

根据 `inspect` 的段落编号，在 JSON 中给出对应的准确原文片段和批注；批注可以落在普通多段文本运行之间或表格单元格中：

```json
{"comments":[{"paragraph":2,"quote":"待核对的原文","text":"这里的问题和修改建议。"}]}
```

执行：`python scripts/ye1_word.py comment input.docx --spec comments.json --out commented.docx`。优先使用能唯一匹配的 `quote` 以准确落到文字；无法安全匹配时可用宿主文档工具，或去掉 `quote` 改为整段批注并向用户说明。不要因锚点失败而声称批注已插入。生成后再次 `inspect`，核对评论内容、锚点和既有批注；复杂文件还应以可用的 Word 工具打开检查。

### 生成带修订痕迹和修改后版本

Agent 基于当前稿件及实际要做的修改生成 JSON：

```json
{"changes":[{"paragraph":2,"old":"原句中的准确文字","new":"修改后的文字"}]}
```

执行：`python scripts/ye1_word.py revise input.docx --spec changes.json --marked revised-marked.docx --clean revised-clean.docx`。工具在 Word 文件中生成原生插入／删除标记，同时生成应用本次修改的版本。它仅安全替换**同一段内相邻普通文本运行**中的唯一匹配，支持跨普通运行和表格单元格，不处理跨段落、脚注、域代码、链接、图片或已有修订内部的替换；遇到这些位置会拒绝，不会猜测修改。可将安全的变更拆成更小的准确片段，或使用宿主的 Word／文档工具处理复杂修改，并核对生成结果。

已有批注的锚点可能把相邻文字分开；如果要修改的片段跨过批注边界，工具也会拒绝。优先将可安全处理的修改拆开，或使用可用的 Word／文档工具处理；不要擅自删除既有批注。确实不能在同一副本完成时，可分别交付可用的批注稿和修订稿，并说清尚未合并的部分。

如果原稿已经含有修订，新生成的“修改后”版本只应用本次修改，原有未决修订仍保留；不要未经用户指示接受或删除它们。需要期刊标记版时，确认其对比基准为审稿人实际看到的稿件。对带修订稿核对新旧文字与回复对应关系，保留图片、表格、引用域、已有批注和原稿；在可用时用 Word 打开核实。工具内的可见文本一致性检查不等于复杂版式或所有文档元素均已验证。

基准稿和修改稿均没有未决修订时，可用 `python scripts/check_marked_manuscript.py --marked revised-marked.docx --original input.docx --revised revised-clean.docx --author Ye1 --strict` 辅助检查接受／拒绝修订后的主文档文字、表格数量，以及基准稿普通批注及基本锚点是否在两份输出中保留。对比基准必须是实际输入的带批注稿，不能因为另一份无批注稿文字相同便用它掩盖批注丢失；原稿有批注时用 `inspect` 对照内容和锚点位置。若暂不能将批注与修订合并，分别保留、交付批注稿及修订稿，并明确说明未合并。此检查不证明批注位置完全正确，也不覆盖现代批注线程、页眉页脚、全部排版和图片内容；有原有修订时不可擅自全部接受以满足检查，应保留并改用适当的 Word 工具核对。

如果已经有原稿与完成的修改稿，修改包括段落增删或较复杂排版，且宿主为 macOS 并可使用 Microsoft Word，可运行 `python scripts/ye1_compare.py --original original.docx --revised revised.docx --out compared.docx --author Ye1`。它通过随包的 MedSci 工具调用 Word 比较，在临时目录生成并做文字／表格数量回查，成功后才另存到新路径。该方法要求基准稿和修改稿本身没有未决修订；不得未经用户指示接受或删除既有修订。它不检查所有对象或页面排版，仍须检查实际稿件。Word 比较可能重复保留原稿和修改稿中相同的批注，检查批注是否重复、丢失或错位；只在确定为重复且用户任务允许时在工作副本处理，不能武断删除。其他操作系统应使用宿主可用的 Word 文档工具；若无法处理，交付实际完成的修改稿和说明，不把普通稿称为修订痕迹版。
