# 元数据采集语义

`metadata.py` 从 JPEG 标记段与明确配置的 XMP 侧车采集结构化候选。照片像素不解码：扫描在首个 SOS 处停止。

## 字段与限定范围

限定 TIFF/EXIF 字段：Make、Model、DateTime（仅作文件修改时间证据）、Artist、Copyright；ExifIFD 的 DateTimeOriginal、DateTimeDigitized、OffsetTimeOriginal、OffsetTimeDigitized、SubSecTimeOriginal、CameraOwnerName、BodySerialNumber、LensSerialNumber；GPSIFD 的经纬度与半球。XMP 只取 `dc:rights`、`dc:creator`、`tiff:Make`、`tiff:Model`、`exif:DateTimeOriginal`、`exif:DateTimeDigitized`、`exif:OffsetTimeOriginal`、`exif:SubSecTimeOriginal`、`photoshop:DateCreated`、`exif:BodySerialNumber`、`exif:LensSerialNumber`、`exif:CameraOwnerName`、`aux:SerialNumber`。

每个候选保留 `source_id`、`media`（EXIF/内嵌 XMP/侧车 XMP）、`namespace`、`context`、`label`、`locator`。TIFF 的 `locator` 是相对 TIFF 起始的 IFD、条目偏移与值偏移，并给出文件内绝对偏移；XMP 的 `locator` 是 XML 路径（属性用 `/@prefix:name`）。字段名不是唯一键：命名空间不同即不同候选。

## 时间

- 数字化时间（DateTimeDigitized、photoshop:DateCreated）是数字化时间，不是拍摄时间，独立成字段，不填补 `capture_time`。
- 无偏移的时间标 `zone_basis=unknown-zone`，不换算地区时间，也不以文件修改时间补空。
- 只有显式偏移（值内偏移或同包同媒体的 OffsetTime* 标签）才产生 UTC。
- 只有明确配置摄影任务时区（`--task-timezone`）时，才把无偏移时钟按该时区换算，并在 `reason` 与 `zone_basis=task-timezone` 中注明。
- `DateTime`（0x0132）不是拍摄时间；它只作为 `metadata_change_time` 候选保留。

## 候选裁决

同字段候选值全部一致才 `confirmed`；任一不一致即 `conflict`，保留全部候选、命名空间与定位，不按读取顺序或最后读到者选择。仅亚秒精度不同的候选按秒级确认，同时列出 `clocks` 与 `precision`。无候选为 `absent`，有声明但值不可识别为 `unknown`。

## 结构安全

- TIFF 字节序支持 II/MM；每个指针与值偏移都做边界检查，越界记 `error` 诊断，同段其余字段仍可读，但该段不冒充完整解析。
- 多个 APP1 段各自独立上下文；偏移与亚秒标签只从同 `source_id`、同媒体借用，不跨段。
- IFD1（缩略图）条目只作证据，不进入主图字段，也不跟随其指针。
- XMP 扩展段（多段拼接）不解析，避免跨段拼接上下文。
- 侧车只按配置配对；缺失记 `missing`，不以同名猜测，也不以其他文件替代。

## 导出

`export(report, profile)` 从采集证据派生，不覆盖内部报告。用途：`internal`（无屏蔽）、`location-safe`（去 GPS）、`editorial`/`public`（去 GPS、设备序列号与相机所有者）。被屏蔽字段只以 `withheld` 出现，不带值、定位或候选细节；包头上下文只保留条目数量。`export_index` 同样不输出被屏蔽字段的值与位置，避免从索引泄漏。
