# FolioScribe Viewer

FolioScribe 的浏览器标注工作台支持多模型证据对照、页面编辑和跨页复核。前端与后端随 Python 包一起安装。

## 启动

从仓库根目录安装并生成合成演示：

```bash
python -m pip install -e '.[dev]'
python examples/make_viewer_demo.py --output data/viewer-demo
folioscribe-viewer --data-root data/viewer-demo
```

打开 `http://127.0.0.1:8745`。真实批次通过 `--data-root /path/to/batch` 指定；需要批次切换时可用 `--batches-root /path/to/batches`。默认监听 `127.0.0.1`；需要团队访问时显式配置 `--host`，并通过可信网络或带认证的反向代理访问。

## 浏览与标注

浏览模式提供 Final、MinerU、Paddle、PDF 和 Contract GT 图层，显示块类型、阅读顺序、模型判断与问题。右侧支持块搜索、表格 HTML 查看、文本复制和任务检查。

标注模式维护独立工作副本，支持：

- 框选新建、移动和缩放 bbox，吸附 PDF 文字证据。
- 修改类型、正文、标题层级、caption 归属及表格 HTML/单元格。
- 删除、恢复、合并块，以及手动调整阅读顺序。
- 文本续页和跨页续表的双向链接。
- 标记难页、记录页面和文档复核结论。
- 自动保存、手动保存、撤销与重做。

页面内的[操作说明](static/guide.html)提供类型与交互指导。仓库里的界面示意图全部来自合成文档。

## 导航与快捷键

| 操作 | 快捷键 / 入口 |
| --- | --- |
| 上一页 / 下一页 | `Q` / `E`，或未编辑框时的左右方向键 |
| 上一篇 / 下一篇 | `A` / `D` |
| 指定页 | 工具栏页码输入 + Enter |
| 下一待复核 / 未标注页 | 左侧导航按钮 |
| 新建框 | `B`，拖拽后 Enter 确认 / Esc 取消 |
| 删除选中框 | Delete |
| 撤销 / 重做 | Ctrl/Cmd+Z / Ctrl/Cmd+Shift+Z |
| 立即保存 | Ctrl/Cmd+S |
| 微调位置 | 选框后方向键，Shift 加大步长 |

文档边界不会循环回到第一篇。跨页链接限制在同一文档相邻页内。

## 保存语义

停止编辑后自动保存；翻页、切模式和切批次前等待未保存改动落盘。保存过程中发生的新修改会继续保存，自动保存不会清空撤销历史。

失败时保留当前页和编辑状态，常驻提示可重试或导出本页 JSON 草稿。关闭页面只会提示未保存编辑，不保证浏览器退出时完成网络请求。暂未实现持久化浏览器草稿和多标签 revision 冲突处理。

人工结果位于 `annotations/`，不会直接覆盖 `runs/` 中的机器结果。任务链接使用 `?t=<queue-key>` 分配标注工作，属于任务路由，不是身份认证机制。

## 输入目录

```text
batch/
  evidence/<sample>/page_0001.json
  runs/<sample>/page_0001/result.json
  annotations/                       # 保存后生成
```

页图路径由 evidence 的 `page_image.path` 指定，需要当前服务器可读。人工纠正不会自动转成训练数据，相关适配器仍在开发计划中。

## 回归与性能测量

```bash
python -m pip install -e '.[dev,browser]'
python -m playwright install chromium
python -m pytest -q tests/browser
python components/annotation/scripts/viewer_perf.py --base http://127.0.0.1:8745 --batch YOUR_BATCH
```

浏览器回归使用合成数据，覆盖保存失败、保存中编辑、撤销/重做、跳页和任务参数；性能工具需要正在运行的 Viewer 与足够多页的测试批次。
