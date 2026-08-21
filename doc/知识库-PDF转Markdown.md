# 知识库前置：PDF 转 Markdown

> 状态：实现前构思  
> 日期：2026-08-21  
> 位置：在「可检索知识库」之前。Agent **不读 PDF**，DeepSeek 也不是多模态。  
> 本工具是本地预处理，不是 Skill，不进 `analyze` 运行时。

经管同学会交准则、证监会公告、教材扫描、论文 PDF。要进 InsightAgent，必须先变成**可编辑的字**。Markdown 是中间格式：人能校对，后面再蒸馏成 `search_methodology` 短条目。

```text
同学 PDF
  → 本工具：本地抽出文字，写成 .md + 元数据
  → 人校对（扫描件、双栏、表格会烂）
  → 另一步：从 md 写成知识库条目（trigger + 短 text + 出处）
  → Agent 检索条目，每次几条纯文本
```

不要：把全书 md 塞进一次 Prompt；不要把 PDF 发给 DeepSeek 或任何云 OCR。

---

## 1. 为什么先转 md，不直接进知识库

- PDF 排版是给打印机的，不是给检索的。页眉页脚、脚注、双栏会把句子撕碎。
- 知识库条目要短、带触发词、能引用。从烂 OCR 自动切条会污染检索。
- md 可以 git 管理、diff、人工改错字；PDF 不行。
- 法规官网常常已有 HTML，能下到网页就不必强行转 PDF。工具要能跳过「已经是 md/html/txt」的文件，只处理 PDF。

---

## 2. 工具边界

| 做 | 不做 |
|----|------|
| 本地读 PDF，写出 `.md` | 调用 DeepSeek / 任何远程多模态 |
| 记下源文件哈希、页数、是否像扫描件 | 上传到非本机服务 |
| 尽量保留标题、段落、简单表格 | 自动写成 methodology JSON |
| 扫描件明确标 `needs_ocr`，宁可不转 | 假装转成功 |
| 一次转一本或一个目录 | 当 Agent 工具给模型乱调 |

依赖放可选 extra（例如 `insightagent[docs]`），不要绑进默认 `analyze` 安装。推荐本地库：**PyMuPDF**（`fitz`）抽文本；有表格时可选 **pdfplumber**。第一版先 PyMuPDF 即可。

扫描件以后若要 OCR，只用**本机**引擎（如 Tesseract），默认关闭。未开 OCR 时扫描 PDF 输出一份说明 md：`status: needs_ocr`，不要生成假正文。

---

## 3. 输入 / 输出放哪

仓库内建议：

```text
data/kb/
  incoming/          # 同学交来的原件（pdf/html），gitignore
  markdown/          # 本工具产出
    2023-65-非经常性损益.md
    sloan-1996.md
  entries/           # 以后才有：蒸馏后的检索条目（本次工具不写这里）
```

`data/kb/incoming/` 和转换缓存进 `.gitignore`。产出的 md 若不含版权全书，可以按篇选进 git；教材全书 md **不要提交**。

每篇 md 文首 YAML：

```yaml
---
source_path: incoming/csrc-2023-65.pdf
source_sha256: ...
page_count: 8
extractor: pymupdf
status: ok | needs_ocr | empty_text
title_guess: 公开发行证券的公司信息披露解释性公告第1号——非经常性损益
---
```

正文按页分开更好校对：

```markdown
## 第 1 页

……抽出的段落……

## 第 2 页
```

不要在这一步做「智能章节重构」。页级已经够用；切条目是下一步人工/半自动的事。

---

## 4. CLI（建议）

挂在现有入口，不新起服务：

```text
python -m insightagent pdf2md path/to/file.pdf
python -m insightagent pdf2md path/to/dir --out data/kb/markdown
```

行为：

- 输入是文件：转一个  
- 输入是目录：只处理 `*.pdf`，已有同名 md 且源哈希不变则跳过  
- 退出码：有 `needs_ocr` / `empty_text` 时非零，方便你看见哪些要人工  
- 不联网

单测用夹具：一两页**内嵌文字**的小 PDF（自己生成，不把受版权教材放进仓库）。断言 md 里出现已知句子、YAML 有 `status: ok`。再准备一个几乎无文字的夹具，断言 `needs_ocr`。

---

## 5. 和知识库的分工（别混）

| 阶段 | 谁 | 产出 |
|------|----|------|
| 本工具 | 代码 | 带元数据的 md |
| 校对 | 人 | 改错、删页眉、标「此页是目录可丢」 |
| 蒸馏 | 人 + 以后可选用模型读 **md 文本** | `id` / `trigger` / `text` / `source_refs` |
| 运行时 | `search_methodology` | 每次最多几条短文本给 DeepSeek |

蒸馏时模型只看 md 字符串，仍然不是多模态。一次只喂一章或数页，不要喂整本教材。

---

## 6. 实现切片（同意后再写代码）

1. `src/insightagent/pdfmd.py`：抽文本、判空页比例、写 YAML+分页 md  
2. `__main__.py` 增加 `pdf2md` 子命令  
3. `tests/test_pdfmd.py` + `tests/fixtures/` 里自造的小 PDF  
4. `pyproject.toml` optional-dependencies `docs = ["pymupdf"]`  
5. `.gitignore` 增加 `data/kb/incoming/`

第一版不做：目录识别、公式、复杂表格还原、自动切 KB 条目、云 OCR。

---

## 7. 验收

- 证监会公告类文字 PDF → md 里能搜到文号和条款句  
- 无字扫描件 → 不编造正文，`status: needs_ocr`  
- `analyze` 默认环境不强制安装 PyMuPDF  
- 全程不把 PDF 发到外网
