/**
 * 卡片内预览的**真实样例数据**：直接来自后端 `services/preview.convert`。
 *
 * 生成方式（可复现，backend 目录下）：
 *   .venv/Scripts/python.exe tests/fixtures/preview/dump_json.py
 * 输入是 backend/tests/fixtures/preview 现场生成的**真文件**（真 zip + 真 XML、
 * 真 PDF 对象表、真 PNG 字节、真代码文件），所以这里就是后端真实产出的形状 ——
 * 前端渲染器测试消费它，等于端到端对了一次线。
 *
 * 不要手改这个文件：改样例就重新跑上面的命令。
 */

export const SAMPLE_DOCX = {
  "kind": "docx",
  "blocks": [
    {
      "type": "heading",
      "level": 1,
      "ordered": null,
      "runs": [
        {
          "text": "季度报告",
          "bold": false,
          "italic": false
        }
      ]
    },
    {
      "type": "paragraph",
      "level": null,
      "ordered": null,
      "runs": [
        {
          "text": "重点：",
          "bold": true,
          "italic": false
        },
        {
          "text": "增长",
          "bold": false,
          "italic": true
        },
        {
          "text": "与成本",
          "bold": false,
          "italic": false
        }
      ]
    },
    {
      "type": "list",
      "level": null,
      "ordered": true,
      "runs": [
        {
          "text": "第一条",
          "bold": false,
          "italic": false
        }
      ]
    },
    {
      "type": "list",
      "level": null,
      "ordered": false,
      "runs": [
        {
          "text": "要点甲",
          "bold": false,
          "italic": false
        }
      ]
    },
    {
      "type": "paragraph",
      "level": null,
      "ordered": null,
      "runs": [
        {
          "text": "结尾段落",
          "bold": false,
          "italic": false
        }
      ]
    }
  ],
  "tables": [
    {
      "rows": [
        [
          "项目",
          "金额"
        ],
        [
          "收入",
          "120"
        ]
      ]
    }
  ],
  "truncated": false
} as Record<string, unknown>;

export const SAMPLE_XLSX = {
  "kind": "xlsx",
  "sheets": [
    {
      "name": "销售",
      "rows": [
        {
          "index": 1,
          "cells": [
            {
              "ref": "A1",
              "value": "季度",
              "formula": null,
              "cached": null
            },
            {
              "ref": "B1",
              "value": "12",
              "formula": null,
              "cached": null
            },
            {
              "ref": "C1",
              "value": "备注文本",
              "formula": null,
              "cached": null
            }
          ]
        },
        {
          "index": 2,
          "cells": [
            {
              "ref": "A2",
              "value": "收入",
              "formula": null,
              "cached": null
            },
            {
              "ref": "B2",
              "value": null,
              "formula": "SUM(B1:B1)",
              "cached": "12"
            }
          ]
        }
      ]
    }
  ],
  "truncated": false
} as Record<string, unknown>;

export const SAMPLE_PPTX = {
  "kind": "pptx",
  "slides": [
    {
      "index": 1,
      "title": "产品路线",
      "texts": [
        "第一步：调研",
        "第二步：原型",
        "备注：备注内容：确认预算"
      ],
      "images": [
        {
          "name": "image1.png",
          "bytes": 74
        }
      ],
      "tables": [
        [
          [
            "阶段",
            "负责人"
          ],
          [
            "调研",
            "小林"
          ]
        ]
      ]
    },
    {
      "index": 2,
      "title": "下一步",
      "texts": [
        "评审"
      ],
      "images": [],
      "tables": []
    }
  ],
  "truncated": false
} as Record<string, unknown>;

export const SAMPLE_PDF = {
  "kind": "pdf",
  "pages": 1,
  "title": "QIO preview sample",
  "encrypted": false,
  "textChars": 19,
  "textProbedPages": 1
} as Record<string, unknown>;

export const SAMPLE_CODE = {
  "kind": "code",
  "language": "python",
  "lines": [
    "\"\"\"样例：真实代码文件（缩进与行数都要保真）。\"\"\"",
    "",
    "",
    "def add(a, b):",
    "    return a + b",
    "",
    "",
    "if __name__ == \"__main__\":",
    "    print(add(1, 2))"
  ],
  "totalLines": 9,
  "encoding": "utf-8",
  "truncated": false
} as Record<string, unknown>;

export const SAMPLE_IMAGE = {
  "kind": "image",
  "width": 8,
  "height": 6,
  "bytes": 74
} as Record<string, unknown>;
