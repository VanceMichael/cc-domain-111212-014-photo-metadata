# 来源格式与业务语义

本地 JPEG 夹具为只含元数据的最小容器，不用于像素解码或显示。EXIF 支持 TIFF IFD0、ExifIFD、GPSIFD 及缩略图下一 IFD，字段类型 BYTE/ASCII/SHORT/LONG/RATIONAL/SRATIONAL；所有 offset 相对 TIFF 起始。XMP 采用 RDF/XML 的属性与元素形式，保留命名空间。侧车仅以配置关系配对，不按同名猜跨文件身份。时间无偏移时标 unknown-zone；导出策略可屏蔽 GPS 和 serial，不修改原件。字段裁决、时间解释与导出语义见 `docs/metadata-contract.md`。

本地演练夹具由 `sampledata.py` 生成，均为虚构业务数据。`sample` 是当前可查看输入，`invalid` 是边界失败输入，其余用于对照来源变体。生成不会下载资源。

本地 HTTP 演练器采用方法与原始路径作为路由键，值为按调用次数返回的响应数组，耗尽后维持最后一项。响应可定义 status、headers、text/base64、require_headers、chunk_bytes、disconnect_after。每次请求保留方法、路径、头与正文摘要表示，便于核对实际访问；演练器只监听回环地址。

原始输入上限 16 MiB 是现有查看入口的保护值，不代表未来持续采集需一次读完整来源。格式含义和截断错误不能被 HTTP 200 覆盖。
