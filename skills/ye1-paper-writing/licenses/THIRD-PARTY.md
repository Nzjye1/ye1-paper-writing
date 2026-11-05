# 第三方代码说明

`scripts/verify_refs.py` 在上游版本基础上经本项目修改：识别 Word 标题边界，并默认禁止未经明确开启的题名联网检索；保留上游版权与 MIT 许可。

`scripts/check_marked_manuscript.py` 在上游版本基础上经本项目修改，增加普通批注及基本锚点保留核查，并明确主文档文字核验的限制；保留上游版权与 MIT 许可。

本 Skill 中以下代码来自 [Aperivue/medsci-skills v6.2.0](https://github.com/Aperivue/medsci-skills/releases/tag/v6.2.0)，提交 `cc462bc2069064b557c2fea12a1f83ed1fe67f66`：

- `scripts/verify_refs.py`：上游 `skills/verify-refs/scripts/verify_refs.py`。
- `scripts/check_rewrite_fidelity.py`：上游 `skills/humanize/scripts/check_rewrite_fidelity.py`。
- `scripts/build_marked_manuscript.py` 与 `scripts/check_marked_manuscript.py`：上游 `skills/sync-submission/scripts/` 对应文件，通过本包的安全封装调用。
- 如果本包包含 `scripts/check_table_percentages.py`：上游 `skills/self-review/scripts/check_table_percentages.py`。
- 如果本包包含 `scripts/check_response_claims.py` 与 `scripts/_quote_match.py`：上游 `skills/revise/scripts/` 对应文件。

上述代码遵循随附 [MEDSCI-MIT.txt](MEDSCI-MIT.txt)，保留原版权和许可。此包不含上游第三方报告规范原文或 CSL 样式；规范及其他第三方资料如需另行复制，应逐项核对许可。

`scripts/check_medical_fidelity.py` 来源于 [ecpp/humanize-medical-writing](https://github.com/ecpp/humanize-medical-writing/tree/5eceb6e4fd614e542f9e4f352cb717f07a54b30a) 的 `scripts/check_fidelity.py`，未修改代码，只更改文件名；遵循 [MEDICAL-HUMANIZER-MIT.txt](MEDICAL-HUMANIZER-MIT.txt) 并保留原许可列明的版权信息。`scripts/check_academic_invariants.py` 来源于 [Yila-AI academic-humanizer](https://github.com/Yila-AI/awesome-research-skills/tree/9457733ecf6dbef4cad8debf2e58cc3bbd378e79/skills/academic-humanizer) 的 `scripts/check_invariants.py`，未修改代码，只更改文件名；学术自然化指引也参考该项目，随包保留其 [Apache 2.0 许可](ACADEMIC-APACHE-2.0.txt)及[第三方声明](ACADEMIC-THIRD-PARTY.md)。

`scripts/ye1_word.py`、`scripts/ye1_table_check.py` 和 `scripts/ye1_pubmed.py` 是本项目为该 Skill 编写的辅助代码，不是上述上游文件；其中个别文件仅存在于相应 Skill 包。

`scripts/literature/pubmed_search.py`、`scripts/literature/local_library.py` 和 `scripts/literature/screen_queue.py` 从 Ye1 医学文献调研 v0.3.0 项目副本复制，用于独立检索及获取可用全文；代码遵循随附 [YE1-LITERATURE-MIT.txt](YE1-LITERATURE-MIT.txt)。本包不包括文献全文，实际检索和全文文件保存在用户任务目录或授权文献库。

`scripts/literature/find_fulltext_links.py` 为本项目在医学文献调研开发副本新增的 Europe PMC 链接查找脚本，复制到写作包中使用；它只寻找公开链接，不下载文献。
