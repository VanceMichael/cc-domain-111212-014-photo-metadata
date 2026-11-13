# photo-metadata

照片内嵌与侧车元数据的结构化采集所用的来源检查与原件留存工具。

## 当前能力

`source.py` 查看来源的标记段结构，不把检查结果冒充完整业务采集。
`metadata.py` 采集限定 TIFF/EXIF 字段与 XMP 侧车，保留候选、命名空间、偏移或 XML 路径及采用理由；不解码像素，不以文件修改时间补空。语义见 `docs/metadata-contract.md`。
`storage.py` 将原始字节、来源标识、摘要和检查结果一起保存在本地 SQLite，重复原件保留同一收件编号。
`sampledata.py` 构造可重复的本地数据；`transport.py` 提供独立的回环 HTTP 演练源与请求记录。
解释范围和来源变体见 `docs/source-contract.md`，当前检查测试与将来新增业务能力的测试应分开保留。

## 环境

Python 3.11 或以上，仅标准库，无需凭据和外部数据。使用容器时可执行 `docker build -t photo-metadata .`，依赖及镜像须在正式运行前准备。
运行入口、测试与演示均不安装包，不访问公网。数据库使用临时路径，不向代码目录写入业务文件。

## 测试与构建

构建工具版本固定在 `requirements-build.txt`，在环境准备阶段执行 `python3 -m pip install -r requirements-build.txt`；容器构建已包含此步骤。应用运行仍仅使用标准库，测试及演示不安装依赖。

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q app.py source.py sampledata.py storage.py transport.py metadata.py
python3 -m build --wheel --no-isolation
```

## 快速演示

```sh
python3 app.py demo
```

演示真实启动本地来源、领取原件、执行检查、采集多来源元数据并保存临时数据库，展示字段确定/冲突/不可知状态、缺失侧车与导出遮蔽，退出后自动清理。正常在数秒内完成，不以预录日志代替执行。

## 单独检查与收件

```sh
python3 app.py fixtures /tmp/photo-metadata-fixtures
python3 app.py inspect /tmp/photo-metadata-fixtures/sample.jpg
python3 app.py capture /tmp/photo-metadata-fixtures/sample.jpg --database /tmp/photo-metadata.sqlite3 --source local-partner
python3 app.py list --database /tmp/photo-metadata.sqlite3
```

生成夹具不会覆盖已有文件。运行错误以 JSON 输出到标准错误并返回退出码 2；原始输入无效时不生成成功收件。内部原件与任何对外展示副本应分开管理，不写入真实个人数据。

## 元数据采集与导出

```sh
python3 app.py metadata /tmp/photo-metadata-fixtures/sample-multi.jpg \
  --sidecar /tmp/photo-metadata-fixtures/sample-multi.xmp \
  --task-timezone Asia/Shanghai --profile editorial
python3 app.py metadata /tmp/photo-metadata-fixtures/sample-conflict.jpg \
  --sidecar /tmp/photo-metadata-fixtures/sample-conflict.xmp --profile internal
python3 app.py metadata /tmp/photo-metadata-fixtures/sample-multi.jpg \
  --sidecar /tmp/photo-metadata-fixtures/sample-multi.xmp --profile public --index
```

`--sidecar` 只接受明确指定的侧车文件；缺失或损坏会在输出中记为 `missing`/`error`，不会用同名或其他文件顶替。`--task-timezone` 是地区时间唯一允许的换算依据；未配置时无偏移时间保持 `unknown-zone`。`--profile` 只影响导出副本，内部证据不被覆盖；`--index` 输出不含被屏蔽字段的值与位置。夹具中的 `sample-multi`、`sample-conflict`、`sample-thumbnail`、`sample-bad-pointer`、`sample-split-context`、`sample-big-endian`、`sample-digitized-only` 分别覆盖同图多候选、冲突、缩略图、坏指针、跨 APP1 上下文、大小端与数字化时间。
